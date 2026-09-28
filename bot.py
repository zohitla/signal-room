"""
Personal news wire bot for Telegram.
Pulls crypto, markets/oil, world, AI/tech and TikTok news from RSS feeds
and posts new headlines to your Telegram channel in wire style:

    🇮🇷 *TRUMP REJECTS IRANIAN PLAN TO REOPEN STRAIT OF HORMUZ
    — Reuters

Runs once per call (GitHub Actions calls it every 10 min).
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

# Stuff you REALLY don't want to miss. Headlines with these get a 🚨 and go first.
WATCHLIST = [
    "bitcoin", "btc", "ethereum", "etf", "sec", "stablecoin", "hack", "exploit",
    "oil", "crude", "brent", "opec", "hormuz", "iran",
    "meta", "muse", "openai", "anthropic", "nvidia", "google", "apple", "tesla",
    "fed", "rate cut", "rate hike", "tariff", "trump", "breaking",
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


def ai_rewrite(title):
    """Optional: have Claude rewrite the headline in terse wire style."""
    if not ANTHROPIC_KEY:
        return title
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 80,
                "messages": [{
                    "role": "user",
                    "content": "Rewrite this news headline as a terse Bloomberg-terminal wire headline. "
                               "Keep every fact, add nothing, max 15 words, no quotes, reply with only the headline:\n\n"
                               + title,
                }],
            },
            timeout=20,
        )
        text = r.json()["content"][0]["text"].strip()
        return text or title
    except Exception:
        return title


def send(text, silent=False):
    if not BOT_TOKEN or not CHAT_ID:
        print("[dry run]", text.replace("\n", " | "))
        return True
    for _ in range(3):
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True,
                  "disable_notification": silent},
            timeout=20,
        )
        if r.status_code == 429:  # Telegram rate limit, wait and retry
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            print("Telegram error:", r.text)
        return r.ok
    return False


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_post(item):
    headline = ai_rewrite(item["title"]).upper()
    prefix = "🚨 " if item["hot"] else ""
    flag = flag_for(item["title"])
    icon = f"{flag} " if flag else f"{item['emoji']} "
    return (
        f"{prefix}{icon}<b>*{esc(headline)}</b>\n"
        f"<i>— {esc(item['source'])}</i> · <a href=\"{esc(item['link'])}\">read</a>"
    )


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
        feed_title = feed.feed.get("title", category).split(" - ")[0].split("|")[0].strip()
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

    posted = 0
    for it in new_items:
        if posted >= MAX_POSTS_PER_RUN:
            break
        if is_duplicate(it["title"], state["recent_titles"]):
            continue
        if send(format_post(it), silent=not (it["hot"] or not LOUD_ONLY_FOR_WATCHLIST)):
            state["recent_titles"].append(sorted(words(it["title"])))
            posted += 1
            time.sleep(1.5)

    price_pulse(state)
    save_state(state)
    print(f"Posted {posted} of {len(new_items)} new items.")


if __name__ == "__main__":
    main()
