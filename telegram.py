import os

import requests as req_lib


def send_telegram(message, chat_id=None):
    token = os.environ["TELEGRAM_TOKEN"]
    if chat_id is None:
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
    try:
        req_lib.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[!] Telegram message failed: {e}")


def send_telegram_photo(path, caption="", chat_id=None):
    token = os.environ["TELEGRAM_TOKEN"]
    if chat_id is None:
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
    try:
        with open(path, "rb") as f:
            req_lib.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=20,
            )
    except Exception as e:
        print(f"[!] Telegram photo failed: {e}")


def get_telegram_updates(offset):
    token = os.environ["TELEGRAM_TOKEN"]
    try:
        r = req_lib.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=10,
        )
        return r.json().get("result", [])
    except Exception:
        return []
