"""
Picster Booking Watcher & Auto-Claimer — Human-like Chrome mode

Attaches to a real, user-launched Chrome over CDP (port 9223) and
reuses its logged-in picster.app session for all HTTP calls — no headless
browser, no automation fingerprint.

How it works:
  1. Run launch_picster_chrome.bat once and log into picster.app in that
     Chrome (same profile/session as before).
  2. Start this bot: `python picster_bot.py`.
  3. The bot polls the bookings grid month views through the browser's
     session, alerts on new bookings via Telegram, and auto-claims
     ("Assign myself") bookings that fall inside the configured windows
     in picster_config.json.

Telegram commands: /status /screenshot /fast /normal /interval /start /stop /subscribers
"""

import asyncio
import json
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Windows cp1252 stdout breaks on emoji (📸, 📹, etc.) — reconfigure to UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests as req_lib
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from telegram import send_telegram
from picster.subscribers import load_subscribers
from picster.config import load_config, config_reloader
from picster.watcher import run_grid_watcher
from picster.reverifier import reverify_bookings_loop
from picster.commands import telegram_command_listener

load_dotenv()

HEARTBEAT_INTERVAL = 6 * 60 * 60
HEARTBEAT_FILE     = Path(__file__).parent / "bot_heartbeat.json"
_BAT_PATH          = Path(__file__).parent / "launch_picster_chrome.bat"


async def _heartbeat_writer(state):
    """Write a timestamp file every 20 s so the UI can detect a live bot."""
    while True:
        try:
            HEARTBEAT_FILE.write_text(
                json.dumps({"ts": datetime.now().isoformat()}),
                encoding="utf-8",
            )
        except Exception:
            pass
        await asyncio.sleep(20)


async def _telegram_heartbeat(state):
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        send_telegram(f"Picster bot still running. {state.get('check_count', 0)} poll cycles done.")


async def ensure_browser_running(cdp_endpoint: str) -> bool:
    """Return True if Chrome is reachable on cdp_endpoint, launching it via the bat if not."""
    version_url = cdp_endpoint.rstrip("/") + "/json/version"

    def _probe():
        try:
            return req_lib.get(version_url, timeout=2).status_code == 200
        except Exception:
            return False

    loop = asyncio.get_event_loop()

    if await loop.run_in_executor(None, _probe):
        print("  [Chrome] Already running.")
        return True

    if not _BAT_PATH.exists():
        print(f"  [Chrome] {_BAT_PATH.name} not found — please launch Chrome manually.")
        return False

    print(f"  [Chrome] Browser not detected — launching {_BAT_PATH.name}...")
    # CREATE_NO_WINDOW (not CREATE_NEW_CONSOLE): no cmd window flashes up for
    # this — the .bat itself already detaches Chrome via `start` and exits
    # immediately, so there's nothing useful to show here anyway.
    subprocess.Popen(
        ["cmd", "/c", str(_BAT_PATH)],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + 30
    while time.time() < deadline:
        await asyncio.sleep(1.5)
        if await loop.run_in_executor(None, _probe):
            print("  [Chrome] Browser ready.")
            return True

    print("  [Chrome] Warning: browser didn't respond within 30s — trying anyway.")
    return False


async def run():
    config       = load_config()
    urls         = config["urls"]
    cdp_endpoint = config["cdp_endpoint"]
    base_url     = config.get("base_url", "https://picster.app")
    crew_name    = config.get("crew_name", "")

    print("Picster Booking Watcher — human Chrome mode")
    print(f"CDP endpoint: {cdp_endpoint}")
    print(f"Watching: {', '.join(e['name'] for e in urls)}")
    print(f"Interval: {config['check_interval_min_seconds']}-{config['check_interval_max_seconds']}s\n")

    # Persist these across Chrome reconnects
    check_count   = 0
    seen_ids      = {}  # {booking_id: booking date} — pruned in watcher.py
    seeded_months = set()
    subs          = load_subscribers()
    first_connect = True

    while True:                             # ── outer restart loop ──
        try:
            restart = await _run_one_session(
                config, urls, cdp_endpoint, base_url, crew_name,
                check_count, seen_ids, seeded_months, subs, first_connect,
            )
        except Exception as e:
            # Covers async_playwright()'s own teardown throwing (e.g. its
            # __aexit__ trying to cleanly stop an already-dead driver pipe)
            # as well as anything else unexpected. Never let this loop die —
            # that's exactly the "bot silently stops for a day" failure mode.
            print(f"[!] Session crashed: {e}\n    Retrying in 10s...")
            await asyncio.sleep(10)
            continue

        check_count, seen_ids, seeded_months, subs, first_connect = restart
        await asyncio.sleep(3)


async def _run_one_session(config, urls, cdp_endpoint, base_url, crew_name,
                            check_count, seen_ids, seeded_months, subs, first_connect):
    """
    Run one Chrome/Playwright session until it dies (Chrome disconnect, or
    the Playwright driver connection itself dying), then return the counters
    that should carry over into the next session.

    async_playwright() is created fresh on every call — a driver-level death
    ("Connection closed while reading from the driver") kills that object
    permanently; reconnecting to Chrome on the same instance would keep
    failing identically. See picster/driver_health.py.
    """
    async with async_playwright() as p:
        await ensure_browser_running(cdp_endpoint)

        try:
            browser = await p.chromium.connect_over_cdp(cdp_endpoint)
        except Exception as e:
            print(f"[!] Could not attach to Chrome: {e}\n    Retrying in 10s...")
            await asyncio.sleep(10)
            return check_count, seen_ids, seeded_months, subs, first_connect

        disconnected = asyncio.Event()
        fatal        = asyncio.Event()   # set by watcher/reverifier on driver-death
        browser.on("disconnected", lambda: disconnected.set())

        context   = browser.contexts[0] if browser.contexts else await browser.new_context()

        # Defensive cleanup: if the previous session died via a broken driver
        # pipe (see picster/driver_health.py), it couldn't get to page.close()
        # on its way out, leaving a stray picster.app tab open in this same,
        # reused Chrome context. Close any before opening today's page, or
        # every driver restart leaks one more tab into the real browser.
        for stray in list(context.pages):
            if (stray.url or "").rstrip("/").startswith(base_url.rstrip("/")):
                try:
                    await stray.close()
                except Exception:
                    pass

        page      = await context.new_page()
        page_lock = asyncio.Lock()

        try:
            await page.goto(f"{base_url}/bookings/",
                            timeout=45_000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[!] Could not open bookings page: {e}")

        state = {
            "check_count":     check_count,
            "seen_ids":        seen_ids,
            "seeded_months":   seeded_months,
            "last_check_time": None,
            "last_poll_ts":    None,
            "next_check_in":   None,
            "interval_min":    config["check_interval_min_seconds"],
            "interval_max":    config["check_interval_max_seconds"],
            "subscribers":     subs,
            "context":         context,
            "page":            page,
            "page_lock":       page_lock,
            "fatal_event":     fatal,
        }

        if first_connect:
            keywords = ", ".join(k for e in urls for k in e.get("keywords", [])) or "(any)"
            send_telegram(
                f"Picster bot started (human Chrome).\n"
                f"Watching {', '.join(e['name'] for e in urls)} every "
                f"{state['interval_min']}-{state['interval_max']}s.\n"
                f"Keywords: {keywords}\n"
                f"Commands: /status, /screenshot, /fast, /normal, /interval"
            )
            first_connect = False
        else:
            send_telegram("Reconnected — Picster bot resuming.")

        request_ctx = context.request

        tasks = [
            asyncio.create_task(telegram_command_listener(state, page_lock, page, config)),
            asyncio.create_task(config_reloader(urls)),
            asyncio.create_task(reverify_bookings_loop(state, request_ctx, base_url, crew_name)),
            asyncio.create_task(_heartbeat_writer(state)),
            asyncio.create_task(_telegram_heartbeat(state)),
        ]
        for entry in urls:
            tasks.append(asyncio.create_task(
                run_grid_watcher(request_ctx, entry, state, config)
            ))

        waiters = [
            asyncio.create_task(disconnected.wait()),
            asyncio.create_task(fatal.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Preserve counters for the next session
            check_count = state["check_count"]
            subs        = state["subscribers"]
            for t in [*waiters, *tasks]:
                t.cancel()
            await asyncio.gather(*waiters, *tasks, return_exceptions=True)
            try:
                await page.close()  # else this tab is orphaned in the real Chrome forever
            except Exception:
                pass

        if fatal.is_set():
            print("[Playwright] Driver connection lost — restarting driver...")
            send_telegram("Playwright driver connection was lost — restarting from scratch...")
        else:
            print("[Chrome] Browser disconnected — waiting to reconnect...")
            send_telegram("Chrome disconnected — attempting to reconnect...")

    return check_count, seen_ids, seeded_months, subs, first_connect


if __name__ == "__main__":
    asyncio.run(run())
