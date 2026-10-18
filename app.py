"""FastAPI server: submit a YouTube URL, watch progress, preview/download
clips, and upload selected clips to YouTube.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import besttime
import db
import highlights
import instagram_upload
import llm
import monetize
import music as music_mod
import paths
import pipeline
import urlguard
import youtube_oauth
import youtube_pw
import youtube_upload
from autopilot import hardening, migrations
from autopilot import api as autopilot_api
from autopilot import connections_api
from autopilot import editor
from autopilot import editor_api
from autopilot import intake
from autopilot import legacy
from autopilot import maintenance
from autopilot import oauth_state
from autopilot import publisher
from autopilot import publish_youtube  # noqa: F401  (registers the YouTube publishing steps)
from autopilot import publish_instagram  # noqa: F401  (registers the Instagram publishing steps)
from autopilot import sources
from autopilot import store
from autopilot.owner import current_owner, get_prefs
from platforms import http as platform_http

log = logging.getLogger(__name__)
from media.ranged import ranged_file_response

# Job ids are 12 lowercase hex characters (uuid4().hex[:12]).
_JOB_ID = re.compile(r"[0-9a-f]{12}")

HERE = Path(__file__).parent

MAX_VIDEO_MB = int(os.environ.get("CLIPMAKER_MAX_UPLOAD_MB", "1024"))
MAX_MUSIC_MB = int(os.environ.get("CLIPMAKER_MAX_MUSIC_MB", "50"))


async def _save_upload(file: UploadFile, dest: Path, max_bytes: int) -> int:
    """Stream an upload to dest with a size cap; clean up + raise on oversize
    or empty input."""
    total, too_big = 0, False
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    too_big = True
                    break
                out.write(chunk)
    finally:
        await file.close()
    if too_big:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            413, f"File exceeds the {max_bytes // (1024 * 1024)} MB limit.")
    if not dest.exists() or not dest.stat().st_size:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "The uploaded file was empty.")
    return total


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    bootstrap()
    yield


app = FastAPI(title="Clip Maker", lifespan=_lifespan)


LOGIN_PAGE = HERE / "static" / "login.html"


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """When auth is enabled, gate everything except the auth endpoints behind a
    valid session. API calls get 401; page requests get the login screen."""
    if not auth.enabled():
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api/auth/"):
        return await call_next(request)
    token = request.cookies.get(auth.COOKIE)
    user = db.session_user(token) if token else None
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Authentication required."},
                                status_code=401)
        # no-store: the login page lives at "/", same URL as the app. Without
        # it the browser heuristically caches the login page and re-serves it
        # after a successful sign-in, which looks like login silently failing.
        return FileResponse(str(LOGIN_PAGE),
                            headers={"Cache-Control": "no-store"})
    request.state.user = user
    resp = await call_next(request)
    # The HTML document at "/" depends on auth state (login page vs app), so
    # never let navigations be cached — otherwise a stale page is re-served
    # after login (or after logout). Static assets stay cacheable.
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# Registered after the auth gate on purpose: Starlette runs the last-registered
# middleware first, so cross-site requests are refused before any session work.
app.middleware("http")(hardening.cross_site_guard)


class AuthRequest(BaseModel):
    email: str
    password: str


def _set_session_cookie(resp: JSONResponse, token: str) -> None:
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax",
                    secure=auth.secure_cookies(), max_age=60 * 60 * 24 * 30,
                    path="/")


@app.post("/api/auth/signup")
def auth_signup(req: AuthRequest):
    mode = auth.signup_mode()
    if mode == "closed":
        raise HTTPException(403, "Signups are closed.")
    if mode == "first" and db.user_count() > 0:
        raise HTTPException(
            403, "Signups are closed — the owner account already exists.")
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "A valid email is required.")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    if db.get_user_by_email(email):
        raise HTTPException(409, "An account with that email already exists.")
    uid = uuid.uuid4().hex
    db.create_user(uid, email, auth.hash_password(req.password))
    token = auth.new_token()
    db.create_session(token, uid)
    resp = JSONResponse({"authenticated": True, "email": email})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/login")
def auth_login(req: AuthRequest):
    email = req.email.strip().lower()
    user = db.get_user_by_email(email)
    if not user or not auth.verify_password(req.password, user["pw_hash"]):
        raise HTTPException(401, "Incorrect email or password.")
    token = auth.new_token()
    db.create_session(token, user["id"])
    resp = JSONResponse({"authenticated": True, "email": email})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = request.cookies.get(auth.COOKIE)
    if token:
        db.delete_session(token)
    resp = JSONResponse({"authenticated": False})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request):
    if not auth.enabled():
        return {"auth_enabled": False, "authenticated": True, "email": None}
    token = request.cookies.get(auth.COOKIE)
    user = db.session_user(token) if token else None
    return {"auth_enabled": True, "authenticated": bool(user),
            "email": (user or {}).get("email")}


def current_uid(request: Request) -> str | None:
    """Owner id used for per-user scoping: the logged-in user's id when auth is
    enabled, else None (single-user/local — no filtering)."""
    if not auth.enabled():
        return None
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Authentication required.")
    return user["id"]


def _own_job_or_404(job_id: str, uid: str | None) -> None:
    if uid is not None and db.job_owner(job_id) != uid:
        raise HTTPException(404, "Not found.")


def _user_llm_ctx(request: Request):
    """Per-request Kimchi config: the logged-in user's key when auth is on,
    else a no-op (global/file config)."""
    if auth.enabled():
        return llm.user_llm(db.settings_get(current_uid(request), "llm") or {})
    return contextlib.nullcontext()

# In-memory job store: job_id -> dict(status, message, pct, result, error)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _set(job_id: str, **fields):
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(fields)
    # Mirror durable progress to the DB so state survives restarts and is
    # readable by any process (the web layer reads DB when not in memory).
    db.update_progress(job_id, status=fields.get("status"),
                       pct=fields.get("pct"), message=fields.get("message"),
                       error=fields.get("error"))


# ── bounded job queue + worker pool ───────────────────────────────────────
# Replaces unbounded per-job threads: caps concurrent ffmpeg/Whisper work so
# the box can't be overwhelmed. Set CLIPMAKER_WORKERS to tune concurrency.
_QUEUE: "queue.Queue" = queue.Queue()
_WORKERS = max(1, int(os.environ.get("CLIPMAKER_WORKERS", "2")))


def _enqueue_job(job_id: str) -> None:
    """Put a job on the worker queue, reloading its request from the stored
    request_json (used by restart recovery and Autopilot intake). A manual
    request becomes a ProcessRequest; an Autopilot request is dispatched by
    autopilot.process in task 6.5, so for now it is queued with its raw dict."""
    with store.connect() as conn:
        row = conn.execute(
            "SELECT autopilot, request_json, url FROM jobs WHERE job_id=?",
            (job_id,)).fetchone()
    if row is None:
        return
    req = json.loads(row["request_json"]) if row["request_json"] else {}
    if row["autopilot"]:
        _QUEUE.put((job_id, {"autopilot": True, **req}, None))
    else:
        source_path = req.pop("source_path", None)
        req.pop("kind", None)
        _QUEUE.put((job_id, ProcessRequest(**req) if req else
                    ProcessRequest(url=row["url"]), source_path))


def _dispatch(job_id: str, req, source_path) -> None:
    """Send a queued job to its pipeline: Autopilot sources to
    autopilot.process.run, everything else to the manual flow (1.8). A source
    deleted while it waited isn't processed (1.9)."""
    if sources.is_deleted(job_id):
        return
    if isinstance(req, dict) and req.get("autopilot"):
        _run_autopilot_job(job_id)
    else:
        _run_job(job_id, req, source_path)


def _worker_loop():
    while True:
        job_id, req, source_path = _QUEUE.get()
        try:
            _dispatch(job_id, req, source_path)
        except Exception:
            traceback.print_exc()
        finally:
            _QUEUE.task_done()


def _run_autopilot_job(job_id: str) -> None:
    """Run one Autopilot source (task 6.5). A failure is already recorded on
    the job and in the activity log, so it is only printed here."""
    from autopilot import process

    try:
        process.run(job_id)
    except Exception:
        traceback.print_exc()


def _start_workers():
    for _ in range(_WORKERS):
        threading.Thread(target=_worker_loop, daemon=True,
                         name="job-worker").start()


class ProcessRequest(BaseModel):
    url: str
    max_clips: int = 6
    target_len: float = 45.0
    burn_captions: bool = True
    caption_style: str = "words"
    burn_hook: bool = True
    write_metadata: bool = True
    rank_by_virality: bool = True
    make_thumbnails: bool = True
    trim_silence: bool = True
    music: str = ""
    music_volume: float = 0.25


def _run_job(job_id: str, req: ProcessRequest, source_path: str | None = None):
    def progress(msg: str, pct: int | None = None):
        fields = {"message": msg}
        if pct is not None:
            fields["pct"] = pct
        _set(job_id, **fields)

    # Per-user account settings (AI key + monetization) when auth is enabled.
    owner = db.job_owner(job_id)
    per_user = auth.enabled()
    mon_cfg = db.settings_get(owner, "monetize") if (per_user and owner) else None
    try:
        if per_user:
            llm.set_request_config(
                (owner and db.settings_get(owner, "llm")) or {})
        _set(job_id, status="running", pct=5, message="Starting...")
        result = pipeline.process(
            req.url, job_id, progress,
            max_clips=req.max_clips, target_len=req.target_len,
            burn_captions=req.burn_captions, caption_style=req.caption_style,
            burn_hook=req.burn_hook,
            write_metadata=req.write_metadata,
            rank_by_virality=req.rank_by_virality,
            make_thumbnails=req.make_thumbnails,
            trim_silence=req.trim_silence,
            music=req.music, music_volume=req.music_volume,
            monetize_config=mon_cfg,
            source_path=source_path)
        _set(job_id, status="done", pct=100, result=result,
             message="Done")
        db.finish(job_id, "done", result=result)
    except Exception as e:  # surface a readable error to the UI
        traceback.print_exc()
        _set(job_id, status="error", error=str(e),
             message=f"Error: {e}")
        db.finish(job_id, "error", error=str(e))
    finally:
        if per_user:
            llm.clear_request_config()
        with JOBS_LOCK:          # DB is now authoritative; free the cache entry
            JOBS.pop(job_id, None)


@app.post("/api/process")
def start_process(req: ProcessRequest, request: Request):
    if not req.url.strip():
        raise HTTPException(400, "A YouTube URL is required.")
    try:
        req.url = urlguard.validate(req.url)
    except urlguard.UrlError as e:
        raise HTTPException(400, str(e))
    uid = current_uid(request)
    job_id = uuid.uuid4().hex[:12]
    db.create(job_id, req.url, uid)
    store.set_request(job_id, req.model_dump())
    _set(job_id, status="queued", pct=0, message="Queued",
         result=None, error=None)
    _QUEUE.put((job_id, req, None))
    return {"job_id": job_id}


_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


@app.post("/api/process-file")
async def start_process_file(
    request: Request,
    file: UploadFile = File(...),
    max_clips: int = Form(6),
    target_len: float = Form(45.0),
    burn_captions: bool = Form(True),
    burn_hook: bool = Form(True),
    trim_silence: bool = Form(True),
    caption_style: str = Form("words"),
    write_metadata: bool = Form(True),
    rank_by_virality: bool = Form(True),
    make_thumbnails: bool = Form(True),
    music: str = Form(""),
    music_volume: float = Form(0.25),
):
    """Same pipeline as /api/process, but the source is an uploaded local video
    (no captions, so the transcript comes from Whisper)."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _VIDEO_EXTS:
        raise HTTPException(
            400, "Unsupported video format. Use mp4, mov, mkv, webm, m4v or avi.")

    job_id = uuid.uuid4().hex[:12]
    job_dir = paths.output_dir() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dest = job_dir / f"source{ext}"
    try:
        await _save_upload(file, dest, MAX_VIDEO_MB * 1024 * 1024)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    req = ProcessRequest(
        url=file.filename or dest.name, max_clips=max_clips,
        target_len=target_len, burn_captions=burn_captions, burn_hook=burn_hook,
        trim_silence=trim_silence, caption_style=caption_style,
        write_metadata=write_metadata, rank_by_virality=rank_by_virality,
        make_thumbnails=make_thumbnails, music=music, music_volume=music_volume)
    db.create(job_id, file.filename or dest.name, current_uid(request))
    store.set_request(job_id, {**req.model_dump(), "source_path": str(dest)})
    _set(job_id, status="queued", pct=0, message="Queued",
         result=None, error=None)
    _QUEUE.put((job_id, req, str(dest)))
    return {"job_id": job_id}


class SourcesRequest(BaseModel):
    urls: list[str] = []
    profile_id: str | None = None
    rights_confirmed: bool = False


@app.post("/api/autopilot/sources/probe")
def autopilot_sources_probe(body: SourcesRequest, request: Request):
    owner = current_owner(request)
    try:
        return {"sources": intake.probe_urls(body.urls, owner)}
    except intake.IntakeError as e:
        raise HTTPException(400, str(e))


@app.post("/api/autopilot/sources")
def autopilot_sources(body: SourcesRequest, request: Request):
    owner = current_owner(request)
    try:
        return {"jobs": intake.submit_urls(
            body.urls, owner, profile_id=body.profile_id,
            rights_confirmed=body.rights_confirmed)}
    except intake.IntakeError as e:
        raise HTTPException(400, str(e))


@app.post("/api/autopilot/sources/file")
async def autopilot_sources_file(
    request: Request,
    file: UploadFile = File(...),
    profile_id: str = Form(None),
    rights_confirmed: bool = Form(False),
):
    owner = current_owner(request)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _VIDEO_EXTS:
        raise HTTPException(
            400, "Unsupported video format. Use mp4, mov, mkv, webm, m4v or avi.")
    staging = paths.output_dir() / f"upload-{uuid.uuid4().hex[:12]}{ext}"
    try:
        await _save_upload(file, staging, MAX_VIDEO_MB * 1024 * 1024)
    except HTTPException:
        staging.unlink(missing_ok=True)
        raise
    try:
        return intake.submit_file(
            staging, file.filename or staging.name, owner,
            profile_id=profile_id, rights_confirmed=rights_confirmed)
    except intake.IntakeError as e:
        staging.unlink(missing_ok=True)
        raise HTTPException(400, str(e))


@app.get("/api/status/{job_id}")
def status(job_id: str, request: Request):
    _own_job_or_404(job_id, current_uid(request))
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job:                       # live job: in-memory has freshest progress
        return JSONResponse(job)
    job = db.get(job_id)          # finished in a previous session
    if not job:
        raise HTTPException(404, "Unknown job.")
    return JSONResponse(job)


@app.get("/api/jobs")
def list_jobs(request: Request):
    return {"jobs": db.list_jobs(current_uid(request))}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, request: Request, confirm: int | None = None):
    """The library's delete (1.9): the same path as DELETE
    /api/autopilot/sources/{id}. `confirm` (the number of unpublished posts
    the user saw) is optional here, so today's requests keep working."""
    if not _JOB_ID.fullmatch(job_id):
        raise HTTPException(404, "Not found.")
    uid = current_uid(request)
    _own_job_or_404(job_id, uid)
    response = _delete_source(job_id, uid, confirm)
    if isinstance(response, JSONResponse):
        return response
    if response is None:
        sources.remove_files(job_id)          # as today: a folder with no row goes too
    return {"ok": True}


def _delete_source(job_id: str, uid: str | None, confirm: int | None):
    try:
        done = sources.delete(job_id, uid, confirm=confirm)
    except sources.SourceChanged as e:
        return JSONResponse({"detail": "This source's posts changed since you confirmed. "
                                       + e.impact["message"], "impact": e.impact},
                            status_code=409)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)
    return done


@app.get("/api/autopilot/sources")
def autopilot_sources_list(request: Request):
    """1.3, 1.5, 1.7, N8.1: the Sources view. The time zone comes along so
    the view can show times in it (5.6)."""
    uid = current_uid(request)
    return {"sources": sources.list_sources(uid),
            "timezone": get_prefs(current_owner(request)).get("timezone")}


@app.get("/api/autopilot/sources/{job_id}/delete-impact")
def autopilot_source_delete_impact(job_id: str, request: Request):
    """1.9: what deleting the source would do, for the confirmation."""
    found = sources.impact(job_id, current_uid(request)) if _JOB_ID.fullmatch(job_id) else None
    if found is None:
        raise HTTPException(404, "Not found.")
    return found


@app.delete("/api/autopilot/sources/{job_id}")
def autopilot_source_delete(job_id: str, request: Request, confirm: int | None = None):
    """1.9: delete a source once the user confirmed the preview. `confirm` is
    the number of unpublished posts the preview showed; if it changed since,
    nothing is deleted and the reply (409) carries the new preview."""
    if not _JOB_ID.fullmatch(job_id):
        raise HTTPException(404, "Not found.")
    if confirm is None:
        raise HTTPException(400, "Add ?confirm=<unpublished> from the delete preview.")
    done = _delete_source(job_id, current_uid(request), confirm)
    if done is None:
        raise HTTPException(404, "Not found.")
    return done


@app.post("/api/autopilot/sources/{job_id}/retry")
def autopilot_source_retry(job_id: str, request: Request):
    """1.10: try a failed source again, with the settings it was submitted
    with. 409 says why it can't be: it isn't failed, it has posts, or its
    uploaded file is gone."""
    if not _JOB_ID.fullmatch(job_id):
        raise HTTPException(404, "Not found.")
    try:
        done = sources.retry(job_id, current_uid(request))
    except sources.RetryRefused as e:
        raise HTTPException(409, str(e))
    if done is None:
        raise HTTPException(404, "Not found.")
    return done


@app.api_route("/api/clip/{job_id}/{name}", methods=["GET", "HEAD"])
def get_clip(job_id: str, name: str, request: Request, download: bool = False):
    if not _JOB_ID.fullmatch(job_id):
        raise HTTPException(404, "Clip not found.")
    _own_job_or_404(job_id, current_uid(request))
    # Prevent path traversal; only serve files within the job dir.
    safe = Path(name).name
    base = (paths.output_dir() / job_id).resolve()
    path = (base / safe).resolve()
    if path.parent != base or not path.is_file():
        raise HTTPException(404, "Clip not found.")
    media = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    }.get(path.suffix.lower(), "video/mp4")
    return ranged_file_response(request, path, media,
                                filename=safe if download else None)


class UploadRequest(BaseModel):
    job_id: str
    file: str            # clip file name within the job dir
    title: str
    description: str = ""
    tags: list[str] = []
    privacy: str = "unlisted"


@app.get("/api/upload-ready")
def upload_ready():
    return {"ready": youtube_upload.credentials_present()}


# ── YouTube account management ────────────────────────────────────────────

class ClientSecretRequest(BaseModel):
    content: str            # raw client_secret.json text


def _yt_status() -> dict:
    """Unified YouTube status across the OAuth API and the browser method."""
    oauth = youtube_upload.account_status()
    pw = youtube_pw.account_status()
    if pw["connected"]:
        method, channel = "browser", pw["channel"]
    elif oauth["connected"]:
        method, channel = "oauth", oauth["channel"]
    else:
        method, channel = None, None
    return {
        "connected": pw["connected"] or oauth["connected"],
        "method": method,
        "channel": channel,
        "client_configured": oauth["client_configured"],
        "oauth_connected": oauth["connected"],
        "pw_available": pw["available"],
        "pw_connected": pw["connected"],
        "pw_manual_only": True,          # 11.6: Autopilot uses the YouTube Data API only
        "pw_note": connections_api.UNOFFICIAL["youtube"],
    }


def _yt_status_user(uid: str | None) -> dict:
    """Per-user YouTube status (web OAuth) for multi-user deployments."""
    stored = db.settings_get(uid, "youtube") if uid else None
    connected = bool(stored and stored.get("token"))
    return {
        "connected": connected,
        "method": "web" if connected else None,
        "channel": (stored or {}).get("channel"),
        "client_configured": youtube_oauth.enabled(),
        "oauth_connected": connected,
        "pw_available": False,
        "pw_connected": False,
        "web_oauth": youtube_oauth.enabled(),
    }


@app.get("/api/youtube/status")
def youtube_status(request: Request):
    if auth.enabled():
        return _yt_status_user(current_uid(request))
    return _yt_status()


@app.get("/api/youtube/oauth/start")
def youtube_oauth_start(request: Request):
    if not auth.enabled():
        raise HTTPException(400, "Per-user OAuth is only for multi-user mode.")
    if not youtube_oauth.enabled():
        raise HTTPException(400, "YouTube OAuth isn't configured on this server.")
    uid = current_uid(request)
    state = oauth_state.issue(uid, "youtube", {"scopes": youtube_oauth.SCOPES,
                                               "autopilot": False})
    return {"url": youtube_oauth.authorization_url(state)}


@app.get("/api/youtube/oauth/callback")
def youtube_oauth_callback(request: Request, code: str = "", state: str = ""):
    if not auth.enabled() or not youtube_oauth.enabled():
        raise HTTPException(400, "YouTube OAuth isn't configured.")
    uid = current_uid(request)
    try:
        # Single-use and 10 minutes old at most (N3.3); taken before the
        # exchange, so a replay fails even when the exchange errors.
        flow = oauth_state.consume(uid, "youtube", state)
    except oauth_state.StateError as e:
        raise HTTPException(400, str(e))
    scopes = flow.get("scopes") or youtube_oauth.SCOPES
    try:
        token = youtube_oauth.exchange_code(code, state, scopes=scopes)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(400, f"OAuth exchange failed: {e}")
    if flow.get("autopilot"):
        db.settings_set(uid, "youtube", {"token": token, "channel": {}})
        outcome = "connected"
        try:
            account = connections_api.finish_youtube_connect(uid)
            channel = {"id": account.get("external_id"), "title": account.get("name"),
                       "thumbnail": account.get("avatar")}
            db.settings_set(uid, "youtube", {**(db.settings_get(uid, "youtube") or {}),
                                             "channel": channel})
        except Exception:
            traceback.print_exc()
            outcome = "error"
        # The page opens the Setup screen for this, and drops the query (task 12.5).
        return RedirectResponse(f"/?youtube={outcome}", status_code=303)
    channel = {}
    try:
        channel = youtube_oauth.channel_info(token)
    except Exception:
        traceback.print_exc()
    db.settings_set(uid, "youtube", {"token": token, "channel": channel})
    return RedirectResponse("/", status_code=303)


@app.post("/api/youtube/pw/connect")
def youtube_pw_connect():
    """Opens a real browser on this machine to log into Google once."""
    if auth.enabled():
        raise HTTPException(
            403, "Browser YouTube login is disabled in multi-user mode — "
                 "connect your channel with Google sign-in instead.")
    try:
        youtube_pw.connect()
    except youtube_pw.YTBrowserError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Connect failed: {e}")
    return _yt_status()


@app.post("/api/youtube/pw/disconnect")
def youtube_pw_disconnect():
    if auth.enabled():
        raise HTTPException(
            403, "Browser YouTube is disabled in multi-user mode.")
    youtube_pw.disconnect()
    return _yt_status()


@app.post("/api/youtube/client-secret")
def youtube_client_secret(req: ClientSecretRequest):
    try:
        youtube_upload.save_client_secret(req.content)
    except youtube_upload.UploadError as e:
        raise HTTPException(400, str(e))
    return _yt_status()


@app.post("/api/youtube/connect")
def youtube_connect():
    """Opens a browser on the server (this machine) for OAuth consent."""
    try:
        youtube_upload.connect()
    except youtube_upload.UploadError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Connect failed: {e}")
    return _yt_status()


@app.post("/api/youtube/disconnect")
def youtube_disconnect(request: Request):
    # The same token serves the manual uploads and Autopilot, so disconnecting
    # here has the 11.7 effects too: revoke, delete, and hold unpublished posts.
    owner = current_owner(request)
    connections_api.disconnect_youtube(owner)
    if auth.enabled():
        return _yt_status_user(current_uid(request))
    return _yt_status()


@app.post("/api/upload")
def upload(req: UploadRequest, request: Request):
    uid = current_uid(request)
    _own_job_or_404(req.job_id, uid)
    safe = Path(req.file).name
    path = (paths.output_dir() / req.job_id / safe).resolve()
    if not path.exists():
        raise HTTPException(404, "Clip not found.")
    try:
        if auth.enabled():
            # Multi-user: upload to the logged-in user's own channel.
            stored = db.settings_get(uid, "youtube")
            if not (stored and stored.get("token")):
                raise HTTPException(400, "Connect your YouTube channel first.")
            return youtube_oauth.upload(
                stored["token"], path, title=req.title,
                description=req.description, tags=req.tags, privacy=req.privacy)
        # Single-user/local: browser session if connected, else official API.
        if youtube_pw.is_connected():
            return youtube_pw.upload_video(
                path, title=req.title, description=req.description,
                tags=req.tags, privacy=req.privacy)
        return youtube_upload.upload_video(
            path, title=req.title, description=req.description,
            tags=req.tags, privacy=req.privacy)
    except HTTPException:
        raise
    except (youtube_upload.UploadError, youtube_pw.YTBrowserError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Upload failed: {e}")


# ── Instagram account + Reels upload ──────────────────────────────────────

class IGConnectRequest(BaseModel):
    username: str
    password: str
    verification_code: str = ""      # 2FA code, when prompted


class IGUploadRequest(BaseModel):
    job_id: str
    file: str
    caption: str = ""


@app.get("/api/instagram/status")
def instagram_status():
    # 11.6: this password login is for manual uploads only; Autopilot uses Meta's API.
    label = {"manual_only": True, "note": connections_api.UNOFFICIAL["instagram"]}
    if auth.enabled():
        # Instagram uses one shared session (ig_session.json) with no per-user
        # scoping, so it's disabled in multi-user mode to stop one user posting
        # to (or disconnecting) another's connected IG account.
        return {"connected": False, "account": None, "disabled": True, **label}
    return {**instagram_upload.account_status(), **label}


def _ig_guard():
    """Block Instagram actions when auth is on (shared, unscoped account)."""
    if auth.enabled():
        raise HTTPException(
            403, "Instagram is disabled in multi-user mode.")


@app.post("/api/instagram/connect")
def instagram_connect(req: IGConnectRequest):
    _ig_guard()
    try:
        instagram_upload.connect(req.username, req.password,
                                 req.verification_code)
    except instagram_upload.TwoFactorRequired as e:
        # 428 signals "code needed" so the UI can reveal the 2FA field.
        raise HTTPException(428, str(e))
    except instagram_upload.InstagramError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Connect failed: {e}")
    return instagram_upload.account_status()


@app.post("/api/instagram/disconnect")
def instagram_disconnect():
    _ig_guard()
    instagram_upload.disconnect()
    return instagram_upload.account_status()


@app.post("/api/instagram/upload")
def instagram_upload_reel(req: IGUploadRequest, request: Request):
    _ig_guard()
    _own_job_or_404(req.job_id, current_uid(request))
    safe = Path(req.file).name
    path = (paths.output_dir() / req.job_id / safe).resolve()
    if not path.exists():
        raise HTTPException(404, "Clip not found.")
    try:
        return instagram_upload.upload_reel(path, caption=req.caption)
    except instagram_upload.InstagramError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Upload failed: {e}")


# ── Viral title / hashtag suggestions ─────────────────────────────────────

class SuggestRequest(BaseModel):
    text: str = ""
    title: str = ""
    duration: float = 30.0


@app.post("/api/suggest")
def suggest(req: SuggestRequest, request: Request):
    with _user_llm_ctx(request):
        return highlights.suggest(req.text, base_title=req.title,
                                  dur=int(req.duration))


class ScoreRequest(BaseModel):
    titles: list[str] = []
    text: str = ""


@app.post("/api/score")
def score(req: ScoreRequest, request: Request):
    with _user_llm_ctx(request):
        return {"scores": highlights.score_titles(req.titles, req.text)}


# ── AI engine (Kimchi) settings ───────────────────────────────────────────

class LLMConfigRequest(BaseModel):
    api_key: str = ""
    model: str = ""


@app.get("/api/llm/status")
def llm_status(request: Request):
    if auth.enabled():
        return llm.status_of(db.settings_get(current_uid(request), "llm"))
    return llm.status()


@app.post("/api/llm/config")
def llm_config(req: LLMConfigRequest, request: Request):
    if auth.enabled():
        uid = current_uid(request)
        if req.api_key.strip():
            check = llm.verify_and_pick(req.api_key.strip(), req.model.strip())
            if not check["ok"]:
                raise HTTPException(400, check["detail"])
            db.settings_set(uid, "llm", {"api_key": req.api_key.strip(),
                                         "model": check["model"]})
        else:  # model-only change against an already-saved key
            cur = db.settings_get(uid, "llm") or {}
            if not cur.get("api_key"):
                raise HTTPException(400, "Enter your Kimchi API key.")
            if req.model.strip():
                cur["model"] = req.model.strip()
            db.settings_set(uid, "llm", cur)
        return llm.status_of(db.settings_get(uid, "llm"))
    # auth off (single-user/local): the shared config file
    if req.api_key.strip():
        check = llm.verify_and_pick(req.api_key.strip(), req.model.strip())
        if not check["ok"]:
            raise HTTPException(400, check["detail"])
        return llm.save_config(api_key=req.api_key, model_id=check["model"])
    return llm.save_config(api_key=req.api_key, model_id=req.model)


@app.post("/api/llm/disconnect")
def llm_disconnect(request: Request):
    if auth.enabled():
        db.settings_delete(current_uid(request), "llm")
        return llm.status_of(None)
    return llm.clear_config()


# ── Monetization kit ──────────────────────────────────────────────────────

class MonetizeRequest(BaseModel):
    enabled: bool = False
    cta: str = ""
    links: str = ""
    default_hashtags: str = ""
    pinned_comment: str = ""


@app.get("/api/monetize/status")
def monetize_status(request: Request):
    if auth.enabled():
        return monetize.status_of(db.settings_get(current_uid(request),
                                                  "monetize"))
    return monetize.status()


@app.post("/api/monetize/config")
def monetize_config(req: MonetizeRequest, request: Request):
    if auth.enabled():
        cfg = monetize.normalize(req.model_dump())
        db.settings_set(current_uid(request), "monetize", cfg)
        return monetize.status_of(cfg)
    return monetize.save(req.model_dump())


# ── Background music tracks ───────────────────────────────────────────────

@app.get("/api/music")
def music_list(request: Request):
    from autopilot import clearance
    owner = current_uid(request) or "local"
    return {"tracks": music_mod.list_tracks(),
            "cleared": clearance.cleared_tracks(owner)}


@app.post("/api/music")
async def music_upload(file: UploadFile = File(...)):
    if not music_mod.is_audio(file.filename or ""):
        raise HTTPException(400, "Unsupported audio format. Use mp3, m4a, aac, "
                                 "wav, ogg or flac.")
    music_mod.ensure_dir()
    dest = paths.music_dir() / music_mod.safe_name(file.filename or "track.mp3")
    await _save_upload(file, dest, MAX_MUSIC_MB * 1024 * 1024)
    return {"tracks": music_mod.list_tracks(), "added": dest.name}


# ── Scheduled uploads ─────────────────────────────────────────────────────

class ScheduleRequest(BaseModel):
    job_id: str
    file: str
    platform: str            # "youtube" | "instagram"
    title: str
    description: str = ""
    tags: list[str] = []
    privacy: str = "unlisted"
    scheduled_at: float      # epoch seconds


@app.get("/api/schedule")
def schedule_list(request: Request):
    return {"now": time.time(),
            "schedules": legacy.list_schedules(current_uid(request))}


@app.post("/api/schedule")
def schedule_create(req: ScheduleRequest, request: Request):
    """A scheduled upload: today's request and reply, stored as a post with
    method 'legacy' that the publisher uploads at its time (task 9.5)."""
    if req.platform not in ("youtube", "instagram"):
        raise HTTPException(400, "platform must be 'youtube' or 'instagram'.")
    if not req.title.strip():
        raise HTTPException(400, "A title is required.")
    uid = current_uid(request)
    _own_job_or_404(req.job_id, uid)
    path = legacy.clip_path(req.job_id, req.file)
    if path is None or not path.resolve().is_relative_to(
            (paths.output_dir() / req.job_id).resolve()) or not path.exists():
        raise HTTPException(404, "Clip not found.")
    return legacy.create(uid, job_id=req.job_id, file=path.name, platform=req.platform,
                         title=req.title, description=req.description, tags=req.tags,
                         privacy=req.privacy, scheduled_at=float(req.scheduled_at))


@app.delete("/api/schedule/{sid}")
def schedule_delete(sid: str, request: Request):
    if not legacy.delete(current_uid(request), sid):
        raise HTTPException(404, "Not found.")
    return {"ok": True}


@app.get("/api/besttime")
def best_time(platform: str = "youtube"):
    return {
        "platform": platform,
        "slots": besttime.suggestions(platform, time.time()),
        "note": "General best-practice windows in your local time — "
                "not personalized analytics.",
    }


_start_workers_lock = threading.Lock()
_background_started = False


def bootstrap() -> None:
    """Start-up work, run once by the lifespan hook before the first request.

    Later steps are added here in the order of design §13. Background threads
    (job workers, the publisher) are skipped when CLIPMAKER_NO_BACKGROUND=1, which
    the test suite sets.
    """
    global _background_started
    platform_http.install_redaction()   # no token reaches a log line from here on (N3.1)
    hardening.set_umask()
    paths.output_dir().mkdir(parents=True, exist_ok=True)
    db.init()
    migrations.run()
    hardening.restrict_data_files()
    intake.set_enqueuer(_enqueue_job)
    intake.requeue_interrupted()   # re-queue or fail jobs left mid-flight by a restart (1.5)
    editor.recover()               # an editor render a restart cut off is marked failed (7.8)

    if os.environ.get("CLIPMAKER_NO_BACKGROUND", "").strip() == "1":
        return
    with _start_workers_lock:
        if _background_started:
            return
        _background_started = True
    _start_workers()
    editor.start_worker()          # the editor's own render worker, beside the job workers
    publisher.start_background()   # Autopilot and scheduled uploads, ticking every 20 s (§8.1)
    maintenance.start_background()  # sign-in checks, metrics, re-planning, purge, hourly (§8.9)


app.include_router(autopilot_api.router)
app.include_router(connections_api.router)
app.include_router(editor_api.router)

app.mount("/", StaticFiles(directory=str(HERE / "static"), html=True),
          name="static")
