"""
Periodic re-verification of claimed picster bookings.

Every REVERIFY_INTERVAL_SECONDS, fetch the detail modal for each future
picster-sourced entry in crew_schedule.json and check:

  • still exists (404 → canceled, remove + alert)
  • status still Active (else remove + alert)
  • date/time unchanged (else update + alert, re-scan for conflicts)
  • still assigned to our crew member (else alert — someone unassigned us)

Only entries whose source starts with "picster" are touched; Legacy-era
entries are left alone.
"""
import asyncio
from datetime import date

from legacy.schedule import (
    load_crew_schedule, save_crew_schedule, times_overlap, SCHEDULE_BUFFER_MINUTES,
)
from legacy.subscribers import broadcast_alert
from picster.booker import fetch_modal

REVERIFY_INTERVAL_SECONDS = 1800
REVERIFY_STAGGER_SECONDS = 1.0


async def _check_slot(request_ctx, base_url, slot, crew_name):
    """Return ("canceled",) | ("ok", date, start, end, assigned_to_us) | ("error", why)."""
    try:
        status, modal = await fetch_modal(request_ctx, base_url, slot["uuid"])
    except Exception as e:
        return ("error", f"request: {e}")

    if status == 404:
        return ("canceled",)
    if modal is None:
        return ("error", f"HTTP {status}")
    if modal["status"] and modal["status"].lower() != "active":
        return ("canceled",)
    if not modal["date"] or not modal["start"]:
        return ("error", "no times in modal")

    assigned = bool(
        crew_name and crew_name.lower() in modal.get("assigned_crew_html", "").lower()
    )
    return ("ok", modal["date"], modal["start"], modal["end"], assigned)


async def reverify_bookings_once(state, request_ctx, base_url, crew_name):
    snapshot = load_crew_schedule()
    today_str = date.today().isoformat()
    targets = [
        s for s in snapshot
        if s.get("date", "") >= today_str
        and str(s.get("source", "")).startswith("picster")
        and s.get("uuid")
    ]

    updates = {}
    for slot in targets:
        updates[slot["uuid"]] = await _check_slot(request_ctx, base_url, slot, crew_name)
        await asyncio.sleep(REVERIFY_STAGGER_SECONDS)

    # Reload from disk so record_slot writes during the fetches are preserved.
    fresh = load_crew_schedule()
    new_schedule, changed, removed, unassigned = [], [], [], []

    for slot in fresh:
        result = updates.get(slot.get("uuid"))
        if result is None or slot.get("date", "") < today_str:
            new_schedule.append(slot)
            continue

        if result[0] == "error":
            print(f"[REVERIFY] skip #{slot['uuid']}: {result[1]}")
            new_schedule.append(slot)
            continue

        if result[0] == "canceled":
            removed.append(slot)
            continue

        _, nd, nts, nte, assigned = result
        if not assigned:
            unassigned.append(slot)

        if nd != slot.get("date") or nts != slot.get("time_start") or nte != slot.get("time_end"):
            updated = dict(slot)
            updated.update(date=nd, time_start=nts, time_end=nte)
            changed.append((slot, nd, nts, nte))
            new_schedule.append(updated)
        else:
            new_schedule.append(slot)

    save_crew_schedule(new_schedule)

    if not changed and not removed and not unassigned:
        print(f"[REVERIFY] no changes ({len(updates)} future picster bookings checked)")
        return

    changed_ids = {slot.get("uuid") for slot, *_ in changed}
    future_slots = [s for s in new_schedule if s.get("date", "") >= today_str]
    conflicts = []
    for i, a in enumerate(future_slots):
        for b in future_slots[i + 1:]:
            if a.get("date") != b.get("date"):
                continue
            if not all([a.get("time_start"), a.get("time_end"),
                        b.get("time_start"), b.get("time_end")]):
                continue
            if a.get("uuid") not in changed_ids and b.get("uuid") not in changed_ids:
                continue
            if times_overlap(a["time_start"], a["time_end"],
                             b["time_start"], b["time_end"], SCHEDULE_BUFFER_MINUTES):
                conflicts.append((a, b))

    lines = ["Picster schedule re-verify — changes detected", ""]
    if changed:
        lines.append("Time changed:")
        for slot, nd, nts, nte in changed:
            lines.append(f"• {slot.get('name','?')} — {slot.get('tour','?')}")
            lines.append(f"  Was: {slot.get('date')} {slot.get('time_start')}–{slot.get('time_end')}")
            lines.append(f"  Now: {nd} {nts}–{nte}")
        lines.append("")
    if removed:
        lines.append("Canceled (removed):")
        for slot in removed:
            lines.append(f"• {slot.get('name','?')} — {slot.get('tour','?')}")
            lines.append(f"  Was: {slot.get('date')} {slot.get('time_start')}–{slot.get('time_end')}")
        lines.append("")
    if unassigned:
        lines.append("NO LONGER ASSIGNED to us (check manually):")
        for slot in unassigned:
            lines.append(f"• {slot.get('name','?')} — {slot.get('tour','?')} @ "
                         f"{slot.get('date')} {slot.get('time_start')}–{slot.get('time_end')}")
        lines.append("")
    if conflicts:
        lines.append("CONFLICT after update:")
        for a, b in conflicts:
            lines.append(f"• {a.get('name','?')} @ {a.get('date')} {a.get('time_start')}–{a.get('time_end')}")
            lines.append(f"  overlaps with {b.get('name','?')} @ {b.get('date')} {b.get('time_start')}–{b.get('time_end')}")

    broadcast_alert(state["subscribers"], "\n".join(lines).strip())
    print(f"[REVERIFY] alert sent: {len(changed)} changed, {len(removed)} canceled, "
          f"{len(unassigned)} unassigned, {len(conflicts)} conflict(s)")


async def reverify_bookings_loop(state, request_ctx, base_url, crew_name):
    print(f"[REVERIFY] background loop started (every {REVERIFY_INTERVAL_SECONDS}s)")
    while True:
        try:
            await reverify_bookings_once(state, request_ctx, base_url, crew_name)
        except Exception as e:
            print(f"[REVERIFY] loop error: {e}")
        await asyncio.sleep(REVERIFY_INTERVAL_SECONDS)
