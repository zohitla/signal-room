"""
Channel copier for Signal Room.
Reads new posts from channels YOU follow (tradfi, CRYPTO NEWS, ...) using your
Telegram account, and reposts them into Signal Room through your bot.

Needs 3 extra GitHub secrets: TG_API_ID, TG_API_HASH, TG_SESSION
"""

import asyncio
import html
import json
import os
import re
import time

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import InputPeerChannel, MessageMediaPhoto

from bot import BOT_TOKEN, CHAT_ID, LOUD_ONLY_FOR_WATCHLIST, on_watchlist

# ───────────────────────── SETTINGS (edit these) ─────────────────────────

# Channels to copy. Use the name EXACTLY like it shows in Telegram, or its @username.
SOURCES = [
    "tradfi",
    "CRYPTO NEWS",
]

MAX_PER_CHANNEL = 10   # max posts copied from one channel per run
MAX_TOTAL = 25         # max posts per run overall
SEND_PHOTOS = True     # copy pictures too (CRYPTO NEWS uses them)

# ─────────────────────────────────────────────────────────────────────────

API_ID = os.environ.get("TG_API_ID", "")
API_HASH = os.environ.get("TG_API_HASH", "")
SESSION = os.environ.get("TG_SESSION", "")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "copier_state.json")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"peers": {}, "last": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def clean_html(s):
    """Keep formatting the Bot API understands, drop custom emoji tags it may reject."""
    s = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", s, flags=re.S)
    return s


def tg_post(method, data, files=None):
    for _ in range(3):
        r = requests.post(f"{API}/{method}", data=data, files=files, timeout=60)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        return r
    return r


def send_text(text_html, text_plain, silent):
    base = {"chat_id": CHAT_ID, "disable_notification": silent, "disable_web_page_preview": True}
    r = tg_post("sendMessage", {**base, "text": text_html, "parse_mode": "HTML"})
    if not r.ok:  # formatting problem -> fall back to plain text
        r = tg_post("sendMessage", {**base, "text": text_plain})
    if not r.ok:
        print("Telegram error:", r.text)
    return r.ok


def send_photo(photo_bytes, caption_html, caption_plain, silent):
    base = {"chat_id": CHAT_ID, "disable_notification": silent}
    files = {"photo": ("photo.jpg", photo_bytes)}
    if len(caption_plain) <= 1000:
        r = tg_post("sendPhoto", {**base, "caption": caption_html, "parse_mode": "HTML"}, files)
        if not r.ok:
            r = tg_post("sendPhoto", {**base, "caption": caption_plain}, {"photo": ("photo.jpg", photo_bytes)})
        if not r.ok:
            print("Telegram photo error:", r.text)
        return r.ok
    # caption too long for a photo: send picture, then the text
    tg_post("sendPhoto", base, files)
    return send_text(caption_html, caption_plain, silent)


async def resolve_sources(client, state):
    missing = [s for s in SOURCES if s not in state["peers"]]
    if not missing:
        return
    found = {}
    for s in missing:
        if s.startswith("@"):
            try:
                ent = await client.get_entity(s)
                found[s] = ent
            except Exception as e:
                print(f"Couldn't find {s}: {e}")
    names = {s.strip().lower(): s for s in missing if not s.startswith("@")}
    if names:
        async for d in client.iter_dialogs():
            key = (d.name or "").strip().lower()
            if key in names and d.is_channel and names[key] not in found:
                found[names[key]] = d.entity
    for s, ent in found.items():
        state["peers"][s] = {"id": ent.id, "hash": ent.access_hash, "name": getattr(ent, "title", s)}
        print(f"Found channel: {s}")
    for s in missing:
        if s not in found:
            print(f"⚠️ Couldn't find a channel called '{s}'. Check the spelling in SOURCES.")


async def run():
    if not (API_ID and API_HASH and SESSION):
        print("Copier secrets not set yet (TG_API_ID / TG_API_HASH / TG_SESSION). Skipping.")
        return
    if not (BOT_TOKEN and CHAT_ID):
        print("Bot token / chat id missing. Skipping.")
        return

    state = load_state()
    try:
        client = TelegramClient(StringSession(SESSION.strip()), int(API_ID), API_HASH.strip())
    except ValueError:
        print("❌ TG_SESSION looks wrong. Re-copy the whole long code from the login step.")
        return
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ Telegram login expired. Make a new TG_SESSION with the login steps.")
        return
    client.parse_mode = "html"

    await resolve_sources(client, state)
    total = 0

    for src in SOURCES:
        p = state["peers"].get(src)
        if not p:
            continue
        peer = InputPeerChannel(p["id"], p["hash"])
        key = str(p["id"])
        last = state["last"].get(key)

        # First time seeing this channel: just remember where we are, don't dump old posts
        if last is None:
            latest = await client.get_messages(peer, limit=1)
            state["last"][key] = latest[0].id if latest else 0
            print(f"Started watching {p['name']}")
            continue

        msgs = await client.get_messages(peer, limit=MAX_PER_CHANNEL * 2, min_id=last)
        msgs = sorted(msgs, key=lambda m: m.id)
        # Albums come as several messages; keep just one (the one with the caption)
        album_pick = {}
        for m in msgs:
            if m.grouped_id and (m.grouped_id not in album_pick or (m.raw_text and not album_pick[m.grouped_id].raw_text)):
                album_pick[m.grouped_id] = m
        copied = 0

        for m in msgs:
            state["last"][key] = max(state["last"][key], m.id)
            if total >= MAX_TOTAL or copied >= MAX_PER_CHANNEL:
                continue  # skip extras but keep moving the bookmark forward
            if m.grouped_id and album_pick.get(m.grouped_id) is not m:
                continue

            plain = (m.raw_text or "").strip()
            is_photo = SEND_PHOTOS and isinstance(m.media, MessageMediaPhoto)
            if not plain and not is_photo:
                continue

            header_html = f"📡 <b>{html.escape(p['name'])}</b>\n"
            header_plain = f"📡 {p['name']}\n"
            body_html = clean_html(m.text or "")
            text_html = header_html + body_html
            text_plain = header_plain + plain

            silent = LOUD_ONLY_FOR_WATCHLIST and not on_watchlist(plain)

            ok = False
            if is_photo:
                try:
                    data = await client.download_media(m, file=bytes)
                    ok = send_photo(data, text_html, text_plain, silent)
                except Exception as e:
                    print("Photo failed, sending text:", e)
                    ok = send_text(text_html, text_plain, silent) if plain else False
            else:
                ok = send_text(text_html, text_plain, silent)

            if ok:
                copied += 1
                total += 1
                time.sleep(1.5)

        if copied:
            print(f"Copied {copied} from {p['name']}")

    await client.disconnect()
    save_state(state)
    print(f"Copier done, {total} posts copied.")


if __name__ == "__main__":
    asyncio.run(run())
