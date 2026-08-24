"""
Detect Playwright *driver*-level connection death, as distinct from a
Chrome/CDP disconnect.

Playwright's Python client talks to a local Node.js "driver" subprocess over
a stdio pipe; the driver in turn talks to Chrome over CDP. `browser.on(
"disconnected")` only fires for the second link (Chrome going away). If the
first link dies instead (driver process killed, pipe broken by system sleep/
resume, resource exhaustion, ...), the whole `async_playwright()` instance is
permanently dead — every subsequent call raises the same error forever, and
no amount of retrying or reconnecting to Chrome will help. The only fix is
tearing down and recreating `async_playwright()` itself.

Callers that see one of these markers should set the shared `fatal_event`
(in `state["fatal_event"]`) so the outer loop in picster_bot.py restarts
everything, instead of looping forever logging the same failure.
"""

_FATAL_MARKERS = (
    "connection closed while reading from the driver",
    "connection closed while writing to the driver",
    "pipe closed",
)


def is_driver_dead(exc_or_msg) -> bool:
    msg = str(exc_or_msg).lower()
    return any(marker in msg for marker in _FATAL_MARKERS)
