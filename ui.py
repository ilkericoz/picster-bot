import asyncio
import hmac
import json
import os
import re
import socket
import time
import uuid as _uuid_mod
from datetime import datetime
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
from werkzeug.security import check_password_hash

from picster.schedule import (
    load_crew_schedule, save_crew_schedule, parse_booking_datetime,
)

load_dotenv()

app = Flask(__name__)

BASE             = Path(__file__).parent
LGC_CONFIG        = BASE / "legacy_config.json"
PICSTER_CONFIG   = BASE / "picster_config.json"  # read-only in this app — never written here, see save_lgc()
LGC_ITEMS_CACHE   = BASE / "lgc_items_cache.json"
BOT_HEARTBEAT    = BASE / "bot_heartbeat.json"
BOT_MAX_AGE_SECS = 60   # heartbeat older than this → bot is considered dead

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
                {"Retry-After": "60", "WWW-Authenticate": 'Basic realm="Legacy UI"'},
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
                {"WWW-Authenticate": 'Basic realm="Legacy UI"'},
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


def _load_cache():
    try:
        with open(LGC_ITEMS_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_cache(data):
    data = dict(data)
    data["scanned_at"] = datetime.now().isoformat()
    with open(LGC_ITEMS_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[UI] Cache saved ({sum(len(v) for v in data.get('city_items', {}).values())} total items)")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
@require_auth
def index():
    try:
        picster = _load(PICSTER_CONFIG)
    except (FileNotFoundError, json.JSONDecodeError):
        picster = {}
    return render_template(
        "index.html",
        lgc=_load(LGC_CONFIG),
        picster=picster,
        cache=_load_cache() or {},
    )



@app.route("/api/status")
@require_auth
def get_status():
    cfg = _load(LGC_CONFIG)
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


@app.route("/api/legacy", methods=["POST"])
@require_auth
def save_lgc():
    d = request.json
    cfg = _load(LGC_CONFIG)
    cfg["check_interval_min_seconds"] = int(d["min_interval"])
    cfg["check_interval_max_seconds"] = int(d["max_interval"])
    cfg["screenshot_on_found"]        = d["screenshot_on_found"]
    cfg["human_dwell_min_seconds"]    = int(d["dwell_min"])
    cfg["human_dwell_max_seconds"]    = int(d["dwell_max"])
    for i, u in enumerate(d.get("urls", [])):
        if i >= len(cfg["urls"]):
            break
        cfg["urls"][i]["autobook"]             = u["autobook"]
        cfg["urls"][i]["keywords"]             = u["keywords"]
        cfg["urls"][i]["exclude_keywords"]     = u["exclude_keywords"]
        cfg["urls"][i]["autobook_date_ranges"] = u["date_ranges"]
    _save(LGC_CONFIG, cfg)
    # NOTE: this used to also mirror autobook/keywords/exclude_keywords/
    # autobook_date_ranges into picster_config.json ("one UI edit drives both
    # bots"). Removed — the Legacy-dashboard bot this page configures is
    # superseded (picster.app polling is the live bot now), and the mirror
    # meant saving this page silently overwrote the real bot's config with
    # this page's stale state. Edit picster_config.json directly instead.
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Legacy item + city scan
# ---------------------------------------------------------------------------

async def _scan_async(cdp_endpoint, grid_url):
    from playwright.async_api import async_playwright

    m = re.search(r'legacy\.com/([^/]+)/', grid_url)
    shortname = m.group(1) if m else None
    if not shortname:
        return {"error": f"Cannot extract company shortname from URL: {grid_url}"}

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_endpoint, timeout=6000)
        ctx  = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.goto(grid_url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_selector(
                'li.sortable[ng-repeat*="customCalendar"]',
                state="attached", timeout=25_000,
            )
            await page.wait_for_timeout(500)

            result = await page.evaluate("""async (sn) => {
                const [calsResp, itemsResp] = await Promise.all([
                    fetch(`/api/v1/orgs/${sn}/calendars/`, {credentials: 'same-origin'}),
                    fetch(`/api/v1/orgs/${sn}/items/?selectable=yes`, {credentials: 'same-origin'}),
                ]);
                const cals     = (await calsResp.json()).custom_calendars || [];
                const allItems = (await itemsResp.json()).items || [];

                const uriName = {};
                for (const it of allItems) uriName[it.uri] = it.name;

                const calendars = [];
                const cityItems = {};
                for (const cal of cals) {
                    calendars.push(cal.name);
                    const itemsMap = (cal.settings && cal.settings.items) || {};
                    cityItems[cal.name] = Object.entries(itemsMap)
                        .filter(([, v]) => v === true)
                        .map(([uri]) => uriName[uri])
                        .filter(Boolean);
                }
                return {calendars, cityItems};
            }""", shortname)

            for name, items in result.get("city_items", result.get("cityItems", {})).items():
                print(f"[UI] {name}: {len(items)} items")

            if "cityItems" in result:
                result["city_items"] = result.pop("cityItems")

            return result

        except Exception as e:
            return {"error": str(e)}
        finally:
            await page.close()


def _cdp_and_url():
    cfg      = _load(LGC_CONFIG)
    cdp      = cfg.get("cdp_endpoint", "http://127.0.0.1:9223").replace("localhost", "127.0.0.1")
    grid_url = cfg["urls"][0]["url"] if cfg.get("urls") else \
               "https://example.invalid/bookings/grid/"
    return cdp, grid_url


@app.route("/api/legacy/items")
@require_auth
def get_lgc_items():
    cdp, grid_url = _cdp_and_url()
    try:
        result = asyncio.run(_scan_async(cdp, grid_url))
        if "error" not in result:
            _save_cache(result)
            result["scanned_at"] = datetime.now().isoformat()
    except Exception as e:
        msg = str(e)
        if "ECONNREFUSED" in msg:
            msg = "Chrome not reachable — start the Legacy bot first"
        result = {"error": msg}
    return jsonify(result)


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


@app.route("/api/crew-schedule/probe")
@require_auth
def probe_crew_schedule():
    """Debug: dumps raw Legacy API data for the first visible booking."""
    cdp, grid_url = _cdp_and_url()
    try:
        result = asyncio.run(_probe_crew_async(cdp, grid_url))
    except Exception as e:
        result = {"error": str(e)}
    return jsonify(result)


async def _probe_crew_async(cdp_endpoint, grid_url):
    from playwright.async_api import async_playwright

    m = re.search(r'legacy\.com/([^/]+)/', grid_url)
    shortname = m.group(1) if m else "picster"

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp_endpoint, timeout=6000)
        ctx  = browser.contexts[0]

        pages_info = []
        for p in ctx.pages:
            try:
                pages_info.append({"url": p.url})
            except Exception:
                pages_info.append({"url": "?"})

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        current_url = page.url

        bookings = await page.evaluate(r"""
        () => Array.from(
            document.querySelectorAll('a.booking-block[data-test-id="test-view-booking-action"]')
        ).map(a => {
            const href = a.getAttribute('href') || '';
            const m = href.match(/bookings\/([0-9a-f-]{8,})\//);
            return {uuid: m ? m[1] : '', name: (a.querySelector('h2')?.innerText||'').trim(), href};
        }).filter(b => b.uuid)
        """)

        out = {
            "current_url": current_url,
            "all_pages": pages_info,
            "booking_blocks_on_page": len(bookings),
            "shortname": shortname,
        }

        if not bookings:
            out["hint"] = "No booking blocks found. Make sure the Legacy dashboard is open and the recent-bookings panel is expanded."
            return out

        uuid = bookings[0]["uuid"]
        out["sample_uuid"] = uuid

        detail = await page.evaluate("""
        async (args) => {
            const r = await fetch('/api/v1/orgs/' + args.sn + '/bookings/' + args.uuid + '/', {
                headers: {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
            });
            if (!r.ok) return {__http_error: r.status};
            return await r.json();
        }
        """, {"sn": shortname, "uuid": uuid})

        out["booking_detail"] = detail

        if not isinstance(detail, dict) or "__http_error" in detail:
            return out

        booking  = detail.get("booking") or detail
        item_id  = (booking.get("item") or {}).get("pk")
        avail_id = (booking.get("availability") or {}).get("pk")
        out["item_id"]  = item_id
        out["avail_id"] = avail_id

        if item_id and avail_id:
            crew_raw = await page.evaluate("""
            async (args) => {
                const r = await fetch(
                    '/api/v1/orgs/' + args.sn +
                    '/items/' + args.itemId +
                    '/availabilities/' + args.availId + '/crew/',
                    {headers: {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}}
                );
                if (!r.ok) return {__http_error: r.status};
                return await r.json();
            }
            """, {"sn": shortname, "itemId": item_id, "availId": avail_id})
            out["crew_members_raw"] = crew_raw

    return out


if __name__ == "__main__":
    app.run(debug=False, port=5100, use_reloader=False)
