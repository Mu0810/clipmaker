"""Connecting the accounts Autopilot publishes to (design §9; Requirements 4.7,
8.1, 11.1, 11.5-11.8, N3.1, N3.3).

Routes under /api/connections. Responses carry account names, dates and
permissions, never a token.

YouTube: the manual flows keep `youtube.upload` and `youtube.readonly`;
"Connect for Autopilot" adds `youtube`, asked with include_granted_scopes.
Single-user mode runs Google's desktop flow on this machine and writes
`token.json`; multi-user mode uses the existing web flow and callback with a
single-use state. After connecting, `accounts` is filled from the channel
and posts that were waiting for a reconnect are brought back. Disconnecting
revokes the token (best effort), deletes it, and moves the account's
unpublished posts to `needs_reconnect` (11.7).

Instagram: Facebook Login for Business (platforms/meta_auth.py). The sign-in
callback and the paste-a-token fallback share steps 3-5: the 60-day token,
the permission check, then the Pages, chosen at once when there is one and by
the user when there are several. A failed attempt keeps a working connection.
Disconnecting deletes the Meta tokens and holds unpublished posts; the
password login used for manual uploads is never touched (11.6).
"""
from __future__ import annotations

import json
import logging
import os
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

import auth
import youtube_oauth
import youtube_upload
from autopilot import accounts, activity, oauth_state, posts
from autopilot.owner import current_owner
from platforms import errors, google_creds, http, meta_auth, youtube_api

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/connections")

YT_AUTOPILOT_SCOPE = "https://www.googleapis.com/auth/youtube"
YT_AUTOPILOT_SCOPES = [*youtube_upload.SCOPES, YT_AUTOPILOT_SCOPE]
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
# What each permission is for, shown on the connect screen (11.8).
YT_PERMISSIONS = [
    {"scope": YT_AUTOPILOT_SCOPE, "for": "autopilot",
     "why": "Manage your YouTube videos: needed to upload, schedule, update or remove the "
            "videos Autopilot posts, and to read their status and views."},
    {"scope": "https://www.googleapis.com/auth/youtube.upload", "for": "manual",
     "why": "Upload videos when you click Upload."},
    {"scope": "https://www.googleapis.com/auth/youtube.readonly", "for": "manual",
     "why": "See which channel is connected."},
]


# ── shared steps ─────────────────────────────────────────────────────────

def youtube_view(owner: str) -> dict:
    """GET /api/connections/youtube (11.5, 11.6, 11.8). `browser_login` says
    whether the browser login for manual uploads is set up on this Mac, and
    why Autopilot never uses it; it's off with sign-in on."""
    import youtube_pw

    scopes = google_creds.granted_scopes(owner)
    multi = auth.enabled()
    return {"connected": google_creds.load(owner) is not None,
            "autopilot": YT_AUTOPILOT_SCOPE in scopes,
            "mode": "web" if multi else "desktop",
            "client_configured": youtube_oauth.enabled() if multi
            else youtube_upload.client_configured(),
            "account": accounts.view(accounts.get(owner, "youtube")),
            "permissions": YT_PERMISSIONS,
            "browser_login": None if multi else {"connected": youtube_pw.is_connected(),
                                                 "note": UNOFFICIAL["youtube"]}}


def finish_youtube_connect(owner: str) -> dict:
    """After a token is stored: record the channel in `accounts`
    (quota_key = 'yt:<client id>') and bring back posts that were waiting for
    a reconnect. Returns the account row."""
    creds = google_creds.load(owner)
    if creds is None:
        raise errors.PlatformError(errors.Classified(
            errors.ACCESS, "not_connected", "YouTube isn't connected.", reason="reconnect"))
    with youtube_api.YouTubeAPI(owner) as api:
        channel = api.channel()
    account = accounts.upsert(owner, "youtube", external_id=channel["id"],
                              name=channel["title"], avatar=channel["avatar"],
                              scopes=creds.scopes, quota_key=f"yt:{creds.client_id}")
    autopilot = YT_AUTOPILOT_SCOPE in creds.scopes
    activity.log(owner, "user", "account.connected",
                 f"Connected YouTube channel {channel['title']}"
                 + ("" if autopilot else " for manual uploads only"),
                 detail={"autopilot": autopilot})
    if autopilot:
        posts.restore_after_reconnect(owner, "youtube")
        posts.replan_blocked(owner, "youtube")         # 11.2: ready now, if the audience is set
    return account


def _revoke(creds: google_creds.GoogleCredentials) -> bool:
    """Revoke the grant at Google, best effort: the token goes in the form
    body, and a failure never stops the disconnect."""
    token = creds.refresh_token or creds.token
    if not token:
        return False
    try:
        with http.client() as client:
            return client.post(REVOKE_URL, data={"token": token}).status_code == 200
    except Exception:
        log.warning("Couldn't revoke the YouTube token; it was deleted here anyway.",
                    exc_info=True)
        return False


def disconnect_youtube(owner: str, *, actor: str = "user") -> dict:
    """11.7: revoke (best effort) and delete the token, mark the account, and
    move its unpublished posts to needs_reconnect."""
    creds = google_creds.load(owner)
    revoked = _revoke(creds) if creds else False
    google_creds.delete(owner)
    if accounts.get(owner, "youtube"):
        accounts.mark_needs_reconnect(owner, "youtube", "YouTube was disconnected.")
    moved = posts.hold_for_reconnect(
        owner, "youtube", "YouTube was disconnected: reconnect it to publish this post.")
    activity.log(owner, actor, "account.disconnected",
                 f"Disconnected YouTube; {moved} unpublished post{'s' if moved != 1 else ''} "
                 "will wait until you reconnect", detail={"posts": moved, "revoked": revoked})
    return {"revoked": revoked, "posts_waiting": moved}


def _desktop_consent():
    """Google's desktop flow on this machine, with the Autopilot scopes."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    # Google may return earlier grants as well; oauthlib rejects that unless relaxed.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(youtube_upload._client_secret()), YT_AUTOPILOT_SCOPES)
    return flow.run_local_server(port=0, prompt="consent", include_granted_scopes="true")


def granted_from(creds) -> list[str]:
    """The scopes Google actually granted. Users can untick a permission on
    Google's consent screen, so the requested list isn't proof."""
    granted = getattr(creds, "granted_scopes", None)
    return sorted(set(granted if granted else (creds.scopes or [])))


# ── routes ───────────────────────────────────────────────────────────────

@router.get("/youtube")
def youtube_status(owner: str = Depends(current_owner)):
    return youtube_view(owner)


@router.post("/youtube/connect")
def youtube_connect(owner: str = Depends(current_owner)):
    """Single-user mode: consent in a browser on this machine (§9.1)."""
    if auth.enabled():
        raise HTTPException(400, "With sign-in on, connect YouTube from "
                                 "/api/connections/youtube/start.")
    if not youtube_upload.client_configured():
        raise HTTPException(400, "Add your Google OAuth client (client_secret.json) first.")
    try:
        creds = _desktop_consent()
    except Exception as exc:                      # consent refused, port in use, bad client
        raise HTTPException(400, f"Could not connect YouTube: {exc}") from exc
    data = json.loads(creds.to_json())
    data["scopes"] = granted_from(creds)
    google_creds.save(owner, google_creds.from_dict(data))
    try:
        finish_youtube_connect(owner)
    except errors.PlatformError as exc:
        raise HTTPException(400, f"Connected, but the channel couldn't be read: {exc}") from exc
    return youtube_view(owner)


@router.get("/youtube/start")
def youtube_start(owner: str = Depends(current_owner)):
    """Multi-user mode: the existing web flow, asking for the Autopilot scopes.
    Google returns to /api/youtube/oauth/callback."""
    if not auth.enabled():
        raise HTTPException(400, "On this Mac, connect with POST /api/connections/youtube/connect.")
    if not youtube_oauth.enabled():
        raise HTTPException(400, "YouTube sign-in isn't configured on this server.")
    state = oauth_state.issue(owner, "youtube", {"scopes": YT_AUTOPILOT_SCOPES,
                                                 "autopilot": True, "at": time.time()})
    return {"url": youtube_oauth.authorization_url(state, scopes=YT_AUTOPILOT_SCOPES)}


@router.post("/youtube/disconnect")
def youtube_disconnect(owner: str = Depends(current_owner)):
    result = disconnect_youtube(owner)
    return {**youtube_view(owner), **result}


# ── Instagram through Meta (§9.2, §9.3) ──────────────────────────────────

IG = "instagram"
IG_CALLBACK_PATH = "/api/connections/instagram/callback"
PENDING_TTL_S = 30 * 60                  # time to choose between several Pages
MAX_PASTED_TOKEN = 1000
# What each permission is for, shown on the connect screen (11.8).
IG_PERMISSIONS = [
    {"scope": "instagram_content_publish", "why": "Publish the Reels Autopilot posts."},
    {"scope": "instagram_basic",
     "why": "See your Instagram professional account's name and picture."},
    {"scope": "instagram_manage_insights",
     "why": "Read views, likes and comments for the post history."},
    {"scope": "pages_show_list",
     "why": "Find the Facebook Page your Instagram account is linked to."},
    {"scope": "pages_read_engagement",
     "why": "Read the linked Page's basic details, which Meta requires to reach the "
            "Instagram account."},
    {"scope": "business_management",
     "why": "Reach the Page when it belongs to a Meta business portfolio."},
]
IG_SETUP_STEPS = [
    "Your Instagram account must be a professional account (Business or Creator) linked "
    "to a Facebook Page you manage.",
    "In Meta for Developers, create an app of type Business and add Facebook Login for "
    "Business.",
    "Create a configuration with the token type \"User access token\" and the permissions "
    "listed here.",
    "Keep the app in development mode, with your Facebook account holding a role on it.",
]
INSIGHTS_WARNING = ("Meta didn't grant instagram_manage_insights, so the post history "
                    "won't show views, likes and comments for Instagram.")
# 11.6: the unofficial logins stay for manual uploads only, and the reason is shown.
UNOFFICIAL = {
    "youtube": "Browser login: manual uploads only. Autopilot never uses it, because "
               "browser automation breaks YouTube's terms and risks blocks or bans.",
    "instagram": "Password login: manual uploads only. Autopilot never uses it, because "
                 "password logins break Instagram's terms and risk blocks or bans.",
}


def _meta_data(owner: str) -> dict:
    return meta_auth.load_token(owner) or {}


def _public_page(page: dict) -> dict:
    return {k: page.get(k) for k in
            ("page_id", "page_name", "ig_user_id", "ig_username", "ig_name", "ig_avatar")}


def _live_pending(data: dict, now: float) -> dict | None:
    pending = data.get("pending")
    if pending and now - float(pending.get("at") or 0) <= PENDING_TTL_S:
        return pending
    return None


def instagram_view(owner: str) -> dict:
    """GET /api/connections/instagram (11.5, 11.8). Never holds a token."""
    app = meta_auth.app_settings(owner)
    data = _meta_data(owner)
    pending = _live_pending(data, time.time())
    return {
        "configured": bool(app and app.get("config_id")),
        "mode": "web" if auth.enabled() else "local",
        "app": None if app is None else {"app_id": app["app_id"],
                                         "config_id": app["config_id"] or None,
                                         "secret_set": bool(app["app_secret"])},
        "connected": bool(data.get("ig_user_id")),
        "account": accounts.view(accounts.get(owner, IG)),
        "choose": None if pending is None else {
            "pages": [_public_page(p) for p in pending["pages"]],
            "expires_at": float(pending["at"]) + PENDING_TTL_S},
        "last_result": data.get("last_result"),
        "permissions": IG_PERMISSIONS,
        "setup_steps": IG_SETUP_STEPS,
        "manual_login_note": UNOFFICIAL["instagram"],
    }


def _record_error(owner: str, message: str) -> None:
    """Remember why a connect attempt failed, for the screen the browser
    returns to. A connection that was already working is kept."""
    data = _meta_data(owner)
    data.pop("pending", None)
    data["last_result"] = {"status": "error", "message": message, "at": time.time()}
    meta_auth.save_token(owner, data)


def _missing_message(missing: list[str], *, finding_pages: bool = False) -> str:
    names = ", ".join(missing)
    why = ("so Clip Maker can't see the Facebook Page your Instagram account is linked to"
           if finding_pages else "which Autopilot needs to publish Reels")
    return (f"Meta didn't grant {names}, {why}. Connect again and allow it; if Meta "
            "doesn't offer it, add it to your Facebook Login for Business configuration.")


def _select_page(owner: str, base: dict, page: dict, *, warnings: list[str],
                 now: float) -> dict:
    """Store the chosen Page's connection, fill `accounts` and bring back
    posts waiting for a reconnect. Any pending choice is dropped."""
    previous = _meta_data(owner)
    data = {**base, "page_id": page["page_id"], "page_name": page.get("page_name"),
            "page_token": page.get("page_token"), "ig_user_id": page["ig_user_id"],
            "ig_username": page.get("ig_username")}
    if previous.get("ig_user_id") == page["ig_user_id"] and previous.get("publish_token"):
        data["publish_token"] = previous["publish_token"]     # same account: keep what works
    name = page.get("ig_username") or page.get("ig_name") or "your account"
    data["last_result"] = {"status": "connected", "message": f"Connected @{name}.",
                           "at": now, "warnings": warnings}
    meta_auth.save_token(owner, data)
    accounts.upsert(owner, IG, external_id=page["ig_user_id"],
                    name=page.get("ig_username") or page.get("ig_name"),
                    avatar=page.get("ig_avatar"), scopes=base["scopes"],
                    quota_key=f"ig:{page['ig_user_id']}", page_id=page["page_id"],
                    token_expires_at=base["user_token_expires_at"],
                    data_access_expires_at=base["data_access_expires_at"], now=now)
    activity.log(owner, "user", "account.connected",
                 f"Connected Instagram @{name} through Meta (Page {page.get('page_name')})",
                 detail={"warnings": warnings} if warnings else None)
    posts.restore_after_reconnect(owner, IG)
    posts.replan_blocked(owner, IG)                    # 11.2: posts waiting for the connection
    return {"status": "connected"}


def connect_with_token(owner: str, token: str, *, now: float | None = None) -> dict:
    """Steps 3-5 of §9.2 for a user token from the sign-in code or pasted:
    the 60-day token, the permission check, then the Pages. One Page is
    selected at once; several leave a choice for `select_page`. Returns
    {"status": "connected" | "choose"}. Raises MetaSetupError with the fix
    (11.4) or PlatformError."""
    now = time.time() if now is None else now
    app = meta_auth.app_settings(owner)
    if app is None:
        raise meta_auth.MetaSetupError("Add your Meta app's id, secret and configuration id "
                                       "first.")
    with http.client() as client:
        long = meta_auth.long_lived(client, app, token, now=now)
        info = meta_auth.debug_token(client, app, long["token"])
        if not info["is_valid"]:
            raise meta_auth.MetaSetupError("Meta says this sign-in isn't valid any more. "
                                           "Connect again, or paste a new token.")
        missing = meta_auth.missing_scopes(info["scopes"])
        if missing:
            raise meta_auth.MetaSetupError(_missing_message(missing))
        try:
            found = meta_auth.pages(client, app, long["token"])
        except meta_auth.MetaSetupError:
            if "pages_show_list" not in info["scopes"]:
                raise meta_auth.MetaSetupError(
                    _missing_message(["pages_show_list"], finding_pages=True)) from None
            raise
    base = {"user_token": long["token"],
            "user_token_expires_at": info["expires_at"] or long["expires_at"],
            "data_access_expires_at": info["data_access_expires_at"],
            "scopes": info["scopes"]}
    warnings = [] if "instagram_manage_insights" in info["scopes"] else [INSIGHTS_WARNING]
    if len(found) == 1:
        return _select_page(owner, base, found[0], warnings=warnings, now=now)
    data = _meta_data(owner)                     # a working connection stays until the choice
    data["pending"] = {**base, "pages": found, "warnings": warnings, "at": now}
    data["last_result"] = {"status": "choose", "at": now,
                           "message": f"You manage {len(found)} Pages with a linked Instagram "
                                      "account. Choose the one Autopilot posts to."}
    meta_auth.save_token(owner, data)
    return {"status": "choose"}


def select_page(owner: str, page_id: str, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    pending = _live_pending(_meta_data(owner), now)
    if pending is None:
        raise HTTPException(409, "There's no Page to choose any more. Connect Instagram again.")
    page = next((p for p in pending["pages"] if p.get("page_id") == page_id), None)
    if page is None:
        return None
    base = {k: pending.get(k) for k in ("user_token", "user_token_expires_at",
                                        "data_access_expires_at", "scopes")}
    return _select_page(owner, base, page, warnings=pending.get("warnings") or [], now=now)


def disconnect_instagram(owner: str, *, actor: str = "user") -> dict:
    """11.7: delete the Meta tokens (and any pending choice), mark the
    account, and move its unpublished posts to needs_reconnect. The password
    login for manual uploads is left alone (11.6)."""
    meta_auth.delete_token(owner)
    if accounts.get(owner, IG):
        accounts.mark_needs_reconnect(owner, IG, "Instagram was disconnected.")
    moved = posts.hold_for_reconnect(
        owner, IG, "Instagram was disconnected: reconnect it to publish this post.")
    activity.log(owner, actor, "account.disconnected",
                 f"Disconnected Instagram; {moved} unpublished post{'s' if moved != 1 else ''} "
                 "will wait until you reconnect", detail={"posts": moved})
    return {"posts_waiting": moved}


def _redirect_uri(request: Request, app: dict) -> str:
    if auth.enabled():
        if not app.get("redirect_uri"):
            raise HTTPException(400, "Set CLIPMAKER_META_REDIRECT to this server's "
                                     f"{IG_CALLBACK_PATH} address.")
        return app["redirect_uri"]
    port = request.url.port
    return f"http://localhost{f':{port}' if port else ''}{IG_CALLBACK_PATH}"


# ── Instagram routes ─────────────────────────────────────────────────────

@router.get("/instagram")
def instagram_status(owner: str = Depends(current_owner)):
    return instagram_view(owner)


@router.post("/instagram/app")
def instagram_app(body: dict = Body(...), owner: str = Depends(current_owner)):
    """Single-user mode: save the Meta app's id, secret and configuration id
    to meta_app.json (mode 600). With sign-in on they come from the server."""
    if auth.enabled():
        raise HTTPException(400, "With sign-in on, the Meta app is set on the server with "
                                 "CLIPMAKER_META_APP_ID, CLIPMAKER_META_APP_SECRET, "
                                 "CLIPMAKER_META_CONFIG_ID and CLIPMAKER_META_REDIRECT.")
    try:
        meta_auth.save_app_settings(body.get("app_id") or "", body.get("app_secret") or "",
                                    body.get("config_id") or "")
    except meta_auth.MetaSetupError as exc:
        raise HTTPException(400, str(exc)) from exc
    activity.log(owner, "user", "prefs.changed", "Saved the Meta app settings for Instagram")
    return instagram_view(owner)


@router.get("/instagram/start")
def instagram_start(request: Request, owner: str = Depends(current_owner)):
    """Step 1: the Meta sign-in URL, with a single-use state (N3.3)."""
    app = meta_auth.app_settings(owner)
    if app is None or not app.get("config_id"):
        raise HTTPException(400, "Add your Meta app's id, secret and configuration id first.")
    redirect = _redirect_uri(request, app)
    state = oauth_state.issue(owner, IG, {"redirect_uri": redirect})
    return {"url": meta_auth.dialog_url(app, state, redirect), "redirect_uri": redirect}


@router.get("/instagram/callback")
def instagram_callback(code: str = "", state: str = "", error: str = "",
                       error_reason: str = "", error_description: str = "",
                       owner: str = Depends(current_owner)):
    """Step 2: Meta sends the browser back here. A missing, reused or expired
    state is rejected before anything else (N3.3); after that, every outcome
    returns the browser to the app, which reads it from GET /instagram."""
    try:
        flow = oauth_state.consume(owner, IG, state)
    except oauth_state.StateError as exc:
        raise HTTPException(400, str(exc)) from exc
    outcome = "error"
    try:
        if error or error_reason:
            if error == "access_denied" or error_reason == "user_denied":
                raise meta_auth.MetaSetupError("You cancelled the Meta sign-in, so Instagram "
                                               "wasn't connected.")
            raise meta_auth.MetaSetupError(f"Meta sign-in failed: {error_description or error}")
        if not code:
            raise meta_auth.MetaSetupError("Meta didn't send a sign-in code. Connect again.")
        app = meta_auth.app_settings(owner)
        if app is None:
            raise meta_auth.MetaSetupError("The Meta app settings are missing; add them and "
                                           "connect again.")
        with http.client() as client:
            short = meta_auth.exchange_code(client, app, code, flow.get("redirect_uri") or "")
        outcome = connect_with_token(owner, short["access_token"])["status"]
    except meta_auth.MetaSetupError as exc:
        _record_error(owner, str(exc))
    except errors.PlatformError as exc:
        _record_error(owner, f"Meta refused the connection: {exc}")
    return RedirectResponse(f"/?instagram={outcome}", status_code=303)


@router.post("/instagram/token")
def instagram_token(body: dict = Body(...), owner: str = Depends(current_owner)):
    """The fallback when Meta refuses the localhost return address: a user
    token from the Graph API Explorer goes through steps 3-5."""
    token = str(body.get("token") or "").strip()
    if not token or len(token) > MAX_PASTED_TOKEN or any(c.isspace() for c in token):
        return JSONResponse({"detail": [{"field": "token", "message":
                             "Paste the user access token from Meta's Graph API Explorer."}]},
                            status_code=422)
    try:
        outcome = connect_with_token(owner, token)["status"]
    except meta_auth.MetaSetupError as exc:
        _record_error(owner, str(exc))
        raise HTTPException(400, str(exc)) from exc
    except errors.PlatformError as exc:
        message = f"Meta refused the token: {exc}"
        _record_error(owner, message)
        raise HTTPException(400, message) from exc
    return {**instagram_view(owner), "outcome": outcome}


@router.post("/instagram/select")
def instagram_select(body: dict = Body(...), owner: str = Depends(current_owner)):
    """Choose which Page's Instagram account Autopilot posts to."""
    if select_page(owner, str(body.get("page_id") or "")) is None:
        return JSONResponse({"detail": [{"field": "page_id", "message":
                             "Choose one of the Pages listed."}]}, status_code=422)
    return {**instagram_view(owner), "outcome": "connected"}


@router.post("/instagram/disconnect")
def instagram_disconnect(owner: str = Depends(current_owner)):
    result = disconnect_instagram(owner)
    return {**instagram_view(owner), **result}
