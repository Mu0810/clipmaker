"""Autopilot source pipeline: one queued source from download to planned posts
(design §4, Requirements 1.2-1.4, 1.7, 2.3, 3.1, 14.3, N8.1).

`run(job_id)` performs, in order:

1. the source: download it (yt-dlp skips a finished download after a restart)
   or move the uploaded file into the job folder;
2. the transcript, with the Autopilot Whisper model and the profile language;
3. clip selection;
4. for each clip: per-platform metadata (autopilot/metadata.py), the initial
   EditSpec, and render version 1 into `clips` and `clip_versions`;
5. the `sensitive` and `text_shaping` holds, kept on the clip with its AI text;
6. posts and slots (autopilot/posts.py), when any clip reaches the minimum
   score; otherwise the source finishes with the best score found (1.7);
7. the result mirrored into `jobs.result_json` in the manual flow's shape, so
   the existing library shows Autopilot clips too.

Settings come from the profile snapshot stored at submission (14.3), so a
profile edited later doesn't change a source already queued. Every AI call
inside the run is added to `jobs.ai_calls` (N8.1), so a source tried again
(1.10) shows the calls of every try. Stages and the overall
% are written to the job row for /api/status (1.3), which is also what lets
processing carry on with the browser closed (1.4): nothing here waits on a
client.

A run that follows an unfinished one (a restart re-run, 1.5, or a failed
source tried again, 1.10) first discards the clips the earlier try left, so
the same moments aren't planned twice; its download and transcript are reused.
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path

import auth
import db
import llm
import paths
from autopilot import activity, clearance, intake, metadata, posts, sources, store
from media import editspec, render, select
from media import text as text_mod
from media import transcript as transcript_mod

log = logging.getLogger(__name__)


class ProcessError(RuntimeError):
    """A source couldn't be processed; the message is shown to the user."""


def _job(job_id: str) -> dict | None:
    with store.connect() as conn:
        return store.decode_row(conn.execute("SELECT * FROM jobs WHERE job_id=?",
                                             (job_id,)).fetchone())


def _stop_if_deleted(job_id: str) -> None:
    """1.9: the user deleted the source while it was processing."""
    if sources.is_deleted(job_id):
        raise sources.SourceDeleted(job_id)


def _discard_earlier_attempt(job_id: str, job_dir: Path) -> int:
    """Remove the clips an unfinished earlier try left: a run a restart cut
    short (1.5), or one that failed and is tried again (1.10). Kept, they'd be
    planned beside the new run's clips, posting the same moments twice, and
    the library's (video, clip number) links would open them instead. Only
    clips without posts go, with their versions and files; a file a kept
    version still names stays. A first run has none. Returns how many went."""
    with store.connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM clips WHERE job_id=? AND NOT EXISTS "
            "(SELECT 1 FROM posts WHERE posts.clip_id = clips.id)", (job_id,)).fetchall()]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        gone = store.decode_rows(conn.execute(
            f"SELECT files_json FROM clip_versions WHERE clip_id IN ({marks})", ids).fetchall())
        kept = store.decode_rows(conn.execute(
            "SELECT v.files_json FROM clip_versions v JOIN clips c ON c.id = v.clip_id "
            f"WHERE c.job_id=? AND c.id NOT IN ({marks})", (job_id, *ids)).fetchall())
        conn.execute(f"DELETE FROM clip_versions WHERE clip_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM clips WHERE id IN ({marks})", ids)
    in_use = {Path(name).name for v in kept for name in (v.get("files") or {}).values() if name}
    root = job_dir.resolve()
    for version in gone:
        for name in (version.get("files") or {}).values():
            if name and Path(name).name not in in_use:
                (root / Path(name).name).unlink(missing_ok=True)
    log.info("Discarded %d clip(s) an earlier try of %s left.", len(ids), job_id)
    return len(ids)


def _update_job(job_id: str, **fields) -> None:
    allowed = {"stage", "pct", "message", "ai_calls", "best_score", "language"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if fields:
        with store.connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} "
                         "WHERE job_id=?", (*fields.values(), job_id))


def _title_from_name(name: str) -> str:
    return re.sub(r"[._\-]+", " ", Path(name or "").stem).strip() or "video"


def _source(job: dict, job_dir: Path) -> tuple[Path, str]:
    """The video to process and its title. An upload is moved out of the
    staging area into the job folder, so deleting the source deletes it."""
    request = job.get("request") or {}
    if request.get("kind") == "file":
        staged = Path(request.get("path") or "")
        dest = job_dir / f"source{staged.suffix.lower() or '.mp4'}"
        if staged.is_file() and staged.resolve() != dest.resolve():
            os.replace(staged, dest)
        if not dest.is_file():
            raise ProcessError("The uploaded video is missing; submit it again.")
        return dest, _title_from_name(request.get("filename") or dest.name)
    import pipeline

    url = request.get("url") or job.get("url")
    if not url:
        raise ProcessError("This video has no link to download.")
    video, info = pipeline.download(url, job_dir, lambda message, pct=None: None)
    return Path(video), info.get("title") or "video"


class _Stages:
    """Writes the stage and overall % (1.3), only when either changes, so a
    render's frequent progress callbacks don't each hit the database. `seen`
    keeps the order of stages, for tests and the activity log."""

    def __init__(self, job_id: str):
        self.job_id, self.last, self.seen = job_id, None, []

    def __call__(self, stage: str, clip: tuple[int, int] | None = None,
                 part: float | None = None) -> None:
        key = (stage, clip, intake.stage_pct(stage, clip, part))
        if key == self.last:
            return
        if not self.seen or self.seen[-1] != (stage, clip):
            self.seen.append((stage, clip))
        self.last = key
        intake.set_stage(self.job_id, stage, clip, part)


def clip_text(transcript: dict, clip: dict, language: str | None) -> str:
    """The words of a selected clip: its sentences, joined the way the
    language writes them (no spaces in Japanese, for example)."""
    sentences = (transcript.get("sentences") or [])[clip["s0"]:clip["s1"]]
    return transcript_mod.separator(language).join(s["text"] for s in sentences).strip()


def _slug(text: str) -> str:
    import pipeline

    return pipeline._slug(text)


def _music_settings(owner: str, settings: dict) -> tuple[dict, str | None]:
    """Use the profile's music only when the owner has cleared the track for
    publishing (3.9); otherwise render without it and say why."""
    track = settings.get("music_track")
    if not track or clearance.is_cleared(owner, track):
        return settings, None
    return ({**settings, "music_track": None},
            f"The music track '{track}' isn't cleared for publishing, so the clips were "
            "made without music.")


def _clip_holds(meta: dict, text: str) -> list[str]:
    """Holds that apply to every post of the clip (§5.3): the metadata's, plus
    `text_shaping` when its captions or hook need shaping that isn't installed."""
    holds = list(meta.get("holds") or [])
    if text_mod.needs_shaping(f"{text} {meta.get('hook') or ''}") \
            and not text_mod.shaping_available():
        holds.append("text_shaping")
    return holds


def _make_clip(job: dict, owner: str, transcript: dict, video: Path, job_dir: Path,
               clip: dict, idx: int, total: int, settings: dict, *, language,
               monetize_cfg, stages) -> dict:
    """Metadata, rows and render for one selected clip. Returns
    {"clip_id", "selected", "meta", "base", "render" (None on failure), "error"}."""
    job_id = job["job_id"]
    stages("metadata", (idx, total), 0.0)
    text = clip_text(transcript, clip, language)
    first = ((transcript.get("sentences") or [])[clip["s0"]] or {}).get("text", "")
    meta = metadata.generate(
        text, language=settings.get("metadata_language") or language,
        brand=settings.get("brand") or {}, monetize_cfg=monetize_cfg,
        fallback_title=clip.get("title") or "", first_sentence=first,
        disclosure=bool(settings.get("ai_disclosure")))
    holds = _clip_holds(meta, text)
    stored_meta = {"youtube": meta["youtube"], "instagram": meta["instagram"],
                   "hook": meta["hook"], "title_suggestions": meta["title_suggestions"],
                   "holds": holds, "sensitive_categories": meta["sensitive_categories"]}
    spec = editspec.initial(clip["start"], clip["end"], settings, hook_text=meta["hook"])
    clip_id, version_id, now = uuid.uuid4().hex[:16], uuid.uuid4().hex[:16], time.time()
    title = meta["youtube"].get("title") or clip.get("title") or "Clip"
    base = f"{idx:02d}-{_slug(title)}"
    with store.connect() as conn:
        store.insert_clip(conn, {
            "id": clip_id, "owner_id": owner, "job_id": job_id, "idx": idx,
            "src_start": clip["start"], "src_end": clip["end"], "score": clip.get("score"),
            "score_reason": clip.get("reason"), "method": clip.get("method"),
            "sensitive": {"flag": "sensitive" in holds,
                          "categories": meta["sensitive_categories"]},
            "meta": stored_meta, "current_version_id": version_id, "created_at": now})
        store.insert_clip_version(conn, {
            "id": version_id, "clip_id": clip_id, "version": 1, "spec": spec.to_dict(),
            "status": "rendering", "created_at": now})

    stages("rendering", (idx, total), 0.1)
    try:
        out = render.render_version(
            job_dir, video, transcript, spec.to_dict(), 1, base,
            lambda step, fraction: stages("rendering", (idx, total), 0.1 + 0.9 * fraction),
            title=title, is_cleared=lambda track: clearance.is_cleared(owner, track))
    except (render.RenderError, editspec.EditSpecError, OSError) as exc:
        with store.connect() as conn:
            store.update_row(conn, "clip_versions", version_id, {
                "status": "failed", "error": str(exc)[:2000], "finished_at": time.time()})
        activity.log(owner, "system", "clip.render_failed",
                     f"Couldn't render clip {idx}: {exc}", job_id=job_id, clip_id=clip_id)
        return {"clip_id": clip_id, "selected": clip, "meta": meta, "base": base,
                "render": None, "error": str(exc)}
    with store.connect() as conn:
        store.update_row(conn, "clip_versions", version_id, {
            "status": "ready", "files": out["files"], "probe": out["probe"],
            "spec": out["spec"], "cover": out["cover"], "finished_at": time.time()})
    activity.log(owner, "system", "clip.rendered",
                 f"Rendered clip {idx} ({out['duration']:.1f} s, {out['layout']}), "
                 f"score {clip.get('score')}", job_id=job_id, clip_id=clip_id,
                 detail={"layout": out["layout"], "cover": out["cover"]})
    return {"clip_id": clip_id, "selected": clip, "meta": meta, "base": base,
            "render": out, "error": None}


def _library_entry(made: dict) -> dict:
    """One clip in the manual flow's result shape, so the library shows it."""
    clip, meta, out = made["selected"], made["meta"], made["render"]
    youtube, files = meta["youtube"], out["files"]
    span = clip["end"] - clip["start"]
    return {
        "index": clip.get("_idx"), "title": youtube.get("title") or clip.get("title"),
        "hook": meta.get("hook") or "", "description": youtube.get("description") or "",
        "tags": youtube.get("tags") or [], "title_suggestions": meta.get("title_suggestions") or [],
        "hashtags": meta["instagram"].get("hashtags") or [],
        "thumbnail": files.get("thumb"), "virality": clip.get("score"),
        "virality_reason": clip.get("reason") or "",
        "start": round(clip["start"], 2), "end": round(clip["end"], 2),
        "duration": round(out["duration"], 2),
        "trimmed": round(max(0.0, span - out["duration"]), 2),
        "original": files.get("original"), "vertical": files.get("vertical"),
    }


def _process(job: dict, owner: str, settings: dict, job_dir: Path, monetize_cfg,
             calls, stages: _Stages) -> dict:
    job_id = job["job_id"]
    stages("downloading")
    video, title = _source(job, job_dir)
    _stop_if_deleted(job_id)

    stages("transcribing")
    transcript = transcript_mod.load_or_transcribe(
        job_dir, video, language=settings.get("language"),
        progress=lambda fraction: stages("transcribing", None, fraction))
    language = transcript.get("language") or settings.get("language")
    _update_job(job_id, language=language)

    stages("choosing")
    chosen, method = select.select_clips(
        transcript, int(settings.get("clips_per_source", 6)),
        float(settings.get("length_min_s", 20)), float(settings.get("length_max_s", 60)),
        language=language)
    if not chosen:
        raise ProcessError("No clips could be chosen: the video has no usable speech.")
    best = max(int(c.get("score") or 0) for c in chosen)
    activity.log(owner, "ai", "clips.chosen",
                 f"Chose {len(chosen)} clip{'s' if len(chosen) != 1 else ''} from {title} "
                 f"({method}); best score {best}", job_id=job_id,
                 detail=[{"start": c["start"], "end": c["end"], "score": c.get("score"),
                          "reason": c.get("reason")} for c in chosen])

    settings, music_note = _music_settings(owner, settings)
    if music_note:
        activity.log(owner, "system", "source.stage", music_note, job_id=job_id)
    made = []
    for idx, clip in enumerate(chosen, 1):
        _stop_if_deleted(job_id)
        clip["_idx"] = idx
        made.append(_make_clip(job, owner, transcript, video, job_dir, clip, idx,
                               len(chosen), settings, language=language,
                               monetize_cfg=monetize_cfg, stages=stages))
    rendered = [m for m in made if m["render"]]
    if not rendered:
        raise ProcessError(f"Couldn't render any clip: {made[0]['error']}")

    _stop_if_deleted(job_id)
    stages("planning")
    min_score = int(settings.get("min_score", 60))
    reaching = [m for m in rendered if int(m["selected"].get("score") or 0) >= min_score]
    planned = (posts.create_posts_for_job(owner, job_id) if reaching
               else {"created": [], "skipped": []})

    result = {"job_id": job_id, "source_title": title, "method": method,
              "transcript_source": transcript.get("source") or "whisper",
              "clips": [_library_entry(m) for m in rendered]}
    failed = len(made) - len(rendered)
    used = int(job.get("ai_calls") or 0) + calls.count     # with earlier tries' (N8.1)
    if not reaching:
        message = (f"No clip reached your minimum score of {min_score} (best: {best}), so "
                   "no posts were planned. Lower the minimum score in the preset, or try "
                   "another video.")
        activity.log(owner, "system", "source.no_clips", message, job_id=job_id,
                     detail={"best_score": best, "min_score": min_score})
    else:
        n = len(planned["created"])
        message = (f"Done: {len(rendered)} clip{'s' if len(rendered) != 1 else ''}, "
                   f"{n} post{'s' if n != 1 else ''} planned")
        if planned["skipped"]:
            message += f", {len(planned['skipped'])} skipped as already posted"
        if failed:
            message += f", {failed} clip{'s' if failed != 1 else ''} failed to render"
        message += "."
        activity.log(owner, "system", "source.done", f"{title}: {message}", job_id=job_id,
                     detail={"posts": n, "ai_calls": used, "best_score": best})
    db.finish(job_id, "done", result=result)
    _update_job(job_id, stage=None, pct=100, message=message, ai_calls=used,
                best_score=best)
    return {"job_id": job_id, "clips": [m["clip_id"] for m in rendered],
            "posts": planned["created"], "skipped": planned["skipped"],
            "failed_clips": failed, "best_score": best, "ai_calls": used,
            "message": message, "stages": list(stages.seen)}


def run(job_id: str) -> dict:
    """Process one queued Autopilot source end to end. Raises ProcessError
    (or the underlying error) after recording the failure on the job, with the
    AI calls made so far, and an activity entry."""
    job = _job(job_id)
    if job is None:
        raise ProcessError("Unknown video.")
    if job.get("deleted_at"):                 # deleted while it waited in the queue (1.9)
        return {"job_id": job_id, "deleted": True}
    if not job.get("autopilot"):
        raise ProcessError("This video was submitted with Autopilot off, so it has no "
                           "posts to plan.")
    owner = job.get("user_id") or "local"
    settings = job.get("profile_snapshot") or {}
    job_dir = paths.output_dir() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    _discard_earlier_attempt(job_id, job_dir)
    per_user = auth.enabled() and owner != "local"
    monetize_cfg = db.settings_get(owner, "monetize") if per_user else None
    db.update_progress(job_id, status="running", pct=0, message="Starting...")
    stages = _Stages(job_id)
    if per_user:
        llm.set_request_config(db.settings_get(owner, "llm") or {})
    try:
        with llm.count_calls() as calls:
            try:
                return _process(job, owner, settings, job_dir, monetize_cfg, calls, stages)
            except Exception as exc:
                if isinstance(exc, sources.SourceDeleted) or sources.is_deleted(job_id):
                    # 1.9: deleted while processing. No posts were planned (the
                    # planner refuses a deleted source), and a render that ran
                    # meanwhile may have made its folder again.
                    log.info("Source %s was deleted while processing; stopped.", job_id)
                    sources.remove_files(job_id, job.get("request"))
                    return {"job_id": job_id, "deleted": True}
                message = str(exc) or exc.__class__.__name__
                log.warning("Autopilot source %s failed", job_id, exc_info=True)
                # The message and calls first: once the status says failed, the
                # user can try it again (1.10), and this mustn't overwrite that.
                _update_job(job_id, stage=None, message=f"Error: {message}",
                            ai_calls=int(job.get("ai_calls") or 0) + calls.count)
                db.finish(job_id, "error", error=message)
                activity.log(owner, "system", "source.failed",
                             f"Couldn't process the video: {message}", job_id=job_id)
                raise
    finally:
        if per_user:
            llm.clear_request_config()
