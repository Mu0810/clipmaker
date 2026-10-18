"""OAuth `state` values for the connect flows (design §16; Requirement N3.3).

A state is issued per owner and purpose ("youtube", "instagram"), is
single-use (taken before the code is exchanged, so a replay fails even if
the exchange errors), expires after 10 minutes, and carries what the
callback needs to finish (for example the scopes that were requested).
"""
from __future__ import annotations

import hmac
import secrets
import time

import db

TTL_S = 10 * 60


class StateError(ValueError):
    """A missing, mismatched, reused or expired state."""


def _key(purpose: str) -> str:
    return f"oauth_state:{purpose}"


def issue(owner: str, purpose: str, extra: dict | None = None, *, now: float | None = None) -> str:
    state = secrets.token_urlsafe(24)
    db.settings_set(owner, _key(purpose), {"state": state,
                                           "issued_at": time.time() if now is None else now,
                                           "extra": extra or {}})
    return state


def consume(owner: str, purpose: str, state: str, *, now: float | None = None) -> dict:
    """The issued state's `extra`, after checking it; raises StateError.
    The stored state is removed whether or not it matched, so it can't be
    tried again."""
    saved = db.settings_get(owner, _key(purpose)) or {}
    db.settings_delete(owner, _key(purpose))
    now = time.time() if now is None else now
    expected = str(saved.get("state") or "")
    if not state or not expected or not hmac.compare_digest(str(state), expected):
        raise StateError("This sign-in link isn't valid any more. Start connecting again.")
    if now - float(saved.get("issued_at") or 0) > TTL_S:
        raise StateError("This sign-in link expired after 10 minutes. Start connecting again.")
    return saved.get("extra") or {}
