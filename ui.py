import asyncio
import hmac
import json
import os
import socket
import time
import uuid as _uuid_mod
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from werkzeug.security import check_password_hash

from picster.schedule import (
    load_crew_schedule, save_crew_schedule, parse_booking_datetime,
)
from picster.parser import build_month_url, parse_grid

load_dotenv()

app = Flask(__name__)

BASE               = Path(__file__).parent
PICSTER_CONFIG     = BASE / "picster_config.json"  # the live bot's config — this app reads AND writes it
PICSTER_ITEMS_CACHE = BASE / "picster_items_cache.json"
BOT_HEARTBEAT      = BASE / "bot_heartbeat.json"
BOT_MAX_AGE_SECS   = 60   # heartbeat older than this → bot is considered dead

# ── Auth ─────────────────────────────────────────────────────────────────────

_rate_buckets: dict = {}
_RATE_MAX    = 5   # failed attempts before lockout
_RATE_WINDOW = 60  # seconds


def _client_ip() -> str:
    # Cloudflare sets CF-Connecting-IP; fall back through proxy headers to direct addr
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )


def _rate_ok(ip: str) -> bool:
    now    = time.monotonic()
    bucket = [t for t in _rate_buckets.get(ip, []) if now - t < _RATE_WINDOW]
    if len(bucket) >= _RATE_MAX:
        _rate_buckets[ip] = bucket
        return False
    bucket.append(now)
    _rate_buckets[ip] = bucket
    return True


def _rate_clear(ip: str) -> None:
    _rate_buckets.pop(ip, None)


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        ip            = _client_ip()
        pw_hash       = os.environ.get("UI_PASSWORD_HASH", "")
        expected_user = os.environ.get("UI_USERNAME", "admin")

        if not pw_hash:
            return Response("Auth not configured — run setup_auth.py first", 503)

        if not _rate_ok(ip):
            print(f"[AUTH] rate-limited {ip}")
            return Response(
                "Too many failed attempts — wait 60 s",
                429,
                {"Retry-After": "60", "WWW-Authenticate": 'Basic realm="Picster UI"'},
            )

        auth = request.authorization
        valid = (
            auth is not None
            and hmac.compare_digest(auth.username or "", expected_user)
            and check_password_hash(pw_hash, auth.password or "")
        )
        if not valid:
            print(f"[AUTH] failed attempt from {ip}")
            return Response(
                "Unauthorized",
                401,
                {"WWW-Authenticate": 'Basic realm="Picster UI"'},
            )

        _rate_clear(ip)
        return f(*args, **kwargs)
    return decorated


@app.after_request
def _security_headers(response):
    h = response.headers
    h["X-Frame-Options"]        = "DENY"
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    h["Permissions-Policy"]     = "geolocation=(), microphone=(), camera=()"
    # unsafe-inline needed for the existing inline scripts/styles in the template
    h["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
        "img-src 'self' data:; "
        "connect-src 'self'"
    )
    # HSTS only over HTTPS (Cloudflare sets X-Forwarded-Proto or CF-Visitor)
    via_https = (
        request.is_secure
        or request.headers.get("X-Forwarded-Proto") == "https"
        or '"scheme":"https"' in request.headers.get("CF-Visitor", "")
    )
    if via_https:
        h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[UI] Saved {path.name}")


def _load_items_cache():
    try:
        with open(PICSTER_ITEMS_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_items_cache(data):
    data = dict(data)
    data["scanned_at"] = datetime.now().isoformat()
    with open(PICSTER_ITEMS_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[UI] Picster item cache saved ({sum(len(v) for v in data.get('city_items', {}).values())} total items)")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
@require_auth
def index():
    try:
        picster = _load(PICSTER_CONFIG)
    except (FileNotFoundError, json.JSONDecodeError):
        picster = {}
    return render_template("index.html", picster=picster, cache=_load_items_cache() or {})


@app.route("/api/status")
@require_auth
def get_status():
    cfg = _load(PICSTER_CONFIG)
    cdp = cfg.get("cdp_endpoint", "http://127.0.0.1:9223").replace("localhost", "127.0.0.1")

    chrome_up = False
    try:
        port = int(cdp.rsplit(":", 1)[-1])
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            chrome_up = True
    except OSError:
        pass

    bot_alive = False
    try:
        data = json.loads(BOT_HEARTBEAT.read_text(encoding="utf-8"))
        age  = (datetime.now() - datetime.fromisoformat(data["ts"])).total_seconds()
        bot_alive = age < BOT_MAX_AGE_SECS
    except Exception:
        pass

    if chrome_up and bot_alive:
        status = "running"
    elif chrome_up:
        status = "chrome_only"
    else:
        status = "offline"

    return jsonify(status=status)


async def _scan_picster_async(cdp_endpoint, base_url, cities, months_ahead=6):
    """Pull real listing/tour names straight from picster.app's own bookings
    grid — same HTTP-through-CDP call watcher.py makes for live polling, just
    scanning further ahead."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_endpoint, timeout=6000)
        ctx = browser.contexts[0]

        months = []
        cur = date.today().replace(day=1)
        for _ in range(months_ahead + 1):
            months.append(cur.isoformat())
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)

        city_items = {c: set() for c in cities}
        for month in months:
            url = build_month_url(base_url, cities, month)
            resp = await ctx.request.get(url)
            try:
                if not resp.ok:
                    continue
                html = await resp.text()
            finally:
                await resp.dispose()  # not a real leak here (context closes per-request) — fixed for consistency
            for b in parse_grid(html):
                city_items.setdefault(b["city"], set()).add(b["shoot"])

        return {
            "calendars": cities,
            "city_items": {c: sorted(v) for c, v in city_items.items()},
        }


@app.route("/api/picster/items")
@require_auth
def get_picster_items():
    cfg = _load(PICSTER_CONFIG)
    cdp = cfg.get("cdp_endpoint", "http://127.0.0.1:9223").replace("localhost", "127.0.0.1")
    base_url = cfg.get("base_url", "https://picster.app")
    cities = sorted({c for u in cfg.get("urls", []) for c in u.get("cities", [])})
    try:
        result = asyncio.run(_scan_picster_async(cdp, base_url, cities))
        _save_items_cache(result)
        result["scanned_at"] = datetime.now().isoformat()
    except Exception as e:
        msg = str(e)
        if "ECONNREFUSED" in msg:
            msg = "Chrome not reachable on the bot's CDP port — make sure the bot's Chrome window is open"
        result = {"error": msg}
    return jsonify(result)


@app.route("/api/picster", methods=["POST"])
@require_auth
def save_picster():
    """Writes autobook/cities/keywords/exclude_keywords/date_ranges straight into
    picster_config.json — the file picster_bot.py's config_reloader actually
    polls (every 10s). This is the live bot's real config, not a mirror."""
    d = request.json
    cfg = _load(PICSTER_CONFIG)
    for i, u in enumerate(d.get("urls", [])):
        if i >= len(cfg.get("urls", [])):
            break
        cfg["urls"][i]["autobook"]             = bool(u["autobook"])
        cfg["urls"][i]["cities"]               = u["cities"]
        cfg["urls"][i]["keywords"]             = u["keywords"]
        cfg["urls"][i]["exclude_keywords"]     = u["exclude_keywords"]
        cfg["urls"][i]["autobook_date_ranges"] = u["date_ranges"]
    _save(PICSTER_CONFIG, cfg)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Crew schedule endpoints
# ---------------------------------------------------------------------------

@app.route("/api/crew-schedule", methods=["GET"])
@require_auth
def get_crew_schedule():
    return jsonify(load_crew_schedule())


@app.route("/api/crew-schedule", methods=["POST"])
@require_auth
def add_crew_slot():
    d = request.json
    entry = {
        "uuid":       d.get("uuid") or str(_uuid_mod.uuid4()),
        "date":       d["date"],
        "time_start": d["time_start"],
        "time_end":   d.get("time_end", ""),
        "name":       d.get("name", ""),
        "tour":       d.get("tour", ""),
        "source":     "manual",
    }
    schedule = load_crew_schedule()
    schedule.append(entry)
    save_crew_schedule(schedule)
    return jsonify(ok=True, entry=entry)


@app.route("/api/crew-schedule/<booking_uuid>", methods=["DELETE"])
@require_auth
def delete_crew_slot(booking_uuid):
    schedule = load_crew_schedule()
    new_sched = [s for s in schedule if s["uuid"] != booking_uuid]
    if len(new_sched) == len(schedule):
        return jsonify(ok=False, error="Not found"), 404
    save_crew_schedule(new_sched)
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=False, port=5100, use_reloader=False)
