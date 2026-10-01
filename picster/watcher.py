"""
Grid watcher for picster.app.

Polls the month-view bookings grid (one HTTP GET per watched month, through
the attached Chrome's session), diffs booking IDs against what's been seen,
and for each genuinely new booking:

  • broadcasts a Telegram alert
  • runs the autobook decision (keywords → date/time window → listing types
    → schedule conflict → in-flight guard → tier delay) and claims it

Months are seeded silently the first time they're polled, so a config change
that adds a new month doesn't flood alerts with pre-existing bookings. Seeding
is keyed on month + cities together, so adding a city to an already-running
entry re-seeds silently too, instead of replaying every pre-existing booking
in the newly-added city as "new".

Any listing name never seen before (regardless of whether it matches
`keywords`) gets a one-time "new listing type" alert via
`known_listing_types.json` — alert only, never auto-added to `keywords`, so a
brand new picster listing (like a new premium tier showing up under
a second city) can't go unnoticed the way it did before, but it also can't start
getting auto-claimed without a human choosing to add it.

Same one-time-alert treatment for cities: the grid HTML already includes
picster.app's own City: filter widget (its full account-wide city list, not
just whatever this entry's `cities` filters by), so a newly unlocked city is
parsed for free out of HTML already being fetched — no extra request. First
sighting of a city not in `known_cities.json` fires an alert; it is never
auto-added to this entry's `cities`.

The same parse also keeps crew_schedule.json in sync: any card claimed by
our crew member that isn't in the schedule yet gets recorded (zero extra
requests).

A schedule conflict hit during the autobook decision is re-verified live
(one modal fetch) before it's trusted: picster/reverifier.py only sweeps
crew_schedule.json for cancellations every 30 minutes, so a slot canceled
and immediately rebooked for the same date/time can otherwise cause a false
CONFLICT against a booking that no longer exists.
"""
import asyncio
import json
import random
from datetime import date, datetime, timedelta

from picster.schedule import (
    find_conflict, get_tier_delay, is_in_date_range, load_crew_schedule,
    remove_slot, times_overlap, SCHEDULE_BUFFER_MINUTES,
)
from picster.subscribers import broadcast_alert
from picster.booker import claim_booking, fetch_modal, _pending_slots
from picster.parser import build_month_url, months_to_watch, parse_grid, parse_available_cities
from picster.driver_health import is_driver_dead

KNOWN_TYPES_PATH = "known_listing_types.json"
KNOWN_CITIES_PATH = "known_cities.json"

# seen_ids only needs to cover bookings whose month is still in months_to_watch()
# (which drops past months entirely — see parser.months_to_watch), so anything
# older than a full month-cycle is safe to forget. Keeps a wide margin over the
# ~31-day max a month can stay in view.
SEEN_ID_STALE_DAYS = 45


def _load_known_types():
    try:
        with open(KNOWN_TYPES_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_known_types(known):
    with open(KNOWN_TYPES_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(known), f, indent=2, ensure_ascii=False)


def _load_known_cities():
    try:
        with open(KNOWN_CITIES_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_known_cities(known):
    with open(KNOWN_CITIES_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(known), f, indent=2, ensure_ascii=False)


def _keyword_match(booking, entry):
    keywords = [k.lower() for k in entry.get("keywords", [])]
    excludes = [k.lower() for k in entry.get("exclude_keywords", [])]
    haystack = f"{booking['city']} {booking['shoot']}".lower()
    if keywords and not any(k in haystack for k in keywords):
        return False
    if excludes and any(k in haystack for k in excludes):
        return False
    return True


def _sync_crew_slots(bookings, crew_name, state):
    """Record any grid booking claimed by our crew that the schedule doesn't know."""
    if not crew_name:
        return
    ours = [b for b in bookings if b["crew"] and crew_name.lower() in b["crew"].lower()]
    if not ours:
        return
    schedule = load_crew_schedule()
    known_ids = {s.get("uuid") for s in schedule}
    added = 0
    for b in ours:
        if b["id"] in known_ids:
            continue
        # Same physical slot may exist under an older uuid — skip duplicates.
        dup = any(
            s.get("date") == b["date"]
            and s.get("time_start") == b["start"]
            and s.get("time_end") == b["end"]
            for s in schedule
        )
        if dup:
            continue
        from picster.schedule import record_slot
        record_slot(b["id"], b["date"], b["start"], b["end"],
                    "(synced)", b["shoot"], source="picster-sync")
        schedule = load_crew_schedule()
        known_ids.add(b["id"])
        added += 1
    if added:
        print(f"[SYNC] +{added} claimed booking(s) added to crew schedule")


def _check_new_listing_type(booking, state):
    """One-time alert the first time a listing name is ever seen — runs
    before the keyword gate below, since that gate would otherwise drop an
    unrecognized type completely silently. Never touches `keywords` itself;
    purely informational so a new picster listing type can't go unnoticed."""
    known = state["known_types"]
    shoot_key = booking["shoot"].strip().lower()
    if shoot_key in known:
        return
    known.add(shoot_key)
    _save_known_types(known)
    print(f"[DISCOVERY] new listing type: {booking['shoot']} ({booking['city']})")
    broadcast_alert(
        state["subscribers"],
        f"🆕 New listing type seen — not in autobook keywords, alert only:\n"
        f"{booking['shoot']} — {booking['city']}\n"
        f"{booking['date']} {booking['start']}–{booking['end']}\n"
        f"Add it to picster_config.json's keywords if you want it auto-claimed.",
    )


def _check_new_cities_available(html, entry, state):
    """One-time alert the first time picster.app's own City: filter widget offers
    a city not seen before — parsed from the grid HTML already fetched every poll,
    so this costs no extra request. Alert only, never auto-added to this entry's
    `cities`, same philosophy as _check_new_listing_type: a newly unlocked city
    can't go unnoticed, but it also can't start being watched/claimed without a
    human choosing to add it."""
    known = state["known_cities"]
    for city in parse_available_cities(html):
        key = city.strip().lower()
        if key in known:
            continue
        known.add(key)
        _save_known_cities(known)
        print(f"[DISCOVERY] new city available: {city}")
        broadcast_alert(
            state["subscribers"],
            f"🌍 New city available on your Picster account: {city}\n"
            f"Not in this entry's autobook cities yet ('{entry.get('name', '?')}') — "
            f"add it in picster_config.json if you want bookings there watched/claimed.",
        )


async def _conflict_is_stale(request_ctx, base_url, conflict):
    """True if a crew_schedule conflict was actually canceled on picster's side.

    crew_schedule.json is only swept for cancellations every REVERIFY_INTERVAL_SECONDS
    (picster/reverifier.py), so a slot that just got canceled and immediately
    rebooked (same date/time, new booking id) can still sit in the schedule as a
    live-looking entry — the new booking then gets a false CONFLICT against a
    booking that no longer exists. One extra modal fetch here, only when a
    conflict is actually hit, closes that race instead of just narrowing it.
    """
    if not str(conflict.get("source", "")).startswith("picster") or not conflict.get("uuid"):
        return False  # not a picster-uuid slot (e.g. an older entry) — can't verify, don't touch
    try:
        status, modal = await fetch_modal(request_ctx, base_url, conflict["uuid"])
    except Exception:
        return False  # can't verify — treat conservatively as still conflicting
    if status == 404:
        return True
    if modal and modal.get("status") and modal["status"].lower() != "active":
        return True
    return False


async def _handle_new_booking(booking, entry, state, request_ctx, base_url):
    """Alert + autobook decision for one newly appeared grid booking."""
    _check_new_listing_type(booking, state)

    if not _keyword_match(booking, entry):
        return

    try:
        booking_date = date.fromisoformat(booking["date"])
    except ValueError:
        return
    if booking_date < date.today():
        print(f"[LIVE] skip past booking ({booking['date']}): {booking['shoot']}")
        return

    state["hit_count"] = state.get("hit_count", 0) + 1
    label = (f"{booking['shoot']} — {booking['city']}\n"
             f"{booking['date']} {booking['start']}–{booking['end']}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [LIVE] new booking "
          f"#{state['hit_count']}: #{booking['id']} {booking['shoot']} "
          f"@ {booking['date']} {booking['start']} crew={booking['crew'] or '(none)'}")

    # Enrich the alert with the client name from the detail modal (best effort).
    client = ""
    try:
        _, modal = await fetch_modal(request_ctx, base_url, booking["id"])
        if modal:
            client = modal.get("client", "")
    except Exception:
        pass
    prefix = f"{client} — " if client else ""
    broadcast_alert(state["subscribers"], f"Picster — new booking:\n• {prefix}{label}")

    if booking["crew"]:
        broadcast_alert(state["subscribers"],
                        f"Already claimed by {booking['crew']} — not claiming:\n{label}")
        return
    if not entry.get("autobook"):
        return

    matched_range = is_in_date_range(booking_date, booking["start"], entry)
    if matched_range is None:
        ranges_str = ", ".join(
            f"{r.get('from','?')}–{r.get('to','?')}"
            + (f" @ {r['time_from']}–{r['time_to']}" if r.get("time_from") else "")
            for r in entry.get("autobook_date_ranges", [])
        )
        print(f"[AUTOBOOK] Skip — {booking['date']} {booking['start']} outside windows [{ranges_str}]")
        broadcast_alert(state["subscribers"],
                        f"New booking skipped (outside configured date/time windows):\n{label}")
        return

    window_types = [k.lower() for k in (matched_range.get("listing_types") or [])]
    haystack = f"{booking['city']} {booking['shoot']}".lower()
    if window_types and not any(k in haystack for k in window_types):
        print(f"[AUTOBOOK] Skip — '{booking['shoot']}' not in this window's listing types")
        broadcast_alert(state["subscribers"],
                        f"New booking skipped (not in window's listing types):\n{label}")
        return

    buf_before = matched_range.get("buffer_before_minutes", SCHEDULE_BUFFER_MINUTES)
    buf_after = matched_range.get("buffer_after_minutes", SCHEDULE_BUFFER_MINUTES)

    conflict = find_conflict(booking_date, booking["start"], booking["end"],
                             buf_before, buf_after)
    if conflict and await _conflict_is_stale(request_ctx, base_url, conflict):
        print(f"[AUTOBOOK] Conflict slot #{conflict['uuid']} already canceled on picster — clearing stale entry")
        remove_slot(conflict["uuid"])
        broadcast_alert(
            state["subscribers"],
            f"Cleared a stale schedule conflict (that booking was already canceled):\n"
            f"{conflict['name']} — {conflict['tour']} @ {conflict['time_start']}–{conflict['time_end']}",
        )
        conflict = find_conflict(booking_date, booking["start"], booking["end"],
                                 buf_before, buf_after)
    if conflict:
        print(f"[AUTOBOOK] Conflict — {conflict['name']} @ "
              f"{conflict['time_start']}–{conflict['time_end']}")
        broadcast_alert(
            state["subscribers"],
            f"Booking CONFLICT — manual action needed:\n"
            f"New:      {prefix}{booking['shoot']} @ {booking['start']}–{booking['end']}\n"
            f"Existing: {conflict['name']} — {conflict['tour']} @ "
            f"{conflict['time_start']}–{conflict['time_end']}",
        )
        return

    in_flight = next(
        (s for s in _pending_slots
         if s[0] == booking["date"] and times_overlap(
             booking["start"], booking["end"], s[1], s[2], max(buf_before, buf_after))),
        None,
    )
    if in_flight:
        print(f"[AUTOBOOK] Conflict with in-flight claim @ {in_flight[1]}–{in_flight[2]}")
        broadcast_alert(
            state["subscribers"],
            f"Booking CONFLICT (in-flight) — manual action needed:\n"
            f"New:      {prefix}{label}\n"
            f"A booking is already being claimed @ {in_flight[1]}–{in_flight[2]}",
        )
        return

    _pending_slots.append((booking["date"], booking["start"], booking["end"]))

    delay_min, delay_max = get_tier_delay(haystack, entry)
    delay = random.uniform(delay_min, delay_max)
    if delay > 0:
        print(f"[AUTOBOOK] Tier delay {delay:.1f}s — {booking['shoot']}")

    async def _delayed_claim(d=delay, b=dict(booking)):
        if d > 0:
            await asyncio.sleep(d)
        await claim_booking(request_ctx, base_url, b, state)

    asyncio.create_task(_delayed_claim())


async def run_grid_watcher(request_ctx, entry, state, config):
    """Poll the month grids forever; diff → alert → claim. Never raises."""
    base_url = config.get("base_url", "https://picster.app")
    crew_name = config.get("crew_name", "")
    months_ahead = config.get("watch_months_ahead", 2)
    sanity = config.get("sanity_phrase", "picster").lower()

    seen_ids = state.setdefault("seen_ids", {})  # {booking_id: booking date} — pruned below
    seeded_months = state.setdefault("seeded_months", set())
    if "known_types" not in state:
        state["known_types"] = _load_known_types() | {
            k.strip().lower() for k in entry.get("keywords", [])
        }
    if "known_cities" not in state:
        state["known_cities"] = _load_known_cities() | {
            c.strip().lower() for c in entry.get("cities", [])
        }
    consecutive_errors = 0

    print(f"[LIVE] grid watcher started — months: "
          f"{', '.join(months_to_watch(entry, months_ahead))}")

    while True:
        cycle_bookings = []
        cycle_ok = True

        for month in months_to_watch(entry, months_ahead):
            cities = entry.get("cities", [])
            # Keyed on cities too, not just month: if the config's city list
            # changes mid-run (e.g. a city gets added), the newly-visible
            # bookings for an already-seeded month must be seeded silently
            # again rather than flooding alerts for every pre-existing
            # booking in the newly-added city.
            seed_key = f"{month}|{','.join(sorted(cities))}"
            url = build_month_url(base_url, cities, month)
            try:
                resp = await request_ctx.get(url)
                try:
                    html = await resp.text()
                finally:
                    await resp.dispose()  # else the body stays in the driver's memory until context close, even if .text() raises
                if not resp.ok:
                    raise RuntimeError(f"HTTP {resp.status}")
                if sanity and sanity not in html.lower():
                    raise RuntimeError("sanity phrase missing — logged out or blocked?")
                if 'id="bookings-table"' not in html and "booking-card" not in html:
                    raise RuntimeError("grid table missing — logged out?")
            except Exception as e:
                cycle_ok = False
                consecutive_errors += 1
                print(f"[LIVE] poll failed ({month}): {e}")

                if is_driver_dead(e):
                    # The Playwright driver connection itself is gone — every
                    # future call on this request_ctx will fail identically
                    # forever. Retrying here is pointless; signal the outer
                    # loop in picster_bot.py to tear down and rebuild
                    # async_playwright() from scratch.
                    print("[LIVE] Playwright driver connection lost — signaling full restart")
                    fatal_event = state.get("fatal_event")
                    if fatal_event:
                        fatal_event.set()
                    return

                # Alert on the first failure and then every 5 after, so a
                # sustained outage keeps nagging instead of going silent
                # after the one alert.
                if consecutive_errors % 5 == 0:
                    broadcast_alert(state["subscribers"],
                                    f"Picster watcher failing repeatedly ({consecutive_errors}x): {e}")
                break

            bookings = parse_grid(html)
            cycle_bookings.extend(bookings)

            try:
                _check_new_cities_available(html, entry, state)
            except Exception as e:
                print(f"[DISCOVERY] city check failed: {e}")

            if seed_key not in seeded_months:
                seeded_months.add(seed_key)
                seen_ids.update((b["id"], b["date"]) for b in bookings)
                print(f"[LIVE] seeded {month} ({','.join(sorted(cities))}) with {len(bookings)} existing booking(s)")
                continue

            for b in bookings:
                if b["id"] in seen_ids:
                    continue
                seen_ids[b["id"]] = b["date"]
                try:
                    await _handle_new_booking(b, entry, state, request_ctx, base_url)
                except Exception as e:
                    print(f"[LIVE] handle_new_booking failed (#{b['id']}): {e}")

        # seen_ids otherwise grows for as long as the process lives (weeks/months
        # of uptime) — every booking id ever polled, kept forever. Bookings whose
        # date has aged out of every month we could possibly still be watching
        # are safe to forget; the month itself will never be re-polled to
        # resurrect them as "new" again.
        stale_cutoff = (date.today() - timedelta(days=SEEN_ID_STALE_DAYS)).isoformat()
        stale_ids = [bid for bid, d in seen_ids.items() if d < stale_cutoff]
        for bid in stale_ids:
            del seen_ids[bid]

        if cycle_ok:
            consecutive_errors = 0
            state["check_count"] = state.get("check_count", 0) + 1
            state["last_check_time"] = datetime.now().strftime("%H:%M:%S")
            state["last_poll_ts"] = datetime.now().isoformat()
            try:
                _sync_crew_slots(cycle_bookings, crew_name, state)
            except Exception as e:
                print(f"[SYNC] crew slot sync failed: {e}")

        wait = random.uniform(state["interval_min"], state["interval_max"])
        state["next_check_in"] = wait
        await asyncio.sleep(wait)
