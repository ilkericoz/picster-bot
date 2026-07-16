"""
Grid watcher for picster.app.

Polls the month-view bookings grid (one HTTP GET per watched month, through
the attached Chrome's session), diffs booking IDs against what's been seen,
and for each genuinely new booking:

  • broadcasts a Telegram alert
  • runs the autobook decision (keywords → date/time window → listing types
    → schedule conflict → in-flight guard → tier delay) and claims it

Months are seeded silently the first time they're polled, so a config change
that adds a new month doesn't flood alerts with pre-existing bookings.

The same parse also keeps crew_schedule.json in sync: any card claimed by
our crew member that isn't in the schedule yet gets recorded (replaces the
old Legacy crew-sync loop — zero extra requests).
"""
import asyncio
import random
from datetime import date, datetime

from picster.schedule import (
    find_conflict, get_tier_delay, is_in_date_range, load_crew_schedule,
    times_overlap, SCHEDULE_BUFFER_MINUTES,
)
from picster.subscribers import broadcast_alert
from picster.booker import claim_booking, fetch_modal, _pending_slots
from picster.parser import build_month_url, months_to_watch, parse_grid


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
        # Same physical slot may exist under a Legacy uuid — skip duplicates.
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


async def _handle_new_booking(booking, entry, state, request_ctx, base_url):
    """Alert + autobook decision for one newly appeared grid booking."""
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

    seen_ids = state.setdefault("seen_ids", set())
    seeded_months = state.setdefault("seeded_months", set())
    consecutive_errors = 0

    print(f"[LIVE] grid watcher started — months: "
          f"{', '.join(months_to_watch(entry, months_ahead))}")

    while True:
        cycle_bookings = []
        cycle_ok = True

        for month in months_to_watch(entry, months_ahead):
            url = build_month_url(base_url, entry.get("cities", ["Testville"]), month)
            try:
                resp = await request_ctx.get(url)
                html = await resp.text()
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
                if consecutive_errors == 5:
                    broadcast_alert(state["subscribers"],
                                    f"Picster watcher failing repeatedly: {e}")
                break

            bookings = parse_grid(html)
            cycle_bookings.extend(bookings)

            if month not in seeded_months:
                seeded_months.add(month)
                seen_ids.update(b["id"] for b in bookings)
                print(f"[LIVE] seeded {month} with {len(bookings)} existing booking(s)")
                continue

            for b in bookings:
                if b["id"] in seen_ids:
                    continue
                seen_ids.add(b["id"])
                try:
                    await _handle_new_booking(b, entry, state, request_ctx, base_url)
                except Exception as e:
                    print(f"[LIVE] handle_new_booking failed (#{b['id']}): {e}")

        if cycle_ok:
            consecutive_errors = 0
            state["check_count"] = state.get("check_count", 0) + 1
            state["last_check_time"] = datetime.now().strftime("%H:%M:%S")
            try:
                _sync_crew_slots(cycle_bookings, crew_name, state)
            except Exception as e:
                print(f"[SYNC] crew slot sync failed: {e}")

        wait = random.uniform(state["interval_min"], state["interval_max"])
        state["next_check_in"] = wait
        await asyncio.sleep(wait)
