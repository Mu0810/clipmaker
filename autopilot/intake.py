"""Autopilot source intake and restart recovery (design §4.1, Requirements
1.2, 1.3, 1.5, 1.6, 13.1, 13.2).

A source is a long video the user hands to Autopilot. `probe_urls` reads each
URL's title, channel and duration with yt-dlp (so the UI can show a rights
confirmation naming each channel), `submit_urls` and `submit_file` turn
confirmed sources into one queued `jobs` row each, and `requeue_interrupted`
re-enqueues jobs a restart left mid-flight, giving up after the second try.

The owner always comes from the session, never from a request (15.3); routes
pass it in. yt-dlp is run through `runner`, which tests replace with a fake.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

import db
import paths
import urlguard
from autopilot import store

MAX_URLS = 20
MAX_DURATION_S = 3 * 60 * 60          # 3 hours (1.6)
PROBE_TIMEOUT_S = 60
MAX_ATTEMPTS = 2                      # one restart retry, then fail (1.5)
INTERRUPTED_TWICE = "Interrupted twice by a restart."

# The job stages and the progress band each covers (1.3).
STAGES = {"downloading": (0, 15), "transcribing": (15, 40), "choosing": (40, 45),
          "metadata": (45, 95), "rendering": (45, 95), "planning": (95, 100)}

_PRINT_FIELDS = "%(id)s\t%(title)s\t%(channel)s\t%(channel_id)s\t%(duration)s"

Runner = Callable[[list[str], int], subprocess.CompletedProcess]


class IntakeError(ValueError):
    """A source was refused; the message is shown to the user."""


def _default_runner(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _own_channel_ids(owner: str) -> set[str]:
    """The connected YouTube account's channel id, for the own-channel test (13.2)."""
    with store.connect() as conn:
        row = conn.execute(
            "SELECT external_id FROM accounts WHERE owner_id=? AND platform='youtube'",
            (owner,)).fetchone()
    return {row["external_id"]} if row and row["external_id"] else set()


def _probe_one(url: str, runner: Runner, own_ids: set[str]) -> dict:
    """Title, channel and duration for one URL, or {"error": …}. Never raises
    for a bad source: a per-URL error is returned so the others still probe."""
    try:
        safe = urlguard.validate(url)
    except urlguard.UrlError as exc:
        return {"url": url, "error": str(exc)}
    from pipeline import _ytdlp_base

    cmd = [*_ytdlp_base(), "--skip-download", "--print", _PRINT_FIELDS, safe]
    try:
        proc = runner(cmd, PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"url": safe, "error": "Timed out reading this video's details."}
    except OSError as exc:
        return {"url": safe, "error": f"Couldn't run yt-dlp: {exc}"}
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return {"url": safe, "error": detail[-1] if detail else "yt-dlp couldn't read this URL."}
    parts = (proc.stdout or "").strip().split("\t")
    if len(parts) < 5:
        return {"url": safe, "error": "yt-dlp returned no details for this URL."}
    vid, title, channel, channel_id, dur = parts[:5]
    try:
        duration = float(dur)
    except ValueError:
        duration = 0.0
    if duration > MAX_DURATION_S:
        return {"url": safe, "error": f"This video is {duration / 3600:.1f} h long; "
                f"the limit is {MAX_DURATION_S // 3600} hours."}
    return {"url": safe, "source_key": f"yt:{vid}", "video_id": vid,
            "title": title or "video", "channel": channel or "",
            "channel_id": channel_id or "", "duration": round(duration, 2),
            "own_channel": bool(channel_id and channel_id in own_ids)}


def probe_urls(urls: list[str], owner: str, *, runner: Runner | None = None) -> list[dict]:
    """Probe up to 20 URLs. Each result carries its details or an error.
    Without `runner`, yt-dlp runs through `_default_runner`, looked up at
    call time so the browser tests can replace it on their live server."""
    cleaned = [u.strip() for u in urls if u and u.strip()]
    if not cleaned:
        raise IntakeError("Add at least one video URL.")
    if len(cleaned) > MAX_URLS:
        raise IntakeError(f"At most {MAX_URLS} URLs at a time; you sent {len(cleaned)}.")
    own_ids = _own_channel_ids(owner)
    run = runner or _default_runner
    return [_probe_one(u, run, own_ids) for u in cleaned]


def _insert_job(conn, *, owner: str, url: str, request: dict, profile: dict,
                source_key: str, channel_id: str, channel_title: str,
                duration: float, rights_basis: str, title: str | None = None) -> str:
    """One queued Autopilot `jobs` row. `owner` is the session owner; a job's
    owner is coalesce(user_id, 'local'), so 'local' is stored as NULL.
    `title` (the probed title, or the file's name) names the source in the
    Sources view until processing stores the downloaded video's own."""
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    user_id = None if owner == "local" else owner
    conn.execute(
        "INSERT INTO jobs(job_id, created_at, url, status, user_id, pct, message, "
        "autopilot, profile_id, profile_snapshot_json, request_json, attempts, "
        "source_key, source_channel_id, source_channel_title, source_duration, "
        "rights_basis, rights_confirmed_at, source_title) "
        "VALUES(?,?,?,?,?,?,?,1,?,?,?,0,?,?,?,?,?,?,?)",
        (job_id, now, url, "queued", user_id, 0, "Queued", profile["id"],
         json.dumps(profile.get("settings") or {}), json.dumps(request),
         source_key, channel_id or None, channel_title or None, duration,
         rights_basis, now, title or None))
    return job_id


def _resolve_profile(owner: str, profile_id: str | None) -> dict:
    from autopilot import profiles

    profile = profiles.get(owner, profile_id) if profile_id else None
    if profile is None:
        profile = profiles.ensure_default(owner)
    return profile


def submit_urls(urls: list[str], owner: str, *, profile_id: str | None = None,
                rights_confirmed: bool = False,
                runner: Runner | None = None) -> list[dict]:
    """Re-probe the URLs server-side, enforce the rights confirmation, and
    queue one job per good source in submission order. Returns one entry per
    URL: a queued job id, or the error that kept it out. Raises IntakeError
    only when nothing can be queued or a needed confirmation is missing."""
    probed = probe_urls(urls, owner, runner=runner)
    good = [p for p in probed if "error" not in p]
    if not good:
        raise IntakeError("None of these URLs could be read: "
                          + "; ".join(p["error"] for p in probed))
    # The confirmation is needed unless every good source is the user's own
    # channel (13.1, 13.2). It is re-checked here, never trusted from the client.
    needs_confirm = any(not p["own_channel"] for p in good)
    if needs_confirm and not rights_confirmed:
        raise IntakeError("Confirm you have the rights to clip these videos before "
                          "submitting.")
    profile = _resolve_profile(owner, profile_id)
    results, queued = [], []
    with store.connect() as conn:
        for p in probed:
            if "error" in p:
                results.append({"url": p["url"], "error": p["error"]})
                continue
            basis = "own_channel" if p["own_channel"] else "confirmed"
            request = {"kind": "url", "url": p["url"], "profile_id": profile["id"]}
            job_id = _insert_job(
                conn, owner=owner, url=p["url"], request=request, profile=profile,
                source_key=p["source_key"], channel_id=p["channel_id"],
                channel_title=p["channel"], duration=p["duration"], rights_basis=basis,
                title=p["title"])
            results.append({"url": p["url"], "job_id": job_id, "title": p["title"]})
            queued.append(job_id)
    enqueue = _enqueuer()
    for job_id in queued:                    # enqueue only after the rows commit
        enqueue(job_id)
    return results


def submit_file(path: Path, filename: str, owner: str, *, profile_id: str | None = None,
                rights_confirmed: bool = False) -> dict:
    """Queue one job for an already-saved uploaded file. Its duration comes
    from ffprobe (1.6) and its source_key from a streamed SHA-256."""
    from media.probe import ProbeError, probe

    if not rights_confirmed:
        raise IntakeError("Confirm you have the rights to clip this video before "
                          "submitting.")
    try:
        duration = probe(path).get("duration") or 0.0
    except ProbeError as exc:
        raise IntakeError(f"Couldn't read the uploaded video: {exc}") from exc
    if duration > MAX_DURATION_S:
        raise IntakeError(f"This video is {duration / 3600:.1f} h long; "
                          f"the limit is {MAX_DURATION_S // 3600} hours.")
    source_key = f"file:{_sha256(path)}"
    profile = _resolve_profile(owner, profile_id)
    request = {"kind": "file", "path": str(path), "filename": filename,
               "profile_id": profile["id"]}
    with store.connect() as conn:
        job_id = _insert_job(
            conn, owner=owner, url=filename, request=request, profile=profile,
            source_key=source_key, channel_id="", channel_title="",
            duration=round(duration, 2), rights_basis="confirmed", title=filename)
    _enqueuer()(job_id)
    return {"job_id": job_id, "title": filename, "duration": round(duration, 2)}


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# The enqueuer is injected so intake doesn't import app (which imports intake).
# app.bootstrap sets it; tests set their own to capture enqueued ids.
_enqueue_fn: Callable[[str], None] | None = None


def set_enqueuer(fn: Callable[[str], None]) -> None:
    global _enqueue_fn
    _enqueue_fn = fn


def _enqueuer() -> Callable[[str], None]:
    if _enqueue_fn is None:
        raise RuntimeError("intake.set_enqueuer() was not called; no job queue is wired.")
    return _enqueue_fn


def requeue_interrupted() -> dict:
    """At startup, deal with jobs a crash or restart left mid-flight (1.5).
    A job still 'queued'/'running' with attempts < 2 is re-enqueued from its
    stored request with attempts + 1; one with attempts already at the limit
    is failed with INTERRUPTED_TWICE. Manual jobs are handled too, since their
    requests are persisted now. Replaces db.reconcile_jobs() in bootstrap().
    Returns {"requeued": [...], "failed": [...]}."""
    requeued, failed = [], []
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT job_id, request_json, attempts FROM jobs "
            "WHERE status IN ('queued','running') AND deleted_at IS NULL "
            "ORDER BY created_at").fetchall()
        for row in rows:
            job_id, attempts = row["job_id"], row["attempts"] or 0
            has_request = bool(row["request_json"])
            if attempts >= MAX_ATTEMPTS or not has_request:
                conn.execute(
                    "UPDATE jobs SET status='error', message=?, error=? WHERE job_id=?",
                    (INTERRUPTED_TWICE, INTERRUPTED_TWICE, job_id))
                failed.append(job_id)
            else:
                conn.execute(
                    "UPDATE jobs SET attempts=?, status='queued', pct=0, "
                    "message='Queued (resumed after a restart)' WHERE job_id=?",
                    (attempts + 1, job_id))
                requeued.append(job_id)
    enqueue = _enqueuer()
    for job_id in requeued:
        enqueue(job_id)
    return {"requeued": requeued, "failed": failed}


_LABELS = {"downloading": "Downloading", "transcribing": "Transcribing",
           "choosing": "Choosing clips", "metadata": "Writing metadata",
           "rendering": "Rendering", "planning": "Planning posts"}
_CLIP_LABELS = {"metadata": "Writing metadata for clip {idx} of {total}",
                "rendering": "Rendering clip {idx} of {total}"}


def stage_pct(stage: str, clip: tuple[int, int] | None = None,
              part: float | None = None) -> int:
    """Overall % for a stage (1.3). With `clip` (1-based index, total), `part`
    is how far through that clip (default: done); without, how far through
    the stage (default: just started)."""
    lo, hi = STAGES.get(stage, (0, 100))
    if clip is not None and clip[1] > 0:
        idx, total = clip
        part = 1.0 if part is None else min(1.0, max(0.0, part))
        return round(lo + (hi - lo) * (idx - 1 + part) / total)
    part = 0.0 if part is None else min(1.0, max(0.0, part))
    return round(lo + (hi - lo) * part)


def stage_message(stage: str, clip: tuple[int, int] | None = None) -> str:
    if clip is not None:
        template = _CLIP_LABELS.get(stage, _LABELS.get(stage, stage.capitalize())
                                    + " clip {idx} of {total}")
        return template.format(idx=clip[0], total=clip[1])
    return _LABELS.get(stage, stage.capitalize())


def set_stage(job_id: str, stage: str, clip: tuple[int, int] | None = None,
              part: float | None = None) -> int:
    """Record a job's stage, message and overall % for /api/status (1.3).
    Returns the % written."""
    pct = stage_pct(stage, clip, part)
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET stage=?, pct=?, message=? WHERE job_id=?",
                     (stage, pct, stage_message(stage, clip), job_id))
    return pct
