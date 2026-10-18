"""Google credentials for the YouTube client (design §9.1; Requirements 10.6, N3.1).

Single-user mode keeps today's `token.json` (google-auth's `to_json()` format);
multi-user mode keeps today's `user_settings.youtube = {token, channel}`.

* The scopes stored with the token are kept as they are, and widened from the
  token endpoint's `scope` reply, never narrowed. Loading with today's
  `youtube.upload` + `youtube.readonly` list and saving would otherwise erase
  the record of the broader Autopilot grant.
* Refreshing uses the guarded platform client (platforms/http.py): a form POST
  to oauth2.googleapis.com with the refresh token and client secret in the
  body, never the URL. A token is refreshed 5 minutes before it expires
  (10.6); one with no known expiry is used until a call answers 401, and the
  YouTube client then forces a refresh once.
* `invalid_grant` (revoked or expired access) raises PlatformError with
  kind `access` and reason `reconnect` (10.7).
* `token.json` is written atomically and readable by this account only.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import db
import paths
from platforms import errors, http

TOKEN_URI = "https://oauth2.googleapis.com/token"
REFRESH_MARGIN_S = 300
SETTINGS_KEY = "youtube"
LOCAL = "local"
_KNOWN = ("token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes", "expiry")
_lock = threading.Lock()


@dataclass
class GoogleCredentials:
    token: str | None
    refresh_token: str | None
    client_id: str | None
    client_secret: str | None
    scopes: list[str] = field(default_factory=list)
    expiry: float | None = None           # UTC epoch seconds; None when unknown
    extra: dict = field(default_factory=dict)   # other stored fields, kept as they are

    def needs_refresh(self, now: float, margin: float = REFRESH_MARGIN_S) -> bool:
        if not self.token:
            return True
        return self.expiry is not None and self.expiry - margin <= now


def _parse_expiry(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().rstrip("Z")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _format_expiry(value: float | None) -> str | None:
    if value is None:
        return None
    # google-auth's own format: naive UTC with a trailing Z.
    return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def from_dict(data: dict) -> GoogleCredentials:
    scopes = data.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return GoogleCredentials(
        token=data.get("token"), refresh_token=data.get("refresh_token"),
        client_id=data.get("client_id"), client_secret=data.get("client_secret"),
        scopes=[str(s) for s in scopes], expiry=_parse_expiry(data.get("expiry")),
        extra={k: v for k, v in data.items() if k not in _KNOWN})


def to_dict(creds: GoogleCredentials) -> dict:
    out = dict(creds.extra)
    out.update({"token": creds.token, "refresh_token": creds.refresh_token,
                "token_uri": TOKEN_URI, "client_id": creds.client_id,
                "client_secret": creds.client_secret, "scopes": list(creds.scopes)})
    if creds.expiry is not None:
        out["expiry"] = _format_expiry(creds.expiry)
    return out


def token_file() -> Path:
    return paths.data_file("token.json")


def load(owner: str) -> GoogleCredentials | None:
    """The owner's stored credentials, or None when YouTube isn't connected."""
    if owner == LOCAL:
        path = token_file()
        if not path.is_file():
            return None
        try:
            return from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
    stored = db.settings_get(owner, SETTINGS_KEY) or {}
    token = stored.get("token")
    return from_dict(token) if isinstance(token, dict) else None


def save(owner: str, creds: GoogleCredentials) -> None:
    data = to_dict(creds)
    if owner == LOCAL:
        path = token_file()
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        return
    stored = db.settings_get(owner, SETTINGS_KEY) or {}
    db.settings_set(owner, SETTINGS_KEY, {**stored, "token": data})


def granted_scopes(owner: str) -> set[str]:
    creds = load(owner)
    return set(creds.scopes) if creds else set()


def delete(owner: str) -> None:
    """Forget the stored Google token (disconnect, 11.7)."""
    if owner == LOCAL:
        token_file().unlink(missing_ok=True)
        return
    stored = db.settings_get(owner, SETTINGS_KEY)
    if stored is not None:
        db.settings_delete(owner, SETTINGS_KEY)


def client_id(owner: str) -> str | None:
    """The OAuth client id, which names the Google Cloud project's quota."""
    creds = load(owner)
    return creds.client_id if creds else None


def _not_connected() -> errors.PlatformError:
    return errors.PlatformError(errors.Classified(
        errors.ACCESS, "not_connected", "YouTube isn't connected; connect it to continue.",
        reason="reconnect"))


def refresh(owner: str, creds: GoogleCredentials, *, client=None,
            now: float | None = None) -> GoogleCredentials:
    """Get a new access token with the refresh token, and save it."""
    if not (creds.refresh_token and creds.client_id and creds.client_secret):
        raise _not_connected()
    form = {"grant_type": "refresh_token", "refresh_token": creds.refresh_token,
            "client_id": creds.client_id, "client_secret": creds.client_secret}
    own = client is None
    client = client or http.client()
    try:
        try:
            response = client.post(TOKEN_URI, data=form)
        except http.RequestRefused:
            raise
        except Exception as exc:                    # network: try again later
            raise errors.PlatformError(errors.classify_exception("youtube", exc)) from exc
    finally:
        if own:
            client.close()
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {"_text": response.text}
    if response.status_code != 200 or not body.get("access_token"):
        raise errors.PlatformError(errors.classify("youtube", response.status_code, body),
                                   response.status_code, http.request_id(response))
    now = time.time() if now is None else now
    creds.token = body["access_token"]
    creds.expiry = now + float(body.get("expires_in") or 3600)
    if body.get("refresh_token"):                  # Google may rotate it
        creds.refresh_token = body["refresh_token"]
    granted = str(body.get("scope") or "").split()
    creds.scopes = sorted(set(creds.scopes) | set(granted)) if granted else creds.scopes
    save(owner, creds)
    return creds


def access_token(owner: str, *, client=None, now: float | None = None,
                 force: bool = False) -> tuple[str, GoogleCredentials]:
    """A usable access token and the credentials it came from, refreshing
    (and saving) first when it is about to expire or `force` is set."""
    now = time.time() if now is None else now
    with _lock:
        creds = load(owner)
        if creds is None:
            raise _not_connected()
        if force or creds.needs_refresh(now):
            creds = refresh(owner, creds, client=client, now=now)
        return creds.token, creds
