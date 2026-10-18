"""Listing and deleting sources (design §3.3, §14; Requirements 1.3, 1.5, 1.7,
1.9, 8.5, N8.1).

`list_sources()` serves GET /api/autopilot/sources: each Autopilot source
with its stage and percentage, whether a restart re-ran it, its outcome
message, best score and AI calls, for the Sources view.

DELETE /api/jobs/{id} (the library's delete button), GET
/api/autopilot/sources/{id}/delete-impact and DELETE
/api/autopilot/sources/{id} share this path.

`impact()` says what deleting a source would do, so the user can confirm
the number of posts it cancels. `delete()` then, in one transaction:
  - marks the source deleted (`jobs.deleted_at`): it leaves the library, the
    status route and the sources list, its queued work never runs and
    processing under way stops before planning anything. The row stays, so
    posts it published keep their source, and 13.4 still recognises their
    clips if the same video is submitted again;
  - cancels its unpublished posts, Autopilot's and scheduled uploads alike,
    with the reason "The video it came from was deleted.";
  - queues the cleanup each cancelled post needs (posts.cleanup_step): a
    YouTube upload that isn't public yet is deleted (8.5), and an upload or
    publish cut short is checked first, so nothing goes public by itself;
  - leaves posts already published, their rows and their metrics, as history.
After the transaction, the source's files are deleted, as today.

A step the publisher is running when the source is deleted is settled by
publisher._withdrawn_meanwhile: what went out is kept, the rest is cleaned up.

`retry()` serves POST /api/autopilot/sources/{id}/retry (design §15,
Requirement 1.10): a failed source is queued again from its stored request,
with the profile settings it was submitted with. Its stage, error, best score
and restart count start afresh; its AI calls stay, and the next run adds to
them (N8.1). The run reuses what the failed try downloaded and transcribed,
and discards the clips it left (process._discard_earlier_attempt). A source
that already has posts, or an upload whose file is gone, is refused, and the
list says which sources can be tried again (`retryable`).
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import paths
from autopilot import activity, intake, posts, store


class SourceDeleted(Exception):
    """Processing stops: the source was deleted while it ran."""


class SourceChanged(Exception):
    """The confirmed number of posts no longer matches (a post was planned or
    published since the preview). `impact` is the current one."""

    def __init__(self, impact: dict):
        super().__init__(impact["message"])
        self.impact = impact


class RetryRefused(Exception):
    """The source can't be tried again; the message says why (1.10)."""


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _classify(post: dict) -> tuple[str, str | None]:
    """("cancel", cleanup step) for a post the deletion cancels, ("published",
    None) for history it keeps, ("keep", None) for one left as it is:
    already withdrawn, or failed with nothing on the platform."""
    if post["status"] in posts.WITHDRAWN:
        return "keep", None
    if posts.is_out(post):
        return "published", None
    step = posts.cleanup_step(post)
    if post["status"] == "failed" and step != "yt_delete":
        return "keep", None                          # a failed attempt stays in the history
    return "cancel", step


def _load(conn, job_id: str, uid: str | None) -> dict | None:
    """The source, unless it's unknown, deleted, or (with sign-in on) not the
    user's. `uid` None is single-user mode, where every source is the user's."""
    job = store.decode_row(conn.execute("SELECT * FROM jobs WHERE job_id=?",
                                        (job_id,)).fetchone())
    if job is None or job.get("deleted_at"):
        return None
    if uid is not None and job.get("user_id") != uid:
        return None
    return job


def _plan(conn, job: dict) -> dict:
    rows = store.decode_rows(conn.execute("SELECT * FROM posts WHERE job_id=? ORDER BY "
                                          "created_at, id", (job["job_id"],)).fetchall())
    plan = {"cancel": [], "published": [], "keep": []}
    for post in rows:
        kind, step = _classify(post)
        plan[kind].append((post, step))
    return plan


def _impact(job: dict, plan: dict) -> dict:
    cancel = [p for p, _ in plan["cancel"]]
    uploads = [p for p, step in plan["cancel"] if step in ("yt_delete", "yt_abandon")]
    scheduled = [p for p in cancel if p.get("method") == "legacy"]
    processing = job.get("status") in ("queued", "running")
    parts = []
    if cancel:
        what = _plural(len(cancel), "post")
        if scheduled:
            what += (f" ({_plural(len(scheduled), 'scheduled upload')}"
                     f"{'' if len(scheduled) == len(cancel) else ' among them'})")
        sentence = (f"Deleting it cancels {what} that "
                    f"{'hasn' if len(cancel) == 1 else 'haven'}'t been published")
        if uploads:
            maybe = [p for p, step in plan["cancel"] if step == "yt_abandon"]
            if maybe:      # an upload cut short may not have reached YouTube at all
                sentence += (f", and deletes their private YouTube uploads (up to "
                             f"{len(uploads)}) before they go public")
            else:
                sentence += (f", and deletes {_plural(len(uploads), 'private YouTube upload')} "
                             f"before {'it goes' if len(uploads) == 1 else 'they go'} public")
        parts.append(sentence + ".")
    if plan["published"]:
        n = len(plan["published"])
        parts.append(f"{_plural(n, 'published post')} {'stays' if n == 1 else 'stay'} in your "
                     "history.")
    if processing:
        parts.append("It's still being processed; that stops too.")
    if not parts:
        parts.append("It has no posts.")
    return {"source": {"id": job["job_id"], "title": job.get("source_title") or job.get("url"),
                       "autopilot": bool(job.get("autopilot")), "processing": processing},
            "unpublished": len(cancel), "youtube_uploads": len(uploads),
            "scheduled_uploads": len(scheduled), "published": len(plan["published"]),
            "message": " ".join(parts)}


def impact(job_id: str, uid: str | None) -> dict | None:
    """What deleting the source would do (1.9), or None when there's no such
    source for this user."""
    with store.connect() as conn:
        job = _load(conn, job_id, uid)
        return None if job is None else _impact(job, _plan(conn, job))


LIST_LIMIT = 100
RETRY_MESSAGE = "Queued to try again"


def _upload_kept(job_id: str, request: dict) -> bool:
    """Whether an uploaded file is still on disk: where it waits, or in the job
    folder, where process._source moves it (as source.<ext>)."""
    staged = Path(request.get("path") or "")
    moved = paths.output_dir() / job_id / f"source{staged.suffix.lower() or '.mp4'}"
    return (bool(request.get("path")) and staged.is_file()) or moved.is_file()


def _retry_refusal(job: dict, post_count: int) -> str | None:
    """Why the source can't be tried again, or None when it can (1.10)."""
    status = job.get("status") or "queued"
    if status in ("queued", "running"):
        return "It's already queued or being processed."
    if status != "error":
        return "It didn't fail, so there's nothing to try again."
    if post_count:
        # Only a run cut short between planning and finishing leaves these.
        return ("It already has posts, so trying again could post the same clips twice. "
                "Delete it and add the video again.")
    request = job.get("request") or {}
    if request.get("kind") == "file" and not _upload_kept(job["job_id"], request):
        return "The uploaded file is gone, so it can't be tried again. Upload the video again."
    return None


def _listed(row) -> dict:
    job = store.decode_row(row)
    request = job.get("request") or {}
    snapshot = job.get("profile_snapshot") or {}
    kind = "file" if request.get("kind") == "file" else "url"
    title = job.get("source_title") or (request.get("filename") if kind == "file" else None)
    return {"id": job["job_id"], "title": title or job.get("url") or "Untitled video",
            "kind": kind, "url": job.get("url") if kind == "url" else None,
            "channel": job.get("source_channel_title"),
            "own_channel": job.get("rights_basis") == "own_channel",
            "duration": job.get("source_duration"),
            "profile": {"id": job.get("profile_id"), "name": job.get("profile_name")},
            "created_at": job.get("created_at"), "status": job.get("status") or "queued",
            "stage": job.get("stage"), "pct": round(job.get("pct") or 0),
            "message": job.get("message"), "error": job.get("error"),
            # One restart re-runs a source (1.5); a second fails it.
            "restarted": (job.get("attempts") or 0) >= 1,
            "ai_calls": job.get("ai_calls") or 0, "best_score": job.get("best_score"),
            "min_score": snapshot.get("min_score"), "posts": job.get("post_count") or 0,
            "retryable": _retry_refusal(job, job.get("post_count") or 0) is None}


def _select(conn, where: str, params: list, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT j.job_id, j.created_at, j.url, j.status, j.source_title, j.error, j.pct, "
        "j.message, j.stage, j.attempts, j.ai_calls, j.best_score, j.profile_id, "
        "j.profile_snapshot_json, j.request_json, j.source_channel_title, "
        "j.source_duration, j.rights_basis, p.name AS profile_name, "
        "(SELECT COUNT(*) FROM posts WHERE posts.job_id=j.job_id) AS post_count "
        "FROM jobs j LEFT JOIN profiles p "
        "ON p.id=j.profile_id AND p.owner_id=coalesce(j.user_id, 'local') "
        f"WHERE {where} ORDER BY j.created_at DESC, j.job_id LIMIT ?",
        (*params, limit)).fetchall()
    return [_listed(r) for r in rows]


def list_sources(uid: str | None, *, limit: int = LIST_LIMIT) -> list[dict]:
    """The user's Autopilot sources, newest first, for the Sources view (1.3,
    1.5, 1.7, 1.10, N8.1). `uid` None is single-user mode, where every source
    is the user's, as in `_load`. Deleted sources and manual jobs aren't
    listed; the file path an upload waits at never leaves the server. The
    profile's name is its current one, or None once the profile is deleted."""
    where, params = "j.autopilot=1 AND j.deleted_at IS NULL", []
    if uid is not None:
        where += " AND j.user_id=?"
        params.append(uid)
    with store.connect() as conn:
        return _select(conn, where, params, limit)


def retry(job_id: str, uid: str | None) -> dict | None:
    """Queue a failed source again (1.10); see the module notes. Returns
    {"source": the source as the list shows it}, None when there's no such
    Autopilot source for this user, or raises RetryRefused saying why not."""
    with store.connect() as conn:
        job = _load(conn, job_id, uid)
        if job is None or not job.get("autopilot"):
            return None
        count = conn.execute("SELECT COUNT(*) FROM posts WHERE job_id=?", (job_id,)).fetchone()[0]
        refusal = _retry_refusal(job, count)
        if refusal:
            raise RetryRefused(refusal)
        conn.execute(
            "UPDATE jobs SET status='queued', stage=NULL, pct=0, message=?, error=NULL, "
            "result_json=NULL, best_score=NULL, attempts=0 WHERE job_id=?",
            (RETRY_MESSAGE, job_id))
        [listed] = _select(conn, "j.job_id=?", [job_id], 1)
        why = (job.get("error") or "").strip()
        if why and why[-1] not in ".!?":
            why += "."
        activity.log(job.get("user_id") or "local", "user", "source.retried",
                     f"Tried “{listed['title']}” again." + (f" It had failed: {why}" if why else ""),
                     job_id=job_id, conn=conn)
    intake._enqueuer()(job_id)                # after the commit, as intake does
    return {"source": listed}


def delete(job_id: str, uid: str | None, *, confirm: int | None = None,
           now: float | None = None) -> dict | None:
    """Delete the source (1.9). With `confirm`, the number of unpublished
    posts the user agreed to cancel must still be right, else SourceChanged
    and nothing changes. Returns what was done, or None when there's no such
    source for this user (already deleted included)."""
    now = time.time() if now is None else now
    with store.connect() as conn:
        job = _load(conn, job_id, uid)
        if job is None:
            return None
        plan = _plan(conn, job)
        summary = _impact(job, plan)
        if confirm is not None and int(confirm) != summary["unpublished"]:
            raise SourceChanged(summary)
        owner = job.get("user_id") or "local"
        conn.execute("UPDATE jobs SET deleted_at=?, stage=NULL WHERE job_id=?", (now, job_id))
        for post, step in plan["cancel"]:
            fields = {"status": "cancelled", "user_locked": 0, "step": step,
                      "next_action_at": now if step else None, "retry_count": 0,
                      "scheduled_publish_at": None, "paused_hold": 0,
                      "last_error": posts.CANCELLED_REASON, "last_error_class": "cancelled",
                      "updated_at": now}
            if post.get("method") == "official":       # a scheduled upload keeps its time
                fields.update({"slot_at": None, "slot_basis": None})
            store.update_post(conn, post["id"], fields)
            note = (" Its private YouTube upload is deleted before it goes public."
                    if step in ("yt_delete", "yt_abandon") else "")
            activity.log(post["owner_id"], "user", "post.cancelled",
                         f"Cancelled the {posts._label(post)}: the video it came from was deleted.{note}",
                         job_id=job_id, clip_id=post.get("clip_id"), post_id=post["id"],
                         detail={"cleanup": step}, conn=conn)
        activity.log(owner, "user", "source.deleted",
                     f"Deleted {summary['source']['title'] or 'a video'}. {summary['message']}",
                     job_id=job_id, detail={k: summary[k] for k in (
                         "unpublished", "youtube_uploads", "scheduled_uploads", "published")},
                     conn=conn)
    remove_files(job_id, job.get("request"))
    return {"deleted": job_id, "cancelled": [p["id"] for p, _ in plan["cancel"]],
            "youtube_uploads": [p["id"] for p, s in plan["cancel"]
                                if s in ("yt_delete", "yt_abandon")],
            "published": [p["id"] for p, _ in plan["published"]]}


def is_deleted(job_id: str) -> bool:
    with store.connect() as conn:
        row = conn.execute("SELECT deleted_at FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return bool(row and row["deleted_at"])


def remove_files(job_id: str, request: dict | None = None) -> None:
    """The source's folder, as today, and an uploaded file still waiting to be
    processed (it waits outside the folder until processing moves it in).
    Only a direct child of output/ is ever removed."""
    root = paths.output_dir().resolve()
    job_dir = (root / str(job_id)).resolve()
    if job_dir.parent == root and job_dir.is_dir():
        shutil.rmtree(job_dir, ignore_errors=True)
    request = request or {}
    if request.get("kind") == "file" and request.get("path"):
        staged = Path(request["path"]).resolve()
        if staged.parent == root and staged.name.startswith("upload-") and staged.is_file():
            staged.unlink(missing_ok=True)
