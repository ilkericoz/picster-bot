"""
Picster reverifier notification simulator.
Runs reverify_bookings_once across several cycles with a synthetic schedule and
fake modal responses to verify the unassigned-alert dedup: an intentional drop
alerts ONCE, stays silent on later cycles, and re-alerts only if the booking
comes back to us and is dropped again.
"""
import asyncio
from datetime import date, timedelta
from unittest.mock import patch

import picster.reverifier as rv

FUTURE = (date.today() + timedelta(days=10)).isoformat()
FUTURE2 = (date.today() + timedelta(days=11)).isoformat()
CREW = "Test"


def make_slot(uuid, name, tour, d, ts, te):
    return {"uuid": uuid, "name": name, "tour": tour,
            "date": d, "time_start": ts, "time_end": te, "source": "picster"}


def make_modal(d, ts, te, assigned=True, status="Active"):
    return {"status": status, "date": d, "start": ts, "end": te,
            "assigned_crew_html": f"<span>{CREW} Crew</span>" if assigned else "",
            "client": "?", "csrf": "x", "crew_id": "1", "can_assign_self": False}


def run_cycle(label, schedule, modals, expect_alert):
    """modals: uuid -> modal dict (or "404"). Returns the saved schedule."""
    print("\n" + "=" * 65)
    print(f"CYCLE: {label}")
    print("=" * 65)

    alerts, saved = [], {"schedule": list(schedule)}

    async def fake_fetch(request_ctx, base_url, uuid):
        m = modals[uuid]
        return (404, None) if m == "404" else (200, m)

    def fake_broadcast(subscribers, msg):
        alerts.append(msg)
        print("\n[TELEGRAM MESSAGE]")
        print("-" * 50)
        print(msg)
        print("-" * 50)

    def fake_save(sched):
        saved["schedule"] = sched

    with patch.object(rv, "load_crew_schedule", side_effect=lambda: list(schedule)), \
         patch.object(rv, "save_crew_schedule", side_effect=fake_save), \
         patch.object(rv, "fetch_modal", side_effect=fake_fetch), \
         patch.object(rv, "broadcast_alert", side_effect=fake_broadcast), \
         patch.object(rv, "REVERIFY_STAGGER_SECONDS", 0):
        asyncio.run(rv.reverify_bookings_once({"subscribers": []}, None, "https://picster.app", CREW))

    if not alerts:
        print("\n[NO TELEGRAM MESSAGE]")
    assert bool(alerts) == expect_alert, \
        f"FAIL: expected alert={expect_alert}, got {len(alerts)} message(s)"
    print(f"OK — alert sent: {bool(alerts)} (expected {expect_alert})")
    return saved["schedule"]


schedule = [
    make_slot("p-001", "Alice Smith", "Photoshoot", FUTURE, "09:00", "10:30"),
    make_slot("p-002", "Bob Jones", "Downtown Premium", FUTURE2, "14:00", "15:30"),
]

# 1. We drop p-001 on the site → one alert, slot flagged.
modals = {"p-001": make_modal(FUTURE, "09:00", "10:30", assigned=False),
          "p-002": make_modal(FUTURE2, "14:00", "15:30")}
schedule = run_cycle("Drop detected (expect ONE alert)", schedule, modals, expect_alert=True)
assert any(s.get("unassigned_alerted") for s in schedule if s["uuid"] == "p-001"), \
    "FAIL: p-001 not flagged unassigned_alerted"

# 2. Still unassigned next cycle → silence.
schedule = run_cycle("Still unassigned (expect silence)", schedule, modals, expect_alert=False)

# 3–4. Booking reassigned to us → flag clears silently; dropped again → alerts again.
modals["p-001"] = make_modal(FUTURE, "09:00", "10:30", assigned=True)
schedule = run_cycle("Reassigned to us (expect silence, flag cleared)", schedule, modals, expect_alert=False)
assert not any(s.get("unassigned_alerted") for s in schedule if s["uuid"] == "p-001"), \
    "FAIL: flag not cleared after reassignment"

modals["p-001"] = make_modal(FUTURE, "09:00", "10:30", assigned=False)
schedule = run_cycle("Dropped a second time (expect ONE alert again)", schedule, modals, expect_alert=True)

# 5. Cancellation still alerts (regression check).
modals["p-002"] = "404"
schedule = run_cycle("p-002 canceled (expect alert)", schedule, modals, expect_alert=True)
assert not any(s["uuid"] == "p-002" for s in schedule), "FAIL: canceled slot not removed"

print("\n" + "=" * 65)
print("All picster reverifier scenarios passed.")
