"""/api/editor routes (design §10.2, §10.4, §14; Requirements 7.1, 7.2, 7.7-7.9,
8.6). Every route takes its owner from the session through `current_owner`
and accepts clips and versions only as ids (N3.2)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from autopilot import editor
from autopilot.owner import current_owner

router = APIRouter(prefix="/api/editor")


def _error(exc: editor.EditorError):
    if exc.status == 422:
        return JSONResponse({"detail": exc.problems or [{"field": "body",
                                                         "message": exc.message}]},
                            status_code=422)
    raise HTTPException(exc.status, exc.message)


@router.get("/clips/{ref}")
def clip_get(ref: str, owner: str = Depends(current_owner)):
    """The clip for the editor. `ref` is a clip id, or `<job id>-<clip number>`
    for a library clip that hasn't been opened before (imported on the spot).
    While `transcript.state` is "preparing", open it again to poll."""
    try:
        return editor.open_clip(owner, ref)
    except editor.EditorError as e:
        return _error(e)


@router.post("/clips/{clip_id}/proxy")
def clip_proxy(clip_id: str, body: dict | None = Body(None),
               owner: str = Depends(current_owner)):
    """{start, end} in source seconds (default: the clip ± 60 s). Post again
    to poll until `state` is "ready"; the proxy is served at `url`."""
    body = body or {}
    try:
        return editor.request_proxy(owner, clip_id, body.get("start"), body.get("end"))
    except editor.EditorError as e:
        return _error(e)


@router.post("/clips/{clip_id}/render", status_code=202)
def clip_render(clip_id: str, body: dict = Body(...), owner: str = Depends(current_owner)):
    """{spec, confirm_replace}: render a new version in the background (7.7).
    `confirm_replace: true` replaces already-uploaded YouTube videos (8.6)."""
    confirm = body.get("confirm_replace", False)
    if not isinstance(confirm, bool):
        return JSONResponse({"detail": [{"field": "confirm_replace",
                                         "message": "Use true or false."}]}, status_code=422)
    try:
        return editor.request_render(owner, clip_id, body.get("spec"), confirm)
    except editor.EditorError as e:
        return _error(e)


@router.get("/renders/{version_id}")
def render_get(version_id: str, owner: str = Depends(current_owner)):
    try:
        return editor.render_status(owner, version_id)
    except editor.EditorError as e:
        return _error(e)
