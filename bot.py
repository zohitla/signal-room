"""
Personal news wire bot for Telegram.
Pulls crypto, markets/oil, world, AI/tech and TikTok news from RSS feeds
and posts new headlines to your Telegram channel in wire style:

    🇮🇷 *TRUMP REJECTS IRANIAN PLAN TO REOPEN STRAIT OF HORMUZ
    — Reuters

Runs once per call (GitHub Actions calls it on a schedule).
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
import requests

# ───────────────────────── SETTINGS (edit these) ─────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")          # e.g. @mynewswire or -100123...
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")   # optional: AI-rewritten headlines

MAX_POSTS_PER_RUN = 15      # stops spam if lots of news drops at once
MAX_AGE_HOURS = 3           # ignore anything older than this
SIMILARITY_CUTOFF = 0.5     # 0-1, how similar two headlines must be to count as duplicates
PRICE_PULSE_EVERY_HOURS = 4 # posts BTC/ETH/SOL prices every X hours (0 = off)

# Phone pings: True = only 🚨 watchlist headlines buzz your phone, everything else
# arrives silently in the channel. False = every headline buzzes.
LOUD_ONLY_FOR_WATCHLIST = True

# AI (needs ANTHROPIC_API_KEY secret, otherwise these are ignored)
AI_SIMPLE_HEADLINES = True   # rewrite every headline short + simple
AI_TAKES = "hot"             # "hot" = 🧠 take under 🚨 stories only, "all" = every story, "off" = none
AI_MODEL = "claude-haiku-4-5-20251001"
MAX_LINE_CHARS = 20          # greentext: every line this short, so it fits an iPhone notification
STYLE = "greentext"          # "greentext" = TAG – move + >why >implication, "wire" = *ALL CAPS style
AI_FILTER = True             # AI skips junk (sports, local stories, fund promos...) before posting
MAX_AI_CALLS_PER_RUN = 40    # cost safety cap

# Always skipped, even without AI (lowercase words/phrases)
BLOCKLIST = [
    "nfl", "nba", "mlb", "nhl", "playoffs", "standings", "touchdown", "super bowl",
    "world series", "premier league", "fantasy football", "box score", "horoscope",
    "recipe", "celebrity", "red carpet", "best deals", "coupon", "how to watch",
    "municipal", "fund q", "fund update", "quarterly update",
]

# Stuff you REALLY don't want to miss. Headlines with these get a 🚨 and go first.
WATCHLIST = [
    "bitcoin", "btc", "ethereum", "etf", "sec", "stablecoin", "hack", "exploit",
    "oil", "crude", "brent", "opec", "hormuz", "iran",
    "meta", "muse", "openai", "anthropic", "nvidia", "google", "apple", "tesla",
    "fed", "powell", "rate cut", "rate hike", "tariff", "trump", "breaking",
    "elon", "musk", "saylor", "cz", "solana", "xrp", "just in",
]


def gnews(query):
    """Google News RSS search, last 2 hours only."""
    return f"https://news.google.com/rss/search?q={quote_plus(query + ' when:2h')}&hl=en-US&gl=US&ceid=US:en"


# (category, emoji, feed url). Add or remove anything you want.
FEEDS = [
    # 🪙 CRYPTO
    ("CRYPTO", "🪙", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CRYPTO", "🪙", "https://cointelegraph.com/rss"),
    ("CRYPTO", "🪙", "https://www.theblock.co/rss.xml"),
    ("CRYPTO", "🪙", "https://decrypt.co/feed"),

    # 📈 MARKETS / OIL
    ("MARKETS", "📈", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("MARKETS", "🛢️", "https://oilprice.com/rss/main"),
    ("MARKETS", "🛢️", gnews("oil prices OR crude OR brent")),
    ("MARKETS", "📈", gnews("stock market OR fed OR treasury yields")),

    # 🌍 WORLD
    ("WORLD", "🌍", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("WORLD", "🌍", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("WORLD", "🌍", gnews("breaking news world")),

    # 🤖 AI / TECH
    ("AI", "🤖", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("AI", "🤖", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("AI", "🤖", gnews("OpenAI OR Anthropic OR Nvidia OR \"Meta AI\" OR Muse")),
    ("AI", "📱", gnews("Meta Platforms OR Zuckerberg")),

    # 🎵 TIKTOK / TRENDS
    ("TRENDS", "🎵", gnews("TikTok trend OR viral TikTok")),
]

# Auto-adds a flag when a country shows up in the headline
FLAGS = {
    "iran": "🇮🇷", "israel": "🇮🇱", "gaza": "🇵🇸", "china": "🇨🇳", "russia": "🇷🇺",
    "ukraine": "🇺🇦", "u.s.": "🇺🇸", "us": "🇺🇸", "trump": "🇺🇸", "america": "🇺🇸",
    "uk": "🇬🇧", "britain": "🇬🇧", "canada": "🇨🇦", "carney": "🇨🇦", "eu": "🇪🇺",
    "europe": "🇪🇺", "france": "🇫🇷", "germany": "🇩🇪", "japan": "🇯🇵", "india": "🇮🇳",
    "saudi": "🇸🇦", "mexico": "🇲🇽", "brazil": "🇧🇷", "korea": "🇰🇷", "taiwan": "🇹🇼",
    "australia": "🇦🇺", "turkey": "🇹🇷", "cuba": "🇨🇺", "venezuela": "🇻🇪",
}

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ───────────────────────── HELPERS ─────────────────────────


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"seen": [], "recent_titles": [], "last_pulse": 0, "initialized": False}


def save_state(state):
    state["seen"] = state["seen"][-3000:]
    state["recent_titles"] = state["recent_titles"][-400:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def words(title):
    return set(re.findall(r"[a-z0-9$]+", title.lower())) - {
        "the", "a", "an", "to", "of", "in", "on", "for", "and", "as", "at", "is", "by", "with", "after", "says"
    }


def is_duplicate(title, recent_titles):
    w = words(title)
    if not w:
        return True
    for old in recent_titles:
        o = set(old)
        if o and len(w & o) / len(w | o) >= SIMILARITY_CUTOFF:
            return True
    return False


def split_source(title, entry, feed_title):
    """Google News titles look like 'Headline - Reuters'. Pull the source off."""
    src = ""
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        if len(tail) < 40:
            return head.strip(), tail.strip()
    if hasattr(entry, "source") and getattr(entry.source, "title", None):
        src = entry.source.title
    return title.strip(), src or feed_title


def entry_age_hours(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return 0
    published = datetime(*t[:6], tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - published).total_seconds() / 3600


def flag_for(title):
    t = title.lower()
    for key, flag in FLAGS.items():
        if re.search(r"(?<![a-z])" + re.escape(key) + r"(?![a-z])", t):
            return flag
    return ""


def on_watchlist(title):
    t = title.lower()
    return any(re.search(r"\b" + re.escape(k) + r"\b", t) for k in WATCHLIST)


def blocked(title):
    t = title.lower()
    return any(re.search(r"\b" + re.escape(k) + r"\b", t) for k in BLOCKLIST)


def clean_source(src):
    """'Al Jazeera – Breaking News, World News and Video from Al Jazeera' -> 'Al Jazeera'"""
    src = re.split(r"\s[-–—|:]\s|\s\|", src or "")[0].strip()
    return src[:40] or "News"


AI_PROMPT = """You write for a fast Telegram news feed read by crypto/stock traders.

Headline: {title}
Source: {source}

Reply with ONLY a JSON object, no other text:
{{"keep": true, "lines": ["...", "..."], "take": "..."}}

keep: true ONLY if a trader or someone following world news would care: markets, stocks,
crypto, oil/commodities, central banks, economy data, big tech/AI, wars, geopolitics,
elections, sanctions/tariffs, or disasters with global impact. false for sports, local or
regional stories with no market impact, lifestyle, celebrity, fund/product promos,
routine company reports nobody trades on, listicles and how-tos.

lines: {style_rule}

take: {take_rule}"""

GREENTEXT_RULE = """exactly 3 strings. HARD LIMIT: every string is 20 characters or less,
count them. cut words, use symbols (+, /, -, %, $) and abbreviations to fit.
1) "TAG – move": TAG = market, ticker, coin or country in CAPS (BTC, OIL, NVDA, KOREA, JPY, FED),
   move = 1-3 lowercase words
2) why it matters, lowercase (do NOT start with >, it gets added)
3) short implication, lowercase (do NOT start with >)
style: blunt, no newsroom language, no emojis, slang ok ("cooked", "smoked", "printing", "bros")
but keep the key number/ticker/country. never invent facts or predict prices.
example for "Seoul stocks fall 2% as Samsung, SK Hynix slide on rising yields":
["KOREA – tech smoked", "kospi -2%", "samsung/hynix hit"]
example for "Oil jumps 4% after Iran rejects Hormuz deal":
["OIL – +4%", "iran said no to deal", "hormuz still shut"]"""

WIRE_RULE = """one line: the headline in the simplest possible words, max 12 words, keep key facts
and numbers, no hype, no invented details."""

TAKE_RULE = ("1-2 short lines on which sectors, stocks (tickers) or coins this could move and why. "
             "{voice} If it's not market-relevant, return an empty string. "
             "Never predict prices or tell anyone to buy or sell.")


def shorten(line, limit=None):
    """Trim to the char limit at a word boundary (safety net if the AI runs long)."""
    limit = limit or MAX_LINE_CHARS
    line = line.strip()
    if len(line) <= limit:
        return line
    cut = line[:limit + 1].rsplit(" ", 1)[0].rstrip(" ,.-–:")
    return cut if len(cut) >= limit // 2 else line[:limit].rstrip()


def fallback_lines(title):
    return [title.lower()] if STYLE == "greentext" else [title]  # no AI: plain headline


def ai_process(title, source, want_take):
    """Returns (keep, lines, take). Falls back to the original title if AI is off or fails."""
    if not ANTHROPIC_KEY or not (AI_SIMPLE_HEADLINES or want_take or AI_FILTER):
        return True, fallback_lines(title), ""
    try:
        voice = ("Same lowercase blunt greentext voice." if STYLE == "greentext" else "Plain language.")
        prompt = AI_PROMPT.format(
            title=title, source=source,
            style_rule=GREENTEXT_RULE if STYLE == "greentext" else WIRE_RULE,
            take_rule=TAKE_RULE.format(voice=voice) if want_take else 'always return an empty string ""',
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": AI_MODEL, "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        if not r.ok:
            print("AI error:", r.status_code, r.text[:200])
            return True, fallback_lines(title), ""
        text = r.json()["content"][0]["text"].strip()
        text = re.sub(r"^```(json)?|```$", "", text).strip()
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0) if m else text)
        lines = data.get("lines") or []
        if isinstance(lines, str):
            lines = [lines]
        lines = [str(l).lstrip(">").strip() for l in lines if str(l).strip()][:3]
        if not AI_SIMPLE_HEADLINES or not lines:
            lines = fallback_lines(title)
        take = (data.get("take") or "").strip() if want_take else ""
        keep = bool(data.get("keep", True)) if AI_FILTER else True
        return keep, lines, take
    except Exception as e:
        print("AI failed:", e)
        return True, fallback_lines(title), ""


def send(text, silent=False, reply_to=None):
    """Posts to Telegram. Returns the message id if it worked, else None."""
    if not BOT_TOKEN or not CHAT_ID:
        print("[dry run]", text.replace("\n", " | "))
        return 1
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True, "disable_notification": silent}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json=payload, timeout=20)
        if r.status_code == 429:  # Telegram rate limit, wait and retry
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            print("Telegram error:", r.text)
            return None
        return r.json()["result"]["message_id"]
    return None


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_post(item, lines):
    link = f"<a href=\"{esc(item['link'])}\">read</a>"
    if STYLE == "greentext":
        if len(lines) > 1:  # AI-written greentext: enforce the limit. plain fallback headline stays whole
            lines = [shorten(l) for l in lines]
        head, rest = lines[0], lines[1:3]
        alert = "🚨 " if item["hot"] else ""
        body = alert + esc(head)
        if rest:
            body += "\n" + "\n".join("&gt;" + esc(l) for l in rest)
        src = esc(item["source"].lower())
        return f"{body}\n<a href=\"{esc(item['link'])}\">{src}</a>"
    headline = " ".join(lines)
    prefix = "🚨 " if item["hot"] else ""
    flag = flag_for(item["title"])
    icon = f"{flag} " if flag else f"{item['emoji']} "
    return (
        f"{prefix}{icon}<b>*{esc(headline.upper())}</b>\n"
        f"<i>— {esc(item['source'])}</i> · {link}"
    )


def format_take(take):
    if STYLE == "greentext":
        lines = [l.strip().lstrip(">").strip() for l in re.split(r"\n|(?<=[.;])\s+", take) if l.strip()]
        return "\n".join("&gt;" + esc(l) for l in lines[:3]) + "\n<i>ai take, nfa</i>"
    return f"🧠 <b>Why it matters:</b> {esc(take)}\n<i>AI take, not financial advice</i>"


def price_pulse(state):
    if PRICE_PULSE_EVERY_HOURS <= 0:
        return
    now = time.time()
    if now - state.get("last_pulse", 0) < PRICE_PULSE_EVERY_HOURS * 3600 - 300:
        return
    try:
        data = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum,solana,ripple", "vs_currencies": "usd",
                    "include_24hr_change": "true"},
            timeout=20,
        ).json()
        names = [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL"), ("ripple", "XRP")]
        lines = []
        for cid, sym in names:
            if cid in data:
                p = data[cid]["usd"]
                ch = data[cid].get("usd_24h_change") or 0
                arrow = "🟢" if ch >= 0 else "🔴"
                lines.append(f"{arrow} <b>{sym}</b> ${p:,.2f} ({ch:+.2f}%)")
        if lines:
            send("📊 <b>MARKET PULSE</b>\n" + "\n".join(lines), silent=True)
            state["last_pulse"] = now
    except Exception as e:
        print("Price pulse failed:", e)


# ───────────────────────── MAIN ─────────────────────────


def main():
    state = load_state()
    seen = set(state["seen"])
    new_items = []

    for category, emoji, url in FEEDS:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 newsbot"})
        except Exception as e:
            print("Feed failed:", url, e)
            continue
        feed_title = clean_source(feed.feed.get("title", category))
        for entry in feed.entries[:25]:
            link = entry.get("link", "")
            raw_title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
            key = link or raw_title
            if not raw_title or key in seen:
                continue
            seen.add(key)
            state["seen"].append(key)
            if entry_age_hours(entry) > MAX_AGE_HOURS:
                continue
            title, source = split_source(raw_title, entry, feed_title)
            source = clean_source(source)
            new_items.append({
                "title": title, "source": source, "link": link,
                "emoji": emoji, "category": category, "hot": on_watchlist(title),
            })

    # First ever run: just remember what's out there, don't dump 200 old posts
    if not state.get("initialized"):
        for it in new_items:
            state["recent_titles"].append(sorted(words(it["title"])))
        state["initialized"] = True
        save_state(state)
        send("✅ <b>NEWS WIRE ONLINE</b> — you'll get new headlines from here on.")
        print(f"Initialized with {len(new_items)} items.")
        return

    # Watchlist stuff first, then everything else
    new_items.sort(key=lambda x: not x["hot"])

    posted = skipped = ai_calls = 0
    for it in new_items:
        if posted >= MAX_POSTS_PER_RUN:
            break
        if is_duplicate(it["title"], state["recent_titles"]):
            continue
        if blocked(it["title"]):
            skipped += 1
            continue
        # greentext has the implication built in, so no separate reply needed
        want_take = STYLE != "greentext" and (AI_TAKES == "all" or (AI_TAKES == "hot" and it["hot"]))
        if ai_calls < MAX_AI_CALLS_PER_RUN:
            ai_calls += 1
            keep, lines, take = ai_process(it["title"], it["source"], want_take)
        else:
            keep, lines, take = True, fallback_lines(it["title"]), ""
        if not keep:
            skipped += 1
            state["recent_titles"].append(sorted(words(it["title"])))  # remember junk, don't re-check copies
            print("Skipped (junk):", it["title"])
            continue
        msg_id = send(format_post(it, lines), silent=not (it["hot"] or not LOUD_ONLY_FOR_WATCHLIST))
        if msg_id:
            state["recent_titles"].append(sorted(words(it["title"])))
            posted += 1
            if take:
                send(format_take(take), silent=True, reply_to=msg_id)
            time.sleep(1.5)

    price_pulse(state)
    save_state(state)
    print(f"Posted {posted}, skipped {skipped} junk, of {len(new_items)} new items.")


if __name__ == "__main__":
    main()
