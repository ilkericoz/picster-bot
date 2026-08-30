"""
Claim ("Assign myself") flow for picster.app.

Two same-origin HTTP round trips through the attached Chrome's session
(no tab, no DOM):

  1. GET  /bookings/modal/<id>/   → fresh csrfmiddlewaretoken + current state
                                    (already claimed? canceled? crew_id)
  2. POST /bookings/              → form_action=assign_self_as_crew
                                    (mirrors the site's own booking-ajax-form)

The POST responds with JSON {ok, booking: {assigned_crew, is_assigned_to_me}}.
"""
import json

from picster.schedule import record_slot
from picster.subscribers import broadcast_alert
from picster.parser import parse_modal

_pending_slots: list = []  # in-memory guard against simultaneous conflicting claims

_XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


async def fetch_modal(request_ctx, base_url, booking_id):
    """GET the booking detail modal. Returns (status_code, parsed_info | None)."""
    url = f"{base_url.rstrip('/')}/bookings/modal/{booking_id}/"
    resp = await request_ctx.get(url, headers=_XHR_HEADERS)
    try:
        status = resp.status
        if not resp.ok:
            return status, None
        text = await resp.text()
        return status, parse_modal(text)
    finally:
        await resp.dispose()  # else the body stays in the driver's memory until context close, even if .text() raises


async def claim_booking(request_ctx, base_url, booking, state):
    """
    Claim one booking as the logged-in crew member.

    booking: parsed grid dict {id, city, shoot, crew, date, start, end}.
    All guards (date window, conflicts, active hours) are pre-checked by the caller,
    which has already appended this slot to _pending_slots.
    """
    bid = booking["id"]
    label = f"{booking['shoot']} @ {booking['date']} {booking['start']}–{booking['end']}"

    try:
        status, modal = await fetch_modal(request_ctx, base_url, bid)

        if modal is None:
            raise RuntimeError(f"modal HTTP {status}")
        if modal["status"] and modal["status"].lower() != "active":
            print(f"[CLAIM] Skip — booking #{bid} status is '{modal['status']}'")
            broadcast_alert(state["subscribers"],
                            f"Claim aborted — booking no longer active ({modal['status']}):\n{label}")
            return
        if not modal["can_assign_self"]:
            crew_html = modal.get("assigned_crew_html", "")
            print(f"[CLAIM] Missed — #{bid} has no 'Assign myself' form (already claimed?)")
            broadcast_alert(state["subscribers"],
                            f"Missed it — booking already claimed:\n{label}")
            return
        if not modal["csrf"]:
            raise RuntimeError("csrfmiddlewaretoken not found in modal HTML")

        form = {
            "csrfmiddlewaretoken": modal["csrf"],
            "form_action": "assign_self_as_crew",
            "booking_id": str(bid),
        }
        if modal["crew_id"]:
            form["crew_id"] = modal["crew_id"]

        post_url = f"{base_url.rstrip('/')}/bookings/"
        resp = await request_ctx.post(
            post_url,
            form=form,
            headers={**_XHR_HEADERS, "Referer": post_url},
        )
        try:
            body = await resp.text()
        finally:
            await resp.dispose()  # else the body stays in the driver's memory until context close, even if .text() raises
        if not resp.ok:
            raise RuntimeError(f"claim POST HTTP {resp.status}: {body[:300]}")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"claim POST returned non-JSON: {body[:300]}")

        b = payload.get("booking") or {}
        assigned = b.get("assigned_crew") or []
        if payload.get("ok") and (b.get("is_assigned_to_me") or assigned):
            record_slot(str(bid), booking["date"], booking["start"], booking["end"],
                        modal.get("client") or "?", booking["shoot"], source="picster")
            names = ", ".join(
                (m.get("name") or "?") for m in assigned if isinstance(m, dict)
            ) or "me"
            print(f"[CLAIM] Done — #{bid} {label} (crew: {names})")
            broadcast_alert(
                state["subscribers"],
                f"Crew assigned:\n{modal.get('client') or '?'} — {booking['shoot']}\n"
                f"{booking['date']} {booking['start']}–{booking['end']}",
            )
        else:
            err = payload.get("error") or payload.get("message") or body[:200]
            raise RuntimeError(f"claim not confirmed: {err}")

    except Exception as e:
        print(f"[CLAIM] Failed (#{bid} {label}): {e}")
        broadcast_alert(state["subscribers"], f"Claim FAILED ({label}):\n{e}")
    finally:
        try:
            _pending_slots.remove((booking["date"], booking["start"], booking["end"]))
        except ValueError:
            pass
