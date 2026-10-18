"""Activity log: every Autopilot decision and action (Requirement 12.1).

Entries are owner-scoped, newest first, and kept for 400 days (the
requirement asks for at least 90). Summaries and details must never contain
tokens; callers pass platform messages through platforms/http.py redaction.
"""
from __future__ import annotations

import json
import time

from autopilot import store

ACTORS = frozenset({"ai", "user", "system"})
KINDS = frozenset({
    # sources (Requirement 1)
    "source.submitted", "source.stage", "source.restarted", "source.failed",
    "source.done", "source.no_clips", "source.deleted", "source.rights_confirmed",
    "source.retried",
    # clips (Requirements 2, 3, 7)
    "clips.chosen", "clip.rendered", "clip.render_failed", "clip.edited", "clip.imported",
    # posts (Requirements 4, 5, 6, 13)
    "post.planned", "post.unscheduled", "post.skipped", "post.held", "post.blocked",
    "post.approved", "post.rejected", "post.edited", "post.reset",
    "post.rescheduled", "post.replanned", "post.added", "post.cancelled",
    "post.waiting_approval", "post.version_changed", "post.retried",
    # publishing (Requirements 8, 9, 10)
    "publish.started", "publish.step", "publish.retry", "publish.deferred",
    "publish.failed", "publish.uploaded", "publish.live", "publish.private",
    "publish.kept_private", "publish.deleted", "publish.missed",
    # accounts and connections (Requirements 10, 11)
    "account.connected", "account.disconnected", "account.needs_reconnect",
    "account.reconnect_reminder", "account.declared",
    # control and settings
    "pause.on", "pause.off", "prefs.changed",
    "profile.created", "profile.updated", "profile.deleted", "profile.duplicated",
    "profile.default_changed", "profile.replanned",
    "music.cleared", "music.uncleared",
    # metrics and maintenance (Requirement 12)
    "metrics.fetched", "maintenance.purged",
})
RETENTION_DAYS = 400
MAX_SUMMARY = 500
MAX_DETAIL_BYTES = 20_000
MAX_PAGE = 200


class ActivityError(ValueError):
    pass


def log(owner: str, actor: str, kind: str, summary: str, *, job_id: str | None = None,
        clip_id: str | None = None, post_id: str | None = None,
        detail=None, at: float | None = None, conn=None) -> int:
    """Append one entry; returns its id. Pass `conn` to write inside the
    caller's open transaction (a second connection would wait on its lock)."""
    if not owner:
        raise ActivityError("owner is required")
    if actor not in ACTORS:
        raise ActivityError(f"Unknown actor: {actor!r}")
    if kind not in KINDS:
        raise ActivityError(f"Unknown activity kind: {kind!r}")
    text = " ".join(str(summary or "").split())[:MAX_SUMMARY]
    if not text:
        raise ActivityError("summary is required")
    detail_json = None
    if detail is not None:
        detail_json = json.dumps(detail, default=str, ensure_ascii=False)
        if len(detail_json.encode("utf-8")) > MAX_DETAIL_BYTES:
            detail_json = json.dumps({"truncated": True, "preview": detail_json[:2000]},
                                     ensure_ascii=False)
    sql = ("INSERT INTO activity(owner_id, at, actor, kind, job_id, clip_id, post_id, "
           "summary, detail_json) VALUES(?,?,?,?,?,?,?,?,?)")
    args = (owner, time.time() if at is None else at, actor, kind, job_id, clip_id,
            post_id, text, detail_json)
    if conn is not None:
        return int(conn.execute(sql, args).lastrowid)
    with store.connect() as own:
        return int(own.execute(sql, args).lastrowid)


def list_entries(owner: str, filters: dict | None = None,
                 before_id: int | None = None, limit: int = 50) -> list[dict]:
    """Newest first. Filters: kind (exact, or a prefix ending in '.'),
    job_id, clip_id, post_id. Page with before_id = the last id you saw."""
    filters = filters or {}
    where, params = ["owner_id=?"], [owner]
    kind = filters.get("kind")
    if kind:
        if kind.endswith("."):
            where.append("substr(kind, 1, ?) = ?")
            params.extend([len(kind), kind])
        else:
            where.append("kind=?")
            params.append(kind)
    for col in ("job_id", "clip_id", "post_id"):
        if filters.get(col):
            where.append(f"{col}=?")
            params.append(filters[col])
    if before_id is not None:
        where.append("id<?")
        params.append(int(before_id))
    limit = max(1, min(int(limit), MAX_PAGE))
    with store.connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM activity WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            (*params, limit)).fetchall()
    return store.decode_rows(rows)


def purge(older_than_days: int = RETENTION_DAYS, now: float | None = None) -> int:
    """Delete entries older than the retention period; returns how many."""
    cutoff = (time.time() if now is None else now) - older_than_days * 86400
    with store.connect() as conn:
        return conn.execute("DELETE FROM activity WHERE at < ?", (cutoff,)).rowcount
