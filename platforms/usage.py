"""Daily usage counters in `platform_usage` (Requirement 10.4).

A counter is a key (for example `yt-uploads:<client id>`) and a window that
starts at local midnight in a given zone. YouTube's quota day is Pacific time
(America/Los_Angeles), so its counters start a new row at Pacific midnight:
they reset by moving to a new window, never by being cleared, and a day across
a daylight-saving change is 23 or 25 hours long, as YouTube's own is.

`reserve()` checks the limit and adds in one locked transaction, so two
uploads racing at 99 can't both pass; `release()` gives back a reservation
whose call failed before the platform counted it.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from autopilot import store

PACIFIC = ZoneInfo("America/Los_Angeles")


def day_start(now: float, tz=PACIFIC) -> float:
    """UTC epoch of the local midnight that starts the day containing `now`."""
    local = datetime.fromtimestamp(now, tz)
    return datetime(local.year, local.month, local.day, tzinfo=tz).timestamp()


def used(key: str, now: float, tz=PACIFIC) -> int:
    with store.connect() as conn:
        row = conn.execute("SELECT count FROM platform_usage WHERE key=? AND window_start=?",
                           (key, day_start(now, tz))).fetchone()
    return int(row["count"]) if row else 0


def reserve(key: str, amount: int, limit: int, now: float, tz=PACIFIC) -> bool:
    """Add `amount` to today's count if that stays within `limit`; False when
    it wouldn't (nothing is added then)."""
    window = day_start(now, tz)
    with store.connect() as conn:
        row = conn.execute("SELECT count FROM platform_usage WHERE key=? AND window_start=?",
                           (key, window)).fetchone()
        current = int(row["count"]) if row else 0
        if current + amount > limit:
            return False
        conn.execute(
            "INSERT INTO platform_usage(key, window_start, count) VALUES(?,?,?) "
            "ON CONFLICT(key, window_start) DO UPDATE SET count = count + excluded.count",
            (key, window, amount))
    return True


def release(key: str, amount: int, now: float, tz=PACIFIC) -> None:
    """Give back part of today's count (never below zero)."""
    with store.connect() as conn:
        conn.execute("UPDATE platform_usage SET count = max(0, count - ?) "
                     "WHERE key=? AND window_start=?", (amount, key, day_start(now, tz)))
