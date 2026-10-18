"""Deleting a source with planned posts (task 9.6, design §3.3; Requirements
1.9, 8.5), against the fake YouTube and the fake Instagram."""
from __future__ import annotations

import json
import os
import uuid

import pytest

import paths
from autopilot import accounts, activity, legacy, posts, profiles, publish_instagram
from autopilot import publish_youtube, publisher, readiness, sources, store
from autopilot import owner as owner_mod
from platforms import google_creds, meta_auth, youtube_api
from platforms.instagram_api import InstagramAPI
from tests.fakes.instagram import APP, IG_USER, PAGE_TOKEN, USER_TOKEN, FakeInstagram
from tests.fakes.youtube import FakeYouTube

NOW = 1_793_590_200.0                      # Mon 2 Nov 2026, 03:30 UTC (09:00 in Kolkata)
H = 3600
CLIENT_ID = "123-abc.apps.googleusercontent.com"
CAPTION = "Dark mode is here #tips"
PROBE = {"duration": 10.0, "bytes": 3500, "format": "mov,mp4,m4a,3gp,3g2,mj2", "bitrate": 2_000_000,
         "width": 1080, "height": 1920, "fps": 30.0, "video_codec": "h264", "pix_fmt": "yuv420p",
         "field_order": "progressive", "video_bitrate": 1_900_000, "audio_codec": "aac",
         "sample_rate": 48000, "channels": 2, "moov_before_mdat": True, "has_edit_list": False}
META = {"youtube": {"title": "My clip", "description": "What happened next", "tags": ["tips"],
                    "category_id": "26"},
        "instagram": {"caption": CAPTION}}


def connect_accounts(owner="local") -> None:
    google_creds.token_file().write_text(json.dumps({
        "token": "ya29.valid", "refresh_token": "1//refresh", "client_id": CLIENT_ID,
        "client_secret": "GOCSPX-secret", "scopes": [readiness.YT_MANAGE_SCOPE],
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": google_creds._format_expiry(NOW + 30 * 86400)}))
    accounts.upsert(owner, "youtube", external_id="UCchannel123", name="Manish Clips",
                    avatar=None, scopes=[readiness.YT_MANAGE_SCOPE], quota_key=f"yt:{CLIENT_ID}")
    accounts.set_declarations(owner, "youtube", made_for_kids=False)
    meta_auth.save_app_settings(APP["app_id"], APP["app_secret"], APP["config_id"])
    meta_auth.save_token(owner, {"user_token": USER_TOKEN, "page_id": "p1",
                                 "page_token": PAGE_TOKEN, "ig_user_id": IG_USER,
                                 "ig_username": "manish"})
    accounts.upsert(owner, "instagram", external_id=IG_USER, name="manish", avatar=None,
                    scopes=[readiness.IG_PUBLISH_SCOPE], quota_key=f"ig:{IG_USER}", page_id="p1")


@pytest.fixture
def w(client, net, monkeypatch):
    owner_mod.set_prefs("local", timezone="Asia/Kolkata")
    profile = profiles.ensure_default("local")
    yt, ig = FakeYouTube(net), FakeInstagram(net)
    connect_accounts()
    monkeypatch.setattr(publisher, "_REGISTRY", {})
    publish_youtube.register_all()
    publish_instagram.register_all()
    monkeypatch.setattr(youtube_api, "CHUNK", 1000)            # 3,500 bytes = 4 chunks
    clock = {"t": NOW}
    monkeypatch.setattr(posts, "_now", lambda: clock["t"])
    pub = publisher.Publisher(clock=lambda: clock["t"], holder="A")
    state = {"yt": yt, "ig": ig, "clock": clock, "profile": profile, "pub": pub,
             "client": client}
    state["job"] = seed_job(profile)
    state["clip"], state["version"], state["video"] = seed_version(state["job"])
    yield state
    pub.close()


def seed_job(profile, *, owner=None, source_key="yt:x", title="Podcast ep 1",
             status="done", request=None) -> str:
    job_id = uuid.uuid4().hex[:12]
    with store.connect() as conn:
        conn.execute("INSERT INTO jobs(job_id, created_at, url, status, user_id, autopilot, "
                     "profile_id, profile_snapshot_json, source_key, rights_basis, language, "
                     "source_title, request_json) VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?)",
                     (job_id, NOW - H, "https://youtu.be/x", status, owner, profile["id"],
                      json.dumps(profile["settings"]), source_key, "confirmed", "en", title,
                      json.dumps(request) if request else None))
    return job_id


def seed_version(job_id, *, owner="local", start=0.0, end=10.0):
    folder = paths.output_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "01-clip-vertical.mp4"
    video.write_bytes(os.urandom(3500))
    clip_id, version_id = uuid.uuid4().hex[:16], uuid.uuid4().hex[:16]
    with store.connect() as conn:
        store.insert_clip(conn, {"id": clip_id, "owner_id": owner, "job_id": job_id, "idx": 1,
                                 "src_start": start, "src_end": end, "score": 80,
                                 "meta": META, "current_version_id": version_id,
                                 "created_at": NOW})
        store.insert_clip_version(conn, {
            "id": version_id, "clip_id": clip_id, "version": 1, "spec": {}, "status": "ready",
            "files": {"vertical": video.name}, "probe": PROBE})
    return clip_id, version_id, video


def seed_post(w, platform="youtube", *, status="scheduled", slot=NOW + 3 * H, step=None,
              due=None, job=None, clip=None, owner="local", extra=None) -> str:
    pid = uuid.uuid4().hex[:16]
    with store.connect() as conn:
        store.insert_post(conn, {
            "id": pid, "owner_id": owner, "job_id": job or w["job"], "clip_id": clip or w["clip"],
            "version_id": w["version"], "platform": platform, "method": "official",
            "status": status, "approval": "approved", "holds": [], "slot_at": slot,
            "target_visibility": "public", "meta": META[platform], "ai_meta": META[platform],
            "step": step, "next_action_at": due, "created_at": NOW, **(extra or {})})
    return pid


def post(pid) -> dict:
    with store.connect() as conn:
        return store.decode_row(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


def tick(w) -> dict:
    report = w["pub"].tick()
    w["pub"].wait()
    return report


def youtube_sessions(w) -> tuple[dict, dict]:
    """One upload that finished on YouTube but whose reply never arrived, and
    one session that never got a byte."""
    snippet = youtube_api.build_snippet(META["youtube"], "en")
    status = youtube_api.build_status(target_visibility="public", slot_at=NOW + 3 * H, now=NOW,
                                      made_for_kids=False, synthetic=False)
    with youtube_api.YouTubeAPI("local", clock=lambda: NOW) as api:
        finished = api.init_upload(w["video"], snippet, status)
        api.upload(finished, w["video"])
        untouched = api.init_upload(w["video"], snippet, status)
    return finished, untouched


def reel(w, *, publish: bool, caption: str = CAPTION) -> dict:
    with InstagramAPI("local", clock=lambda: NOW) as api:
        container = api.create_container(caption)
        api.upload(container, w["video"])
        if publish:
            api.publish(container["container_id"])
    return container


def seed_everything(w) -> dict:
    """A source with a post in every state deletion treats differently."""
    w["yt"].add_video("vidC", title="My clip", status={
        "privacyStatus": "private", "publishAt": youtube_api.rfc3339(NOW + 3 * H)})
    finished, untouched = youtube_sessions(w)
    reel_out, reel_back = reel(w, publish=True), reel(w, publish=False)
    resolving = reel(w, publish=True, caption="Another reel")
    session = {"title": "My clip", "duration": 10.0}
    p = {
        # cancelled
        "scheduled": seed_post(w),
        "waiting": seed_post(w, "instagram", status="waiting_approval", slot=None),
        "uploaded": seed_post(w, status="uploaded", extra={
            "platform_post_id": "vidC", "scheduled_publish_at": NOW + 3 * H}),
        "upload_done": seed_post(w, status="publishing", step="yt_upload", due=NOW + 600,
                                 extra={"step_data": {**session, "session": finished}}),
        "upload_new": seed_post(w, status="retrying", step="yt_upload", due=NOW + H,
                                extra={"step_data": {**session, "session": untouched}}),
        "reel_out": seed_post(w, "instagram", status="retrying", step="ig_publish", due=NOW + H,
                              extra={"step_data": {"container": reel_out, "caption": CAPTION}}),
        "reel_back": seed_post(w, "instagram", status="retrying", step="ig_publish", due=NOW + H,
                               extra={"step_data": {"container": reel_back, "caption": CAPTION}}),
        # published, kept
        "live": seed_post(w, status="live", slot=NOW - 86400, extra={
            "platform_post_id": "vidF", "url": "https://www.youtube.com/shorts/vidF",
            "published_at": NOW - 86400}),
        "resolving": seed_post(w, "instagram", status="publishing", step="ig_resolve", due=NOW,
                               extra={"platform_post_id": "m2", "step_data": {
                                   "container": resolving, "media_id": "m2"}}),
        "kept_private": seed_post(w, status="kept_private", slot=NOW - 2 * 86400, extra={
            "platform_post_id": "vidK"}),
        # left as they are
        "failed": seed_post(w, "instagram", status="failed", slot=None,
                            extra={"last_error": "Gave up after 3 retries"}),
        "rejected": seed_post(w, status="rejected", slot=None),
    }
    p["legacy_scheduled"] = legacy.create(
        None, job_id=w["job"], file=w["video"].name, platform="youtube", title="By hand",
        description="", tags=[], privacy="unlisted", scheduled_at=NOW + 600)["id"]
    p["legacy_done"] = legacy.create(
        None, job_id=w["job"], file=w["video"].name, platform="youtube", title="Done by hand",
        description="", tags=[], privacy="unlisted", scheduled_at=NOW - 86400)["id"]
    with store.connect() as conn:
        store.update_post(conn, p["legacy_done"], {"status": "live", "url": "https://youtu.be/m",
                                                   "published_at": NOW - 86400})
        conn.execute("INSERT INTO post_metrics(post_id, fetched_at, age_hours, views, likes, "
                     "comments) VALUES(?,?,?,?,?,?)", (p["live"], NOW - H, 23.0, 340, 12, 3))
    return p


CANCELLED = {"scheduled": None, "waiting": None, "uploaded": "yt_delete",
             "upload_done": "yt_abandon", "upload_new": "yt_abandon", "reel_out": "ig_abandon",
             "reel_back": "ig_abandon", "legacy_scheduled": None}
KEPT = ("live", "resolving", "kept_private", "legacy_done", "failed", "rejected")


@pytest.mark.req("1.9")
def test_the_preview_says_what_deleting_cancels_deletes_and_keeps(w):
    seed_everything(w)
    other = seed_job(w["profile"], source_key="yt:other")
    seed_post(w, job=other)
    impact = w["client"].get(f"/api/autopilot/sources/{w['job']}/delete-impact").json()
    assert {k: impact[k] for k in ("unpublished", "youtube_uploads", "scheduled_uploads",
                                   "published")} == {"unpublished": 8, "youtube_uploads": 3,
                                                     "scheduled_uploads": 1, "published": 4}
    assert impact["source"] == {"id": w["job"], "title": "Podcast ep 1", "autopilot": True,
                                "processing": False}
    assert impact["message"] == (
        "Deleting it cancels 8 posts (1 scheduled upload among them) that haven't been "
        "published, and deletes their private YouTube uploads (up to 3) before they go public. "
        "4 published posts stay in your history.")
    assert w["client"].get(f"/api/autopilot/sources/{other}/delete-impact").json()[
        "unpublished"] == 1
    assert w["client"].get("/api/autopilot/sources/ffffffffffff/delete-impact").status_code == 404


@pytest.mark.req("1.9", "8.5")
def test_deleting_cancels_unpublished_posts_and_keeps_published_history(w):
    p = seed_everything(w)
    before = {name: post(p[name]) for name in KEPT}
    other = seed_job(w["profile"], source_key="yt:other")
    _, _, other_video = seed_version(other)
    other_post = seed_post(w, job=other)
    response = w["client"].delete(f"/api/autopilot/sources/{w['job']}?confirm=8")
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == w["job"]
    assert set(body["cancelled"]) == {p[n] for n in CANCELLED}
    assert set(body["youtube_uploads"]) == {p["uploaded"], p["upload_done"], p["upload_new"]}
    assert set(body["published"]) == {p[n] for n in ("live", "resolving", "kept_private",
                                                     "legacy_done")}
    for name, step in CANCELLED.items():
        got = post(p[name])
        assert (got["status"], got["step"], got["last_error"], got["user_locked"]) == (
            "cancelled", step, "The video it came from was deleted.", 0), name
        if got["method"] == "official":
            assert got["slot_at"] is None, name
    assert post(p["legacy_scheduled"])["slot_at"] == NOW + 600     # a scheduled upload keeps its time
    for name in KEPT:
        assert post(p[name]) == before[name], name                 # untouched
    with store.connect() as conn:
        metrics = conn.execute("SELECT views FROM post_metrics WHERE post_id=?",
                               (p["live"],)).fetchall()
        job = conn.execute("SELECT deleted_at FROM jobs WHERE job_id=?", (w["job"],)).fetchone()
    assert [m["views"] for m in metrics] == [340] and job["deleted_at"]

    client = w["client"]
    assert w["job"] not in [j["job_id"] for j in client.get("/api/jobs").json()["jobs"]]
    assert client.get(f"/api/status/{w['job']}").status_code == 404
    assert not (paths.output_dir() / w["job"]).exists()
    [scheduled] = [s for s in client.get("/api/schedule").json()["schedules"]
                   if s["id"] == p["legacy_scheduled"]]
    assert (scheduled["status"], scheduled["error"], scheduled["scheduled_at"]) == (
        "error", "The video it came from was deleted.", NOW + 600)
    assert post(other_post)["status"] == "scheduled" and other_video.is_file()
    assert client.get(f"/api/autopilot/sources/{w['job']}/delete-impact").status_code == 404
    [entry] = activity.list_entries("local", {"kind": "source.deleted"})
    assert entry["summary"].startswith("Deleted Podcast ep 1. Deleting it cancels 8 posts")
    assert len(activity.list_entries("local", {"kind": "post.cancelled"})) == 8


@pytest.mark.req("1.9", "8.5", "10.1")
def test_after_deleting_the_publisher_cleans_up_and_keeps_what_went_out(w, monkeypatch):
    from autopilot import pause

    p = seed_everything(w)
    uploads = []
    monkeypatch.setattr(legacy, "upload", lambda post, path: uploads.append(post["id"]) or {})
    sources.delete(w["job"], None, now=NOW)
    pause.set_paused("local", True)                 # cleanup runs even while paused
    w["clock"]["t"] = NOW + H
    tick(w)
    yt, ig = w["yt"], w["ig"]
    assert "vidC" not in yt.videos                                  # the uploaded video
    assert "vid_sess1" not in yt.videos                             # the unrecorded finished one
    assert post(p["upload_done"])["platform_post_id"] == "vid_sess1"
    assert post(p["upload_new"])["step_data"]["session_dropped"] is True
    assert yt.sessions["sess2"]["data"] == bytearray()              # nothing sent to it
    out = post(p["reel_out"])                                       # it had gone out: history
    assert (out["status"], out["url"]) == ("live", "https://www.instagram.com/reel/m1/")
    assert post(p["resolving"])["status"] == "live"
    back = post(p["reel_back"])
    assert (back["status"], back["step"]) == ("cancelled", None)
    assert len(ig.media) == 2 and ig.containers[back["step_data"]["container"][
        "container_id"]]["status_code"] == "FINISHED"               # never published
    for name in ("uploaded", "upload_done", "upload_new"):
        assert (post(p[name])["status"], post(p[name])["step"]) == ("cancelled", None), name
    assert uploads == []
    pause.set_paused("local", False)
    tick(w)
    assert uploads == [] and post(p["legacy_scheduled"])["status"] == "cancelled"   # never sent
    notes = [e["summary"] for e in activity.list_entries("local", {"post_id": p["reel_out"]})]
    assert any("already been published when you deleted the video it came from" in n for n in notes)


# ── confirmation and today's replies ─────────────────────────────────────

@pytest.mark.req("1.9", "N6.1")
def test_the_confirmed_count_must_still_be_right(w):
    client, job = w["client"], w["job"]
    first = seed_post(w)
    assert client.delete(f"/api/autopilot/sources/{job}").status_code == 400
    stale = client.delete(f"/api/autopilot/sources/{job}?confirm=0")
    assert stale.status_code == 409
    assert stale.json()["impact"]["unpublished"] == 1
    assert "posts changed since you confirmed" in stale.json()["detail"]
    assert client.delete(f"/api/jobs/{job}?confirm=3").status_code == 409
    assert post(first)["status"] == "scheduled" and not sources.is_deleted(job)   # nothing changed
    assert (paths.output_dir() / job).is_dir()
    assert client.delete(f"/api/jobs/{job}").json() == {"ok": True}    # today's request and reply
    assert post(first)["status"] == "cancelled" and sources.is_deleted(job)
    assert client.delete(f"/api/jobs/{job}").json() == {"ok": True}    # as today, single-user
    assert client.delete(f"/api/autopilot/sources/{job}?confirm=0").status_code == 404


@pytest.mark.req("1.9", "15.3", "N3.2")
def test_each_user_can_preview_and_delete_only_their_own_sources(two_users):
    import db

    alice, bob = two_users
    owner = db.get_user_by_email("alice@example.com")["id"]
    job_id = uuid.uuid4().hex[:12]
    db.create(job_id, "https://youtu.be/x", user_id=owner)
    db.finish(job_id, "done")
    (paths.output_dir() / job_id).mkdir(parents=True)
    for client in (bob,):
        assert client.get(f"/api/autopilot/sources/{job_id}/delete-impact").status_code == 404
        assert client.delete(f"/api/autopilot/sources/{job_id}?confirm=0").status_code == 404
        assert client.delete(f"/api/jobs/{job_id}").status_code == 404
    assert (paths.output_dir() / job_id).is_dir()
    assert job_id in [j["job_id"] for j in alice.get("/api/jobs").json()["jobs"]]
    preview = alice.get(f"/api/autopilot/sources/{job_id}/delete-impact").json()
    assert (preview["unpublished"], preview["message"]) == (0, "It has no posts.")
    assert alice.delete(f"/api/jobs/{job_id}").json() == {"ok": True}
    assert not (paths.output_dir() / job_id).exists()
    assert alice.delete(f"/api/jobs/{job_id}").status_code == 404      # as today, with sign-in
    assert alice.get("/api/jobs").json()["jobs"] == []


# ── a deletion that lands while a step runs ──────────────────────────────

@pytest.mark.req("1.9", "8.5", "N2.2")
def test_a_deletion_during_an_upload_still_deletes_the_finished_video(w, monkeypatch):
    fake, pid = w["yt"], seed_post(w, slot=NOW + 3 * H)
    original, done = fake._chunk, {"hit": False}

    def delete_mid_upload(request, sid):
        if not done["hit"] and fake.sessions[sid]["data"]:
            done["hit"] = True
            sources.delete(w["job"], None)                  # the user deletes the source now
        return original(request, sid)

    monkeypatch.setattr(fake, "_chunk", delete_mid_upload)
    tick(w)
    p = post(pid)
    assert done["hit"] and "vid_sess1" in fake.videos               # the upload finished...
    assert (p["status"], p["step"], p["platform_post_id"]) == ("cancelled", "yt_delete", "vid_sess1")
    assert p["last_error"] == "The video it came from was deleted."
    tick(w)
    assert "vid_sess1" not in fake.videos                           # ...and was deleted
    assert (post(pid)["status"], post(pid)["step"]) == ("cancelled", None)
    notes = [e["summary"] for e in activity.list_entries("local", {"post_id": pid})]
    assert any("was uploading when you deleted the video it came from" in n for n in notes)


@pytest.mark.req("1.9", "10.1")
def test_a_deletion_during_an_instagram_publish_keeps_the_reel_as_history(w, monkeypatch):
    fake, pid = w["ig"], seed_post(w, "instagram", slot=NOW)
    tick(w)                                                         # uploaded; processing next
    original, done = fake._graph, {"hit": False}

    def delete_then_publish(request, rest, form, token):
        if not done["hit"] and rest.endswith("/media_publish"):
            done["hit"] = True
            sources.delete(w["job"], None)
        return original(request, rest, form, token)

    monkeypatch.setattr(fake, "_graph", delete_then_publish)
    w["clock"]["t"] = NOW + 60
    tick(w)
    p = post(pid)
    assert done["hit"] and list(fake.media) == ["m1"]
    assert (p["status"], p["url"]) == ("live", "https://www.instagram.com/reel/m1/")
    notes = [e["summary"] for e in activity.list_entries("local", {"post_id": pid})]
    assert any("already been published when you deleted the video it came from" in n for n in notes)


@pytest.mark.req("1.9", "15.4")
@pytest.mark.parametrize("succeeds", [True, False])
def test_a_scheduled_upload_running_when_its_source_is_deleted(w, monkeypatch, succeeds):
    sid = legacy.create(None, job_id=w["job"], file=w["video"].name, platform="youtube",
                        title="By hand", description="", tags=[], privacy="unlisted",
                        scheduled_at=NOW)["id"]

    def upload(post_row, path):
        sources.delete(w["job"], None)
        if not succeeds:
            raise RuntimeError("Upload failed: the file vanished")
        return {"video_id": "v9", "url": "https://youtu.be/v9"}

    monkeypatch.setattr(legacy, "upload", upload)
    tick(w)
    row = legacy.get(sid)
    if succeeds:                                    # it went out, so it's history
        assert (row["status"], row["url"]) == ("done", "https://youtu.be/v9")
    else:
        assert (row["status"], row["error"]) == ("error", "The video it came from was deleted.")


def test_a_scheduled_upload_whose_source_goes_first_never_starts(w, monkeypatch):
    sid = legacy.create(None, job_id=w["job"], file=w["video"].name, platform="youtube",
                        title="By hand", description="", tags=[], privacy="unlisted",
                        scheduled_at=NOW)["id"]
    calls = []
    monkeypatch.setattr(legacy, "upload", lambda post_row, path: calls.append(1) or {})
    # The files go a moment later than the row, so the upload passes its file
    # check and is stopped only by the claim (publisher._withdrawn_meanwhile).
    monkeypatch.setattr(sources, "remove_files", lambda job_id, request=None: None)
    original = publisher.Publisher._load

    def load_then_delete(self, post_id):            # deleted between the check and the claim
        row = original(self, post_id)
        if row and row.get("method") == "legacy" and not calls and not sources.is_deleted(
                w["job"]):
            sources.delete(w["job"], None)
        return row

    monkeypatch.setattr(publisher.Publisher, "_load", load_then_delete)
    tick(w)
    assert calls == [] and w["video"].is_file()
    assert (legacy.get(sid)["status"], legacy.get(sid)["error"]) == (
        "error", "The video it came from was deleted.")


# ── rejecting uses the same cleanup ──────────────────────────────────────

@pytest.mark.req("8.5", "6.5")
def test_rejecting_a_post_cut_off_mid_upload_deletes_the_video_instead_of_resuming(w):
    finished, _ = youtube_sessions(w)
    accounts.mark_needs_reconnect("local", "youtube", "Access was lost.")
    pid = seed_post(w, status="needs_reconnect", step="yt_upload", extra={
        "step_data": {"session": finished, "title": "My clip", "duration": 10.0}})
    rejected = posts.reject("local", pid)
    assert (rejected["status"], rejected["step"]) == ("rejected", "yt_abandon")
    connect_accounts()                                      # the user reconnects
    posts.restore_after_reconnect("local", "youtube")
    sent = len(w["yt"].calls("PUT", "/upload/youtube/v3/videos"))
    tick(w)
    p = post(pid)
    assert (p["status"], p["step"]) == ("rejected", None)
    assert "vid_sess1" not in w["yt"].videos                # deleted, not published
    after = w["yt"].calls("PUT", "/upload/youtube/v3/videos")[sent:]
    assert [r.headers["content-range"] for r in after] == ["bytes */3500"]   # asked, sent nothing


# ── after the deletion ───────────────────────────────────────────────────

@pytest.mark.req("13.4", "1.9")
def test_a_deleted_sources_published_clips_still_count_as_posted(w):
    seed_post(w, status="live", extra={"platform_post_id": "vidL", "published_at": NOW - H})
    sources.delete(w["job"], None)
    again = seed_job(w["profile"], source_key="yt:x")            # the same video, again
    clip, _, _ = seed_version(again, start=2.0, end=12.0)         # overlaps the posted clip
    result = posts.create_posts_for_job("local", again)
    assert [(s["clip_id"], s["platform"]) for s in result["skipped"]] == [(clip, "youtube")]
    assert [post(p)["platform"] for p in result["created"]] == ["instagram"]


def test_nothing_new_is_planned_or_added_for_a_deleted_source(w):
    sources.delete(w["job"], None)
    with pytest.raises(posts.PostError) as planned:
        posts.create_posts_for_job("local", w["job"])
    with pytest.raises(posts.PostError) as added:
        posts.add("local", w["clip"], "youtube", NOW + 5 * H)
    assert (planned.value.status, added.value.status) == (404, 404)


def test_a_queued_job_whose_source_was_deleted_isnt_processed(w, monkeypatch):
    import app as app_module

    ran = []
    monkeypatch.setattr(app_module, "_run_autopilot_job", ran.append)
    monkeypatch.setattr(app_module, "_run_job", lambda job_id, req, source: ran.append(job_id))
    kept = seed_job(w["profile"], source_key="yt:kept")
    sources.delete(w["job"], None)
    app_module._dispatch(w["job"], {"autopilot": True}, None)
    app_module._dispatch(kept, {"autopilot": True}, None)
    assert ran == [kept]
