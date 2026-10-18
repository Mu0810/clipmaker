"""YouTube publishing steps for the publisher (design §8.2, §8.4; Requirements
8.1-8.9, 10.1-10.4, N2.2).

Each step makes its YouTube calls through platforms/youtube_api.py and
returns a publisher.StepResult; the publisher stores it with the attempt row
in one transaction. The upload is split at the points where something must
be stored before going on (§8.4):

  yt_init      create the resumable session; its URI, the file's size and
               SHA-256 are stored before any video byte is sent
  yt_upload    send (or resume) the file; a session that's gone goes to
  yt_lookup    find a finished upload by title, time and duration, so a lost
               response never becomes a second video; none found: yt_init
  yt_finalize  store the video id and link, set the thumbnail (8.9)
  yt_verify    15 min after the publish time, and again at 2 h: live, or
               kept private by YouTube (8.7)
  yt_update    apply edits made after the upload (8.4)
  yt_unschedule / yt_reschedule   remove or restore the publish time (pause)
  yt_delete    delete a rejected upload that isn't public (8.5)
  yt_replace   delete the upload and start again with the clip's newer
               version, after the user confirms (8.6)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import paths
from autopilot import accounts, planner, posts, publisher, readiness, store
from autopilot.publisher import StepResult
from platforms import errors, youtube_api

SHORTS_URL = "https://www.youtube.com/shorts/{id}"
SECOND_CHECK_S = 2 * 3600
KEPT_PRIVATE = ("YouTube kept this video {privacy}. Videos uploaded through a Google Cloud "
                "project that hasn't passed YouTube's API audit stay private. Request the "
                f"audit ({readiness.YT_AUDIT_FORM}), then set this video to Public in YouTube "
                "Studio.")


def _error(kind: str, code: str, message: str) -> errors.PlatformError:
    return errors.PlatformError(errors.Classified(kind, code, message))


def _api(ctx) -> youtube_api.YouTubeAPI:
    return youtube_api.YouTubeAPI(ctx.owner, clock=ctx.publisher.clock)


def _version(post: dict, version_id: str | None = None) -> dict:
    vid = version_id or post.get("version_id")
    with store.connect() as conn:
        version = store.decode_row(conn.execute("SELECT * FROM clip_versions WHERE id=?",
                                                (vid,)).fetchone()) if vid else None
    if version is None or version.get("status") != "ready":
        raise _error(errors.PERMANENT, "noVersion", "This post has no rendered video to upload.")
    return version


def _job_file(post: dict, name: str | None) -> Path:
    if not name:
        raise _error(errors.PERMANENT, "fileMissing", "This post has no rendered video to upload.")
    return paths.output_dir() / str(post.get("job_id") or "") / Path(name).name


def _language(post: dict) -> str | None:
    """The language the title and description are written in (defaultLanguage)."""
    with store.connect() as conn:
        row = conn.execute("SELECT language, profile_snapshot_json FROM jobs WHERE job_id=?",
                           (post.get("job_id"),)).fetchone()
    if row is None:
        return None
    try:
        snapshot = json.loads(row["profile_snapshot_json"] or "{}")
    except ValueError:
        snapshot = {}
    return snapshot.get("metadata_language") or row["language"] or None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _label(owner: str, t: float | None) -> str:
    return planner.local_label(t, posts.owner_timezone(owner)) if t else "its slot"


def _video_id(ctx) -> str | None:
    return ctx.post.get("platform_post_id") or ctx.step_data.get("video_id")


def _privacy(api: youtube_api.YouTubeAPI, video_id: str) -> str | None:
    items = api.videos([video_id], "status")
    return ((items[0].get("status") or {}).get("privacyStatus")) if items else None


# ── the upload ───────────────────────────────────────────────────────────

def yt_init(ctx) -> StepResult:
    """Create the resumable session (§8.2). A post that already holds a
    session goes on with it, so a session is only ever replaced once YouTube
    has confirmed the old one produced no video (yt_lookup, yt_replace)."""
    data = ctx.step_data
    if data.get("session"):
        return StepResult(status="publishing", next_step="yt_upload",
                          message="Resuming the upload session already started.")
    account = accounts.get(ctx.owner, "youtube") or {}
    if account.get("made_for_kids") is None:          # 4.7: never guessed
        message = ("Confirm your channel's audience (made for kids or not) in Autopilot's setup "
                   "before this video can be uploaded.")
        return StepResult(status="blocked", outcome="blocked", message=message,
                          fields={"last_error": message, "last_error_class": "setup"})
    version = _version(ctx.post)
    path = _job_file(ctx.post, (version.get("files") or {}).get("vertical"))
    if not path.is_file():
        raise _error(errors.PERMANENT, "fileMissing",
                     "The rendered video is missing; render the clip again.")
    target = ctx.post.get("target_visibility") or "public"
    snippet = youtube_api.build_snippet(ctx.post.get("meta") or {}, _language(ctx.post))
    status = youtube_api.build_status(
        target_visibility=target, slot_at=ctx.post.get("slot_at"), now=ctx.now,
        made_for_kids=bool(account["made_for_kids"]),
        synthetic=bool(ctx.post.get("disclosure_ai")))
    with _api(ctx) as api:
        session = api.init_upload(path, snippet, status)
    publish_at = youtube_api.parse_time(status.get("publishAt"))
    data = {k: v for k, v in data.items() if k not in ("video_id", "verify_checks", "deleted")}
    data.update({"session": session, "version_id": version["id"], "file": path.name,
                 "title": snippet["title"],
                 "duration": (version.get("probe") or {}).get("duration"),
                 "publish_at": publish_at, "target": target})
    return StepResult(status="publishing", next_step="yt_upload",
                      fields={"step_data": data, "scheduled_publish_at": publish_at},
                      message="Created the YouTube upload session.")


def yt_upload(ctx) -> StepResult:
    """Send the file in 8 MiB chunks. Every run first asks YouTube how much it
    already has (an empty PUT), so a restart resumes instead of re-sending."""
    data = ctx.step_data
    session = data.get("session")
    if not session:
        return StepResult(status="publishing", next_step="yt_init")
    path = _job_file(ctx.post, data.get("file"))
    if not path.is_file():
        raise _error(errors.PERMANENT, "fileMissing",
                     "The rendered video is missing; render the clip again.")
    if _sha256(path) != session.get("sha256"):
        raise _error(errors.PERMANENT, "fileChanged",
                     "The video file changed after its upload started, so it can't be resumed. "
                     "Choose Don't post for it, then add the clip again.")
    try:
        with _api(ctx) as api:
            done = api.upload(session, path, resume=True)
    except youtube_api.SessionGone:
        return StepResult(status="publishing", next_step="yt_lookup", outcome="session_gone",
                          message="The upload session expired; checking YouTube for the video.")
    video_id = done.get("video_id")
    if not video_id:
        raise _error(errors.TEMPORARY, "noVideoId",
                     "YouTube finished the upload but didn't return the video id.")
    return StepResult(status="publishing", next_step="yt_finalize",
                      fields={"step_data": {**data, "video_id": video_id},
                              "platform_post_id": video_id,
                              "url": SHORTS_URL.format(id=video_id)},
                      message=f"Uploaded {path.name}.")


def yt_lookup(ctx) -> StepResult:
    """The session is gone: did the upload finish before it went? Only when
    YouTube shows no matching video is a new session started (§8.4)."""
    data = ctx.step_data
    session = data.get("session") or {}
    with _api(ctx) as api:
        video_id = api.find_upload(data.get("title") or "",
                                   float(session.get("started_at") or ctx.now),
                                   data.get("duration"))
    if video_id:
        return StepResult(
            status="publishing", next_step="yt_finalize",
            fields={"step_data": {**data, "video_id": video_id, "found_by_lookup": True},
                    "platform_post_id": video_id, "url": SHORTS_URL.format(id=video_id)},
            message="Found the finished upload on YouTube.",
            activity=[("publish.step", "The upload session had expired, but the video was "
                       "already on YouTube, so it wasn't uploaded again.",
                       {"video_id": video_id})])
    rest = {k: v for k, v in data.items() if k != "session"}
    return StepResult(
        status="publishing", next_step="yt_init",
        fields={"step_data": {**rest, "previous_session_gone": True}},
        message="No matching upload on YouTube; starting a new upload.",
        activity=[("publish.step", "The upload session expired before the video finished "
                   "uploading, so it's being uploaded again.", None)])


# ── after the upload ─────────────────────────────────────────────────────

def yt_finalize(ctx) -> StepResult:
    """Store the video id and link (8.8) and set the 16:9 thumbnail, unless
    YouTube refuses custom thumbnails for this channel (8.9). A thumbnail
    problem never holds the post back: the video is up either way."""
    video_id = _video_id(ctx)
    if not video_id:
        return StepResult(status="publishing", next_step="yt_lookup")
    url = SHORTS_URL.format(id=video_id)
    account = accounts.get(ctx.owner, "youtube") or {}
    notes, account_fields = [], {"last_publish_at": ctx.now}
    thumb_name = ((_version(ctx.post).get("files") or {}).get("thumb"))
    thumb = _job_file(ctx.post, thumb_name) if thumb_name else None
    if account.get("thumbnail_capability") != "unavailable" and thumb and thumb.is_file():
        try:
            with _api(ctx) as api:
                accepted = api.set_thumbnail(video_id, thumb)
        except errors.PlatformError as exc:
            if exc.kind == errors.ACCESS:
                raise
            notes.append(("publish.step", f"Couldn't set the thumbnail ({exc}); YouTube "
                          "chooses the frame for now.", None))
        else:
            account_fields["thumbnail_capability"] = "ok" if accepted else "unavailable"
            if not accepted:
                notes.append(("publish.step", "YouTube doesn't accept custom thumbnails for "
                              "this channel, so YouTube chooses the frame.", None))
    fields = {"platform_post_id": video_id, "url": url}
    if (ctx.post.get("target_visibility") or "public") == "public":
        when = ctx.post.get("scheduled_publish_at") or ctx.step_data.get("publish_at")
        return StepResult(status="uploaded", fields={**fields, "scheduled_publish_at": when},
                          account_fields=account_fields, message=url,
                          activity=notes + [("publish.uploaded", "Uploaded to YouTube as "
                                             f"private; it goes public at "
                                             f"{_label(ctx.owner, when)}.", {"url": url})])
    return StepResult(status="private", fields={**fields, "published_at": ctx.now},
                      account_fields=account_fields, message=url,
                      activity=notes + [("publish.private", "Uploaded to YouTube as a private "
                                         "video.", {"url": url})])


def yt_verify(ctx) -> StepResult:
    """Read the video's visibility 15 minutes after its publish time and again
    at 2 hours (8.7): public is `live`; still not public at the second check
    is `kept_private`, with the audit explanation and link."""
    video_id = _video_id(ctx)
    if not video_id:
        raise _error(errors.PERMANENT, "noVideo", "This post has no YouTube video to check.")
    with _api(ctx) as api:
        items = api.videos([video_id], "status")
    if not items:
        raise _error(errors.PERMANENT, "videoGone", "The video is no longer on YouTube.")
    status = items[0].get("status") or {}
    privacy = status.get("privacyStatus") or "private"
    url = ctx.post.get("url") or SHORTS_URL.format(id=video_id)
    data = ctx.step_data
    if privacy == "public":
        return StepResult(status="live", fields={"published_at": ctx.post.get(
            "scheduled_publish_at") or ctx.now, "step_data": data},
            activity=[("publish.live", f"The YouTube video is posted: {url}", {"url": url})])
    later = youtube_api.parse_time(status.get("publishAt"))
    if later and later > ctx.now:                 # someone moved the publish time on YouTube
        return StepResult(status="uploaded", next_step="yt_verify",
                          next_action_at=later + publisher.VERIFY_AFTER_S,
                          fields={"scheduled_publish_at": later},
                          message="YouTube shows a later publish time; checking again then.")
    checks = int(data.get("verify_checks") or 0) + 1
    if checks < 2:
        base = ctx.post.get("scheduled_publish_at") or ctx.post.get("slot_at") or ctx.now
        return StepResult(status="uploaded", next_step="yt_verify",
                          next_action_at=base + SECOND_CHECK_S,
                          fields={"step_data": {**data, "verify_checks": checks}},
                          message=f"Still {privacy} on YouTube; checking again at 2 h.")
    message = KEPT_PRIVATE.format(privacy=privacy)
    return StepResult(status="kept_private", outcome="kept_private", message=message,
                      fields={"step_data": {**data, "verify_checks": checks},
                              "last_error": message, "last_error_class": "kept_private"},
                      activity=[("publish.kept_private", message,
                                 {"url": url, "privacy": privacy,
                                  "audit_form": readiness.YT_AUDIT_FORM})])


def yt_update(ctx) -> StepResult:
    """Send edits made after the upload (8.4). YouTube deletes properties an
    update leaves out, so youtube_api.update_video sends the full objects.
    A Private target removes the publish time. An edit YouTube refuses is
    recorded on the post, which stays uploaded."""
    video_id = _video_id(ctx)
    account = accounts.get(ctx.owner, "youtube") or {}
    snippet = youtube_api.build_snippet(ctx.post.get("meta") or {}, _language(ctx.post))
    changes = {"containsSyntheticMedia": bool(ctx.post.get("disclosure_ai"))}
    if account.get("made_for_kids") is not None:
        changes["selfDeclaredMadeForKids"] = bool(account["made_for_kids"])
    private = ctx.post.get("target_visibility") == "private"
    if private:
        changes.update({"privacyStatus": "private", "publishAt": None})
    try:
        with _api(ctx) as api:
            api.update_video(video_id, snippet=snippet, status=changes)
    except errors.PlatformError as exc:
        if exc.kind != errors.PERMANENT:
            raise
        message = f"YouTube refused the edit: {exc}"
        return StepResult(status=ctx.post["status"], outcome="failed", message=message,
                          fields={"remote_dirty": 0, "last_error": message,
                                  "last_error_class": "permanent"},
                          activity=[("publish.failed", message, None)])
    fields = {"remote_dirty": 0}
    if private:
        fields.update({"scheduled_publish_at": None, "published_at": ctx.now})
    return StepResult(status="private" if private else "uploaded", fields=fields,
                      activity=[("publish.step", "Updated the YouTube video with your edits.",
                                 None)])


def yt_unschedule(ctx) -> StepResult:
    """Pause (6.9): remove YouTube's publish time; the video stays private and
    the post is held (`paused_hold`) until the resume gives the time back. A
    video already public isn't held: the post is live. A replacement the user
    asked for (`replace_with`) stays pending and runs after the resume."""
    video_id = _video_id(ctx)
    pending = "yt_replace" if ctx.step_data.get("replace_with") else None
    with _api(ctx) as api:
        privacy = _privacy(api, video_id)
        if privacy is None:
            raise _error(errors.PERMANENT, "videoGone", "The video is no longer on YouTube.")
        if privacy == "public":
            url = ctx.post.get("url") or SHORTS_URL.format(id=video_id)
            return StepResult(status="live", fields={
                "published_at": ctx.post.get("scheduled_publish_at") or ctx.now},
                activity=[("publish.live", "The YouTube video had already gone public, so the "
                           f"pause couldn't hold it: {url}", {"url": url})])
        api.update_video(video_id, status={"publishAt": None})
    return StepResult(status="uploaded", next_step=pending,
                      fields={"paused_hold": 1, "scheduled_publish_at": None},
                      activity=[("publish.step", "Publishing is paused, so YouTube's scheduled "
                                 "publish time was removed. The video stays private.", None)])


def yt_reschedule(ctx) -> StepResult:
    """Resume (6.10): the publish time comes back at the later of the slot and
    15 minutes from now. A video made public meanwhile (in YouTube Studio) is
    live. A post switched to Private during the pause gets no publish time
    back; yt_update then makes it private."""
    video_id = _video_id(ctx)
    if ctx.post.get("target_visibility") == "private":
        return StepResult(status="uploaded", fields={"paused_hold": 0, "remote_dirty": 1},
                          message="Set to Private during the pause; no publish time to restore.")
    slot = ctx.post.get("slot_at") or ctx.post.get("scheduled_publish_at") or ctx.now
    when = max(slot, ctx.now + youtube_api.PUBLISH_LEAD_S)
    with _api(ctx) as api:
        privacy = _privacy(api, video_id)
        if privacy is None:
            raise _error(errors.PERMANENT, "videoGone", "The video is no longer on YouTube.")
        if privacy == "public":
            url = ctx.post.get("url") or SHORTS_URL.format(id=video_id)
            return StepResult(status="live", fields={"paused_hold": 0, "published_at": ctx.now},
                              activity=[("publish.live", "The YouTube video was made public "
                                         f"while publishing was paused: {url}", {"url": url})])
        api.update_video(video_id, status={"privacyStatus": "private",
                                           "publishAt": youtube_api.rfc3339(when)})
    return StepResult(status="uploaded", fields={"paused_hold": 0, "scheduled_publish_at": when},
                      activity=[("publish.step", "Publishing resumed; the video goes public at "
                                 f"{_label(ctx.owner, when)}.", None)])


def yt_delete(ctx) -> StepResult:
    """Delete a rejected (or cancelled) upload (8.5). A video that already went
    public is left on YouTube: it's published, and its history is kept."""
    video_id = _video_id(ctx)
    data = ctx.step_data
    if not video_id:
        return StepResult(status=ctx.post["status"], fields={"step_data": data})
    with _api(ctx) as api:
        privacy = _privacy(api, video_id)
        if privacy == "public":
            if ctx.post["status"] == "cancelled":       # 1.9: it's out, so it's history
                url = ctx.post.get("url") or SHORTS_URL.format(id=video_id)
                return StepResult(status="live", fields={
                    "published_at": ctx.post.get("published_at") or ctx.now,
                    "platform_post_id": video_id, "url": url, "scheduled_publish_at": None,
                    "step_data": {**data, "deleted": False}},
                    activity=[("publish.live", "The YouTube video had already gone public when "
                               f"you deleted the video it came from, so it's kept in your history: {url}",
                               {"url": url})])
            summary = ("The video had already gone public on YouTube, so it wasn't deleted. "
                       "Remove it in YouTube Studio if you want it gone.")
            deleted = False
        else:
            deleted = api.delete_video(video_id) if privacy is not None else False
            summary = ("Deleted the private YouTube upload." if deleted
                       else "The YouTube upload was already gone.")
    return StepResult(status=ctx.post["status"],
                      fields={"step_data": {**data, "deleted": deleted},
                              "scheduled_publish_at": None},
                      activity=[("publish.deleted", summary, {"video_id": video_id})])


def yt_abandon(ctx) -> StepResult:
    """A withdrawn post (rejected, or its source deleted) whose upload had
    started but whose video id isn't known (1.9, 8.5). YouTube is asked
    whether the upload finished, so a video nobody tracks can't go public by
    itself: a finished one goes to yt_delete. An unfinished session is
    dropped, since YouTube makes no video from it."""
    data = ctx.step_data
    session, status = data.get("session"), ctx.post["status"]
    if not session:
        return StepResult(status=status)
    with _api(ctx) as api:
        try:
            state = api.upload_status(session)
            video_id = state.get("video_id") if state.get("done") else None
        except youtube_api.SessionGone:
            video_id = api.find_upload(data.get("title") or "",
                                       float(session.get("started_at") or ctx.now),
                                       data.get("duration"))
    rest = {k: v for k, v in data.items() if k != "session"}
    if video_id:
        return StepResult(status=status, next_step="yt_delete",
                          fields={"platform_post_id": video_id,
                                  "step_data": {**rest, "video_id": video_id}},
                          message="The upload had finished; deleting the video.")
    return StepResult(status=status, fields={"step_data": {**rest, "session_dropped": True}},
                      message="The upload hadn't finished, so YouTube has no video from it.")


def yt_replace(ctx) -> StepResult:
    """After the user confirms (8.6): delete the upload, then upload the
    clip's newer version from yt_init. A video that already went public is
    kept, and the post is marked live instead."""
    data = ctx.step_data
    new_version = data.get("replace_with")
    _version(ctx.post, new_version)                   # ready, before anything is deleted
    old = _video_id(ctx)
    if old:
        with _api(ctx) as api:
            privacy = _privacy(api, old)
            if privacy == "public":
                return StepResult(status="live", fields={"published_at": ctx.now,
                                                         "step_data": {k: v for k, v in
                                                                       data.items()
                                                                       if k != "replace_with"}},
                                  activity=[("publish.live", "The video had already gone public, "
                                             "so it wasn't replaced.", {"video_id": old})])
            if privacy is not None:
                api.delete_video(old)
    return StepResult(
        status="publishing", next_step="yt_init",
        fields={"version_id": new_version, "platform_post_id": None, "url": None,
                "scheduled_publish_at": None, "remote_dirty": 0, "paused_hold": 0,
                "step_data": {"replaced_video": old}},
        activity=[("publish.deleted", "Deleted the old upload; uploading the new version.",
                   {"video_id": old})])


STEPS = {"yt_init": yt_init, "yt_upload": yt_upload, "yt_lookup": yt_lookup,
         "yt_finalize": yt_finalize, "yt_verify": yt_verify, "yt_update": yt_update,
         "yt_unschedule": yt_unschedule, "yt_reschedule": yt_reschedule,
         "yt_delete": yt_delete, "yt_replace": yt_replace, "yt_abandon": yt_abandon}


def register_all() -> None:
    for name, handler in STEPS.items():
        publisher.register(name, "youtube", handler)


register_all()
