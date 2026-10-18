"""Music tracks the owner has marked as cleared for publishing (Requirement 3.9).

Only cleared tracks are ever mixed into a clip that Autopilot publishes. The
music folder itself is shared; clearance is per owner.
"""
from __future__ import annotations

import time

from autopilot import store


def cleared_tracks(owner: str) -> list[str]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT track FROM music_clearance WHERE owner_id=? ORDER BY track",
            (owner,)).fetchall()
    return [r["track"] for r in rows]


def is_cleared(owner: str, track: str) -> bool:
    with store.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM music_clearance WHERE owner_id=? AND track=?",
            (owner, track)).fetchone()
    return row is not None


def set_cleared(owner: str, track: str, cleared: bool) -> None:
    with store.connect() as conn:
        if cleared:
            conn.execute(
                "INSERT INTO music_clearance(owner_id, track, cleared_at) VALUES(?,?,?) "
                "ON CONFLICT(owner_id, track) DO NOTHING",
                (owner, track, time.time()))
        else:
            conn.execute("DELETE FROM music_clearance WHERE owner_id=? AND track=?",
                         (owner, track))
