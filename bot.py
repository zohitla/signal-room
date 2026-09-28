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
from urllib.parse import quote_plus, unquote_plus

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
MAX_LINE_CHARS = 40          # greentext: every line this short, so it fits an iPhone notification
STYLE = "greentext"          # "greentext" = TAG – move + >why >implication, "wire" = *ALL CAPS style
AI_FILTER = True             # AI skips junk (sports, local stories, fund promos...) before posting
MAX_AI_CALLS_PER_RUN = 40    # cost safety cap

# Always skipped, even without AI (lowercase words/phrases)
BLOCKLIST = [
    "nfl", "nba", "mlb", "nhl", "playoffs", "standings", "touchdown", "super bowl",
    "world series", "premier league", "fantasy football", "box score", "horoscope",
    "recipe", "red carpet", "best deals", "coupon", "how to watch",
    "municipal", "fund q", "fund update", "quarterly update",
]

# ───────── ALERTS ─────────
MIN_SCORE = 4                # AI importance below this doesn't get posted (kills filler)
MAJOR_SCORE = 8              # AI importance 1-10. this or higher = 🚨🚨 MAJOR headline (always buzzes)

# 🎯 Price alerts: (coin id on coingecko, "above" or "below", price in USD)
# find a coin's id in its coingecko URL, e.g. coingecko.com/en/coins/solana -> "solana"
PRICE_ALERTS = [
    ("bitcoin", "above", 90000),
    ("bitcoin", "below", 80000),
]

ALERTS_ON = True
MAJORS = ["bitcoin", "ethereum", "solana", "ripple"]
MAJOR_MOVE_PCT = 3           # ⚡ BTC/ETH/SOL/XRP moving this % in 1 hour
ALT_MOVE_PCT = 10            # ⚡ any other top-250 coin moving this % in 1 hour
MIN_VOLUME_USD = 20_000_000  # ignore tiny illiquid coins
VOLUME_SPIKE_X = 2.0         # 📊 rolling 24h volume vs its own recent average (rough "unusually busy" signal)
TRENDING_ALERTS = True       # 🔥 coin enters coingecko trending top 7
# 🩳💥📈 Shorts / futures alerts (free data from Hyperliquid perps)
SHORTS_ALERTS = True
CROWDED_FUNDING = -0.00005   # hourly funding at or below this = shorts crowded (-0.005%/h)
SQUEEZE_PCT = 4              # 💥 squeeze RISK: price up this % since last check while funding is negative
OI_SPIKE_X = 1.3             # 📈 open interest this many times its normal level
MIN_OI_USD = 5_000_000       # ignore small markets
ALERT_COOLDOWN_HOURS = 3     # don't repeat the same alert for the same coin within this

# Stuff you REALLY don't want to miss. Headlines with these get a 🚨 and go first.
WATCHLIST = [
    "bitcoin", "btc", "ethereum", "etf", "sec", "stablecoin", "hack", "hacked", "exploit", "exploited",
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

QUEUE_MAX_HOURS = 6          # undelivered stories keep retrying this long, then expire
QUEUE_MAX_ITEMS = 150        # queue size cap
HEALTH_WARN_AFTER = 3        # ⚠️ post a warning if a part fails this many runs in a row
HEALTH = {}                  # filled during each run: name -> "ok" / error text

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ───────────────────────── HELPERS ─────────────────────────


def mark(part, ok, detail=""):
    """Record whether a part worked this run. Any success in a run counts as ok."""
    if ok:
        HEALTH[part] = "ok"
    elif HEALTH.get(part) != "ok":
        HEALTH[part] = detail or "failed"


def check_health(state):
    h = state.setdefault("health", {})
    for part, result in HEALTH.items():
        rec = h.setdefault(part, {"fails": 0, "warned": False})
        if result == "ok":
            if rec["warned"] and not send(f"✅ <b>{esc(part.upper())} BACK TO NORMAL</b>", silent=True):
                continue  # couldn't announce recovery yet, keep state and retry
            rec["fails"], rec["warned"] = 0, False
        else:
            rec["fails"] += 1
            if rec["fails"] >= HEALTH_WARN_AFTER and not rec["warned"]:
                if send(f"⚠️ <b>{esc(part.upper())} NOT WORKING</b>\n"
                        f"&gt;failed {rec['fails']} runs in a row\n&gt;{esc(result[:120])}"):
                    rec["warned"] = True  # if even the warning can't send, try again next run


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"seen": [], "recent_titles": [], "last_pulse": 0, "initialized": False}


def save_state(state):
    state["seen"] = state["seen"][-3000:]
    state["recent_titles"] = state["recent_titles"][-400:]
    state["recent_rewrites"] = state.get("recent_rewrites", [])[-150:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def words(title):
    t = title.lower().replace("−", "-").replace("–", "-")
    # numbers keep their sign and % ("+3%" vs "-3%"), plus normal words
    toks = re.findall(r"[+-]?\$?\d[\d,.]*%?[kmb]?|[a-z$]+", t)
    return set(x.rstrip(".,") for x in toks) - {
        "the", "a", "an", "to", "of", "in", "on", "for", "and", "as", "at", "is", "by", "with", "after", "says"
    }


CONTRAST = [
    {"accept", "accepts", "accepted", "reject", "rejects", "rejected", "denies", "denied"},
    {"approve", "approves", "approved", "block", "blocks", "blocked", "deny", "delays", "delayed"},
    {"rise", "rises", "rose", "up", "gain", "gains", "jump", "jumps", "surge", "surges", "rally", "rallies",
     "fall", "falls", "fell", "down", "drop", "drops", "slide", "slides", "plunge", "plunges", "crash", "crashes"},
    {"win", "wins", "won", "lose", "loses", "lost"},
    {"hike", "hikes", "cut", "cuts", "hold", "holds", "pause", "pauses"},
    {"buy", "buys", "sell", "sells", "sold"},
    {"open", "opens", "reopen", "reopens", "close", "closes", "shut", "shuts"},
    {"ceasefire", "truce", "attack", "attacks", "strike", "strikes"},
]
NEGATIONS = {"not", "no", "never", "denies", "denied", "fails", "failed", "halts", "halted", "cancels", "canceled"}


def is_duplicate(title, recent_titles):
    w = words(title)
    if not w:
        return True
    for old in recent_titles:
        o = set(old)
        if not o or len(w & o) / len(w | o) < SIMILARITY_CUTOFF:
            continue
        diff = w ^ o
        # different numbers = a new update (btc 90k vs 95k), not a copy
        if any(re.search(r"\d", t) for t in diff):
            continue
        # opposite outcomes (accepts vs rejects, up vs down) = new story
        if any(len(diff & group) > 0 and len(w & group) > 0 and len(o & group) > 0 and (w & group) != (o & group)
               for group in CONTRAST):
            continue
        if diff & NEGATIONS:
            continue
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
{{"keep": true, "score": 5, "lines": ["...", "..."], "take": "..."}}

keep: true ONLY if a trader or someone following world news would care: markets, stocks,
crypto, oil/commodities, central banks, economy data, big tech/AI, wars, geopolitics,
elections, sanctions/tariffs, or disasters with global impact. ALSO true for celebrity or
viral internet stories that could spark a memecoin or crypto narrative (famous person launches
or tweets about a coin, viral moment everyone's talking about). false for sports, local or
regional stories with no market impact, lifestyle/gossip with no crypto angle, fund/product promos,
routine company reports nobody trades on, listicles and how-tos.

score: 1-10, how big this is for markets/the world right now. 9-10 = everyone will be talking
about it (war escalation, fed surprise, major hack, huge crash/pump, giant company news).
7-8 = important. 1-6 = normal news. be strict, most stories are 3-6.

lines: {style_rule}

take: {take_rule}"""

GREENTEXT_RULE = """exactly 3 strings. each string 20-40 characters (count them, never over 40).
use symbols (+, /, -, %, $) and short words to fit. always full words, never cut a word off.
be SPECIFIC: every post must contain the concrete fact that makes it news (the number, the
name, the country, what actually happened). vague lines like "markets nervous" or
"bros cooked" with no fact are not allowed. vary your wording, don't reuse the same slang
every post.
1) "TAG – move": TAG = market, ticker, coin or country in CAPS (BTC, OIL, NVDA, KOREA, JPY, FED),
   move = 1-3 lowercase words
2) why it matters, lowercase (do NOT start with >, it gets added)
3) short implication, lowercase (do NOT start with >)
write it so a 16 year old new investor instantly gets it: simple everyday words, no jargon
(say "rates going up" not "hawkish repricing"), but keep it accurate.
style: blunt, no newsroom language, no emojis, slang ok ("cooked", "smoked", "printing", "bros")
but keep the key number/ticker/country. never invent facts or predict prices.
example for "Seoul stocks fall 2% as Samsung, SK Hynix slide on rising yields":
["KOREA – kospi -2%", "samsung + sk hynix sliding", "blame: bond yields rising"]
example for "Oil jumps 4% after Iran rejects Hormuz deal":
["OIL – +4% on iran news", "iran rejected hormuz deal", "strait stays shut = supply risk"]"""

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
    return cut if " " in line[:limit + 1] and cut else line  # no space to cut at: leave it whole


def fallback_lines(title):
    return [title.lower()] if STYLE == "greentext" else [title]  # no AI: plain headline


def ai_process(title, source, want_take):
    """Returns (keep, lines, take, score). Falls back to the original title if AI is off or fails."""
    if not ANTHROPIC_KEY or not (AI_SIMPLE_HEADLINES or want_take or AI_FILTER):
        return True, fallback_lines(title), "", 0
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
            why = "out of credits, posting plain headlines" if "credit" in r.text.lower() else f"error {r.status_code}"
            mark("ai", False, why)
            return True, fallback_lines(title), "", 0
        text = r.json()["content"][0]["text"].strip()
        text = re.sub(r"^```(json)?|```$", "", text).strip()
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0) if m else text)
        mark("ai", True)
        lines = data.get("lines") or []
        if isinstance(lines, str):
            lines = [lines]
        lines = [str(l).lstrip(">").strip() for l in lines if str(l).strip()][:3]
        if not AI_SIMPLE_HEADLINES or not lines:
            lines = fallback_lines(title)
        take = (data.get("take") or "").strip() if want_take else ""
        keep = bool(data.get("keep", True)) if AI_FILTER else True
        try:
            score = int(data.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        return keep, lines, take, score
    except Exception as e:
        print("AI failed:", e)
        mark("ai", False, f"bad response: {str(e)[:80]}")
        return True, fallback_lines(title), "", 0


TELEGRAM_DOWN = False   # set when Telegram can't be reached, so the run stops hammering it


def send(text, silent=False, reply_to=None):
    """Posts to Telegram. Returns the message id if it worked, else None. Never crashes the run."""
    global TELEGRAM_DOWN
    if not BOT_TOKEN or not CHAT_ID:
        print("[dry run]", text.replace("\n", " | "))
        return 1
    if TELEGRAM_DOWN:
        return None
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True, "disable_notification": silent}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    for attempt in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                              json=payload, timeout=20)
        except requests.RequestException as e:
            print("Telegram unreachable:", e)
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:  # Telegram rate limit, wait and retry
            try:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
            except ValueError:
                wait = 5
            time.sleep(min(wait, 30) + 1)
            continue
        if r.status_code == 400 and "parse" in r.text.lower() and payload.get("parse_mode"):
            # formatting problem: resend as plain text instead of losing the story
            payload.pop("parse_mode")
            payload["text"] = re.sub(r"<[^>]+>", "", text).replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
            continue
        if not r.ok:
            print("Telegram error:", r.status_code, r.text[:200])
            return None
        try:
            return r.json()["result"]["message_id"]
        except (ValueError, KeyError):
            return 1
    TELEGRAM_DOWN = True
    mark("telegram posting", False, "can't reach telegram")
    return None


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_post(item, lines):
    link = f"<a href=\"{esc(item['link'])}\">read</a>"
    if STYLE == "greentext":
        if len(lines) > 1:  # AI-written: only trim runaway lines, never normal ones
            lines = [shorten(l, MAX_LINE_CHARS + 20) for l in lines]
        head, rest = lines[0], lines[1:3]
        alert = "🚨🚨 MAJOR: " if item.get("major") else ("🚨 " if item["hot"] else "")
        body = f"<b>{alert}{esc(head.upper())}</b>"
        if rest:
            body += "\n" + "\n".join("&gt;" + esc(l) for l in rest)
        src = esc(item["source"].lower())
        return f"{body}\n<a href=\"{esc(item['link'])}\">{src}</a>"
    headline = " ".join(lines)
    prefix = "🚨🚨 MAJOR: " if item.get("major") else ("🚨 " if item["hot"] else "")
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


# ───────────────────────── ALERTS ENGINE ─────────────────────────

CG = "https://api.coingecko.com/api/v3"


def cg_get(path, **params):
    try:
        r = requests.get(CG + path, params=params, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0 signalroom"})
        if r.status_code == 429:
            print("CoinGecko rate limit, skipping alerts this run")
            mark("coingecko alerts", False, "rate limited")
            return None
        r.raise_for_status()
        mark("coingecko alerts", True)
        return r.json()
    except Exception as e:
        print("CoinGecko failed:", e)
        mark("coingecko alerts", False, str(e))
        return None


def money(x):
    if x >= 1e9:
        return f"${x/1e9:.1f}B"
    if x >= 1e6:
        return f"${x/1e6:.0f}M"
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:,.2f}"
    if x <= 0:
        return "$0"
    digits = max(2, -int(f"{x:e}".split("e")[1]) + 3)   # keep 3-4 meaningful digits
    return f"${x:.{digits}f}".rstrip("0").rstrip(".")


def pct(x):
    return f"{x:+.1f}%"


def coin_link(cid, label):
    return f"<a href=\"https://www.coingecko.com/en/coins/{esc(cid)}\">{esc(label)}</a>"


def alert_msg(icon, sym, what, lines, cid):
    body = f"<b>{icon} {esc(sym.upper())} – {esc(what.upper())}</b>"
    body += "\n" + "\n".join("&gt;" + esc(l) for l in lines)
    return body + "\n" + coin_link(cid, "coingecko")


def cooled(state, key):
    """True if this alert is allowed now. Call sent_ok(state, key) only after it actually delivered."""
    last = state["alerts"]["cooldown"].get(key, 0)
    return time.time() - last >= ALERT_COOLDOWN_HOURS * 3600


def sent_ok(state, key):
    state["alerts"]["cooldown"][key] = time.time()


def alert(state, key, text):
    """Send an alert if off cooldown; start the cooldown only if it really went out."""
    if not cooled(state, key):
        return 0
    if send(text):
        sent_ok(state, key)
        return 1
    mark("telegram posting", False, "alert failed to send")
    return 0


def hl_markets():
    """Hyperliquid perps: returns list of dicts with sym, price, funding, oi_usd, vol_usd."""
    try:
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"},
                          timeout=20, headers={"Content-Type": "application/json"})
        r.raise_for_status()
        meta, ctxs = r.json()
        mark("shorts alerts", True)
        out = []
        for asset, ctx in zip(meta.get("universe", []), ctxs):
            try:
                price = float(ctx.get("markPx") or ctx.get("midPx") or 0)
                if not price:
                    continue
                out.append({
                    "sym": asset["name"],
                    "price": price,
                    "prev_day": float(ctx.get("prevDayPx") or 0),
                    "funding": float(ctx.get("funding") or 0),
                    "oi_usd": float(ctx.get("openInterest") or 0) * price,
                    "vol_usd": float(ctx.get("dayNtlVlm") or 0),
                })
            except (TypeError, ValueError, KeyError):
                continue
        return out
    except Exception as e:
        print("Hyperliquid failed:", e)
        mark("shorts alerts", False, str(e))
        return None


def hl_link(sym):
    return f"<a href=\"https://app.hyperliquid.xyz/trade/{esc(sym)}\">hyperliquid</a>"


def futures_msg(icon, sym, what, lines):
    body = f"<b>{icon} {esc(sym.upper())} – {esc(what.upper())}</b>"
    body += "\n" + "\n".join("&gt;" + esc(l) for l in lines)
    return body + "\n" + hl_link(sym)


def run_shorts_alerts(state):
    a = state["alerts"]
    a.setdefault("hl_price", {})
    a.setdefault("oi_avg", {})
    a.setdefault("oi_n", {})
    mkts = hl_markets()
    if not mkts:
        return 0
    sent = 0
    for m in mkts:
        sym, price, fund, oi = m["sym"], m["price"], m["funding"], m["oi_usd"]
        if oi < MIN_OI_USD:
            continue
        last = a["hl_price"].get(sym)
        fund_txt = f"funding {fund*100:+.3f}%/h"

        # 💥 short squeeze: price ripping since last check while shorts pay funding
        if last and fund < 0:
            move = (price / last - 1) * 100
            if move >= SQUEEZE_PCT:
                sent += alert(state, f"squeeze:{sym}", futures_msg("💥", sym, "squeeze risk",
                              [f"{pct(move)} since last check", "shorts paying funding", f"oi {money(oi)}"]))

        # 🩳 crowded shorts
        if fund <= CROWDED_FUNDING:
            day = (price / m["prev_day"] - 1) * 100 if m["prev_day"] else 0
            sent += alert(state, f"crowded:{sym}", futures_msg("🩳", sym, "shorts crowded",
                          [fund_txt, f"oi {money(oi)}", f"24h {pct(day)}"]))

        # 📈 open interest spike vs its running average
        avg, n = a["oi_avg"].get(sym), a["oi_n"].get(sym, 0)
        if avg and n >= 6 and oi >= OI_SPIKE_X * avg:
            sent += alert(state, f"oi:{sym}", futures_msg("📈", sym, "oi spike",
                          [f"open bets {oi/avg:.1f}x recent avg", f"now {money(oi)}", fund_txt]))

        a["hl_price"][sym] = price
        a["oi_avg"][sym] = oi if not avg else avg * 0.9 + oi * 0.1
        a["oi_n"][sym] = n + 1
    return sent


def run_alerts(state):
    if not ALERTS_ON:
        return 0
    a = state.setdefault("alerts", {})
    a.setdefault("cooldown", {})
    a.setdefault("vol_avg", {})
    a.setdefault("vol_n", {})
    a.setdefault("armed", {})
    a.setdefault("trending", [])
    sent = 0

    coins = cg_get("/coins/markets", vs_currency="usd", order="market_cap_desc",
                   per_page=250, page=1, price_change_percentage="1h,24h")
    if coins:
        by_id = {c["id"]: c for c in coins}

        # 🎯 price alerts (fire once, re-arm after price moves back 1% the other way)
        for cid, side, level in PRICE_ALERTS:
            c = by_id.get(cid) or {}
            price = c.get("current_price")
            if price is None:
                data = cg_get("/simple/price", ids=cid, vs_currencies="usd")
                price = (data or {}).get(cid, {}).get("usd")
                if price is None:
                    continue
            key = f"{cid}:{side}:{level}"
            hit = price >= level if side == "above" else price <= level
            if hit and a["armed"].get(key, True):
                sym = c.get("symbol", cid)
                lines = [f"now {money(price)}", f"24h {pct(c.get('price_change_percentage_24h') or 0)}"]
                if send(alert_msg("🎯", sym, f"{side} {money(level)}", lines, cid)):
                    a["armed"][key] = False  # only disarm once it's actually delivered
                    sent += 1
            elif not hit:
                back = price < level * 0.99 if side == "above" else price > level * 1.01
                if back:
                    a["armed"][key] = True

        for c in coins:
            cid, sym = c["id"], c.get("symbol", "")
            vol = c.get("total_volume") or 0
            ch1 = c.get("price_change_percentage_1h_in_currency")
            price = c.get("current_price") or 0

            # ⚡ big 1h moves
            limit = MAJOR_MOVE_PCT if cid in MAJORS else ALT_MOVE_PCT
            if ch1 is not None and abs(ch1) >= limit and (vol >= MIN_VOLUME_USD or cid in MAJORS):
                what = "pumping" if ch1 > 0 else "dumping"
                sent += alert(state, f"move:{cid}",
                              alert_msg("⚡", sym, what, [f"{pct(ch1)} in 1h", f"now {money(price)}"], cid))

            # 📊 volume spikes vs its own running average
            avg = a["vol_avg"].get(cid)
            n = a["vol_n"].get(cid, 0)
            if avg and n >= 6 and vol >= MIN_VOLUME_USD and vol >= VOLUME_SPIKE_X * avg:
                lines = [f"24h vol {vol/avg:.1f}x its recent avg", f"24h vol {money(vol)}",
                         f"price {pct(c.get('price_change_percentage_24h') or 0)}"]
                sent += alert(state, f"vol:{cid}", alert_msg("📊", sym, "volume up", lines, cid))
            if vol:
                a["vol_avg"][cid] = vol if not avg else avg * 0.9 + vol * 0.1
                a["vol_n"][cid] = n + 1

        # keep state small: only coins still in the top 250
        for k in ("vol_avg", "vol_n"):
            a[k] = {cid: v for cid, v in a[k].items() if cid in by_id}

    # 🔥 trending
    if TRENDING_ALERTS:
        t = cg_get("/search/trending")
        if t and t.get("coins"):
            now_ids = []
            for rank, item in enumerate(t["coins"][:7], 1):
                it = item.get("item", {})
                cid = it.get("id")
                if not cid:
                    continue
                now_ids.append(cid)
                if a["trending"] and cid not in a["trending"]:
                    ch = ((it.get("data") or {}).get("price_change_percentage_24h") or {}).get("usd")
                    lines = [f"#{rank} on coingecko"]
                    if ch is not None:
                        lines.append(f"24h {pct(ch)}")
                    ok = alert(state, f"trend:{cid}", alert_msg("🔥", it.get("symbol", cid), "trending", lines, cid))
                    sent += ok
                    if not ok and cooled(state, f"trend:{cid}"):
                        now_ids.remove(cid)  # delivery failed: leave it "new" so next run retries
            a["trending"] = now_ids  # first run just learns the list

    # 🩳💥📈 futures / shorts
    if SHORTS_ALERTS:
        sent += run_shorts_alerts(state)

    # clean old cooldowns
    cutoff = time.time() - 2 * ALERT_COOLDOWN_HOURS * 3600
    a["cooldown"] = {k: v for k, v in a["cooldown"].items() if v > cutoff}
    if sent:
        print(f"Sent {sent} alerts.")
    return sent


# ───────────────────────── MAIN ─────────────────────────


def main():
    state = load_state()
    seen = set(state["seen"])
    new_items = []

    def done(key):
        """Only mark a story seen once we've actually handled it (posted, or decided to skip)."""
        state["seen"].append(key)

    for category, emoji, url in FEEDS:
        name = url.split("/")[2].replace("www.", "")
        is_search = "news.google.com" in url
        if is_search:
            name = "google news: " + unquote_plus(url.split("q=")[1].split("&")[0]).replace(" when:2h", "")[:30]
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 newsbot"})
        except Exception as e:
            print("Feed failed:", url, e)
            mark(f"feed {name}", False, str(e))
            continue
        if feed.entries or (is_search and not feed.bozo):
            mark(f"feed {name}", True)  # a search with no results in the last 2h is normal, not broken
        else:
            print("Feed empty or broken:", url)
            mark(f"feed {name}", False, "no stories coming through")
        feed_title = clean_source(feed.feed.get("title", category))
        for entry in feed.entries[:25]:
            link = entry.get("link", "")
            raw_title = re.sub(r"\s+", " ", entry.get("title", "")).strip()
            key = link or raw_title
            if not raw_title or key in seen:
                continue
            seen.add(key)  # avoids double-handling within this run; saved as seen only when handled
            if entry_age_hours(entry) > MAX_AGE_HOURS:
                done(key)
                continue
            title, source = split_source(raw_title, entry, feed_title)
            source = clean_source(source)
            new_items.append({
                "key": key, "title": title, "source": source, "link": link,
                "emoji": emoji, "category": category, "hot": on_watchlist(title),
            })

    # First ever run: just remember what's out there, don't dump 200 old posts
    if not state.get("initialized"):
        for it in new_items:
            done(it["key"])
            state["recent_titles"].append(sorted(words(it["title"])))
        state["initialized"] = True
        save_state(state)
        send("✅ <b>NEWS WIRE ONLINE</b> — you'll get new headlines from here on.")
        print(f"Initialized with {len(new_items)} items.")
        return

    # Saved queue: stories that haven't been delivered yet survive between runs,
    # even if they drop out of the RSS feed. They expire after QUEUE_MAX_HOURS.
    queue = state.setdefault("queue", [])
    queued_keys = {q["key"] for q in queue}
    for it in new_items:
        if it["key"] not in queued_keys:
            it["queued_at"] = time.time()
            queue.append(it)
            queued_keys.add(it["key"])
    cutoff = time.time() - QUEUE_MAX_HOURS * 3600
    expired = [q for q in queue if q.get("queued_at", 0) < cutoff]
    for q in expired:
        print("Expired from queue (too old now):", q["title"])
        done(q["key"])
    queue[:] = [q for q in queue if q.get("queued_at", 0) >= cutoff][-QUEUE_MAX_ITEMS:]

    # Watchlist stuff first, then oldest first
    queue.sort(key=lambda x: (not x["hot"], x.get("queued_at", 0)))

    posted = skipped = ai_calls = 0
    finished = set()
    for it in queue:
        if posted >= MAX_POSTS_PER_RUN or TELEGRAM_DOWN:
            break  # rest stays in the queue for next run
        key = it["key"]
        if is_duplicate(it["title"], state["recent_titles"]):
            finished.add(key)
            continue
        if blocked(it["title"]):
            skipped += 1
            finished.add(key)
            continue
        # AI result is saved on the item, so a retry doesn't pay for the AI again
        if "lines" not in it:
            want_take = STYLE != "greentext" and (AI_TAKES == "all" or (AI_TAKES == "hot" and it["hot"]))
            if ai_calls < MAX_AI_CALLS_PER_RUN:
                ai_calls += 1
                keep, lines, take, score = ai_process(it["title"], it["source"], want_take)
            else:
                keep, lines, take, score = True, fallback_lines(it["title"]), "", 0
            if ANTHROPIC_KEY and 0 < score < MIN_SCORE:
                keep = False
            it.update(keep=keep, lines=lines, take=take, score=score)
        it["major"] = it["score"] >= MAJOR_SCORE
        if not it["keep"]:
            skipped += 1
            finished.add(key)
            print("Skipped (junk):", it["title"])
            continue
        lines = it["lines"]
        rewritten = " ".join(lines)
        if len(lines) > 1 and is_duplicate(rewritten, state.get("recent_rewrites", [])):
            skipped += 1
            finished.add(key)
            state["recent_titles"].append(sorted(words(it["title"])))
            print("Skipped (same story):", it["title"])
            continue
        loud = it["major"] or it["hot"] or not LOUD_ONLY_FOR_WATCHLIST
        msg_id = send(format_post(it, lines), silent=not loud)
        if msg_id:
            finished.add(key)
            state["recent_titles"].append(sorted(words(it["title"])))
            state.setdefault("recent_rewrites", []).append(sorted(words(rewritten)))
            posted += 1
            if it.get("take"):
                send(format_take(it["take"]), silent=True, reply_to=msg_id)
            time.sleep(1.5)
        else:
            it["tries"] = it.get("tries", 0) + 1  # stays queued, retried next run until it expires
            mark("telegram posting", False, "messages failing to send")

    for key in finished:
        done(key)
    queue[:] = [q for q in queue if q["key"] not in finished]
    waiting = len(queue)

    run_alerts(state)
    price_pulse(state)
    check_health(state)
    state.pop("send_fails", None)
    save_state(state)
    print(f"Posted {posted}, skipped {skipped}, {waiting} still queued, {len(new_items)} new this run.")


if __name__ == "__main__":
    main()
