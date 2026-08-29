"""
End-to-end synthetic booking test for the picster bot.

Injects fake grid/modal/claim HTTP responses through the real watcher and
booker code paths and asserts the claim POST is exactly what picster.app's
own "Assign myself" form would send. No Telegram messages are sent, no real
crew_schedule.json is touched (runs in a temp cwd), no real claims are made.

    python test_picster_payload.py            # synthetic end-to-end
    python test_picster_payload.py --live     # + read-only parse of the real site via CDP

Run after ANY change to the autobook/claim logic.
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import date, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Run in a temp dir so crew_schedule.json / subscriber writes don't touch real data
_REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO)
_TMP = tempfile.mkdtemp(prefix="picster_test_")
os.chdir(_TMP)

import picster.watcher as watcher
import picster.booker as booker
from picster.parser import parse_grid, parse_modal, months_to_watch

TOMORROW = (date.today() + timedelta(days=1)).isoformat()

# ---------------------------------------------------------------------------
# Fixtures — realistic markup captured from picster.app 2026-07-06
# ---------------------------------------------------------------------------

def grid_html(cards):
    rows = ""
    for shoot, card in cards:
        rows += f'''
        <tr class="photoshoot-group-row" data-group-base="Testville: X" data-city="Testville" data-group-id="Testville||X" data-photoshoot-id="1">
            <th><div class="row-head"><div>
                <div class="row-city">Testville</div>
                <div class="row-shoot">{shoot}</div>
            </div></div></th>
            <td class="bookings-cell">{card}</td>
        </tr>'''
    return f'<html><body>picster<table id="bookings-table"><tbody>{rows}</tbody></table></body></html>'


def card_html(bid, starts, ends, crew=None):
    crew_div = (f'<div class="booking-card-crew" id="card-crew-{bid}">{crew}</div>' if crew
                else f'<div class="booking-card-crew is-empty" id="card-crew-{bid}"></div>')
    return f'''<button type="button" class="booking-card booked" id="booking-card-{bid}"
        data-bs-toggle="modal" data-bs-target="#bookingModal" data-booking-id="{bid}" title="x">
        <div class="booking-time booking-time-dual" id="card-time-{bid}"
             data-starts-at="{starts}" data-ends-at="{ends}"
             data-city-timezone="Europe/Berlin" data-city-label="Testville">
            <span class="booking-time-line">18:00</span></div>{crew_div}</button>'''


def modal_html(bid, csrf="TESTCSRF123", assignable=True, status="Active", client="Test Client"):
    assign_form = f'''
        <form method="post" class="booking-ajax-form" data-booking-id="{bid}">
            <input type="hidden" name="csrfmiddlewaretoken" value="{csrf}">
            <input type="hidden" name="form_action" value="assign_self_as_crew">
            <input type="hidden" name="booking_id" value="{bid}">
            <input type="hidden" name="crew_id" value="59">
            <button type="submit">Assign myself</button>
        </form>''' if assignable else ''
    return f'''
    <div class="modal-header booking-modal-header">
        <div class="booking-hero-meta">BOOKING #34318{bid}</div>
        <div class="booking-hero-title">Private Photoshoot 📸</div>
        <div class="booking-hero-time" id="booking-time-pill-{bid}"
             data-starts-at="{TOMORROW}T16:00:00+00:00" data-ends-at="{TOMORROW}T16:30:00+00:00"
             data-city-timezone="Europe/Berlin" data-city-label="Testville"></div>
        <span class="booking-status-pill" id="booking-status-pill-{bid}">{status}</span>
    </div>
    <div class="modal-body">
        <div class="booking-detail-sheet" data-client-name="{client}" data-client-email="" data-client-phone="">
        {assign_form}
        <tbody id="assigned-crew-list-{bid}">
            <tr class="booking-crew-empty-row"><td>No crew assigned yet</td></tr>
        </tbody>
        </div>
    </div>'''


class FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.status = body, status
        self.ok = 200 <= status < 300

    async def text(self):
        return self._body

    async def dispose(self):
        pass


class FakeRequestCtx:
    """Stands in for Playwright's APIRequestContext."""
    def __init__(self):
        self.modals = {}      # booking_id → (status, html)
        self.claim_response = {"ok": True, "booking": {
            "is_assigned_to_me": True,
            "assigned_crew": [{"name": "Crew"}],
        }}
        self.posts = []

    async def get(self, url, headers=None):
        for bid, (status, html) in self.modals.items():
            if f"/bookings/modal/{bid}/" in url:
                return FakeResponse(html, status)
        return FakeResponse("not found", 404)

    async def post(self, url, form=None, headers=None):
        self.posts.append({"url": url, "form": form, "headers": headers})
        return FakeResponse(json.dumps(self.claim_response))


PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def make_entry(**over):
    entry = {
        "name": "test", "url": "https://picster.app/bookings/",
        "cities": ["Testville"],
        "keywords": ["Private Photoshoot 📸"],
        "exclude_keywords": [],
        "autobook": True,
        "booking_tiers": [],
        "autobook_date_ranges": [{
            "from": TOMORROW, "to": TOMORROW,
            "time_from": "07:00", "time_to": "21:00",
            "listing_types": ["Private Photoshoot 📸"],
            "buffer_before_minutes": 0, "buffer_after_minutes": 0,
        }],
    }
    entry.update(over)
    return entry


def make_state():
    # known_types mirrors what run_grid_watcher() seeds before ever calling
    # _handle_new_booking() in production (watcher.py) — without it,
    # _check_new_listing_type()'s state["known_types"] lookup raises KeyError.
    return {"subscribers": {}, "hit_count": 0, "seen_ids": set(), "seeded_months": set(),
            "known_types": set()}


alerts = []
def fake_broadcast(subs, msg):
    alerts.append(msg)
    print(f"    [alert] {msg.splitlines()[0]}")


watcher.broadcast_alert = fake_broadcast
booker.broadcast_alert = fake_broadcast


async def scenario_claim_inside_window():
    print("\n--- new unclaimed booking inside window → must claim ---")
    alerts.clear()
    booker._pending_slots.clear()
    ctx = FakeRequestCtx()
    ctx.modals["101"] = (200, modal_html("101"))
    booking = {"id": "101", "city": "Testville", "shoot": "Private Photoshoot 📸",
               "crew": "", "date": TOMORROW, "start": "18:00", "end": "18:30"}

    await watcher._handle_new_booking(booking, make_entry(), make_state() | {"subscribers": {}},
                                      ctx, "https://picster.app")
    await asyncio.sleep(0.2)  # let the claim task run

    check("claim POST sent", len(ctx.posts) == 1, f"posts={ctx.posts}")
    if ctx.posts:
        f = ctx.posts[0]["form"]
        check("POST url", ctx.posts[0]["url"] == "https://picster.app/bookings/")
        check("form_action", f.get("form_action") == "assign_self_as_crew", str(f))
        check("booking_id", f.get("booking_id") == "101", str(f))
        check("crew_id from modal", f.get("crew_id") == "59", str(f))
        check("csrf from modal", f.get("csrfmiddlewaretoken") == "TESTCSRF123", str(f))
        check("XHR header", ctx.posts[0]["headers"].get("X-Requested-With") == "XMLHttpRequest")
    sched = json.load(open("crew_schedule.json", encoding="utf-8")) if os.path.exists("crew_schedule.json") else []
    check("slot recorded", any(s["uuid"] == "101" and s["source"] == "picster" for s in sched), str(sched))
    check("success alert", any("Crew assigned" in a for a in alerts), str(alerts))
    check("pending slot released", not booker._pending_slots, str(booker._pending_slots))


async def scenario_outside_window():
    print("\n--- new booking outside window → alert only, no claim ---")
    alerts.clear()
    booker._pending_slots.clear()
    ctx = FakeRequestCtx()
    ctx.modals["102"] = (200, modal_html("102"))
    far = (date.today() + timedelta(days=90)).isoformat()
    booking = {"id": "102", "city": "Testville", "shoot": "Private Photoshoot 📸",
               "crew": "", "date": far, "start": "18:00", "end": "18:30"}
    await watcher._handle_new_booking(booking, make_entry(), make_state(), ctx, "https://picster.app")
    await asyncio.sleep(0.2)
    check("no claim POST", not ctx.posts, str(ctx.posts))
    check("skip alert sent", any("outside configured" in a for a in alerts), str(alerts))


async def scenario_conflict():
    print("\n--- new booking conflicts with existing slot → no claim ---")
    alerts.clear()
    booker._pending_slots.clear()
    from picster.schedule import save_crew_schedule
    save_crew_schedule([{"uuid": "X1", "date": TOMORROW, "time_start": "18:00",
                         "time_end": "19:00", "name": "Existing", "tour": "T", "source": "picster"}])
    ctx = FakeRequestCtx()
    ctx.modals["103"] = (200, modal_html("103"))
    booking = {"id": "103", "city": "Testville", "shoot": "Private Photoshoot 📸",
               "crew": "", "date": TOMORROW, "start": "18:15", "end": "18:45"}
    await watcher._handle_new_booking(booking, make_entry(), make_state(), ctx, "https://picster.app")
    await asyncio.sleep(0.2)
    check("no claim POST", not ctx.posts, str(ctx.posts))
    check("conflict alert", any("CONFLICT" in a for a in alerts), str(alerts))
    save_crew_schedule([])


async def scenario_already_claimed_on_modal():
    print("\n--- modal has no assign form (raced, someone claimed first) → missed alert ---")
    alerts.clear()
    booker._pending_slots.clear()
    ctx = FakeRequestCtx()
    ctx.modals["104"] = (200, modal_html("104", assignable=False))
    booking = {"id": "104", "city": "Testville", "shoot": "Private Photoshoot 📸",
               "crew": "", "date": TOMORROW, "start": "10:00", "end": "10:30"}
    await watcher._handle_new_booking(booking, make_entry(), make_state(), ctx, "https://picster.app")
    await asyncio.sleep(0.2)
    check("no claim POST", not ctx.posts, str(ctx.posts))
    check("missed alert", any("already claimed" in a for a in alerts), str(alerts))
    check("pending slot released", not booker._pending_slots, str(booker._pending_slots))


async def scenario_claimed_card_not_autoclaimed():
    print("\n--- card already claimed by another crew → alert only ---")
    alerts.clear()
    booker._pending_slots.clear()
    ctx = FakeRequestCtx()
    ctx.modals["105"] = (200, modal_html("105"))
    booking = {"id": "105", "city": "Testville", "shoot": "Private Photoshoot 📸",
               "crew": "Marinela", "date": TOMORROW, "start": "12:00", "end": "12:30"}
    await watcher._handle_new_booking(booking, make_entry(), make_state(), ctx, "https://picster.app")
    await asyncio.sleep(0.2)
    check("no claim POST", not ctx.posts, str(ctx.posts))
    check("claimed-by-other alert", any("Already claimed by Marinela" in a for a in alerts), str(alerts))


async def scenario_watcher_loop():
    print("\n--- full watcher loop: seed month silently, claim only the NEW booking ---")
    alerts.clear()
    booker._pending_slots.clear()
    from picster.schedule import save_crew_schedule
    save_crew_schedule([])

    existing = card_html("301", f"{TOMORROW}T08:00:00+00:00", f"{TOMORROW}T08:30:00+00:00")
    new_card = card_html("302", f"{TOMORROW}T12:00:00+00:00", f"{TOMORROW}T12:30:00+00:00")

    ctx = FakeRequestCtx()
    ctx.modals["302"] = (200, modal_html("302"))
    ctx.grid_pages = [grid_html([("Private Photoshoot 📸", existing)])] * 2 + \
                     [grid_html([("Private Photoshoot 📸", existing + new_card)])] * 30

    orig_get = ctx.get
    async def get(url, headers=None):
        if "/bookings/?" in url:
            return FakeResponse(ctx.grid_pages.pop(0) if ctx.grid_pages else grid_html([]))
        return await orig_get(url, headers)
    ctx.get = get

    state = make_state() | {"interval_min": 0.05, "interval_max": 0.05, "check_count": 0}
    config = {"base_url": "https://picster.app", "crew_name": "Crew",
              "watch_months_ahead": 0, "sanity_phrase": "picster"}
    task = asyncio.create_task(watcher.run_grid_watcher(ctx, make_entry(), state, config))
    await asyncio.sleep(1.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    check("existing booking seeded, not claimed",
          all(p["form"].get("booking_id") != "301" for p in ctx.posts), str(ctx.posts))
    check("new booking claimed exactly once",
          sum(1 for p in ctx.posts if p["form"].get("booking_id") == "302") == 1, str(ctx.posts))
    check("multiple poll cycles ran", state["check_count"] >= 3, f"cycles={state['check_count']}")
    check("no alert for seeded booking",
          not any("#301" in a for a in alerts), str(alerts))
    save_crew_schedule([])


def scenario_parser():
    print("\n--- parser unit checks ---")
    html = grid_html([
        ("Private Photoshoot 📸",
         card_html("201", f"{TOMORROW}T16:00:00+00:00", f"{TOMORROW}T16:30:00+00:00")),
        ("Testville: Downtown Premium",
         card_html("202", "2026-07-06T04:30:00+00:00", "2026-07-06T05:30:00+00:00", crew="Dan")),
    ])
    rows = parse_grid(html)
    check("2 cards parsed", len(rows) == 2, str(rows))
    by_id = {r["id"]: r for r in rows}
    check("unclaimed detected", by_id["201"]["crew"] == "")
    check("crew name parsed", by_id["202"]["crew"] == "Dan", str(by_id.get("202")))
    check("UTC→Exampleport conversion (summer +2)", by_id["202"]["start"] == "06:30", str(by_id.get("202")))
    check("shoot from row", by_id["201"]["shoot"] == "Private Photoshoot 📸")

    m = parse_modal(modal_html("201"))
    check("modal csrf", m["csrf"] == "TESTCSRF123", str(m))
    check("modal client", m["client"] == "Test Client", str(m))
    check("modal assignable", m["can_assign_self"] is True)
    check("modal crew_id", m["crew_id"] == "59", str(m))
    check("modal status", m["status"] == "Active", str(m))

    months = months_to_watch(make_entry(), months_ahead=1, today=date(2026, 7, 6))
    check("months include current", "2026-07-01" in months, str(months))


async def live_smoke():
    print("\n--- LIVE (read-only): parse the real grid via CDP ---")
    from playwright.async_api import async_playwright
    from picster.parser import build_month_url
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9223")
        ctx = browser.contexts[0]
        url = build_month_url("https://picster.app", ["Testville"], date.today().replace(day=1).isoformat())
        resp = await ctx.request.get(url)
        html = await resp.text()
        rows = parse_grid(html)
        check("live grid fetch ok", resp.ok, f"status={resp.status}")
        check("live cards parsed", len(rows) > 0, f"{len(rows)} rows")
        print(f"    parsed {len(rows)} bookings, "
              f"{sum(1 for r in rows if not r['crew'])} unclaimed, "
              f"{sum(1 for r in rows if r['crew'])} claimed")
        # live modal parse on one real booking
        if rows:
            from picster.booker import fetch_modal
            status, modal = await fetch_modal(ctx.request, "https://picster.app", rows[0]["id"])
            check("live modal fetch ok", modal is not None, f"status={status}")
            if modal:
                check("live modal csrf found", bool(modal["csrf"]))
                check("live modal times parsed", bool(modal["date"] and modal["start"]), str(modal))


async def main():
    scenario_parser()
    await scenario_claim_inside_window()
    await scenario_outside_window()
    await scenario_conflict()
    await scenario_already_claimed_on_modal()
    await scenario_claimed_card_not_autoclaimed()
    await scenario_watcher_loop()
    if "--live" in sys.argv:
        await live_smoke()
    print(f"\n{'='*50}\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
