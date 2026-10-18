"""Instagram publishing steps for the publisher (design §8.3, §8.4; Requirements
9.1-9.5, 10.1-10.4, N2.2).

Each step makes its Graph API calls through platforms/instagram_api.py and
returns a publisher.StepResult, stored with the attempt row in one
transaction. A Reel goes out in these steps, split where something must be
stored before going on (§8.4):

  ig_limit       read the account's publishing limit (10.4); when it's
                 reached, move the post to the first free slot after the
                 limit resets and say so (9.5)
  ig_container   create the resumable REELS container with the caption, the
                 cover frame and the AI label (9.2); its id and upload URI
                 are stored before any byte is sent. A container already
                 stored is asked about first, and replaced only once
                 Instagram shows it wasn't published
  ig_upload      stream the file to rupload.facebook.com, resuming from the
                 byte count Instagram reports
  ig_processing  check the container once a minute, at most 5 times (9.3)
  ig_publish     media_publish, after asking whether the container is already
                 published; a lost reply is settled the same way (10.1)
  ig_resolve     store the media id and link (9.4), finding the Reel by its
                 caption when the media id was lost

A post's retry count carries across these steps until the Reel is published,
so a container Instagram keeps failing gives up after the three retries of
10.2 instead of starting new containers forever.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime

from autopilot import planner, posts, publisher, store
from autopilot.publish_youtube import _job_file, _sha256, _version
from autopilot.publisher import StepResult
from platforms import errors, instagram_api
from platforms.http import redact

MAX_STATUS_CHECKS = 5           # Meta: once a minute, for no more than 5 minutes (9.3)
LOOKBACK_S = 60                 # find_media looks from the container's start minus this
LIMIT_WINDOW_S = 24 * 3600      # when Meta doesn't say


def _error(kind: str, code: str, message: str) -> errors.PlatformError:
    return errors.PlatformError(errors.Classified(kind, code, message))


def _api(ctx) -> instagram_api.InstagramAPI:
    return instagram_api.InstagramAPI(ctx.owner, clock=ctx.publisher.clock)


def _label(owner: str, t: float) -> str:
    return planner.local_label(t, posts.owner_timezone(owner))


def _carry(ctx, fields: dict | None = None) -> dict:
    """Post fields for a step before the Reel is published: the retry count
    carries on instead of resetting (see the module docstring)."""
    return {**(fields or {}), "retry_count": int(ctx.post.get("retry_count") or 0)}


def _container(data: dict) -> dict | None:
    container = data.get("container")
    return container if isinstance(container, dict) and container.get("container_id") else None


def _cover_ms(version: dict) -> int | None:
    """The clip's cover frame (9.2) as Instagram's thumb_offset, in ms of the
    rendered video: the time the render chose, else the one set in the edit."""
    t = (version.get("cover") or {}).get("t")
    if t is None:
        t = ((version.get("spec") or {}).get("cover") or {}).get("t")
    return None if t is None else max(0, int(round(float(t) * 1000)))


def _parse_time(value) -> float | None:
    """A Graph timestamp, such as 2026-11-02T06:00:00+0000."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S%z").timestamp() if value else None
    except ValueError:
        return None


def _new_container_error(ctx, result: errors.Classified, data: dict,
                         cause: errors.PlatformError | None = None) -> errors.PlatformError:
    """A temporary failure whose retry needs a new container (§8.6). The
    stored container is marked unusable; ig_container asks Instagram about it
    before replacing it, so a published container is never replaced (§8.4)."""
    return errors.PlatformError(
        result, getattr(cause, "status", None), getattr(cause, "request_id", None),
        retry_step="ig_container",
        step_data={**data, "container_failed": True, "status_checks": 0})


def _to_resolve(data: dict, note: str | None = None) -> StepResult:
    """The container is published: only its link is left to find. The retry
    count starts again, since the Reel is out."""
    return StepResult(status="publishing", next_step="ig_resolve", fields={"step_data": data},
                      message=note or "The container is published.",
                      activity=[("publish.step", note, None)] if note else [])


# ── the publishing limit (10.4, 9.5) ─────────────────────────────────────

def _limit_reached(ctx, used, limit, window) -> StepResult:
    """The account's publishing limit is reached. Its reset is estimated as
    the oldest Autopilot publish within Meta's window plus the window, and the
    post moves to the first free slot after it (9.5). When Clip Maker's
    records don't explain the limit (posts from other apps count too), the
    post checks again in an hour instead."""
    owner, now, data = ctx.owner, ctx.now, ctx.step_data
    window = int(window or LIMIT_WINDOW_S)
    count = f" ({used} of {limit} posts in {window // 3600} h)" if limit else ""
    with store.connect() as conn:
        oldest = conn.execute(
            "SELECT min(published_at) FROM posts WHERE owner_id=? AND platform='instagram' "
            "AND method='official' AND published_at IS NOT NULL AND published_at > ?",
            (owner, now - window)).fetchone()[0]
    reset = oldest + window if oldest is not None else None
    detail = {"reason": "publishing_limit", "used": used, "limit": limit, "reset_at": reset}
    if reset is None or reset <= now:
        when = now + publisher.LIMIT_RECHECK_S
        message = (f"Instagram's publishing limit for this account is reached{count}, and the "
                   "posts counting toward it aren't Clip Maker's, so when it resets isn't known. "
                   f"Checking again at {_label(owner, when)}.")
        first = not data.get("limit_wait")
        return StepResult(status="retrying", next_step="ig_limit", next_action_at=when,
                          outcome="deferred", message=message,
                          fields={"step_data": {**data, "limit_wait": True},
                                  "last_error": message, "last_error_class": "deferred"},
                          activity=[("publish.deferred", message, detail)] if first else [])
    plan = posts.free_slot_after(owner, ctx.post, reset, now=now)
    dropped = (" The time you chose for it was dropped." if ctx.post.get("user_locked") else "")
    data = {k: v for k, v in data.items() if k != "limit_wait"}
    fields = {"slot_basis": plan["slot_basis"], "user_locked": 0, "step_data": data}
    if plan["status"] == "scheduled":
        message = (f"Instagram's publishing limit for this account is reached{count}. It resets "
                   f"around {_label(owner, reset)}, so the post moved to "
                   f"{_label(owner, plan['slot_at'])}.{dropped}")
        # The message stays on the post, so the review queue says why it moved
        # (9.5); the next step's result clears it.
        return StepResult(status="scheduled", outcome="deferred", message=message,
                          fields={**fields, "slot_at": plan["slot_at"], "last_error": message,
                                  "last_error_class": "deferred"},
                          activity=[("publish.deferred", message,
                                     {**detail, "slot_at": plan["slot_at"]})])
    message = (f"Instagram's publishing limit for this account is reached{count}. It resets "
               f"around {_label(owner, reset)}, and there's no free slot in the 14 days after "
               f"that.{dropped}")
    return StepResult(status="unscheduled", outcome="deferred", message=message,
                      fields={**fields, "slot_at": None, "last_error": plan["reason"]},
                      activity=[("publish.deferred", message, detail)])


def _limit_error(ctx, api: instagram_api.InstagramAPI) -> StepResult:
    """Meta refused a call because the limit is reached (code 9 / 2207042):
    the same rule as ig_limit, with the numbers read again when possible."""
    try:
        info = api.publishing_limit()
    except errors.PlatformError:
        info = {"used": None, "limit": None, "window_s": LIMIT_WINDOW_S}
    return _limit_reached(ctx, info["used"], info["limit"], info["window_s"])


def ig_limit(ctx) -> StepResult:
    """Read content_publishing_limit before starting (10.4)."""
    with _api(ctx) as api:
        info = api.publishing_limit()
    used, limit = info["used"], info["limit"]
    if limit is not None and used >= limit:
        return _limit_reached(ctx, used, limit, info["window_s"])
    data = {k: v for k, v in ctx.step_data.items() if k != "limit_wait"}
    return StepResult(status="publishing", next_step="ig_container",
                      fields=_carry(ctx, {"step_data": data}),
                      message=f"Instagram publishing limit: {used} of {limit or 'unknown'} used.")


# ── the container and the upload ─────────────────────────────────────────

def ig_container(ctx) -> StepResult:
    """Create the resumable REELS container (§8.3) and store it before any
    byte is sent. A container already stored is asked about first (§8.4):
    published, it only needs its link; still usable, it's kept; replaced only
    when Instagram shows it can't be published (ERROR, EXPIRED, gone) or a
    failure marked it unusable."""
    data, notes = ctx.step_data, []
    old = _container(data)
    if old:
        try:
            with _api(ctx) as api:
                state = api.container_status(old["container_id"]).get("status_code")
        except errors.PlatformError as exc:
            if exc.kind != errors.PERMANENT:
                raise
            state = "GONE"                           # Instagram no longer knows it
        if state == "PUBLISHED":
            return _to_resolve(data, "The Reel was already published from the container "
                                     "created earlier, so it wasn't created again.")
        if not data.get("container_failed") and state in ("FINISHED", "IN_PROGRESS"):
            return StepResult(status="publishing",
                              next_step="ig_publish" if state == "FINISHED" else "ig_upload",
                              fields=_carry(ctx),
                              message=f"Continuing with the container created earlier ({state}).")
        notes.append(("publish.step", f"Instagram can't publish the earlier upload (container "
                      f"{state}), so the video is being uploaded again.",
                      {"container_id": old["container_id"], "status": state}))
        replaced = list(data.get("replaced_containers") or []) + [old["container_id"]]
        data = {k: v for k, v in data.items()
                if k not in ("container", "container_failed", "status_checks", "uploaded_at")}
        data["replaced_containers"] = replaced
    version = _version(ctx.post)
    path = _job_file(ctx.post, (version.get("files") or {}).get("vertical"))
    if not path.is_file():
        raise _error(errors.PERMANENT, "fileMissing",
                     "The rendered video is missing; render the clip again.")
    caption = str((ctx.post.get("meta") or {}).get("caption") or "")
    size, digest = path.stat().st_size, _sha256(path)
    with _api(ctx) as api:
        try:
            container = api.create_container(caption, thumb_offset_ms=_cover_ms(version),
                                             ai_generated=bool(ctx.post.get("disclosure_ai")))
        except errors.PlatformError as exc:
            if exc.result.resume == "publishing_limit":
                return _limit_error(ctx, api)
            raise
    data.update({"container": container, "version_id": version["id"], "file": path.name,
                 "size": size, "sha256": digest, "caption": caption, "status_checks": 0})
    return StepResult(status="publishing", next_step="ig_upload",
                      fields=_carry(ctx, {"step_data": data}),
                      message="Created the Instagram container.", activity=notes)


def ig_upload(ctx) -> StepResult:
    """Stream the file to rupload.facebook.com. Every run first asks how many
    bytes Instagram has (video_status.uploading_phase.bytes_transferred), so a
    retry or a restart resumes instead of sending the file twice."""
    data = ctx.step_data
    container = _container(data)
    if not container:
        return StepResult(status="publishing", next_step="ig_container", fields=_carry(ctx))
    path = _job_file(ctx.post, data.get("file"))
    if not path.is_file():
        raise _error(errors.PERMANENT, "fileMissing",
                     "The rendered video is missing; render the clip again.")
    if _sha256(path) != data.get("sha256"):
        raise _error(errors.PERMANENT, "fileChanged",
                     "The video file changed after its upload started, so it can't be resumed. "
                     "Choose Don't post for it, then add the clip again.")
    with _api(ctx) as api:
        api.resume_upload(container, path)
    return StepResult(status="publishing", next_step="ig_processing",
                      next_action_at=ctx.now + publisher.PROCESSING_RECHECK_S,
                      fields=_carry(ctx, {"step_data": {**data, "status_checks": 0,
                                                        "uploaded_at": ctx.now}}),
                      message=f"Uploaded {path.name}.")


def ig_processing(ctx) -> StepResult:
    """Check the container a minute after the upload and then once a minute,
    at most 5 times (9.3). ERROR, EXPIRED, or not ready after the fifth check
    is a temporary failure whose retry makes a new container."""
    data = ctx.step_data
    container = _container(data)
    if not container:
        return StepResult(status="publishing", next_step="ig_container", fields=_carry(ctx))
    checks = int(data.get("status_checks") or 0) + 1
    with _api(ctx) as api:
        state = api.container_status(container["container_id"])
    code, data = state.get("status_code"), {**data, "status_checks": checks}
    if code == "PUBLISHED":
        return _to_resolve(data)
    if code == "FINISHED":
        return StepResult(status="publishing", next_step="ig_publish",
                          fields=_carry(ctx, {"step_data": data}),
                          message="Instagram finished processing the video.")
    problem = errors.classify_container(code, checks, max_checks=MAX_STATUS_CHECKS)
    if problem is not None:
        said = " ".join(str(state.get("status") or "").split())
        if said and code in ("ERROR", "EXPIRED"):
            message = redact(f"{problem.message} Instagram said: {said}")[:errors.MAX_MESSAGE]
            problem = dataclasses.replace(problem, message=message)
        raise _new_container_error(ctx, problem, data)
    return StepResult(status="publishing", next_step="ig_processing",
                      next_action_at=ctx.now + publisher.PROCESSING_RECHECK_S,
                      fields=_carry(ctx, {"step_data": data}),
                      message=f"Instagram is still processing the video (check {checks} of "
                              f"{MAX_STATUS_CHECKS}).")


# ── publishing ───────────────────────────────────────────────────────────

def ig_publish(ctx) -> StepResult:
    """media_publish (§8.3). It first asks whether the container is already
    published, so a resumed or retried publish never posts the Reel twice
    (10.1). When the reply is lost or fails, the container is read again:
    PUBLISHED goes on to the link; FINISHED retries the publish later."""
    data = ctx.step_data
    container = _container(data)
    if not container:
        return StepResult(status="publishing", next_step="ig_container", fields=_carry(ctx))
    if data.get("media_id"):
        return _to_resolve(data)
    cid = container["container_id"]
    with _api(ctx) as api:
        code = api.container_status(cid).get("status_code")
        if code == "PUBLISHED":
            return _to_resolve(data, "Instagram had already published this Reel, so it wasn't "
                                     "published again.")
        if code in ("ERROR", "EXPIRED"):
            raise _new_container_error(ctx, errors.classify_container(code, 1), data)
        if code != "FINISHED":
            return StepResult(status="publishing", next_step="ig_processing",
                              next_action_at=ctx.now + publisher.PROCESSING_RECHECK_S,
                              fields=_carry(ctx, {"step_data": data}),
                              message="Instagram is still processing the video.")
        try:
            media_id = api.publish(cid)
        except errors.PlatformError as exc:
            if exc.result.resume == "publishing_limit":
                return _limit_error(ctx, api)
            if exc.kind == errors.PROCESSING:       # 9007 / 2207027: not ready after all
                raise errors.PlatformError(exc.result, exc.status, exc.request_id,
                                           retry_step="ig_processing") from exc
            if exc.result.new_container:
                raise _new_container_error(ctx, exc.result, data, exc) from exc
            if exc.kind == errors.TEMPORARY:
                try:
                    again = api.container_status(cid).get("status_code")
                except errors.PlatformError:
                    again = None
                if again == "PUBLISHED":
                    return _to_resolve(data, "The reply to the publish request was lost, but "
                                             "Instagram had published the Reel, so it wasn't "
                                             "published again.")
            raise
    return StepResult(status="publishing", next_step="ig_resolve",
                      fields={"platform_post_id": media_id,
                              "step_data": {**data, "media_id": media_id,
                                            "published_at": ctx.now}},
                      message="Published the Reel.")


def ig_resolve(ctx) -> StepResult:
    """Store the media id and link (9.4). Without a media id (the publish
    reply was lost), the Reel is found among the account's media by its
    caption, from a minute before the container was created. The Reel is
    published either way, so when its link still can't be read on the last
    retry, or Instagram refuses the lookup, the post is live without it."""
    data = ctx.step_data
    media_id = data.get("media_id") or ctx.post.get("platform_post_id")
    last_try = errors.retry_delay(int(ctx.post.get("retry_count") or 0)) is None
    item, problem = None, None
    try:
        with _api(ctx) as api:
            if media_id:
                item = api.media(media_id)
            else:
                since = float((_container(data) or {}).get("started_at") or ctx.now)
                item = api.find_media(data.get("caption") or "", since - LOOKBACK_S)
    except errors.PlatformError as exc:
        if exc.kind == errors.ACCESS or (exc.kind != errors.PERMANENT and not last_try):
            raise
        problem = str(exc)
    if not item and problem is None and not last_try:
        raise _error(errors.TEMPORARY, "mediaNotFound", "Instagram shows the Reel as published, "
                                                        "but it isn't in the account's posts yet.")
    if not item:
        message = ("The Reel is published on Instagram, but its link couldn't be read"
                   + (f": {problem}" if problem else "."))
        return StepResult(status="live", message=message,
                          fields={"platform_post_id": media_id,
                                  "published_at": data.get("published_at") or ctx.now,
                                  "last_error": message, "last_error_class": "link"},
                          account_fields={"last_publish_at": ctx.now},
                          activity=[("publish.live", message, {"media_id": media_id})])
    media_id = str(item.get("id") or media_id)
    url = item.get("permalink")
    published = _parse_time(item.get("timestamp")) or data.get("published_at") or ctx.now
    return StepResult(status="live", message=url,
                      fields={"platform_post_id": media_id, "url": url, "published_at": published,
                              "step_data": {**data, "media_id": media_id,
                                            "media_product_type": item.get("media_product_type")}},
                      account_fields={"last_publish_at": ctx.now},
                      activity=[("publish.live", f"The Instagram Reel is posted: {url}",
                                 {"url": url, "media_id": media_id})])


def ig_abandon(ctx) -> StepResult:
    """A withdrawn post (rejected, or its source deleted) whose publish may
    have gone through. The container is read: PUBLISHED means the Reel is
    out, so it's published history after all (1.9) and only its link is
    fetched; otherwise nothing was published and nothing is left to do."""
    data, status = ctx.step_data, ctx.post["status"]
    container = _container(data)
    if not container:
        return StepResult(status=status)
    try:
        with _api(ctx) as api:
            code = api.container_status(container["container_id"]).get("status_code")
    except errors.PlatformError as exc:
        if exc.kind != errors.PERMANENT:
            raise
        code = None                                  # Instagram no longer knows it
    if code == "PUBLISHED":
        why = "you deleted the video it came from" if status == "cancelled" else "you decided not to post it"
        return StepResult(status="publishing", next_step="ig_resolve", fields={"step_data": data},
                          message="The Reel had been published.",
                          activity=[("publish.step", f"The Reel had already been published when "
                                     f"{why}, so it's kept in your history.", None)])
    return StepResult(status=status, message=f"Nothing was published on Instagram ({code}).")


STEPS = {"ig_limit": ig_limit, "ig_container": ig_container, "ig_upload": ig_upload,
         "ig_processing": ig_processing, "ig_publish": ig_publish, "ig_resolve": ig_resolve,
         "ig_abandon": ig_abandon}


def register_all() -> None:
    for name, handler in STEPS.items():
        publisher.register(name, "instagram", handler)


register_all()
