"""Local hardening: private file permissions and a cross-site request check.

The app keeps passwords hashes, session tokens and platform logins in files
next to the code. They are restricted to the account running the app.

The server listens on 127.0.0.1, but any web page open in the user's browser
can still send it a request (a plain HTML form can POST to localhost). The
check below refuses state-changing /api/ requests that come from another site.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse

import paths

PRIVATE_FILES = (
    "clipmaker.db", "clipmaker.db-wal", "clipmaker.db-shm",
    "token.json", "client_secret.json", "meta_app.json", "meta_token.json",
    "ig_session.json", "ig_account.json", "llm_config.json",
    "monetize_config.json", "yt_pw_account.json",
)
PRIVATE_DIRS = ("yt_pw_profile",)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def set_umask() -> None:
    """Files the app creates from now on are readable by this account only."""
    os.umask(0o077)


def _restrict(path: Path, mode: int) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):        # never follow a link out of the data folder
        return
    if stat.S_IMODE(st.st_mode) != mode:
        path.chmod(mode)


def restrict_data_files() -> None:
    """Restrict existing secret-bearing files (600) and folders (700)."""
    root = paths.data_dir()
    for name in PRIVATE_FILES:
        _restrict(root / name, 0o600)
    for name in PRIVATE_DIRS:
        _restrict(root / name, 0o700)
    for backup in root.glob("clipmaker.db.bak-*"):
        _restrict(backup, 0o600)


def _origin_host(value: str) -> str | None:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:                      # IPv6 literal
        host = f"[{host}]"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{host}:{port}"


def _request_host(host_header: str, scheme: str) -> str | None:
    if not host_header:
        return None
    return _origin_host(f"{scheme}://{host_header}")


def is_cross_site(method: str, path: str, headers, scheme: str) -> bool:
    """True when a state-changing /api/ request comes from another site.

    Requests that carry neither Origin nor Referer (curl, scripts) pass: a
    browser always sends Origin on cross-site POST, PUT, PATCH and DELETE.
    """
    if method.upper() in SAFE_METHODS or not path.startswith("/api/"):
        return False
    own = _request_host(headers.get("host", ""), scheme)
    origin = headers.get("origin")
    if origin is not None:
        if origin.strip().lower() == "null":
            return True
        return own is None or _origin_host(origin) != own
    referer = headers.get("referer")
    if referer:
        return own is None or _origin_host(referer) != own
    return False


async def cross_site_guard(request, call_next):
    if is_cross_site(request.method, request.url.path, request.headers,
                     request.url.scheme):
        return JSONResponse({"detail": "Cross-site request blocked."},
                            status_code=403)
    return await call_next(request)
