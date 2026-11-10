"""Print the chat ids your bot can currently see.

Add the bot to the group, send any message there, then run this:

    python tools/get_chat_id.py

Group ids are negative. Put the one you want into TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not token:
    sys.exit("TELEGRAM_BOT_TOKEN is not set (put it in .env)")

r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
r.raise_for_status()
updates = r.json().get("result", [])

if not updates:
    sys.exit(
        "No updates. Send a message in the group (mention the bot if it has "
        "privacy mode on), then run this again within a few minutes."
    )

seen: dict[int, str] = {}
for update in updates:
    msg = update.get("message") or update.get("channel_post") or {}
    chat = msg.get("chat") or {}
    if "id" in chat:
        label = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        seen[chat["id"]] = f"{chat.get('type', '?')} {label}".strip()

for chat_id, label in seen.items():
    print(f"{chat_id}\t{label}")
