"""Connected platform accounts (design §3.2 `accounts`, §9; Requirements 4.7,
11.1, 11.5, 11.7).

One row per owner and platform. The row never holds a token: tokens stay in
`token.json`, `meta_token.json` or `user_settings`. It holds what the
readiness checklist and the connections screen show: the account's name and
avatar, granted scopes, expiry dates, the declarations the user made, and
whether it needs reconnecting.
"""
from __future__ import annotations

import json
import time
import uuid

from autopilot import store

DECLARATIONS = ("made_for_kids", "audit_declared", "oauth_production_declared")


def get(owner: str, platform: str) -> dict | None:
    with store.connect() as conn:
        return store.decode_row(conn.execute(
            "SELECT * FROM accounts WHERE owner_id=? AND platform=?", (owner, platform)).fetchone())


def upsert(owner: str, platform: str, *, external_id: str, name: str | None,
           avatar: str | None, scopes, quota_key: str | None, page_id: str | None = None,
           token_expires_at: float | None = None, data_access_expires_at: float | None = None,
           now: float | None = None) -> dict:
    """Record a successful connection: status `ok`. Reconnecting keeps the
    declarations; connecting a different channel or account clears the ones
    that belong to it (audience, thumbnail capability)."""
    now = time.time() if now is None else now
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO accounts(id, owner_id, platform, external_id, name, avatar, page_id, "
            "scopes_json, token_expires_at, data_access_expires_at, status, status_reason, "
            "quota_key, thumbnail_capability, connected_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'ok',NULL,?,'unknown',?) "
            "ON CONFLICT(owner_id, platform) DO UPDATE SET "
            "made_for_kids = CASE WHEN accounts.external_id IS excluded.external_id "
            "  THEN accounts.made_for_kids ELSE NULL END, "
            "thumbnail_capability = CASE WHEN accounts.external_id IS excluded.external_id "
            "  THEN accounts.thumbnail_capability ELSE 'unknown' END, "
            "external_id=excluded.external_id, name=excluded.name, avatar=excluded.avatar, "
            "page_id=excluded.page_id, scopes_json=excluded.scopes_json, "
            "token_expires_at=excluded.token_expires_at, "
            "data_access_expires_at=excluded.data_access_expires_at, status='ok', "
            "status_reason=NULL, quota_key=excluded.quota_key, connected_at=excluded.connected_at",
            (uuid.uuid4().hex[:16], owner, platform, external_id, name, avatar, page_id,
             json.dumps(sorted(set(scopes or ()))), token_expires_at, data_access_expires_at,
             quota_key, now))
    return get(owner, platform)


def set_scopes(owner: str, platform: str, scopes) -> None:
    with store.connect() as conn:
        conn.execute("UPDATE accounts SET scopes_json=? WHERE owner_id=? AND platform=?",
                     (json.dumps(sorted(set(scopes or ()))), owner, platform))


def set_declarations(owner: str, platform: str, **values) -> dict | None:
    """Audience (4.7) and the advisory declarations (11.1). True/False, or
    None for "not confirmed"."""
    fields = {k: (None if v is None else int(bool(v))) for k, v in values.items()
              if k in DECLARATIONS}
    if fields:
        with store.connect() as conn:
            conn.execute(f"UPDATE accounts SET {', '.join(f'{k}=?' for k in fields)} "
                         "WHERE owner_id=? AND platform=?", (*fields.values(), owner, platform))
    return get(owner, platform)


def mark_needs_reconnect(owner: str, platform: str, reason: str, *, conn=None) -> None:
    sql = ("UPDATE accounts SET status='needs_reconnect', status_reason=? "
           "WHERE owner_id=? AND platform=?")
    if conn is not None:
        conn.execute(sql, (reason, owner, platform))
        return
    with store.connect() as own:
        own.execute(sql, (reason, owner, platform))


def view(account: dict | None) -> dict | None:
    """What the connections screen shows (11.5)."""
    if not account:
        return None
    flag = lambda v: None if v is None else bool(v)            # noqa: E731
    return {"platform": account["platform"], "external_id": account.get("external_id"),
            "name": account.get("name"), "avatar": account.get("avatar"),
            "page_id": account.get("page_id"), "status": account.get("status"),
            "status_reason": account.get("status_reason"),
            "scopes": account.get("scopes") or [],
            "connected_at": account.get("connected_at"),
            "token_expires_at": account.get("token_expires_at"),
            "data_access_expires_at": account.get("data_access_expires_at"),
            "last_publish_at": account.get("last_publish_at"),
            "made_for_kids": flag(account.get("made_for_kids")),
            "audit_declared": flag(account.get("audit_declared")),
            "oauth_production_declared": flag(account.get("oauth_production_declared")),
            "thumbnail_capability": account.get("thumbnail_capability")}
