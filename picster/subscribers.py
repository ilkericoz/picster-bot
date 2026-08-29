import json
import os

from telegram import send_telegram, send_telegram_photo

SUBSCRIBERS_PATH = "picster_subscribers.json"


def load_subscribers():
    """Returns {chat_id (str): name} dict. .env TELEGRAM_CHAT_ID is always included."""
    subs = {}
    try:
        with open(SUBSCRIBERS_PATH, encoding="utf-8") as f:
            for chat_id, name in json.load(f).items():
                subs[str(chat_id)] = name
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    seed = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if seed and seed not in subs:
        subs[seed] = "admin"
    return subs


def save_subscribers(subs):
    with open(SUBSCRIBERS_PATH, "w", encoding="utf-8") as f:
        json.dump(subs, f, indent=2)


def broadcast_alert(subs, message):
    """Fan out an alert to every subscriber."""
    for chat_id in subs:
        send_telegram(message, chat_id=chat_id)


def broadcast_photo(subs, path, caption=""):
    for chat_id in subs:
        send_telegram_photo(path, caption=caption, chat_id=chat_id)


def reply_telegram(chat_id, message):
    """Reply to a specific chat (the command sender)."""
    send_telegram(message, chat_id=chat_id)


def reply_telegram_photo(chat_id, path, caption=""):
    send_telegram_photo(path, caption=caption, chat_id=chat_id)
