"""Today's scheduled uploads (the "Scheduled posts" modal and the per-clip
Schedule buttons) on top of `posts` (design §8.7, §13.2; Requirements 11.6,
15.4, N6.1).

A scheduled upload is a post with method 'legacy': approved (the user
scheduled it) and locked to the time the user chose. At that time the
publisher uploads it through today's functions, with today's rule: YouTube
through the browser session when one is connected, otherwise the Data API
upload behind the Upload button; Instagram through instagrapi, with the
title and description as the caption. It is tried once. A failure, or an
upload interrupted by a restart, is recorded and never retried, so a
scheduled upload can't post twice.

Autopilot never creates one (11.6): store.insert_post refuses method
'legacy' unless the caller is this module.

`/api/schedule` keeps today's request and response shapes, read from the
post: status pending | uploading | done | error, plus title, description,
tags, privacy and file from its metadata.
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

import instagram_upload
import paths
import youtube_pw
import youtube_upload
from autopilot import activity, planner, posts, store

STATUS = {"scheduled": "pending", "publishing": "uploading", "live": "done", "failed": "error"}
INTERRUPTED = "Interrupted by a restart — please re-schedule."
FILE_MISSING = "Clip file not found."
REJECTED = "You decided not to post it in the review queue."
LIMIT = 200                                   # today's list size
_JOB_ID = re.compile(r"[0-9a-f]{12}")         # app.py's job id format


def clip_path(job_id, file) -> Path | None:
    """The clip in its job folder, or None when `job_id` isn't a job id (so
    `..` can't point outside the job folders). `.name` guards the file name."""
    if not _JOB_ID.fullmatch(str(job_id or "")):
        return None
    return paths.output_dir() / str(job_id) / Path(str(file or "")).name


def to_schedule(post: dict) -> dict:
    """A legacy post in today's /api/schedule row shape."""
    meta = post.get("meta") or {}
    owner = post.get("owner_id")
    error = post.get("last_error")
    if post.get("status") == "rejected" and not error:
        error = REJECTED
    return {"id": post["id"], "created_at": post.get("created_at"), "job_id": post.get("job_id"),
            "file": meta.get("file") or "", "platform": post.get("platform"),
            "title": meta.get("title") or "", "description": meta.get("description") or "",
            "tags": list(meta.get("tags") or []), "privacy": meta.get("privacy") or "unlisted",
            "scheduled_at": post.get("slot_at"), "status": STATUS.get(post.get("status"), "error"),
            "error": error, "url": post.get("url"),
            "user_id": None if owner == "local" else owner}


def list_schedules(uid: str | None) -> list[dict]:
    """Soonest first, like today. `uid` None (single-user mode) lists all."""
    where, params = "method='legacy'", ()
    if uid is not None:
        where, params = where + " AND owner_id=?", (uid,)
    with store.connect() as conn:
        rows = conn.execute(f"SELECT * FROM posts WHERE {where} ORDER BY slot_at ASC LIMIT ?",
                            (*params, LIMIT)).fetchall()
    return [to_schedule(store.decode_row(r)) for r in rows]


def get(sid: str) -> dict | None:
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id=? AND method='legacy'",
                           (sid,)).fetchone()
    return to_schedule(store.decode_row(row)) if row else None


def create(uid: str | None, *, job_id: str, file: str, platform: str, title: str,
           description: str, tags: list[str], privacy: str, scheduled_at: float) -> dict:
    """Store a scheduled upload, shaped like the migration's copies (§13.1).
    The route has checked the platform, the title, the job and the file."""
    sid, now = uuid.uuid4().hex[:12], time.time()
    owner = uid or "local"
    meta = {"title": title, "description": description, "tags": list(tags or []),
            "privacy": privacy, "file": file}
    with store.connect() as conn:
        store.insert_post(conn, {
            "id": sid, "owner_id": owner, "job_id": job_id, "platform": platform,
            "method": "legacy", "status": "scheduled", "approval": "approved", "holds": [],
            "slot_at": float(scheduled_at), "slot_basis": planner.BASIS_USER, "user_locked": 1,
            "target_visibility": "private" if privacy == "private" else "public",
            "meta": meta, "ai_meta": meta, "created_at": now, "updated_at": now}, legacy=True)
        activity.log(owner, "user", "post.added",
                     f"Scheduled a {posts.PLATFORM_NAMES.get(platform, platform)} upload for "
                     f"{planner.local_label(float(scheduled_at), posts.owner_timezone(owner))}",
                     job_id=job_id, post_id=sid, conn=conn)
    return get(sid)


def delete(uid: str | None, sid: str) -> bool:
    """Remove a scheduled upload from the list, as today (Cancel or Remove).
    False when it doesn't exist, isn't a scheduled upload, or isn't the user's."""
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id=? AND method='legacy'",
                           (sid,)).fetchone()
        if row is None or (uid is not None and row["owner_id"] != uid):
            return False
        conn.execute("DELETE FROM posts WHERE id=?", (sid,))
        conn.execute("DELETE FROM publish_attempts WHERE post_id=?", (sid,))
        if row["status"] == "scheduled":
            activity.log(row["owner_id"], "user", "post.cancelled",
                         f"Cancelled the scheduled "
                         f"{posts.PLATFORM_NAMES.get(row['platform'], row['platform'])} upload",
                         job_id=row["job_id"], conn=conn)
    return True


# ── the upload, with today's rule (moved from scheduler.py) ──────────────

def _upload_youtube(path: Path, s: dict) -> dict:
    privacy = s.get("privacy") or "unlisted"
    if youtube_pw.is_connected():
        return youtube_pw.upload_video(
            path, title=s["title"], description=s["description"],
            tags=s["tags"], privacy=privacy)
    return youtube_upload.upload_video(
        path, title=s["title"], description=s["description"],
        tags=s["tags"], privacy=privacy)


def _upload_instagram(path: Path, s: dict) -> dict:
    caption = "\n\n".join(x for x in (s["title"], s["description"]) if x)
    return instagram_upload.upload_reel(path, caption=caption)


def upload(post: dict, path: Path) -> dict:
    """Upload a scheduled post's clip now. Raises on any failure."""
    meta = post.get("meta") or {}
    s = {"title": meta.get("title") or "", "description": meta.get("description") or "",
         "tags": list(meta.get("tags") or []), "privacy": meta.get("privacy") or "unlisted"}
    if post["platform"] == "youtube":
        return _upload_youtube(path, s)
    if post["platform"] == "instagram":
        return _upload_instagram(path, s)
    raise ValueError(f"Unknown platform: {post['platform']}")
