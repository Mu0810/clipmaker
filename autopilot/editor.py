"""The clip editor's server side (design §10.2, §10.4; Requirements 7.1, 7.2,
7.7-7.9, 8.6, N1.4).

- `open_clip(owner, ref)`: what the editor shows (7.1). `ref` is a clip id,
  or `<job id>-<clip number>` for a clip of the manual flow that has no clip
  row yet: it's imported then from the job's result_json as version 1, with
  the files it already has and a spec rebuilt from the clip's start and end
  and the job's options. When the job's transcript is missing or only
  estimated from captions, the window around the clip is transcribed with
  word timings on the render worker, and the editor polls.
- `request_proxy(owner, clip_id, start, end)`: a small, fast-seeking copy of
  a window of the source (540 px tall, a keyframe every 15 frames), built on
  the render worker and served by /api/clip/{job}/{name} with ranges.
- `request_render(owner, clip_id, spec, confirm_replace)`: validates the
  spec, creates the version in `rendering` and queues it. When it's ready,
  the clip's unpublished posts switch to it (7.7); uploaded YouTube videos
  are replaced only with `confirm_replace` (8.6); published posts don't
  change (7.9). A failed render leaves the previous version in use (7.8).
- `render_status(owner, version_id)`: a render's progress.

The heavy work runs on one render worker thread with its own queue,
separate from the job workers, so an edit never waits behind a source being
processed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import paths
from autopilot import activity, clearance, posts, sources, store
from media import editspec, keep, render
from media import transcript as transcript_mod
from media.probe import ProbeError, probe
from platforms import limits

log = logging.getLogger(__name__)

WINDOW_PAD_S = 60.0                 # the default window: the clip ± 60 s (§10.2)
MAX_WINDOW_S = 20 * 60.0            # a proxy covers at most 20 minutes
PROXY_HEIGHT, PROXY_GOP, PROXY_CRF, PROXY_AUDIO = 540, 15, 30, "96k"
MANUAL_MIN_S = 1.0                  # the manual flow has always made clips this short
VIDEO_EXTS = frozenset({".mp4", ".mkv", ".webm", ".mov"})
# 7.7: unpublished posts that take the new version, when nothing was sent yet.
SWITCH_STATUSES = frozenset({"scheduled", "waiting_approval", "unscheduled", "blocked",
                             "retrying", "needs_reconnect"})
PUBLISHED = frozenset({"live", "private", "kept_private"})
# Steps an uploaded post can have pending and still be replaced (they redo
# themselves after the replacement).
REPLACEABLE_STEPS = (None, "yt_verify", "yt_update")
INTERRUPTED = "Interrupted by a restart; render it again."
_REF = re.compile(r"^([0-9a-f]{12})-(\d{1,3})$")
_CLIP_ID = re.compile(r"^[0-9a-f]{16}$")
_VERSION_ID = re.compile(r"^[0-9a-f]{16}$")


class EditorError(Exception):
    """A refused request: `status` for the API, `problems` [{field, message}] for a 422."""

    def __init__(self, status: int, message: str, problems: list[dict] | None = None):
        super().__init__(message)
        self.status, self.message, self.problems = status, message, problems or []


def _not_found() -> EditorError:
    return EditorError(404, "Not found.")


# ── the render worker ────────────────────────────────────────────────────

class RenderWorker:
    """One queue and one thread for proxies, window transcripts and renders.
    Tasks are keyed, so asking again while one is queued or running doesn't
    queue a second; `state(key)` reports {"state", "progress", "error"}."""

    def __init__(self):
        self.queue: queue.Queue = queue.Queue()
        self._states: dict[tuple, dict] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def submit(self, key: tuple, fn: Callable[[Callable[[float], None]], None]) -> dict:
        with self._lock:
            current = self._states.get(key)
            if current and current["state"] in ("queued", "running"):
                return dict(current)
            current = {"state": "queued", "progress": 0.0, "error": None}
            self._states[key] = current
            self.queue.put((key, fn))
            return dict(current)

    def state(self, key: tuple) -> dict | None:
        with self._lock:
            current = self._states.get(key)
            return dict(current) if current else None

    def acknowledge(self, key: tuple) -> None:
        """Forget a finished task's state, so asking again queues it anew."""
        with self._lock:
            current = self._states.get(key)
            if current and current["state"] in ("done", "failed"):
                del self._states[key]

    def _set(self, key: tuple, **fields) -> None:
        with self._lock:
            if key in self._states:
                self._states[key].update(fields)

    def run_one(self, *, block: bool = True, timeout: float | None = None) -> bool:
        """Run the next task; False when there was none."""
        try:
            key, fn = self.queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return False
        self._set(key, state="running")
        try:
            fn(lambda fraction: self._set(key, progress=round(max(0.0, min(1.0, fraction)), 3)))
        except Exception as exc:                  # recorded, never kills the worker
            log.exception("Editor task %s failed", key)
            self._set(key, state="failed", error=str(exc) or exc.__class__.__name__)
        else:
            self._set(key, state="done", progress=1.0)
        finally:
            self.queue.task_done()
        return True

    def drain(self) -> int:
        """Run every queued task in this thread (tests). Returns how many ran."""
        count = 0
        while self.run_one(block=False):
            count += 1
        return count

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self.run_one(timeout=0.5)

        self._thread = threading.Thread(target=loop, daemon=True, name="render-worker")
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None


WORKER = RenderWorker()


def start_worker() -> None:
    """Started from bootstrap() (skipped in tests, which drain or start it)."""
    WORKER.start()


# ── jobs, sources and clips ──────────────────────────────────────────────

def _job(conn, job_id: str) -> dict | None:
    return store.decode_row(conn.execute("SELECT * FROM jobs WHERE job_id=?",
                                         (job_id,)).fetchone())


def _owns(job: dict | None, owner: str) -> bool:
    return bool(job) and not job.get("deleted_at") and (job.get("user_id") or "local") == owner


def source_path(job_id: str) -> Path | None:
    """The job's source video: `source.<ext>` in its folder, for downloads,
    uploads and Autopilot sources alike."""
    for path in sorted((paths.output_dir() / job_id).glob("source.*")):
        if path.suffix.lower() in VIDEO_EXTS and path.is_file():
            return path
    return None


def _probe_source(job_id: str) -> tuple[Path, dict]:
    path = source_path(job_id)
    if path is None:
        raise EditorError(409, "The source video isn't in the job folder any more, so this "
                               "clip can't be edited.")
    try:
        info = probe(path)
    except ProbeError as exc:
        raise EditorError(409, f"The source video can't be read: {exc}") from exc
    if not info.get("duration"):
        raise EditorError(409, "The source video's length can't be read.")
    return path, info


def _versions(conn, clip_id: str) -> list[dict]:
    return store.decode_rows(conn.execute(
        "SELECT * FROM clip_versions WHERE clip_id=? ORDER BY version", (clip_id,)).fetchall())


def _load(owner: str, clip_id: str) -> tuple[dict, dict, list[dict]]:
    """The owner's clip, its job and its versions; 404 otherwise (another
    owner's clip, a deleted source)."""
    if not _CLIP_ID.fullmatch(str(clip_id or "")):
        raise _not_found()
    with store.connect() as conn:
        clip = store.get_clip(conn, owner, clip_id)
        job = _job(conn, clip["job_id"]) if clip else None
        if clip is None or not _owns(job, owner):
            raise _not_found()
        return clip, job, _versions(conn, clip_id)


# ── clips made before the editor (§10.4) ─────────────────────────────────

# Today's process options, for jobs from before they were stored (app.ProcessRequest).
MANUAL_DEFAULTS = {"burn_captions": True, "caption_style": "words", "burn_hook": True,
                   "trim_silence": True, "music": "", "music_volume": 0.25}


def imported_id(job_id: str, idx: int) -> str:
    """A fixed id per job and clip number, so importing twice can't make two."""
    return hashlib.sha256(f"clip:{job_id}:{idx}".encode()).hexdigest()[:16]


def _options(job: dict) -> dict:
    stored = job.get("request") or {}
    return {**MANUAL_DEFAULTS, **{k: stored[k] for k in MANUAL_DEFAULTS if k in stored}}


def import_spec(entry: dict, options: dict) -> dict:
    """Version 1's spec, rebuilt the way the manual flow renders: its span,
    Fit layout, and the job's caption, hook, trim and music options."""
    t_in, t_out = float(entry["start"]), float(entry["end"])
    style = options["caption_style"]
    hook = " ".join(str(entry.get("hook") or "").split())[:editspec.HOOK_MAX].strip()
    data = {"in": t_in, "out": t_out, "cuts": [], "auto_trim": bool(options["trim_silence"]),
            "layout": "fit",
            "captions": {"enabled": bool(options["burn_captions"]),
                         "style": style if style in ("words", "karaoke") else "phrases"},
            "hook": {"enabled": bool(options["burn_hook"] and hook), "text": hook,
                     "duration": round(min(2.5, max(1.0, t_out - t_in)), 2)},
            "music": {"track": options["music"] or None,
                      "volume": min(1.0, max(0.0, float(options["music_volume"] or 0.25)))},
            "cover": {"t": None}}
    try:
        return editspec.parse(data).to_dict()
    except editspec.EditSpecError:
        return editspec.parse({"in": t_in, "out": max(t_out, t_in + 0.1), "layout": "fit",
                               "hook": {"enabled": False}}).to_dict()


def _import_meta(entry: dict) -> dict:
    title = str(entry.get("title") or "")
    description = str(entry.get("description") or "")
    hashtags = " ".join(str(h) for h in entry.get("hashtags") or [])
    return {"youtube": {"title": title, "description": description,
                        "tags": [str(t) for t in entry.get("tags") or []],
                        "category_id": limits.DEFAULT_CATEGORY},
            "instagram": {"caption": "\n\n".join(x for x in (title, description, hashtags) if x)},
            "hook": str(entry.get("hook") or ""), "imported": True}


def _import(job: dict, idx: int) -> str | None:
    """Create the clip row and version 1 for clip `idx` of a manual job from
    its result_json, with the files it already has. Returns the clip id, or
    None when the job has no such clip."""
    entry = next((c for c in (job.get("result") or {}).get("clips") or []
                  if str(c.get("index")) == str(idx)), None)
    if entry is None or entry.get("start") is None or entry.get("end") is None:
        return None
    job_id, owner = job["job_id"], job.get("user_id") or "local"
    files = {"original": entry.get("original"), "vertical": entry.get("vertical"),
             "thumb": entry.get("thumbnail"), "cover": None, "crop_track": None}
    files = {k: Path(v).name if v else None for k, v in files.items()}
    vertical = paths.output_dir() / job_id / files["vertical"] if files["vertical"] else None
    info = None
    if vertical is not None and vertical.is_file():
        try:
            info = probe(vertical)                     # outside the transaction: it's a subprocess
        except ProbeError:
            info = None
    clip_id, version_id = imported_id(job_id, idx), uuid.uuid4().hex[:16]
    created = job.get("created_at") or time.time()
    score = entry.get("virality")
    title = entry.get("title") or f"Clip {idx}"
    with store.connect() as conn:
        if conn.execute("SELECT 1 FROM clips WHERE id=?", (clip_id,)).fetchone():
            return clip_id                            # imported meanwhile
        store.insert_clip(conn, {
            "id": clip_id, "owner_id": owner, "job_id": job_id, "idx": idx,
            "src_start": float(entry["start"]), "src_end": float(entry["end"]),
            "score": int(round(score)) if isinstance(score, (int, float)) else None,
            "score_reason": entry.get("virality_reason") or None,
            "method": (job.get("result") or {}).get("method"), "meta": _import_meta(entry),
            "current_version_id": version_id, "created_at": created})
        store.insert_clip_version(conn, {
            "id": version_id, "clip_id": clip_id, "version": 1,
            "spec": import_spec(entry, _options(job)), "status": "ready", "files": files,
            "probe": info, "created_at": created, "finished_at": created})
        activity.log(owner, "user", "clip.imported",
                     f"Opened clip {idx} ({title}) in the editor; its current video became "
                     "version 1.", job_id=job_id, clip_id=clip_id, conn=conn)
    return clip_id


def resolve(owner: str, ref: str) -> str:
    """The clip id `ref` names: a clip id, or `<job id>-<clip number>`, which
    imports a manual-flow clip on first open (Autopilot clips have rows)."""
    ref = str(ref or "")
    if _CLIP_ID.fullmatch(ref):
        return ref
    match = _REF.fullmatch(ref)
    if not match:
        raise _not_found()
    job_id, idx = match.group(1), int(match.group(2))
    with store.connect() as conn:
        job = _job(conn, job_id)
        if not _owns(job, owner):
            raise _not_found()
        row = conn.execute("SELECT id FROM clips WHERE job_id=? AND idx=? AND owner_id=? "
                           "ORDER BY created_at LIMIT 1", (job_id, idx, owner)).fetchone()
    if row:
        return row["id"]
    if job.get("autopilot"):
        raise _not_found()
    clip_id = _import(job, idx)
    if clip_id is None:
        raise _not_found()
    return clip_id


# ── the clip's transcript (7.1) ──────────────────────────────────────────

def window_transcript_path(job_id: str, clip_id: str) -> Path:
    return paths.output_dir() / job_id / f"editor-{clip_id}.transcript.json"


def _load_window(job_id: str, clip_id: str) -> dict | None:
    try:
        data = json.loads(window_transcript_path(job_id, clip_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = (isinstance(data, dict) and data.get("v") == transcript_mod.SCHEMA_VERSION
          and isinstance(data.get("words"), list))
    return data if ok else None


def _save_window(job_id: str, clip_id: str, data: dict) -> None:
    target = window_transcript_path(job_id, clip_id)
    fd, tmp = tempfile.mkstemp(prefix=".editor-transcript.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def default_window(clip: dict, duration: float) -> tuple[float, float]:
    """The clip ± 60 s, inside the source (§10.2), to the millisecond."""
    a = max(0.0, float(clip["src_start"]) - WINDOW_PAD_S)
    b = min(float(duration), float(clip["src_end"]) + WINDOW_PAD_S)
    return round(a, 3), round(b, 3)


def _transcribe_window(job_id: str, clip_id: str, source: Path, start: float, end: float,
                       language: str | None, report) -> None:
    """Whisper on the window only, with word timings shifted to source time."""
    job_dir = paths.output_dir() / job_id
    with tempfile.TemporaryDirectory(prefix=".editor-", dir=job_dir) as tmp:
        cut = Path(tmp) / "window.m4a"
        render.run_ffmpeg(["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
                           "-vn", "-c:a", "aac", "-b:a", "128k", str(cut)],
                          step="window audio")
        got = transcript_mod.transcribe_words(cut, language=language, progress=report)

    def shift(item: dict) -> dict:
        return {**item, "s": round(float(item["s"]) + start, 3),
                "e": round(float(item["e"]) + start, 3)}

    data = transcript_mod.build(
        [shift(w) for w in got.get("words") or []], language=got.get("language") or language,
        source="whisper", estimated=bool(got.get("estimated")),
        language_probability=got.get("language_probability"),
        segments=[shift(s) for s in got.get("segments") or []], model=got.get("model"),
        requested_language=language,
        media={"name": source.name, "bytes": source.stat().st_size})
    data["window"] = [start, end]
    _save_window(job_id, clip_id, data)


def clip_transcript(job: dict, clip: dict) -> tuple[dict | None, str]:
    """The transcript a clip is edited and rendered with, and where it came
    from: "job" (the job's transcript, with real word timings), "window"
    (made for this clip, because the job's was missing or estimated from
    captions), "pending" (that window transcript is still being made), or
    "estimated"/"none" (it couldn't be made: the job's estimated transcript,
    or none, is used, so the clip can still be rendered). Caption edits are
    keyed by word number in whichever one this returns."""
    job_dir = paths.output_dir() / job["job_id"]
    full = transcript_mod.load(job_dir)
    if full and full.get("words") and not full.get("estimated"):
        return full, "job"
    window = _load_window(job["job_id"], clip["id"])
    if window is not None:
        return window, "window"
    state = WORKER.state(("transcribe", clip["id"]))
    if state and state["state"] == "failed":
        return (full, "estimated") if full and full.get("words") else (None, "none")
    return None, "pending"


def _ensure_transcript(job: dict, clip: dict, source: Path, duration: float) -> dict | None:
    """Queue the window transcription when the clip needs one; returns the
    task's state, or None when nothing is needed."""
    _, kind = clip_transcript(job, clip)
    if kind != "pending":
        return None
    a, b = default_window(clip, duration)
    return WORKER.submit(("transcribe", clip["id"]), lambda report: _transcribe_window(
        job["job_id"], clip["id"], source, a, b, job.get("language"), report))


# ── proxies (§10.2) ──────────────────────────────────────────────────────

def proxy_name(start: float, end: float) -> str:
    return f"proxy_{int(round(start * 1000))}_{int(round(end * 1000))}.mp4"


def _build_proxy(source: Path, out: Path, start: float, end: float, report) -> None:
    part = out.with_name(f".{out.stem}.part.mp4")
    try:
        render.run_ffmpeg(
            ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
             "-map", "0:v:0", "-map", "0:a:0?", "-vf", f"scale=-2:{PROXY_HEIGHT}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", str(PROXY_CRF),
             "-g", str(PROXY_GOP), "-keyint_min", str(PROXY_GOP), "-sc_threshold", "0",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", PROXY_AUDIO, "-ac", "2",
             "-movflags", "+faststart", str(part)],
            duration=end - start, on_progress=report, step="proxy")
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)


def _proxy_view(job_id: str, start: float, end: float, state: str, **extra) -> dict:
    name = proxy_name(start, end)
    return {"state": state, "name": name, "start": start, "end": end,
            "url": f"/api/clip/{job_id}/{name}", **extra}


def request_proxy(owner: str, clip_id: str, start=None, end=None) -> dict:
    """A proxy of the source window [start, end] (default: the clip ± 60 s),
    built on the render worker. Ask again to poll: "queued", "running",
    "ready" or "failed"."""
    clip, job, _ = _load(owner, clip_id)
    source, info = _probe_source(job["job_id"])
    duration = float(info["duration"])
    if start is None and end is None:
        a, b = default_window(clip, duration)
    else:
        try:
            a = round(max(0.0, float(0.0 if start is None else start)), 3)
            b = round(min(duration, float(duration if end is None else end)), 3)
        except (TypeError, ValueError):
            raise EditorError(422, "Use seconds.", [{"field": "start",
                                                     "message": "Use seconds."}]) from None
        if not a < b:
            raise EditorError(422, "Bad window.", [{"field": "end", "message":
                              "The window must end after it starts, inside the video."}])
        if b - a > MAX_WINDOW_S:
            raise EditorError(422, "Bad window.", [{"field": "end", "message":
                              "A preview window can be at most 20 minutes long."}])
    out = paths.output_dir() / job["job_id"] / proxy_name(a, b)
    if out.is_file():
        return _proxy_view(job["job_id"], a, b, "ready", progress=1.0)
    key = ("proxy", job["job_id"], out.name)
    state = WORKER.state(key)
    if state and state["state"] in ("failed", "done"):
        WORKER.acknowledge(key)                     # failed (or built, then deleted): retry
        if state["state"] == "failed":
            return _proxy_view(job["job_id"], a, b, "failed", error=state["error"])
    state = WORKER.submit(key, lambda report: _build_proxy(source, out, a, b, report))
    return _proxy_view(job["job_id"], a, b, state["state"], progress=state["progress"])


# ── the clip as the editor shows it (7.1) ────────────────────────────────

def _started(post: dict) -> bool:
    """Something of the post is already on the platform or on its way there."""
    return posts.has_platform_state(post)


def _clip_posts(conn, owner: str, clip_id: str) -> list[dict]:
    return store.decode_rows(conn.execute(
        "SELECT * FROM posts WHERE clip_id=? AND owner_id=? AND method='official' "
        "ORDER BY created_at, id", (clip_id, owner)).fetchall())


def _groups(clip_posts: list[dict]) -> dict[str, list[dict]]:
    out = {"published": [], "uploaded": [], "switch": [], "busy": []}
    for post in clip_posts:
        status = post["status"]
        if status in PUBLISHED:
            out["published"].append(post)
        elif status == "uploaded":
            out["uploaded"].append(post)
        elif status in SWITCH_STATUSES and not _started(post):
            out["switch"].append(post)
        elif status in SWITCH_STATUSES or status == "publishing":
            out["busy"].append(post)
    return out


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _notes(groups: dict, confirm_replace: bool | None = None) -> list[dict]:
    """7.9 and 8.6: what a new version will and won't change."""
    notes = []
    if groups["published"]:
        n = len(groups["published"])
        notes.append({"code": "published", "message": f"{_plural(n, 'post is', 'posts are')} "
                      "already published and won't change."})
    if groups["uploaded"]:
        n = len(groups["uploaded"])
        what = _plural(n, "YouTube video is", "YouTube videos are")
        if confirm_replace is None:
            them, keep = ("it", "keeps") if n == 1 else ("them", "keep")
            message = f"{what} already uploaded and {keep} the current version unless you replace {them} under Posts."
        elif confirm_replace:
            message = f"{what} already uploaded and will be replaced with the new version."
        else:
            message = f"{what} already uploaded and will keep the current version."
        notes.append({"code": "uploaded", "message": message})
    if groups["busy"]:
        n = len(groups["busy"])
        notes.append({"code": "busy", "message": f"{_plural(n, 'post is', 'posts are')} being "
                      "published right now and will keep the current version."})
    return notes


def _length_notes(length: float) -> list[dict]:
    """7.2: a length outside a platform's limits."""
    notes = []
    if length > limits.YT_MAX_DURATION_S:
        notes.append({"code": "youtube_length", "message": f"The clip is {length:.1f} s; YouTube "
                      f"Shorts can be at most {limits.YT_MAX_DURATION_S} s."})
    if length < limits.IG_MIN_DURATION_S or length > limits.IG_MAX_DURATION_S:
        notes.append({"code": "instagram_length", "message": f"The clip is {length:.1f} s; "
                      "Instagram Reels must be 3 s to 15 min long."})
    return notes


def _version_view(v: dict) -> dict:
    return {k: v.get(k) for k in ("id", "version", "status", "error", "spec", "files", "probe",
                                  "cover", "created_at", "finished_at")}


def _transcript_view(transcript: dict | None, kind: str, task: dict | None,
                     a: float, b: float) -> dict:
    states = {"job": "ready", "window": "ready", "pending": "preparing",
              "estimated": "estimated", "none": "unavailable"}
    words = [{"i": i, "w": w["w"], "s": w["s"], "e": w["e"]}
             for i, w in enumerate((transcript or {}).get("words") or [])
             if w.get("e", 0) >= a and w.get("s", 0) <= b]
    view = {"state": states[kind], "source": kind,
            "language": (transcript or {}).get("language"),
            "estimated": bool((transcript or {}).get("estimated")), "words": words}
    if kind == "pending":
        view["progress"] = (task or {}).get("progress", 0.0)
    if kind in ("estimated", "none"):
        view["error"] = (task or {}).get("error")
    return view


def open_clip(owner: str, ref: str) -> dict:
    """Everything the editor shows for a clip (7.1). Re-open to poll while the
    transcript is being prepared."""
    clip_id = resolve(owner, ref)
    clip, job, versions = _load(owner, clip_id)
    source, info = _probe_source(job["job_id"])
    duration = float(info["duration"])
    _ensure_transcript(job, clip, source, duration)
    transcript, kind = clip_transcript(job, clip)
    a, b = default_window(clip, duration)
    with store.connect() as conn:
        clip_posts = _clip_posts(conn, owner, clip_id)
    groups = _groups(clip_posts)
    current = next((v for v in versions if v["id"] == clip.get("current_version_id")),
                   versions[-1] if versions else None)
    rendering = next((v for v in versions if v["status"] == "rendering"), None)
    proxy = paths.output_dir() / job["job_id"] / proxy_name(a, b)
    meta = clip.get("meta") or {}
    imported = bool(meta.get("imported"))
    return {
        "clip": {"id": clip_id, "job_id": job["job_id"], "idx": clip["idx"],
                 "title": (meta.get("youtube") or {}).get("title") or f"Clip {clip['idx']}",
                 "source_title": job.get("source_title") or None,
                 "score": clip.get("score"), "score_reason": clip.get("score_reason"),
                 "src_start": clip["src_start"], "src_end": clip["src_end"],
                 "imported": imported},
        "source": {"duration": duration, "width": info.get("width"),
                   "height": info.get("height"), "fps": info.get("fps")},
        "version": _version_view(current) if current else None,
        "versions": [{k: v.get(k) for k in ("id", "version", "status", "error", "created_at",
                                            "finished_at")} for v in reversed(versions)],
        "rendering": {"version_id": rendering["id"], "version": rendering["version"],
                      **(WORKER.state(("render", rendering["id"]))
                         or {"state": "queued", "progress": 0.0, "error": None})}
        if rendering else None,
        "window": {"start": a, "end": b},
        "proxy": _proxy_view(job["job_id"], a, b, "ready" if proxy.is_file() else "missing"),
        "transcript": _transcript_view(transcript, kind,
                                       WORKER.state(("transcribe", clip_id)), a, b),
        "posts": [{"id": p["id"], "platform": p["platform"], "status": p["status"],
                   "approval": p["approval"], "slot_at": p.get("slot_at"), "url": p.get("url"),
                   "version_id": p.get("version_id"),
                   "uses_current": p.get("version_id") == (current or {}).get("id")}
                  for p in clip_posts],
        "notes": _notes(groups),
        "switching": len(groups["switch"]),          # posts a new version would reach
        "limits": {"youtube_max_s": limits.YT_MAX_DURATION_S,
                   "instagram_min_s": limits.IG_MIN_DURATION_S,
                   "instagram_max_s": limits.IG_MAX_DURATION_S},
        "min_length": MANUAL_MIN_S if imported else editspec.MIN_LENGTH_S,
    }


# ── rendering (7.7-7.9, 8.6) ─────────────────────────────────────────────

def _min_length(clip: dict) -> float:
    return MANUAL_MIN_S if (clip.get("meta") or {}).get("imported") else editspec.MIN_LENGTH_S


def _cleared(owner: str):
    return lambda track: clearance.is_cleared(owner, track)


def base_name(clip: dict, versions: list[dict]) -> str:
    """The name every version's files share, from the first version that has
    files: `NN-slug` (the vertical is `NN-slug-vertical[.vN].mp4`)."""
    for version in versions:
        files = version.get("files") or {}
        name = Path(files.get("vertical") or files.get("original") or "").name
        if "-vertical" in name:
            return name.split("-vertical")[0]
        if name:
            return re.sub(r"(\.v\d+)?\.mp4$", "", name)
    return f"{int(clip.get('idx') or 0):02d}-clip"


def request_render(owner: str, clip_id: str, spec, confirm_replace: bool = False) -> dict:
    """Validate the spec (§4.4) and queue version N+1 in `rendering` (7.7).
    `confirm_replace` lets it replace uploaded YouTube videos (8.6)."""
    clip, job, versions = _load(owner, clip_id)
    if not isinstance(spec, dict):
        raise EditorError(422, "Send the spec.", [{"field": "spec", "message": "Send the spec."}])
    if any(v["status"] == "rendering" for v in versions):
        raise EditorError(409, "This clip is already rendering; wait for it to finish.")
    source, info = _probe_source(job["job_id"])
    transcript, kind = clip_transcript(job, clip)
    if kind == "pending":
        raise EditorError(409, "The clip's word timings are still being prepared; render once "
                               "they're ready.")
    words = (transcript or {}).get("words") or []
    try:
        checked = editspec.validate(spec, float(info["duration"]), words=words or None,
                                    is_cleared=_cleared(owner), min_length=_min_length(clip))
    except editspec.EditSpecError as exc:
        raise EditorError(422, "Some edits aren't valid.", exc.problems) from None
    length = keep.kept_duration(keep.keep_spans(words, checked.in_, checked.out, checked.cuts,
                                                checked.auto_trim))
    version_id, now = uuid.uuid4().hex[:16], time.time()
    with store.connect() as conn:
        if conn.execute("SELECT 1 FROM clip_versions WHERE clip_id=? AND status='rendering'",
                        (clip_id,)).fetchone():
            raise EditorError(409, "This clip is already rendering; wait for it to finish.")
        number = (conn.execute("SELECT max(version) FROM clip_versions WHERE clip_id=?",
                               (clip_id,)).fetchone()[0] or 0) + 1
        store.insert_clip_version(conn, {"id": version_id, "clip_id": clip_id, "version": number,
                                         "spec": checked.to_dict(), "status": "rendering",
                                         "created_at": now})
        groups = _groups(_clip_posts(conn, owner, clip_id))
        activity.log(owner, "user", "clip.edited",
                     f"Rendering version {number} of clip {clip['idx']}", job_id=job["job_id"],
                     clip_id=clip_id, detail={"version": number,
                                              "confirm_replace": bool(confirm_replace)},
                     conn=conn)
    WORKER.submit(("render", version_id), lambda report: _render(
        owner, clip_id, version_id, bool(confirm_replace), report))
    return {"version_id": version_id, "version": number, "status": "rendering",
            "length": round(length, 3),
            "notes": _length_notes(length) + _notes(groups, bool(confirm_replace))}


def _fail(owner: str, clip: dict, version: dict, message: str) -> None:
    message = " ".join(str(message).split())[:2000] or "Unknown error"
    with store.connect() as conn:
        store.update_row(conn, "clip_versions", version["id"],
                         {"status": "failed", "error": message, "finished_at": time.time()})
        activity.log(owner, "system", "clip.render_failed",
                     f"Couldn't render version {version['version']} of clip {clip['idx']}: "
                     f"{message} The previous version stays in use.", job_id=clip["job_id"],
                     clip_id=clip["id"], conn=conn)


def _render(owner: str, clip_id: str, version_id: str, confirm_replace: bool, report) -> None:
    """The render worker's task: render, then finish or fail (7.7, 7.8)."""
    with store.connect() as conn:
        clip = store.get_clip(conn, owner, clip_id)
        job = _job(conn, clip["job_id"]) if clip else None
        versions = _versions(conn, clip_id) if clip else []
    version = next((v for v in versions if v["id"] == version_id), None)
    if clip is None or version is None or version["status"] != "rendering":
        return
    try:
        if not _owns(job, owner):
            raise EditorError(410, "The video it came from was deleted.")
        source, _ = _probe_source(job["job_id"])
        transcript, _kind = clip_transcript(job, clip)
        title = ((clip.get("meta") or {}).get("youtube") or {}).get("title") or \
            f"Clip {clip['idx']}"
        out = render.render_version(
            paths.output_dir() / job["job_id"], source, transcript, version["spec"],
            version["version"], base_name(clip, versions),
            lambda step, fraction: report(fraction), title=title, is_cleared=_cleared(owner),
            min_length=_min_length(clip))
    except Exception as exc:                      # recorded on the version (7.8)
        log.warning("Rendering version %s of clip %s failed", version["version"], clip_id,
                    exc_info=not isinstance(exc, (render.RenderError, EditorError,
                                                  editspec.EditSpecError)))
        _fail(owner, clip, version, exc.message if isinstance(exc, EditorError) else str(exc))
        return
    _finish(owner, clip, version, out, confirm_replace)


def _finish(owner: str, clip: dict, version: dict, out: dict, confirm_replace: bool) -> None:
    """The new version is ready: it becomes the clip's current version and its
    unpublished posts' version (7.7); uploaded videos are replaced only when
    confirmed (8.6); published posts don't change (7.9). One transaction."""
    now, number = time.time(), version["version"]
    tz = posts.owner_timezone(owner)
    with store.connect() as conn:
        job = _job(conn, clip["job_id"])
        if store.get_clip(conn, owner, clip["id"]) is None or not _owns(job, owner):
            store.update_row(conn, "clip_versions", version["id"], {
                "status": "failed", "error": "The video it came from was deleted while this version rendered.",
                "finished_at": now})
            return
        spec = out["spec"]
        store.update_row(conn, "clip_versions", version["id"], {
            "status": "ready", "files": out["files"], "probe": out["probe"],
            "cover": out["cover"], "spec": spec, "finished_at": now})
        store.update_row(conn, "clips", clip["id"], {
            "current_version_id": version["id"], "src_start": spec["in"], "src_end": spec["out"]})
        groups = _groups(_clip_posts(conn, owner, clip["id"]))
        refs = {"job_id": clip["job_id"], "clip_id": clip["id"], "conn": conn}
        for post in groups["switch"]:
            fields = {"version_id": version["id"], "updated_at": now}
            if post["status"] == "blocked" and post.get("last_error_class") == "format":
                # The new version may fit the platform now: plan it again.
                fields.update({"status": "unscheduled", "slot_at": None, "user_locked": 0,
                               "last_error": None, "last_error_class": None})
            store.update_post(conn, post["id"], fields)
            activity.log(owner, "system", "post.version_changed",
                         f"The {posts._label(post, clip['idx'])} now uses version {number}.",
                         post_id=post["id"], **refs)
        replan = [{**p, "status": "unscheduled", "slot_at": None, "user_locked": 0}
                  for p in groups["switch"]
                  if p["status"] == "blocked" and p.get("last_error_class") == "format"]
        if replan:
            posts._plan_posts(conn, owner, replan, now=now, tz=tz)
        replaced = []
        for post in groups["uploaded"]:
            if not (confirm_replace and post["platform"] == "youtube"
                    and post.get("step") in REPLACEABLE_STEPS + ("yt_replace",)):
                continue
            store.update_post(conn, post["id"], {
                "step": "yt_replace", "next_action_at": now, "retry_count": 0,
                "step_data": {**(post.get("step_data") or {}), "replace_with": version["id"]},
                "updated_at": now})
            activity.log(owner, "user", "post.version_changed",
                         f"Replacing the YouTube upload of the {posts._label(post, clip['idx'])} "
                         f"with version {number}.", post_id=post["id"], **refs)
            replaced.append(post["id"])
        parts = [f"Rendered version {number} of clip {clip['idx']} ({out['duration']:.1f} s)."]
        if groups["switch"]:
            parts.append(f"{_plural(len(groups['switch']), 'unpublished post uses', 'unpublished posts use')} it now.")
        if replaced:
            parts.append(f"{_plural(len(replaced), 'uploaded YouTube video is', 'uploaded YouTube videos are')} being replaced.")
        kept = len(groups["uploaded"]) - len(replaced)
        if kept:
            parts.append(f"{_plural(kept, 'uploaded YouTube video keeps', 'uploaded YouTube videos keep')} the previous version.")
        if groups["busy"]:
            parts.append(f"{_plural(len(groups['busy']), 'post that is publishing now keeps', 'posts that are publishing now keep')} "
                         "the previous version.")
        if groups["published"]:
            parts.append(f"{_plural(len(groups['published']), 'published post doesn', 'published posts don')}'t change.")
        activity.log(owner, "system", "clip.rendered", " ".join(parts),
                     detail={"version": number, "switched": [p["id"] for p in groups["switch"]],
                             "replaced": replaced}, **refs)


def render_status(owner: str, version_id: str) -> dict:
    """A version's render: status, progress, error and files."""
    if not _VERSION_ID.fullmatch(str(version_id or "")):
        raise _not_found()
    with store.connect() as conn:
        row = store.decode_row(conn.execute(
            "SELECT v.*, c.owner_id AS clip_owner, c.current_version_id, c.job_id "
            "FROM clip_versions v JOIN clips c ON c.id = v.clip_id WHERE v.id=?",
            (version_id,)).fetchone())
        job = _job(conn, row["job_id"]) if row else None
    if row is None or row["clip_owner"] != owner or not _owns(job, owner):
        raise _not_found()
    task = WORKER.state(("render", version_id)) or {}
    progress = {"ready": 1.0}.get(row["status"], task.get("progress", 0.0))
    return {"id": row["id"], "clip_id": row["clip_id"], "version": row["version"],
            "status": row["status"],
            "state": task.get("state") if row["status"] == "rendering" else row["status"],
            "progress": progress, "error": row.get("error"), "files": row.get("files"),
            "current": row["current_version_id"] == row["id"],
            "created_at": row.get("created_at"), "finished_at": row.get("finished_at")}


def recover(now: float | None = None) -> int:
    """At start-up: an editor render a restart interrupted is marked failed;
    the previous version stays in use (7.8). Version 1 belongs to the
    pipeline, whose restart recovery is intake's."""
    now = time.time() if now is None else now
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT v.id, v.version, c.id AS clip_id, c.idx, c.owner_id, c.job_id "
            "FROM clip_versions v JOIN clips c ON c.id = v.clip_id "
            "WHERE v.status='rendering' AND v.version > 1").fetchall()
        for r in rows:
            store.update_row(conn, "clip_versions", r["id"],
                             {"status": "failed", "error": INTERRUPTED, "finished_at": now})
            activity.log(r["owner_id"], "system", "clip.render_failed",
                         f"Rendering version {r['version']} of clip {r['idx']} was interrupted "
                         "by a restart; the previous version stays in use.",
                         job_id=r["job_id"], clip_id=r["clip_id"], conn=conn)
    return len(rows)
