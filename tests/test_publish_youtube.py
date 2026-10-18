"""YouTube publishing steps, against the fake YouTube (task 9.2, design §8.2, §8.4)."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from autopilot import accounts, activity, posts, profiles, publish_youtube, publisher, store
from autopilot import owner as owner_mod
from autopilot import readiness
from platforms import errors, google_creds, usage, youtube_api
from tests.fakes.youtube import FakeYouTube

KOL = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=KOL).timestamp()      # 03:30 UTC, the fake's upload time
CLIENT_ID = "123-abc.apps.googleusercontent.com"
PROBE = {"duration": 10.0, "bytes": 3500, "format": "mov,mp4,m4a,3gp,3g2,mj2", "bitrate": 2_000_000,
         "width": 1080, "height": 1920, "fps": 30.0, "video_codec": "h264", "pix_fmt": "yuv420p",
         "field_order": "progressive", "video_bitrate": 1_900_000, "audio_codec": "aac",
         "sample_rate": 48000, "channels": 2, "moov_before_mdat": True, "has_edit_list": False}
META = {"title": "My clip", "description": "What happened next", "tags": ["tips", "shorts"],
        "category_id": "26"}


class Crash(BaseException):
    """Stands in for the process dying: not an Exception, so nothing catches it."""


@pytest.fixture
def yt(_isolated_data, net, monkeypatch):
    import db
    from autopilot import migrations

    db.init()
    migrations.run()
    owner_mod.set_prefs("local", timezone="Asia/Kolkata")
    profile = profiles.ensure_default("local")
    fake = FakeYouTube(net)
    google_creds.token_file().write_text(json.dumps({
        "token": "ya29.valid", "refresh_token": "1//refresh", "client_id": CLIENT_ID,
        "client_secret": "GOCSPX-secret", "scopes": [readiness.YT_MANAGE_SCOPE],
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": google_creds._format_expiry(NOW + 30 * 86400)}))
    accounts.upsert("local", "youtube", external_id="UCchannel123", name="Manish Clips",
                    avatar=None, scopes=[readiness.YT_MANAGE_SCOPE], quota_key=f"yt:{CLIENT_ID}")
    accounts.set_declarations("local", "youtube", made_for_kids=False)
    monkeypatch.setattr(publisher, "_REGISTRY", {})
    publish_youtube.register_all()
    monkeypatch.setattr(youtube_api, "CHUNK", 1000)            # 3,500 bytes = 4 chunks
    clock = {"t": NOW}
    monkeypatch.setattr(posts, "_now", lambda: clock["t"])
    made = []

    def make(holder="A"):
        p = publisher.Publisher(clock=lambda: clock["t"], holder=holder)
        made.append(p)
        return p

    w = {"fake": fake, "clock": clock, "make": make, "profile": profile}
    w["job"] = seed_job(profile)
    w["clip"], w["version"], w["video"] = seed_version(w["job"])
    yield w
    for p in made:
        p.close()


def seed_job(profile) -> str:
    job_id = uuid.uuid4().hex[:12]
    with store.connect() as conn:
        conn.execute("INSERT INTO jobs(job_id, created_at, url, status, autopilot, profile_id, "
                     "profile_snapshot_json, source_key, rights_basis, language) "
                     "VALUES(?,?,?,?,1,?,?,?,?,?)",
                     (job_id, NOW - 3600, "https://youtu.be/x", "done", profile["id"],
                      json.dumps(profile["settings"]), "yt:x", "confirmed", "en"))
    return job_id


def seed_version(job_id, *, clip_id=None, number=1, name="01-clip-vertical.mp4"):
    import paths

    folder = paths.output_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / name
    video.write_bytes(os.urandom(3500))
    (folder / name.replace("-vertical.mp4", "-thumb.jpg")).write_bytes(b"\xff\xd8 thumb")
    version_id = uuid.uuid4().hex[:16]
    with store.connect() as conn:
        if clip_id is None:
            clip_id = uuid.uuid4().hex[:16]
            store.insert_clip(conn, {"id": clip_id, "owner_id": "local", "job_id": job_id,
                                     "idx": 1, "src_start": 0.0, "src_end": 10.0, "score": 80,
                                     "current_version_id": version_id, "created_at": NOW})
        else:
            conn.execute("UPDATE clips SET current_version_id=? WHERE id=?", (version_id, clip_id))
        store.insert_clip_version(conn, {
            "id": version_id, "clip_id": clip_id, "version": number, "spec": {}, "status": "ready",
            "files": {"vertical": name, "thumb": name.replace("-vertical.mp4", "-thumb.jpg")},
            "probe": PROBE})
    return clip_id, version_id, video


def seed_post(w, *, status="scheduled", slot=NOW + 3 * 3600, target="public", step=None,
              due=None, extra=None) -> str:
    pid = uuid.uuid4().hex[:16]
    with store.connect() as conn:
        store.insert_post(conn, {
            "id": pid, "owner_id": "local", "job_id": w["job"], "clip_id": w["clip"],
            "version_id": w["version"], "platform": "youtube", "method": "official",
            "status": status, "approval": "approved", "holds": [], "slot_at": slot,
            "target_visibility": target, "meta": META, "ai_meta": META, "step": step,
            "next_action_at": due, "created_at": NOW, **(extra or {})})
    return pid


def post(pid) -> dict:
    with store.connect() as conn:
        return store.decode_row(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


def attempts(pid) -> list[dict]:
    with store.connect() as conn:
        return store.decode_rows(conn.execute(
            "SELECT * FROM publish_attempts WHERE post_id=? ORDER BY finished_at, rowid",
            (pid,)).fetchall())


def run(pub) -> dict:
    report = pub.tick()
    pub.wait()
    return report


def puts(fake) -> list[str]:
    return [r.headers["content-range"] for r in fake.calls("PUT", "/upload/youtube/v3/videos")]


@pytest.mark.req("8.1", "8.2", "8.8", "8.9", "10.4", "4.7", "4.8")
def test_a_public_target_uploads_privately_with_its_publish_time(yt, monkeypatch):
    fake = yt["fake"]
    pid = seed_post(yt, extra={"disclosure_ai": 1})
    seen = {}
    original = fake._chunk

    def first_chunk(request, sid):
        seen.setdefault("step_data", post(pid)["step_data"])     # what was stored before bytes
        return original(request, sid)

    monkeypatch.setattr(fake, "_chunk", first_chunk)
    run(yt["make"]())

    assert seen["step_data"]["session"]["uri"].endswith("upload_id=sess1")
    assert seen["step_data"]["session"]["sha256"] == hashlib.sha256(
        yt["video"].read_bytes()).hexdigest()
    p = post(pid)
    assert (p["status"], p["step"], p["platform_post_id"]) == ("uploaded", None, "vid_sess1")
    assert p["url"] == "https://www.youtube.com/shorts/vid_sess1"
    assert p["scheduled_publish_at"] == NOW + 3 * 3600
    [video] = fake.videos.values()
    assert video["status"]["privacyStatus"] == "private"
    assert video["status"]["publishAt"] == youtube_api.rfc3339(NOW + 3 * 3600)
    assert video["status"]["selfDeclaredMadeForKids"] is False            # from the account
    assert video["status"]["containsSyntheticMedia"] is True              # the post's disclosure
    assert (video["snippet"]["title"], video["snippet"]["categoryId"],
            video["snippet"]["defaultLanguage"]) == ("My clip", "26", "en")
    assert fake.thumbnails["vid_sess1"] == b"\xff\xd8 thumb"
    account = accounts.get("local", "youtube")
    assert (account["thumbnail_capability"], account["last_publish_at"]) == ("ok", NOW)
    assert [a["step"] for a in attempts(pid)] == ["yt_init", "yt_upload", "yt_finalize"]
    assert puts(fake) == ["bytes */3500", "bytes 0-999/3500", "bytes 1000-1999/3500",
                          "bytes 2000-2999/3500", "bytes 3000-3499/3500"]
    assert usage.used(f"yt-uploads:{CLIENT_ID}", NOW) == 1
    assert activity.list_entries("local", {"kind": "publish.uploaded", "post_id": pid})


@pytest.mark.req("8.3")
def test_a_private_target_uploads_at_its_slot_and_stays_private(yt):
    pid = seed_post(yt, target="private", slot=NOW + 3600)
    pub = yt["make"]()
    run(pub)
    assert post(pid)["status"] == "scheduled" and yt["fake"].sessions == {}   # not before its slot
    yt["clock"]["t"] = NOW + 3600
    run(pub)
    p = post(pid)
    assert (p["status"], p["published_at"], p["scheduled_publish_at"]) == ("private", NOW + 3600,
                                                                           None)
    [video] = yt["fake"].videos.values()
    assert video["status"]["privacyStatus"] == "private" and "publishAt" not in video["status"]


@pytest.mark.req("N2.2", "10.1")
def test_a_crash_after_two_chunks_resumes_in_a_new_publisher_with_one_video(yt, monkeypatch):
    fake = yt["fake"]
    pid = seed_post(yt)
    original, crashed = fake._chunk, {"done": False}

    def dies_after_two_chunks(request, sid):
        response = original(request, sid)
        if not crashed["done"] and len(fake.sessions[sid]["data"]) == 2000:
            crashed["done"] = True
            raise Crash("the process died with YouTube holding 2 chunks")
        return response

    monkeypatch.setattr(fake, "_chunk", dies_after_two_chunks)
    a = yt["make"]("A")
    a.tick()
    with pytest.raises(Crash):
        a.wait()
    p = post(pid)
    assert (p["status"], p["step"]) == ("publishing", "yt_upload")      # the session was stored

    yt["clock"]["t"] += 61                                              # A's lease runs out
    b = yt["make"]("B")
    report = run(b)
    assert report["lease"] is True
    p = post(pid)
    assert (p["status"], p["platform_post_id"]) == ("uploaded", "vid_sess1")
    assert len(fake.sessions) == 1 and list(fake.videos) == ["vid_sess1"]
    assert bytes(fake.sessions["sess1"]["data"]) == yt["video"].read_bytes()
    assert puts(fake)[-3:] == ["bytes */3500", "bytes 2000-2999/3500", "bytes 3000-3499/3500"]


def interrupted_post(yt, *, expire=True) -> tuple[str, dict]:
    """A post whose upload session was created and stored, then lost."""
    snippet = youtube_api.build_snippet(META, "en")
    status = youtube_api.build_status(target_visibility="public", slot_at=NOW + 3 * 3600,
                                      now=NOW, made_for_kids=False, synthetic=False)
    with youtube_api.YouTubeAPI("local", clock=lambda: NOW) as api:
        session = api.init_upload(yt["video"], snippet, status)
    if expire:
        yt["fake"].expire_session("sess1", 404)
    data = {"session": session, "version_id": yt["version"], "file": yt["video"].name,
            "title": "My clip", "duration": 10.0, "publish_at": NOW + 3 * 3600, "target": "public"}
    pid = seed_post(yt, status="publishing", step="yt_upload", due=NOW,
                    extra={"step_data": data, "scheduled_publish_at": NOW + 3 * 3600})
    return pid, data


@pytest.mark.req("10.1", "8.2")
def test_a_lost_session_whose_video_is_up_creates_no_duplicate(yt):
    fake = yt["fake"]
    pid, _ = interrupted_post(yt)
    fake.add_video("vid_done", title="My clip", published="2026-11-02T03:30:30Z", duration="PT10S")
    run(yt["make"]())
    p = post(pid)
    assert (p["status"], p["platform_post_id"]) == ("uploaded", "vid_done")
    assert p["step_data"]["found_by_lookup"] is True
    assert len(fake.sessions) == 1                                  # no new session
    assert [a["step"] for a in attempts(pid)] == ["yt_upload", "yt_lookup", "yt_finalize"]
    assert "wasn't uploaded again" in activity.list_entries(
        "local", {"kind": "publish.step", "post_id": pid})[-1]["summary"]


@pytest.mark.req("10.1", "8.2")
def test_a_lost_session_with_no_video_starts_a_new_upload(yt):
    fake = yt["fake"]
    pid, _ = interrupted_post(yt)
    fake.add_video("vid_other", title="Another clip", published="2026-11-02T03:30:30Z")
    fake.add_video("vid_early", title="My clip", published="2026-11-02T03:20:00Z")   # 10 min early
    run(yt["make"]())
    p = post(pid)
    assert (p["status"], p["platform_post_id"]) == ("uploaded", "vid_sess2")
    assert set(fake.sessions) == {"sess1", "sess2"}
    assert sorted(fake.videos) == ["vid_early", "vid_other", "vid_sess2"]
    assert p["step_data"]["session"]["uri"].endswith("upload_id=sess2")
    assert p["step_data"]["previous_session_gone"] is True
    assert [a["step"] for a in attempts(pid)] == ["yt_upload", "yt_lookup", "yt_init",
                                                  "yt_upload", "yt_finalize"]
    assert usage.used(f"yt-uploads:{CLIENT_ID}", NOW) == 2


def test_a_post_that_already_holds_a_session_never_starts_another(yt):
    pid, data = interrupted_post(yt, expire=False)
    with store.connect() as conn:
        store.update_post(conn, pid, {"step": "yt_init"})          # however it got back here
    run(yt["make"]())
    p = post(pid)
    assert (p["status"], p["platform_post_id"]) == ("uploaded", "vid_sess1")
    assert len(yt["fake"].sessions) == 1


@pytest.mark.req("10.4", "10.2")
@pytest.mark.parametrize("status,reason,wait", [
    (403, "quotaExceeded", "pacific_midnight"),
    (400, "uploadLimitExceeded", "24h"),
])
def test_quota_and_the_daily_upload_limit_defer_without_using_a_retry(yt, status, reason, wait):
    yt["fake"].fail_next = (status, {"error": {"code": status, "message": reason,
                                               "errors": [{"reason": reason}]}})
    pid = seed_post(yt)
    run(yt["make"]())
    p = post(pid)
    expected = errors.next_pacific_midnight(NOW) if wait == "pacific_midnight" else NOW + 86400
    assert (p["status"], p["step"], p["retry_count"], p["next_action_at"]) == (
        "retrying", "yt_init", 0, expected)
    assert usage.used(f"yt-uploads:{CLIENT_ID}", NOW) == 0          # the upload wasn't counted
    assert yt["fake"].sessions == {}


@pytest.mark.req("8.9")
def test_a_refused_thumbnail_marks_the_channel_and_youtube_picks_the_frame(yt):
    fake = yt["fake"]
    fake.thumbnail_status = 403
    first = seed_post(yt)
    pub = yt["make"]()
    run(pub)
    assert post(first)["status"] == "uploaded"                      # the video is up regardless
    assert accounts.get("local", "youtube")["thumbnail_capability"] == "unavailable"
    notes = [e["summary"] for e in activity.list_entries("local", {"post_id": first})]
    assert any("YouTube chooses the frame" in n for n in notes)
    asked = len(fake.calls("POST", "/upload/youtube/v3/thumbnails/set"))
    second = seed_post(yt, slot=NOW + 8 * 3600)
    run(pub)
    assert post(second)["status"] == "uploaded"
    assert len(fake.calls("POST", "/upload/youtube/v3/thumbnails/set")) == asked   # not asked again


def uploaded_post(yt, video_id="vidA", *, publish_at=NOW, privacy="private", extra=None) -> str:
    yt["fake"].add_video(video_id, title="My clip", status={
        "privacyStatus": privacy, "publishAt": youtube_api.rfc3339(publish_at)})
    return seed_post(yt, status="uploaded", slot=publish_at, extra={
        "platform_post_id": video_id, "url": f"https://www.youtube.com/shorts/{video_id}",
        "scheduled_publish_at": publish_at, **(extra or {})})


@pytest.mark.req("8.7")
def test_verification_at_15_minutes_and_2_hours_gives_live_or_kept_private(yt, client):
    fake = yt["fake"]
    goes_live, stays_private = uploaded_post(yt, "vidA"), uploaded_post(yt, "vidB")
    pub = yt["make"]()
    yt["clock"]["t"] = NOW + 10 * 60
    assert run(pub)["dispatched"] == []                             # not before 15 minutes
    yt["clock"]["t"] = NOW + 16 * 60
    run(pub)
    for pid in (goes_live, stays_private):
        p = post(pid)
        assert (p["status"], p["step"], p["next_action_at"]) == ("uploaded", "yt_verify",
                                                                 NOW + 2 * 3600)
    fake.videos["vidA"]["status"]["privacyStatus"] = "public"
    yt["clock"]["t"] = NOW + 2 * 3600
    run(pub)
    live = post(goes_live)
    assert (live["status"], live["published_at"], live["step"]) == ("live", NOW, None)
    kept = post(stays_private)
    assert kept["status"] == "kept_private"
    assert readiness.YT_AUDIT_FORM in kept["last_error"] and "API audit" in kept["last_error"]
    view = client.get(f"/api/autopilot/posts/{stays_private}").json()
    assert view["audit_form"] == readiness.YT_AUDIT_FORM
    assert activity.list_entries("local", {"kind": "publish.live", "post_id": goes_live})
    assert activity.list_entries("local", {"kind": "publish.kept_private", "post_id": stays_private})


def test_a_publish_time_moved_on_youtube_is_checked_then(yt):
    pid = uploaded_post(yt, "vidC")
    yt["fake"].videos["vidC"]["status"]["publishAt"] = youtube_api.rfc3339(NOW + 5 * 3600)
    yt["clock"]["t"] = NOW + 16 * 60
    run(yt["make"]())
    p = post(pid)
    assert (p["step"], p["next_action_at"], p["scheduled_publish_at"]) == (
        "yt_verify", NOW + 5 * 3600 + 15 * 60, NOW + 5 * 3600)


@pytest.mark.req("8.4")
def test_edits_after_the_upload_update_the_video(yt):
    fake = yt["fake"]
    pid = uploaded_post(yt, "vidD", publish_at=NOW + 3600)
    out = posts.patch("local", pid, {"meta": {"title": "A better title"}})
    assert out["post"]["remote_dirty"] == 1
    pub = yt["make"]()
    run(pub)
    video = fake.videos["vidD"]
    assert (video["snippet"]["title"], video["snippet"]["description"]) == (
        "A better title", "What happened next")
    assert video["status"]["publishAt"] == youtube_api.rfc3339(NOW + 3600)   # kept
    p = post(pid)
    assert (p["status"], p["remote_dirty"]) == ("uploaded", 0)

    posts.patch("local", pid, {"target_visibility": "private"})
    run(pub)
    assert "publishAt" not in fake.videos["vidD"]["status"]
    assert post(pid)["status"] == "private"


def test_an_edit_youtube_refuses_is_recorded_and_the_post_stays_uploaded(yt):
    pid = uploaded_post(yt, "vidE", publish_at=NOW + 3600)
    posts.patch("local", pid, {"meta": {"title": "Changed"}})
    yt["fake"].fail_next = (400, {"error": {"code": 400, "message": "Invalid title",
                                            "errors": [{"reason": "invalidTitle"}]}})
    run(yt["make"]())
    p = post(pid)
    assert (p["status"], p["remote_dirty"]) == ("uploaded", 0)
    assert p["last_error"].startswith("YouTube refused the edit")


@pytest.mark.req("6.9", "6.10")
def test_unschedule_and_reschedule_remove_and_restore_the_publish_time(yt):
    fake = yt["fake"]
    pid = uploaded_post(yt, "vidF", publish_at=NOW + 3600)
    with store.connect() as conn:
        store.update_post(conn, pid, {"step": "yt_unschedule", "next_action_at": NOW})
    pub = yt["make"]()
    run(pub)
    assert "publishAt" not in fake.videos["vidF"]["status"]
    assert (post(pid)["paused_hold"], post(pid)["status"]) == (1, "uploaded")
    yt["clock"]["t"] = NOW + 2 * 3600                               # the slot passed meanwhile
    with store.connect() as conn:
        store.update_post(conn, pid, {"step": "yt_reschedule", "next_action_at": yt["clock"]["t"]})
    run(pub)
    when = NOW + 2 * 3600 + 15 * 60
    assert fake.videos["vidF"]["status"]["publishAt"] == youtube_api.rfc3339(when)
    p = post(pid)
    assert (p["paused_hold"], p["scheduled_publish_at"]) == (0, when)


@pytest.mark.req("8.5")
def test_rejecting_an_upload_deletes_it_unless_it_went_public(yt):
    fake = yt["fake"]
    private = uploaded_post(yt, "vidG", publish_at=NOW + 3600)
    public = uploaded_post(yt, "vidH", publish_at=NOW + 3600)
    posts.reject("local", private)
    posts.reject("local", public)
    fake.videos["vidH"]["status"]["privacyStatus"] = "public"        # went live meanwhile
    run(yt["make"]())
    assert "vidG" not in fake.videos and "vidH" in fake.videos
    p = post(private)
    assert (p["status"], p["step"], p["step_data"]["deleted"]) == ("rejected", None, True)
    assert post(public)["step_data"]["deleted"] is False
    summaries = [e["summary"] for e in activity.list_entries("local", {"kind": "publish.deleted"})]
    assert any("Deleted the private YouTube upload" in s for s in summaries)
    assert any("already gone public" in s for s in summaries)


@pytest.mark.req("8.6")
def test_replacing_an_upload_with_a_newer_version(yt, client):
    fake = yt["fake"]
    pid = uploaded_post(yt, "vidOld", publish_at=NOW + 3600)
    url = f"/api/autopilot/posts/{pid}/replace"
    assert client.post(url).status_code == 409                       # no newer version yet
    _, newer, newer_file = seed_version(yt["job"], clip_id=yt["clip"], number=2,
                                        name="01-clip-vertical.v2.mp4")
    r = client.post(url)
    assert r.status_code == 200 and r.json()["step"] == "yt_replace"
    run(yt["make"]())
    assert "vidOld" not in fake.videos
    p = post(pid)
    assert (p["status"], p["version_id"], p["platform_post_id"]) == ("uploaded", newer, "vid_sess1")
    assert bytes(fake.sessions["sess1"]["data"]) == newer_file.read_bytes()
    assert p["scheduled_publish_at"] == NOW + 3600
    assert activity.list_entries("local", {"kind": "post.version_changed", "post_id": pid})


@pytest.mark.req("10.10", "10.1", "10.2")
def test_a_failed_upload_tried_again_resumes_its_session_and_makes_one_video(yt, monkeypatch):
    fake = yt["fake"]
    pid = seed_post(yt)
    original, down = fake._chunk, {"on": True}

    def flaky(request, sid):
        return httpx.Response(503) if down["on"] else original(request, sid)

    monkeypatch.setattr(fake, "_chunk", flaky)
    pub = yt["make"]()
    for _ in range(6):                                   # the first try and three retries
        run(pub)
        if post(pid)["status"] == "failed":
            break
        yt["clock"]["t"] = post(pid)["next_action_at"]
    p = post(pid)
    assert (p["status"], p["step"], p["retry_count"]) == ("failed", "yt_upload", 3)
    assert p["last_error"].startswith("Gave up after 3 retries")

    down["on"] = False
    tried = posts.retry("local", pid)
    assert (tried["status"], tried["step"], tried["retry_count"]) == ("retrying", "yt_upload", 0)
    run(pub)
    p = post(pid)
    assert (p["status"], p["platform_post_id"]) == ("uploaded", "vid_sess1")
    assert list(fake.sessions) == ["sess1"] and list(fake.videos) == ["vid_sess1"]   # one upload
    assert bytes(fake.sessions["sess1"]["data"]) == yt["video"].read_bytes()


@pytest.mark.req("10.10", "7.7", "10.3")
def test_a_failed_post_with_nothing_uploaded_starts_again_with_the_clips_current_version(yt):
    fake = yt["fake"]
    pid = seed_post(yt)
    fake.fail_next = (400, {"error": {"code": 400, "message": "Invalid title",
                                      "errors": [{"reason": "invalidTitle"}]}})
    pub = yt["make"]()
    run(pub)
    p = post(pid)
    assert (p["status"], p["step"], fake.sessions) == ("failed", None, {})  # nothing reached YouTube
    # The clip was edited meanwhile: a failed post doesn't follow it until it's tried again.
    _, newer, video = seed_version(yt["job"], clip_id=yt["clip"], number=2,
                                   name="01-clip-v2-vertical.mp4")
    assert post(pid)["version_id"] == yt["version"]

    tried = posts.retry("local", pid)
    assert (tried["status"], tried["version_id"], tried["slot_at"]) == ("scheduled", newer,
                                                                        NOW + 3 * 3600)
    run(pub)
    p = post(pid)
    assert (p["status"], p["platform_post_id"], p["version_id"]) == ("uploaded", "vid_sess1", newer)
    assert list(fake.sessions) == ["sess1"]
    assert bytes(fake.sessions["sess1"]["data"]) == video.read_bytes()
