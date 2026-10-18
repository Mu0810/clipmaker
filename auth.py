"""Optional authentication for public / multi-user deployments.

OFF by default — the single-user localhost experience is unchanged. Enable it
for a public deploy by setting the env var ``CLIPMAKER_AUTH=1``; the whole app
is then gated behind login.

Passwords are hashed with PBKDF2-HMAC-SHA256 (Python stdlib, no extra deps).
Sessions are random opaque tokens stored in the DB and carried in an HttpOnly
cookie.

NOTE: this increment gates *access*. Per-user data isolation (scoping each
job/clip/schedule and connected account to its owner) is a separate, larger
increment — until then, treat auth as suitable for an invite-only/trusted
group rather than a fully isolated multi-tenant service.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

COOKIE = "cm_session"
_ITERATIONS = 200_000


def enabled() -> bool:
    return os.environ.get("CLIPMAKER_AUTH", "").strip().lower() in (
        "1", "true", "yes", "on")


def signup_mode() -> str:
    """How new-account signup is allowed (only relevant when auth is enabled):

    - ``first`` (default): allow signup only until the first account exists,
      letting the deployer claim the owner account once; then it's closed.
    - ``open``: always allow new signups (a truly public service).
    - ``closed``: never allow signups (accounts must be provisioned out-of-band).

    Set via ``CLIPMAKER_SIGNUP``.
    """
    mode = os.environ.get("CLIPMAKER_SIGNUP", "").strip().lower()
    return mode if mode in ("first", "open", "closed") else "first"


def secure_cookies() -> bool:
    """Whether to mark the session cookie Secure (HTTPS only)."""
    return os.environ.get("CLIPMAKER_SECURE_COOKIE", "").strip().lower() in (
        "1", "true", "yes", "on")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)
