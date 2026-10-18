"""Who owns a request, and that owner's Autopilot preferences.

The owner is the signed-in user's id in multi-user mode, or the literal
'local' in single-user mode (design KD12). It always comes from the session,
never from a request parameter (Requirement 15.3).
"""
from __future__ import annotations

import functools
import threading
import zoneinfo

from fastapi import HTTPException, Request

import auth
import db

LOCAL_OWNER = "local"
PREFS_KEY = "autopilot_prefs"
DEFAULT_PREFS = {
    "timezone": None,           # IANA name; None until set (the UI offers the browser's)
    "paused": False,            # set only through POST /api/autopilot/pause
    "last_profile_id": None,
    "autopilot_default": False,
}

_PREFS_LOCK = threading.Lock()


class PrefsError(ValueError):
    pass


def current_owner(request: Request) -> str:
    """FastAPI dependency: the owner id for this request."""
    if not auth.enabled():
        return LOCAL_OWNER
    user = getattr(request.state, "user", None)
    if not user or not user.get("id"):
        raise HTTPException(401, "Authentication required.")
    return user["id"]


@functools.lru_cache(maxsize=1)
def _known_zones() -> frozenset[str]:
    try:
        return frozenset(zoneinfo.available_timezones())
    except Exception:
        return frozenset()


def validate_timezone(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PrefsError("Time zone must be a name such as Asia/Kolkata.")
    name = value.strip()
    zones = _known_zones()
    if zones and name not in zones:
        raise PrefsError(f"Unknown time zone: {name}")
    try:
        zoneinfo.ZoneInfo(name)
    except Exception:
        raise PrefsError(f"Unknown time zone: {name}") from None
    return name


def get_prefs(owner: str) -> dict:
    stored = db.settings_get(owner, PREFS_KEY) or {}
    prefs = dict(DEFAULT_PREFS)
    prefs.update({k: v for k, v in stored.items() if k in DEFAULT_PREFS})
    return prefs


def set_prefs(owner: str, **changes) -> dict:
    """Validate and store preference changes; returns the full preferences."""
    unknown = set(changes) - set(DEFAULT_PREFS)
    if unknown:
        raise PrefsError(f"Unknown preference: {', '.join(sorted(unknown))}")
    with _PREFS_LOCK:
        prefs = get_prefs(owner)
        if "timezone" in changes:
            prefs["timezone"] = validate_timezone(changes["timezone"])
        for key in ("paused", "autopilot_default"):
            if key in changes:
                if not isinstance(changes[key], bool):
                    raise PrefsError(f"{key} must be true or false.")
                prefs[key] = changes[key]
        if "last_profile_id" in changes:
            value = changes["last_profile_id"]
            if value is not None and (not isinstance(value, str) or not value
                                      or len(value) > 64):
                raise PrefsError("last_profile_id must be a profile id.")
            prefs["last_profile_id"] = value
        db.settings_set(owner, PREFS_KEY, prefs)
    return prefs
