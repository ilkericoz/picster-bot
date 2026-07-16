import json
import re
from datetime import date, datetime, time as dtime

CREW_SCHEDULE_PATH = "crew_schedule.json"
SCHEDULE_BUFFER_MINUTES = 15


def load_crew_schedule():
    try:
        with open(CREW_SCHEDULE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_crew_schedule(schedule):
    with open(CREW_SCHEDULE_PATH, "w", encoding="utf-8") as f:
        json.dump(schedule, f, indent=2)


def record_slot(uuid, booking_date, time_start, time_end, booking_name, tour, source="auto"):
    schedule = load_crew_schedule()
    if any(s["uuid"] == uuid for s in schedule):
        return
    schedule.append({
        "uuid": uuid,
        "date": str(booking_date),
        "time_start": time_start,
        "time_end": time_end,
        "name": booking_name,
        "tour": tour,
        "source": source,
    })
    save_crew_schedule(schedule)


def parse_booking_datetime(text):
    """Parse 'Wednesday, 20 May 2026 @ 17:00' → (date, time_start, time_end)."""
    m = re.search(r'(\d{1,2} \w+ \d{4}) @ (\d{1,2}:\d{2})(?:\s*[–\-—]\s*(\d{1,2}:\d{2}))?', text)
    if not m:
        return None, None, None
    try:
        d = datetime.strptime(m.group(1), "%d %B %Y").date()
        return d, m.group(2), m.group(3)
    except ValueError:
        return None, None, None


def is_in_date_range(booking_date, booking_time_start, entry):
    """Return the matched range dict, or None if no window matched.

    Returns {} (empty dict) when no ranges are configured — meaning accept all.
    Callers MUST use `if matched is None:` (not `if not matched:`) because an
    empty dict is falsy but represents a valid "accept all" result.
    """
    ranges = entry.get("autobook_date_ranges", [])
    if not ranges:
        return {}  # no restriction configured — accept all, use default buffers
    for r in ranges:
        d_from = date.fromisoformat(r["from"]) if r.get("from") else None
        d_to   = date.fromisoformat(r["to"])   if r.get("to")   else None
        if not ((d_from is None or booking_date >= d_from) and
                (d_to   is None or booking_date <= d_to)):
            continue
        t_from = r.get("time_from")
        t_to   = r.get("time_to")
        if not t_from and not t_to:
            return r  # no time restriction — accept
        if t_from and t_to:
            if not booking_time_start:
                return r  # booking time unparseable — accept
            bt = dtime(*map(int, booking_time_start.split(":")))
            tf = dtime(*map(int, t_from.split(":")))
            tt = dtime(*map(int, t_to.split(":")))
            if tf <= bt <= tt:
                return r
        # partial config (only one bound set) — no match, try next range
    return None  # no range matched


def get_tier_delay(haystack, entry):
    """Return (delay_min, delay_max) for this booking. First matching tier wins."""
    tiers = entry.get("booking_tiers", [])
    haystack_lower = haystack.lower()
    for tier in tiers:
        if any(k.lower() in haystack_lower for k in tier.get("keywords", [])):
            return tier.get("delay_min", 0), tier.get("delay_max", 0)
    return 0, 0


def times_overlap(s1, e1, s2, e2, buffer_minutes=0):
    """True if time ranges [s1,e1) and [s2,e2) overlap (HH:MM strings).

    buffer_minutes expands slot 1 in both directions — used for in-flight checks.
    """
    def to_mins(s):
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    return to_mins(s1) - buffer_minutes < to_mins(e2) and to_mins(s2) < to_mins(e1) + buffer_minutes


def find_conflict(booking_date, time_start, time_end,
                  buffer_before=None, buffer_after=None):
    """Return the first conflicting crew schedule entry, or None.

    buffer_before: min gap (minutes) required between new booking's end and an
                   existing booking's start  (gap *before* the existing slot)
    buffer_after:  min gap (minutes) required between an existing booking's end
                   and the new booking's start (gap *after* the existing slot)
    """
    if buffer_before is None:
        buffer_before = SCHEDULE_BUFFER_MINUTES
    if buffer_after is None:
        buffer_after = SCHEDULE_BUFFER_MINUTES

    def to_mins(s):
        h, m = s.split(":")
        return int(h) * 60 + int(m)

    for slot in load_crew_schedule():
        if slot["date"] != str(booking_date):
            continue
        se, ee = slot.get("time_start", ""), slot.get("time_end", "")
        if not se or not ee:
            continue
        # Conflict when:
        #   new starts before (existing ends + after-gap)   → too close after existing
        #   existing starts before (new ends + before-gap)  → too close before existing
        if to_mins(time_start) < to_mins(ee) + buffer_after and \
           to_mins(se) < to_mins(time_end) + buffer_before:
            return slot
    return None
