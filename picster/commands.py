"""Telegram command listener for the picster bot (port of legacy/commands.py)."""
import asyncio
import os
from datetime import datetime

from telegram import get_telegram_updates
from legacy.subscribers import (
    reply_telegram, reply_telegram_photo, save_subscribers,
)


async def _cmd_start(chat_id, sender_name, raw, state, page_lock, page, cfg):
    subs = state["subscribers"]
    cid = str(chat_id)
    if cid in subs:
        reply_telegram(chat_id, f"Already subscribed as {subs[cid]}. You'll get new booking alerts.")
    else:
        subs[cid] = sender_name
        save_subscribers(subs)
        keywords = ", ".join(k for e in cfg["urls"] for k in e.get("keywords", [])) or "(any)"
        reply_telegram(chat_id,
            f"Subscribed as {sender_name}. You'll now get a ping when a new "
            f"booking matching {keywords} appears.\n"
            f"Send /stop to unsubscribe, /status for bot state."
        )
        print(f"[SUB] +{sender_name} ({cid}) — total {len(subs)}")


async def _cmd_stop(chat_id, sender_name, raw, state, page_lock, page, cfg):
    subs = state["subscribers"]
    cid = str(chat_id)
    if cid in subs:
        del subs[cid]
        save_subscribers(subs)
        reply_telegram(chat_id, "Unsubscribed. Send /start to re-enable alerts.")
        print(f"[SUB] -{cid} — total {len(subs)}")
    else:
        reply_telegram(chat_id, "You're not subscribed. Send /start to subscribe.")


async def _cmd_subscribers(chat_id, sender_name, raw, state, page_lock, page, cfg):
    subs = state["subscribers"]
    if not subs:
        reply_telegram(chat_id, "No subscribers.")
    else:
        lines = [f"Subscribers ({len(subs)}):"]
        lines.extend(f"• {name} ({cid})" for cid, name in subs.items())
        reply_telegram(chat_id, "\n".join(lines))


async def _cmd_status(chat_id, sender_name, raw, state, page_lock, page, cfg):
    imin, imax = state["interval_min"], state["interval_max"]
    next_in = state["next_check_in"]
    keywords = ", ".join(k for e in cfg["urls"] for k in e.get("keywords", [])) or "(none)"
    reply_telegram(chat_id,
        f"Picster bot (human Chrome)\n"
        f"Poll cycles: {state['check_count']}\n"
        f"Last: {state['last_check_time'] or 'not yet'}\n"
        f"Next in: {f'{next_in:.0f}s' if next_in is not None else 'soon'}\n"
        f"Interval: {imin}-{imax}s\n"
        f"Watching: {', '.join(e['name'] for e in cfg['urls'])}\n"
        f"Keywords: {keywords}\n"
        f"Subscribers: {len(state['subscribers'])}"
    )


async def _cmd_screenshot(chat_id, sender_name, raw, state, page_lock, page, cfg):
    reply_telegram(chat_id, "Taking screenshots...")
    async with page_lock:
        for entry in cfg["urls"]:
            try:
                target = entry["url"].rstrip("/")
                current = (page.url or "").rstrip("/").split("?")[0]
                if not current.startswith(target):
                    await page.goto(entry["url"], timeout=45_000, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = f"picster_cmd_{entry['name']}_{ts}.png"
                await page.screenshot(path=path, full_page=True)
                reply_telegram_photo(chat_id, path, caption=entry["name"])
            except Exception as e:
                reply_telegram(chat_id, f"Screenshot failed ({entry['name']}): {e}")


async def _cmd_fast(chat_id, sender_name, raw, state, page_lock, page, cfg):
    state["interval_min"] = 2
    state["interval_max"] = 4
    reply_telegram(chat_id, "Fast mode: polling every 2-4s.")


async def _cmd_normal(chat_id, sender_name, raw, state, page_lock, page, cfg):
    state["interval_min"] = cfg["default_min"]
    state["interval_max"] = cfg["default_max"]
    reply_telegram(chat_id, f"Normal mode: polling every {cfg['default_min']}-{cfg['default_max']}s.")


async def _cmd_interval(chat_id, sender_name, raw, state, page_lock, page, cfg):
    try:
        parts = raw.split()
        imin = int(parts[1])
        imax = int(parts[2])
        if imin < 2:
            reply_telegram(chat_id, "Minimum interval is 2s.")
        elif imin >= imax:
            reply_telegram(chat_id, "First value must be less than second.")
        else:
            state["interval_min"] = imin
            state["interval_max"] = imax
            reply_telegram(chat_id, f"Interval set to {imin}-{imax}s.")
    except (IndexError, ValueError):
        reply_telegram(chat_id, "Usage: /interval <min> <max> (seconds)")


_DISPATCH = {
    "/start":       _cmd_start,
    "/subscribe":   _cmd_start,
    "/stop":        _cmd_stop,
    "/unsubscribe": _cmd_stop,
    "/subscribers": _cmd_subscribers,
    "/status":      _cmd_status,
    "/screenshot":  _cmd_screenshot,
    "/fast":        _cmd_fast,
    "/normal":      _cmd_normal,
    "/interval":    _cmd_interval,
}

_ADMIN_CMDS = {"/subscribers", "/screenshot", "/fast", "/normal", "/interval"}


async def telegram_command_listener(state, page_lock, page, config):
    cfg = {
        "urls": config["urls"],
        "default_min": config["check_interval_min_seconds"],
        "default_max": config["check_interval_max_seconds"],
    }
    offset = 0

    while True:
        updates = await asyncio.get_event_loop().run_in_executor(
            None, get_telegram_updates, offset
        )

        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            if not chat_id:
                continue
            raw = msg.get("text", "").strip()
            cmd = raw.lower().split()[0] if raw.split() else ""
            sender_name = (
                msg.get("from", {}).get("username")
                or msg.get("from", {}).get("first_name")
                or str(chat_id)
            )

            owner_id = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
            if cmd in _ADMIN_CMDS and str(chat_id) != owner_id:
                reply_telegram(chat_id, "Unauthorized.")
                continue

            handler = _DISPATCH.get(cmd)
            if handler:
                print(f"[CMD] {cmd}")
                try:
                    await handler(chat_id, sender_name, raw, state, page_lock, page, cfg)
                except Exception as e:
                    print(f"[CMD] {cmd} failed: {e}")

        await asyncio.sleep(3)
