"""Counts, activity and post history for the screens (design §11, §14;
Requirements 12.1, 12.2, 12.4, 12.5, 15.4).

- `counts(owner)`: {failed, waiting_approval, needs_reconnect, paused} for
  the header badge, which polls it every 30 s (12.2). It counts Autopilot
  posts only: a manual scheduled upload has its own list (the "Scheduled
  posts" modal), and the failures carried over from before the upgrade
  would otherwise sit in the badge for good. `waiting_approval` counts every
  unpublished post still waiting for the user's approval (held posts, and
  Review-mode posts whose slot hasn't come yet), not only those whose slot
  already passed (status `waiting_approval`): the badge is there so the
  user approves in time. A failed post leaves the count when it's rejected.
- `activity_page(owner, ...)`: the activity log, newest first, filtered by
  kind (exact, or a prefix such as "publish."), source, clip or post, paged
  by `before_id` (12.1).
- `history(owner, ...)`: posts that went out or failed (`live`, `private`,
  `kept_private`, `failed`), newest first, each with its account, link and
  latest view counts (12.4, 12.5). Manual scheduled uploads are included,
  carried over as 15.4 asks, with no account: they went out through the
  manual connections. The account shown is the one connected now.
"""
from __future__ import annotations

from autopilot import activity, planner, posts, store
from autopilot.owner import get_prefs

HISTORY_STATUSES = ("live", "private", "kept_private", "failed")
NEEDS_APPROVAL = tuple(sorted(posts.APPROVABLE))
PAGE, MAX_PAGE = 50, 200
TITLE_MAX = 100
# The time a history entry is listed under: when it went out, else when it
# was due to (a post YouTube kept private, a failure).
WHEN = "coalesce(published_at, scheduled_publish_at, slot_at, updated_at, created_at, 0)"


class ReportError(ValueError):
    """A filter the API refuses with 422, naming the field."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field, self.message = field, message


def _limit(limit) -> int:
    return max(1, min(int(limit or PAGE), MAX_PAGE))


# ── counts (12.2) ────────────────────────────────────────────────────────

def counts(owner: str) -> dict:
    with store.connect() as conn:
        row = conn.execute(
            "SELECT coalesce(sum(status='failed'), 0) AS failed, "
            "coalesce(sum(approval='pending' AND status IN "
            f"({','.join('?' for _ in NEEDS_APPROVAL)})), 0) AS waiting_approval, "
            "coalesce(sum(status='needs_reconnect'), 0) AS needs_reconnect "
            "FROM posts WHERE owner_id=? AND method='official'",
            (*NEEDS_APPROVAL, owner)).fetchone()
    return {"failed": int(row["failed"]), "waiting_approval": int(row["waiting_approval"]),
            "needs_reconnect": int(row["needs_reconnect"]),
            "paused": bool(get_prefs(owner).get("paused"))}


# ── activity (12.1) ──────────────────────────────────────────────────────

def _known_kind(kind: str) -> bool:
    if kind.endswith("."):
        return any(k.startswith(kind) for k in activity.KINDS)
    return kind in activity.KINDS


def activity_page(owner: str, *, kind: str | None = None, source: str | None = None,
                  clip: str | None = None, post: str | None = None,
                  before_id: int | None = None, limit: int = PAGE) -> dict:
    """One page of the owner's activity, newest first. `next_before_id` is
    set when the page is full, for the next request."""
    if kind and not _known_kind(kind):
        raise ReportError("kind", f"Unknown activity kind: {kind}")
    limit = _limit(limit)
    rows = activity.list_entries(owner, {"kind": kind, "job_id": source, "clip_id": clip,
                                         "post_id": post}, before_id=before_id, limit=limit)
    tz = posts.owner_timezone(owner)
    entries = [{"id": e["id"], "at": e["at"], "at_label": planner.local_label(e["at"], tz),
                "actor": e["actor"], "kind": e["kind"], "summary": e["summary"],
                "detail": e.get("detail"), "job_id": e.get("job_id"),
                "clip_id": e.get("clip_id"), "post_id": e.get("post_id")} for e in rows]
    return {"entries": entries, "timezone": tz,
            "next_before_id": entries[-1]["id"] if len(entries) == limit else None}


# ── history (12.4, 12.5) ─────────────────────────────────────────────────

def _title(meta: dict) -> str:
    """A short label: the YouTube title, else the caption's first line."""
    text = str((meta or {}).get("title") or "").strip()
    if not text:
        lines = [l.strip() for l in str((meta or {}).get("caption") or "").splitlines()]
        text = next((l for l in lines if l), "")
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1].rstrip() + "…"


def _latest_metrics(conn, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT post_id, fetched_at, age_hours, views, likes, comments FROM post_metrics "
        f"WHERE post_id IN ({','.join('?' for _ in ids)}) ORDER BY fetched_at, rowid",
        ids).fetchall()
    latest: dict[str, dict] = {}
    for r in rows:                                   # later snapshots replace earlier ones
        latest[r["post_id"]] = {k: r[k] for k in ("fetched_at", "age_hours", "views", "likes",
                                                  "comments")}
    return latest


def history(owner: str, *, platform: str | None = None, status: str | None = None,
            before: float | None = None, before_id: str | None = None,
            limit: int = PAGE) -> dict:
    """One page of the post history, newest first. Page with `next`, which
    carries the last entry's time and id, so entries listed under the same
    time (a clip scheduled to both platforms at once) are never skipped."""
    if status is not None and status not in HISTORY_STATUSES:
        raise ReportError("status", "Use live, private, kept_private or failed.")
    if platform is not None and platform not in posts.PLATFORMS:
        raise ReportError("platform", "Use youtube or instagram.")
    limit = _limit(limit)
    statuses = [status] if status else list(HISTORY_STATUSES)
    where = ["owner_id=?", f"status IN ({','.join('?' for _ in statuses)})"]
    params: list = [owner, *statuses]
    if platform:
        where.append("platform=?")
        params.append(platform)
    if before is not None:
        where.append(f"({WHEN} < ? OR ({WHEN} = ? AND id < ?))")
        params += [float(before), float(before), str(before_id or "")]
    tz, paused = posts.owner_timezone(owner), bool(get_prefs(owner).get("paused"))
    with store.connect() as conn:
        rows = store.decode_rows(conn.execute(
            f"SELECT *, {WHEN} AS listed_at FROM posts WHERE {' AND '.join(where)} "
            "ORDER BY listed_at DESC, id DESC LIMIT ?", (*params, limit)).fetchall())
        listed = [r.pop("listed_at") for r in rows]
        items = posts._views(conn, owner, rows, tz, paused)
        metrics = _latest_metrics(conn, [r["id"] for r in rows])
        accounts = {r["platform"]: {"name": r["name"], "avatar": r["avatar"],
                                    "external_id": r["external_id"]}
                    for r in conn.execute("SELECT platform, name, avatar, external_id "
                                          "FROM accounts WHERE owner_id=?", (owner,))}
        jobs = {r.get("job_id") for r in rows if r.get("job_id")}
        deleted = {r["job_id"] for r in conn.execute(
            f"SELECT job_id FROM jobs WHERE job_id IN ({','.join('?' for _ in jobs)}) "
            "AND deleted_at IS NOT NULL", tuple(jobs))} if jobs else set()
    out = []
    for item, when in zip(items, listed):
        out.append({**item, "title": _title(item.get("meta") or {}),
                    "account": accounts.get(item["platform"])
                    if item.get("method") == "official" else None,
                    "metrics": metrics.get(item["id"]),
                    "publish_time": when, "publish_time_label": planner.local_label(when, tz),
                    "source_deleted": item.get("job_id") in deleted})
    last = out[-1] if len(out) == limit else None
    return {"posts": out, "timezone": tz,
            "next": {"before": last["publish_time"], "before_id": last["id"]} if last else None}
