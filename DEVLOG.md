# Devlog

Chronological history of what changed in this bot and why. Newest first.
This file is the narrative git log doesn't give you — root causes, what
broke, what was investigated and ruled out.

---

## 2026-09-27 — Fix false conflicts against just-cancelled bookings

A user report: when a booking gets cancelled on picster.app and the slot
goes to someone else (a new booking for the same date/time), the bot
"doesn't notice" — confirmed via a screenshot showing a "Booking
Cancelled" notification followed ~4 minutes later by the bot alerting a
"Booking CONFLICT — manual action needed" for a new booking in that exact
slot, instead of claiming it.

Root cause: `crew_schedule.json` (the bot's own record of claimed slots) is
only swept for cancellations by `picster/reverifier.py`, which runs every
`REVERIFY_INTERVAL_SECONDS` (30 min). `find_conflict()` in
`picster/schedule.py` trusts that file at face value with no liveness check.
So a slot cancelled and immediately rebooked (well within that 30-minute
window) still looked occupied to the autobook conflict check, and the new
booking got a false conflict alert against a booking that no longer existed.

Fix: when `_handle_new_booking()` in `picster/watcher.py` hits a conflict,
it now re-fetches that slot's own modal live (one extra request, only on
an actual conflict hit) before trusting it. A 404 or non-Active status means
it was already cancelled — the stale entry is removed from
`crew_schedule.json` (`schedule.remove_slot()`, new) and the autobook flow
continues normally instead of alerting. Added
`scenario_stale_conflict_cleared` to `test_picster_payload.py` covering this,
and fixed `scenario_conflict` (which now registers a still-Active modal for
the conflicting slot) since the fake request context previously defaulted
unmocked ids to 404 — indistinguishable from a real cancellation.

## 2026-09-26 — Overlap toggle for autobook gaps; auto-discover new cities

- Added an opt-in "⚠ Overlap" checkbox next to each autobook window's Gap
  before/after fields (`templates/index.html`). Unchecked (default) keeps the
  existing `min=0` behavior. Checked, it unlocks negative buffer minutes
  (clamped to -90..0) so a window can claim a booking that starts inside the
  tail/head of an already-claimed slot — e.g. a 90-minute block where the
  photographer is realistically free well before the block "ends".
  Verified with a standalone JS harness (checkbox toggle, fat-finger clamp,
  pre-existing-negative-value render) before shipping.
- Bot now auto-discovers new cities becoming available on the account.
  picster.app's own City: filter widget lists every city the account can see
  regardless of the `cities=` filter applied to the request, and it's
  already embedded in the same grid HTML the watcher polls every 4-8s — so
  detection costs zero extra requests, just one more regex parse. First
  sighting of an unrecognized city fires a one-time Telegram alert
  (`known_cities.json`), same alert-only/never-auto-added philosophy as the
  existing new-listing-type check.
- Found and fixed a real bug while testing the above: `KNOWN_TYPES_PATH` (and
  the new `KNOWN_CITIES_PATH`) were anchored via
  `Path(__file__).resolve().parent.parent`, which ignores
  `test_picster_payload.py`'s `os.chdir()` isolation — so running the test
  suite was silently overwriting the real `known_listing_types.json`. This
  had already happened once this session before being caught. Switched both
  to plain relative paths, like every other state file (`crew_schedule.json`
  etc.), and cleared the polluted file.

## 2026-09-16 — Chrome "stutter" report was a false alarm

A diagnosis claimed `picster_bot.py` had spawned 8 root Chrome instances on
the bot's profile plus 21 orphans (3.16 GB). Re-checked: that profile had
exactly 1 root + 7 normal children (utility/renderer/gpu/crashpad), 481 MB
total. The "orphans" had no `--user-data-dir` on their command line — they
were the user's own separate Chrome, not the bot's. No code changed; the
lesson was procedural: count processes by PPID, not by grepping the profile
path into every command line, since that counts children too.

## 2026-08-30 — Unbounded-growth fixes; dispose() hardening; silent Chrome launch

Three separate slow leaks found and fixed in one pass, distinct from the
driver memory leak below:
1. `state["seen_ids"]` — grew forever; switched to `{id: date}` with a
   45-day prune (safe since watched months are already dropped past that
   window).
2. An orphaned Chrome tab left behind on every driver restart/reconnect —
   closed in the session's `finally`, plus a defensive sweep for tabs
   orphaned by a session that died via a broken driver pipe.
3. `/screenshot` PNGs never deleted after sending to Telegram — added
   `finally: os.remove(path)`.

Follow-up hardening pass: the three `dispose()` call sites added for the
memory leak below called `dispose()` *after* `.text()`, not in a `finally` —
if `.text()` itself raised, that one body was never freed. Not observed
actually leaking (memory stayed flat for ~19h before this pass), but fixed
to `try/finally` anyway. Same pass made `ui.py`'s scan function consistent
even though its short-lived context made it a non-issue there.

Also: Chrome now launches with no console window, minimized, detached, so it
can't be closed by a stray Ctrl+C or an accidentally-closed terminal.

All verified via `test_picster_payload.py` (34/34 at the time) with the bot
restarted against the fixed code and heartbeat freshness checked before
committing.

## 2026-08-29 — Playwright driver memory leak

- Root-caused a `playwright/driver/node.exe` process observed at ~4.3 GB
  working set: every `request_ctx.get()/.post()` read `.text()` but never
  called `.dispose()` on the `APIResponse`, so response bodies stayed in
  memory until context close — and the context only gets rebuilt on the
  driver-death path (below), not on a schedule. ~37,000 undisposed bodies
  accumulated over ~20.5h of uptime. Fixed at all three call sites
  (`watcher.py` grid poll, `booker.py` GET + POST).
- Running `test_picster_payload.py` *after* this fix (as it should be)
  caught a real gap: the test's `FakeResponse` mock didn't implement
  `dispose()` and crashed against the new calls. Fixed in the test, not
  production.
- `picster_config.json` untracked from git: it constantly drifts from the
  bot's own live writes, so it's runtime state, not source.

## 2026-08-25 → 2026-08-26 — Second city added; dashboard rebuilt

A second city added to the watch list; watcher now reseeds silently when an
entry's `cities` list changes mid-run instead of replaying every
pre-existing booking in a newly-added city as "new"; new/unseen listing
types get a one-time alert instead of silently matching or silently
dropping. Dashboard rewritten around the bot's own config with the dead
config panel removed; autobook windows and the items list collapsed by
default for a less overwhelming UI.

## 2026-08-24 — Fixed the day-long silent outage (Playwright driver death)

Bot had been going fully dead for up to a day at a time, silently, only
fixed by a manual restart. Root cause: `"Connection closed while reading
from the driver"` is not a Chrome/network error — Playwright's own
Python↔Node driver pipe had died (system sleep/resume, resource pressure),
one layer below the Chrome connection the old reconnect loop watched for.
Every subsequent Playwright call failed identically forever, but each was
swallowed by a local `except` and just logged. The heartbeat writer is
independent `asyncio.sleep`-based, so `ui.py`'s `/status` kept reporting
"running" throughout — no visible signal anything was wrong.

Fix: new `picster/driver_health.py::is_driver_dead()` matches driver-pipe
death error strings; on a match, `watcher.py`/`reverifier.py` set a shared
`fatal_event` and stop immediately instead of retrying a dead connection;
`picster_bot.py` restructured so `async_playwright()` itself gets rebuilt on
every session, not just the Chrome/CDP connection. Repeated-failure alert
changed from firing once at 5 failures to firing every 5, so a sustained
outage keeps nagging instead of going silent.

## 2026-07 — picster.app bot added (first commit in this repo)

The picster.app bot proper: month-grid watcher, auto-claim, background
reverifier and Telegram commands, with extra listing-name variants folded
into the keyword filters. Shortly after, unassigned bookings were changed to
alert once instead of on every reverifier cycle.

---

## Open / pending (not yet done)

- **Cloudflare Tunnel permanent URL** for the dashboard (`ui.py`,
  `localhost:5100`) — blocked on activating a Cloudflare plan.
