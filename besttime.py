"""Suggested "best time to post" slots per platform.

These are GENERAL best-practice windows (audience-evening, higher-engagement
weekdays) — not personalized analytics. True best times depend on your own
audience; this just gives sensible starting points. The server runs locally,
so slots are computed in your machine's local timezone.
"""
from __future__ import annotations

import datetime as _dt

# weekday (Mon=0 .. Sun=6) -> recommended local hours (24h). Loosely based on
# common short-form engagement guidance; tweak to taste.
_WINDOWS: dict[str, dict[int, list[int]]] = {
    "youtube": {            # Shorts: late afternoon/evening; weekends strong
        0: [15, 18], 1: [12, 18], 2: [15, 18], 3: [12, 17, 19],
        4: [12, 15, 18], 5: [10, 14, 20], 6: [10, 15, 19],
    },
    "instagram": {          # Reels: late morning + evening; midweek strong
        0: [11, 19], 1: [11, 13, 19], 2: [11, 13, 20], 3: [11, 19],
        4: [11, 14], 5: [10, 12], 6: [11, 19],
    },
}
_DEFAULT_HOURS = [11, 18]


def suggestions(platform: str, now: float, count: int = 6) -> list[float]:
    """Upcoming recommended slot timestamps (epoch seconds), soonest first.

    Only returns slots at least a few minutes in the future, looking up to a
    week ahead.
    """
    windows = _WINDOWS.get(platform, {})
    base = _dt.datetime.fromtimestamp(now).replace(
        minute=0, second=0, microsecond=0)
    out: list[float] = []
    for d in range(0, 8):
        date = base + _dt.timedelta(days=d)
        for h in windows.get(date.weekday(), _DEFAULT_HOURS):
            ts = date.replace(hour=h).timestamp()
            if ts > now + 300:                 # at least 5 minutes ahead
                out.append(ts)
        if len(out) >= count:
            break
    out.sort()
    return out[:count]
