"""
Parse the picster.app bookings grid HTML into structured booking dicts.

The grid is server-rendered: each photoshoot type is a <tr class="photoshoot-group-row">
with data-city, containing <button class="booking-card"> cells that carry
data-booking-id, UTC data-starts-at/data-ends-at, and the city timezone.
The crew div inside a card has class "is-empty" when the booking is unclaimed;
otherwise its text is the assigned crew member's name.
"""
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ROW_RE = re.compile(
    r'<tr class="photoshoot-group-row"[^>]*data-city="([^"]*)"[^>]*>(.*?)</tr>',
    re.S,
)
SHOOT_RE = re.compile(r'<div class="row-shoot">([^<]*)</div>')
CARD_RE = re.compile(
    r'<button[^>]*class="booking-card[^"]*"[^>]*data-booking-id="(\d+)"[^>]*>(.*?)</button>',
    re.S,
)
TIME_RE = re.compile(
    r'data-starts-at="([^"]+)"[^>]*data-ends-at="([^"]+)"[^>]*data-city-timezone="([^"]+)"'
)
CREW_RE = re.compile(
    r'<div class="booking-card-crew([^"]*)"[^>]*>\s*(.*?)\s*</div>', re.S
)

# Modal detail page bits (used for alert enrichment and reverification)
MODAL_CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')
MODAL_CLIENT_RE = re.compile(r'data-client-name="([^"]*)"')
MODAL_REF_RE = re.compile(r'BOOKING #(\w+)')
MODAL_STATUS_RE = re.compile(r'class="booking-status-pill"[^>]*>\s*([^<]*?)\s*<')
MODAL_TITLE_RE = re.compile(r'class="booking-hero-title"[^>]*>\s*([^<]*?)\s*<')
MODAL_TIME_RE = re.compile(
    r'class="booking-hero-time"[\s\S]*?data-starts-at="([^"]+)"[\s\S]*?'
    r'data-ends-at="([^"]+)"[\s\S]*?data-city-timezone="([^"]+)"'
)
MODAL_ASSIGN_FORM_RE = re.compile(r'name="form_action" value="assign_self_as_crew"')
MODAL_CREW_ID_RE = re.compile(
    r'value="assign_self_as_crew"[\s\S]{0,400}?name="crew_id" value="(\d+)"'
)
MODAL_CREW_LIST_RE = re.compile(
    r'id="assigned-crew-list-\d+">([\s\S]*?)</tbody>'
)


def _localize(starts_at, ends_at, tzname):
    """UTC ISO strings + tz name → (iso_date, 'HH:MM', 'HH:MM') in city-local time."""
    tz = ZoneInfo(tzname)
    s = datetime.fromisoformat(starts_at).astimezone(tz)
    e = datetime.fromisoformat(ends_at).astimezone(tz)
    return s.date().isoformat(), s.strftime("%H:%M"), e.strftime("%H:%M")


def parse_grid(html):
    """Return a list of booking dicts:
    {id, city, shoot, crew, date ('YYYY-MM-DD'), start ('HH:MM'), end ('HH:MM')}
    crew == "" means unclaimed. Cards missing time attrs are skipped.
    """
    out = []
    for city, row_html in ROW_RE.findall(html):
        sm = SHOOT_RE.search(row_html)
        shoot = sm.group(1).strip() if sm else "?"
        for bid, card_html in CARD_RE.findall(row_html):
            tm = TIME_RE.search(card_html)
            if not tm:
                continue
            cm = CREW_RE.search(card_html)
            crew = ""
            if cm and "is-empty" not in cm.group(1):
                crew = re.sub(r"<[^>]+>", "", cm.group(2)).strip()
            try:
                d, ts, te = _localize(tm.group(1), tm.group(2), tm.group(3))
            except Exception:
                continue
            out.append({
                "id": bid, "city": city, "shoot": shoot, "crew": crew,
                "date": d, "start": ts, "end": te,
            })
    return out


def build_month_url(base_url, cities, month_first_day, statuses=("booked",)):
    """Grid URL for one calendar month (range_days=30 anchors to week_start's month)."""
    params = [
        ("statuses", s) for s in statuses
    ] + [
        ("cities", c) for c in cities
    ] + [
        ("claimed", "claimed"), ("claimed", "unclaimed"),
        ("crew_scope", "all"),
        ("range_days", "30"),
        ("week_start", month_first_day),
    ]
    return f"{base_url.rstrip('/')}/bookings/?{urlencode(params)}"


def months_to_watch(entry, months_ahead=2, today=None):
    """First-of-month ISO dates covering: current month + months_ahead, plus every
    month any autobook window touches. Past months are dropped."""
    today = today or date.today()
    months = set()

    def add(d):
        if (d.year, d.month) >= (today.year, today.month):
            months.add(d.replace(day=1))

    def next_month(d):
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1)

    cur = today.replace(day=1)
    for _ in range(months_ahead + 1):
        add(cur)
        cur = next_month(cur)

    for r in entry.get("autobook_date_ranges", []):
        try:
            d_from = date.fromisoformat(r["from"]) if r.get("from") else today
            d_to = date.fromisoformat(r["to"]) if r.get("to") else d_from
        except (ValueError, KeyError):
            continue
        cur = d_from.replace(day=1)
        while cur <= d_to:
            add(cur)
            cur = next_month(cur)

    return [m.isoformat() for m in sorted(months)]


def parse_modal(html):
    """Extract useful fields from a booking modal HTML fragment."""
    def first(rx, default=""):
        m = rx.search(html)
        return m.group(1).strip() if m else default

    info = {
        "csrf": first(MODAL_CSRF_RE),
        "client": first(MODAL_CLIENT_RE),
        "ref": first(MODAL_REF_RE),
        "status": first(MODAL_STATUS_RE),
        "shoot": first(MODAL_TITLE_RE),
        "can_assign_self": bool(MODAL_ASSIGN_FORM_RE.search(html)),
        "crew_id": first(MODAL_CREW_ID_RE),
        "date": "", "start": "", "end": "",
        "assigned_crew_html": first(MODAL_CREW_LIST_RE),
    }
    tm = MODAL_TIME_RE.search(html)
    if tm:
        try:
            info["date"], info["start"], info["end"] = _localize(
                tm.group(1), tm.group(2), tm.group(3)
            )
        except Exception:
            pass
    return info
