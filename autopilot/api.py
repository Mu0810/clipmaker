"""/api/autopilot routes (design §14). Every route takes its owner from the
session through `current_owner`; no route accepts a user id from the request."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import ValidationError

import music as music_mod
from autopilot import activity, clearance, languages, pause, posts, profiles, readiness, reporting
from autopilot.owner import PrefsError, current_owner, get_prefs, set_prefs

router = APIRouter(prefix="/api/autopilot")

PREF_FIELDS = ("timezone", "last_profile_id", "autopilot_default")


def _invalid(exc: ValidationError) -> JSONResponse:
    return JSONResponse({"detail": profiles.validation_details(exc)}, status_code=422)


def _profile_error(exc: profiles.ProfileError) -> JSONResponse:
    """A refused profile change. One that names a field is a 422 with
    [{field, message}], shown beside that field; the rest keep their status."""
    if exc.field:
        return JSONResponse({"detail": [{"field": exc.field, "message": exc.message}]},
                            status_code=422)
    raise HTTPException(exc.status, exc.message)


# ── preferences ──────────────────────────────────────────────────────────

@router.get("/prefs")
def prefs_get(owner: str = Depends(current_owner)):
    return get_prefs(owner)


@router.patch("/prefs")
def prefs_patch(body: dict = Body(...), owner: str = Depends(current_owner)):
    if "paused" in body:
        raise HTTPException(400, "Use POST /api/autopilot/pause to pause or resume.")
    unknown = set(body) - set(PREF_FIELDS)
    if unknown:
        raise HTTPException(400, f"Unknown preference: {', '.join(sorted(unknown))}")
    if body.get("last_profile_id") and not profiles.get(owner, body["last_profile_id"]):
        raise HTTPException(404, "Preset not found.")
    try:
        prefs = set_prefs(owner, **body)
    except PrefsError as e:
        raise HTTPException(400, str(e))
    if body:
        activity.log(owner, "user", "prefs.changed",
                     "Changed " + ", ".join(sorted(body)), detail=body)
    return prefs


@router.post("/pause")
def pause_set(body: dict = Body(...), owner: str = Depends(current_owner)):
    """6.8: the one "Pause all publishing" switch. {"paused": true} holds
    everything (6.9); {"paused": false} resumes and re-plans passed slots (6.10)."""
    if set(body) != {"paused"} or not isinstance(body.get("paused"), bool):
        raise HTTPException(400, 'Send {"paused": true} to pause or {"paused": false} to resume.')
    return pause.set_paused(owner, body["paused"])


# ── readiness ────────────────────────────────────────────────────────────

@router.get("/readiness")
def readiness_get(owner: str = Depends(current_owner)):
    return readiness.check(owner)


# ── profiles ─────────────────────────────────────────────────────────────

@router.get("/profiles")
def profiles_list(owner: str = Depends(current_owner)):
    return {"profiles": profiles.list_profiles(owner),
            "defaults": profiles.ProfileSettings().model_dump(),
            "languages": languages.options()}


@router.post("/profiles", status_code=201)
def profiles_create(body: dict = Body(...), owner: str = Depends(current_owner)):
    settings = body.get("settings") or {}
    if not isinstance(settings, dict):
        raise HTTPException(400, "settings must be an object.")
    try:
        profile = profiles.create(owner, body.get("name"), settings)
    except ValidationError as e:
        return _invalid(e)
    except profiles.ProfileError as e:
        return _profile_error(e)
    activity.log(owner, "user", "profile.created", f"Created preset {profile['name']}")
    return profile


@router.get("/profiles/{pid}")
def profiles_get(pid: str, owner: str = Depends(current_owner)):
    profile = profiles.get(owner, pid)
    if profile is None:
        raise HTTPException(404, "Preset not found.")
    return profile


@router.patch("/profiles/{pid}")
def profiles_patch(pid: str, body: dict = Body(...), owner: str = Depends(current_owner)):
    unknown = set(body) - {"name", "settings", "is_default"}
    if unknown:
        raise HTTPException(400, f"Unknown field: {', '.join(sorted(unknown))}")
    settings = body.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise HTTPException(400, "settings must be an object.")
    if body.get("is_default") is False:
        raise HTTPException(400, "Make another preset the default instead.")
    try:
        profile = profiles.update(owner, pid, name=body.get("name"),
                                  settings_patch=settings,
                                  make_default=body.get("is_default") is True)
    except ValidationError as e:
        return _invalid(e)
    except profiles.ProfileError as e:
        return _profile_error(e)
    kind = "profile.default_changed" if body.get("is_default") else "profile.updated"
    activity.log(owner, "user", kind, f"Updated preset {profile['name']}",
                 detail={"fields": sorted(body)})
    return profile


@router.delete("/profiles/{pid}")
def profiles_delete(pid: str, owner: str = Depends(current_owner)):
    existing = profiles.get(owner, pid)
    try:
        profiles.delete(owner, pid)
    except profiles.ProfileError as e:
        return _profile_error(e)
    activity.log(owner, "user", "profile.deleted", f"Deleted preset {existing['name']}")
    return {"ok": True}


@router.post("/profiles/{pid}/duplicate", status_code=201)
def profiles_duplicate(pid: str, body: dict | None = Body(None),
                       owner: str = Depends(current_owner)):
    name = (body or {}).get("name")
    try:
        profile = profiles.duplicate(owner, pid, name)
    except ValidationError as e:
        return _invalid(e)
    except profiles.ProfileError as e:
        return _profile_error(e)
    activity.log(owner, "user", "profile.duplicated", f"Created preset {profile['name']}")
    return profile


# ── music clearance ──────────────────────────────────────────────────────

@router.post("/music/{track}/clearance")
def music_clearance(track: str, body: dict = Body(...),
                    owner: str = Depends(current_owner)):
    cleared = body.get("cleared")
    if not isinstance(cleared, bool):
        raise HTTPException(400, "cleared must be true or false.")
    path = music_mod.resolve(track)
    if path is None or path.name != track:
        raise HTTPException(404, "Track not found.")
    clearance.set_cleared(owner, track, cleared)
    kind = "music.cleared" if cleared else "music.uncleared"
    verb = "Cleared" if cleared else "Removed clearance from"
    activity.log(owner, "user", kind, f"{verb} {track} for publishing")
    return {"track": track, "cleared": cleared,
            "cleared_tracks": clearance.cleared_tracks(owner)}


# ── counts, activity and history (task 10.2, design §11) ─────────────────

def _report_error(exc: reporting.ReportError) -> JSONResponse:
    return JSONResponse({"detail": [{"field": exc.field, "message": exc.message}]},
                        status_code=422)


@router.get("/counts")
def counts_get(owner: str = Depends(current_owner)):
    """12.2: the header badge's numbers, polled every 30 s."""
    return reporting.counts(owner)


@router.get("/activity")
def activity_get(kind: str | None = None, source: str | None = None,
                 clip: str | None = None, post: str | None = None,
                 before_id: int | None = None,
                 limit: int = Query(reporting.PAGE, ge=1, le=reporting.MAX_PAGE),
                 owner: str = Depends(current_owner)):
    """12.1: the activity log, newest first; page with next_before_id."""
    try:
        return reporting.activity_page(owner, kind=kind, source=source, clip=clip, post=post,
                                       before_id=before_id, limit=limit)
    except reporting.ReportError as e:
        return _report_error(e)


@router.get("/history")
def history_get(platform: str | None = None, status: str | None = None,
                before: float | None = None, before_id: str | None = None,
                limit: int = Query(reporting.PAGE, ge=1, le=reporting.MAX_PAGE),
                owner: str = Depends(current_owner)):
    """12.4, 12.5: posts that went out or failed, with links and latest view
    counts; page with `next` (before, before_id)."""
    try:
        return reporting.history(owner, platform=platform, status=status, before=before,
                                 before_id=before_id, limit=limit)
    except reporting.ReportError as e:
        return _report_error(e)


# ── posts and the review queue (task 6.4, design §14) ────────────────────

def _post_error(exc: posts.PostError):
    if exc.status == 422:
        return JSONResponse({"detail": exc.problems or [{"field": "body",
                             "message": exc.message}]}, status_code=422)
    raise HTTPException(exc.status, exc.message)


@router.get("/posts")
def posts_list(status: str | None = None, platform: str | None = None,
               source: str | None = None, start: float | None = Query(None, alias="from"),
               end: float | None = Query(None, alias="to"),
               owner: str = Depends(current_owner)):
    try:
        return posts.list_posts(owner, status=status, platform=platform, source=source,
                                start=start, end=end)
    except posts.PostError as e:
        return _post_error(e)


@router.post("/posts", status_code=201)
def posts_add(body: dict = Body(...), owner: str = Depends(current_owner)):
    try:
        return posts.add(owner, body.get("clip_id"), body.get("platform"), body.get("slot_at"))
    except posts.PostError as e:
        return _post_error(e)


@router.post("/posts/bulk")
def posts_bulk(body: dict = Body(...), owner: str = Depends(current_owner)):
    try:
        return {"results": posts.bulk(owner, body.get("ids"), body.get("action"))}
    except posts.PostError as e:
        return _post_error(e)


@router.get("/posts/{post_id}")
def posts_get(post_id: str, owner: str = Depends(current_owner)):
    try:
        return posts.get(owner, post_id)
    except posts.PostError as e:
        return _post_error(e)


@router.patch("/posts/{post_id}")
def posts_patch(post_id: str, body: dict = Body(...), owner: str = Depends(current_owner)):
    try:
        return posts.patch(owner, post_id, body)
    except posts.PostError as e:
        return _post_error(e)


@router.post("/posts/{post_id}/approve")
def posts_approve(post_id: str, owner: str = Depends(current_owner)):
    try:
        return posts.approve(owner, post_id)
    except posts.PostError as e:
        return _post_error(e)


@router.post("/posts/{post_id}/reject")
def posts_reject(post_id: str, owner: str = Depends(current_owner)):
    try:
        return posts.reject(owner, post_id)
    except posts.PostError as e:
        return _post_error(e)


@router.post("/posts/{post_id}/retry")
def posts_retry(post_id: str, owner: str = Depends(current_owner)):
    """10.10: try a failed post again."""
    try:
        return posts.retry(owner, post_id)
    except posts.PostError as e:
        return _post_error(e)


@router.post("/posts/{post_id}/reset")
def posts_reset(post_id: str, body: dict = Body(...), owner: str = Depends(current_owner)):
    try:
        return posts.reset(owner, post_id, str(body.get("field") or ""))
    except posts.PostError as e:
        return _post_error(e)


@router.post("/posts/{post_id}/replace")
def posts_replace(post_id: str, owner: str = Depends(current_owner)):
    """8.6: confirm replacing an uploaded YouTube video with the clip's newer version."""
    try:
        return posts.replace(owner, post_id)
    except posts.PostError as e:
        return _post_error(e)


@router.post("/profiles/{pid}/replan")
def profiles_replan(pid: str, owner: str = Depends(current_owner)):
    try:
        return posts.replan_profile(owner, pid)
    except posts.PostError as e:
        return _post_error(e)


# ── YouTube declarations (4.7, 11.1) ─────────────────────────────────────

@router.post("/youtube/declarations")
def youtube_declarations(body: dict = Body(...), owner: str = Depends(current_owner)):
    """The channel audience (made for kids or not; Autopilot never guesses it,
    4.7) and the two advisory declarations: API audit passed, OAuth app in
    production. `made_for_kids: null` means not confirmed."""
    from autopilot import accounts

    if accounts.get(owner, "youtube") is None:
        raise HTTPException(409, "Connect YouTube first.")
    problems = [{"field": k, "message": "This field can't be set here."}
                for k in body if k not in accounts.DECLARATIONS]
    values = {}
    for key in accounts.DECLARATIONS:
        if key not in body:
            continue
        value = body[key]
        if not (isinstance(value, bool) or (value is None and key == "made_for_kids")):
            problems.append({"field": key, "message": "Use true or false."})
        else:
            values[key] = value
    if not values and not problems:
        problems.append({"field": "body", "message": "Send at least one declaration."})
    if problems:
        return JSONResponse({"detail": problems}, status_code=422)
    accounts.set_declarations(owner, "youtube", **values)
    if "made_for_kids" in values:
        posts.replan_blocked(owner, "youtube")         # 11.2: the audience was the missing item
    said = []
    if "made_for_kids" in values:
        said.append({True: "made for kids", False: "not made for kids",
                     None: "audience not confirmed"}[values["made_for_kids"]])
    for key, yes, no in (("audit_declared", "API audit passed", "API audit not passed"),
                         ("oauth_production_declared", "OAuth app in production",
                          "OAuth app not in production")):
        if key in values:
            said.append(yes if values[key] else no)
    activity.log(owner, "user", "account.declared", "YouTube: " + ", ".join(said),
                 detail=values)
    return accounts.view(accounts.get(owner, "youtube"))
