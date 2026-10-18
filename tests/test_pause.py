"""Pause and resume (task 9.4, design §7; Requirements 6.8-6.10), against the
fake YouTube and the fake Instagram."""
from __future__ import annotations

import json
import os
import uuid

import pytest

from autopilot import accounts, activity, pause, planner, posts, profiles, publish_instagram
from autopilot import publish_youtube, publisher, readiness, store
from autopilot import owner as owner_mod
from platforms import google_creds, meta_auth, youtube_api
from tests.fakes.instagram import APP, IG_USER, PAGE_TOKEN, USER_TOKEN, FakeInstagram
from tests.fakes.youtube import FakeYouTube

NOW = 1_793_590_200.0                      # Mon 2 Nov 2026, 03:30 UTC (09:00 in Kolkata)
TZ = "Asia/Kolkata"
H = 3600
CLIENT_ID = "123-abc.apps.googleusercontent.com"
PROBE = {"duration": 10.0, "bytes": 3500, "format": "mov,mp4,m4a,3gp,3g2,mj2", "bitrate": 2_000_000,
         "width": 1080, "height": 1920, "fps": 30.0, "video_codec": "h264", "pix_fmt": "yuv420p",
         "field_order": "progressive", "video_bitrate": 1_900_000, "audio_codec": "aac",
         "sample_rate": 48000, "channels": 2, "moov_before_mdat": True, "has_edit_list": False}
META = {"youtube": {"title": "My clip", "description": "What happened next",
                    "tags": ["tips"], "category_id": "26"},
        "instagram": {"caption": "Dark mode is here #tips"}}


@pytest.fixture
def w(_isolated_data, net, monkeypatch):
    import db
    from autopilot import migrations

    db.init()
    migrations.run()
    owner_mod.set_prefs("local", timezone=TZ)
    profile = profiles.ensure_default("local")
    yt, ig = FakeYouTube(net), FakeInstagram(net)
    google_creds.token_file().write_text(json.dumps({
        "token": "ya29.valid", "refresh_token": "1//refresh", "client_id": CLIENT_ID,
        "client_secret": "GOCSPX-secret", "scopes": [readiness.YT_MANAGE_SCOPE],
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": google_creds._format_expiry(NOW + 30 * 86400)}))
    accounts.upsert("local", "youtube", external_id="UCchannel123", name="Manish Clips",
                    avatar=None, scopes=[readiness.YT_MANAGE_SCOPE], quota_key=f"yt:{CLIENT_ID}")
    accounts.set_declarations("local", "youtube", made_for_kids=False)
    meta_auth.save_app_settings(APP["app_id"], APP["app_secret"], APP["config_id"])
    meta_auth.save_token("local", {"user_token": USER_TOKEN, "page_id": "p1",
                                   "page_token": PAGE_TOKEN, "ig_user_id": IG_USER,
                                   "ig_username": "manish"})
    accounts.upsert("local", "instagram", external_id=IG_USER, name="manish", avatar=None,
                    scopes=[readiness.IG_PUBLISH_SCOPE], quota_key=f"ig:{IG_USER}",
                    page_id="p1")
    monkeypatch.setattr(publisher, "_REGISTRY", {})
    publish_youtube.register_all()
    publish_instagram.register_all()
    monkeypatch.setattr(youtube_api, "CHUNK", 1000)            # 3,500 bytes = 4 chunks
    clock = {"t": NOW}
    monkeypatch.setattr(posts, "_now", lambda: clock["t"])
    made = []

    def make(holder="A"):
        p = publisher.Publisher(clock=lambda: clock["t"], holder=holder)
        made.append(p)
        return p

    state = {"yt": yt, "ig": ig, "clock": clock, "profile": profile}
    state["job"] = seed_job(profile)
    state["clip"], state["version"] = seed_version(state["job"])
    state["pub"] = make()
    state["make"] = make
    yield state
    for p in made:
        p.close()


def seed_job(profile) -> str:
    job_id = uuid.uuid4().hex[:12]
    with store.connect() as conn:
        conn.execute("INSERT INTO jobs(job_id, created_at, url, status, autopilot, profile_id, "
                     "profile_snapshot_json, source_key, rights_basis, language) "
                     "VALUES(?,?,?,?,1,?,?,?,?,?)",
                     (job_id, NOW - H, "https://youtu.be/x", "done", profile["id"],
                      json.dumps(profile["settings"]), "yt:x", "confirmed", "en"))
    return job_id


def seed_version(job_id, *, clip_id=None, number=1):
    import paths

    name = f"01-clip-vertical-v{number}.mp4"
    folder = paths.output_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(os.urandom(3500))
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
            "cover": {"t": 3.2, "face": True, "chosen": "auto"}, "files": {"vertical": name},
            "probe": PROBE})
    return clip_id, version_id


def seed_post(w, platform="youtube", *, status="scheduled", slot=NOW + 3 * H, target="public",
              approval="approved", step=None, due=None, extra=None) -> str:
    pid = uuid.uuid4().hex[:16]
    meta = META[platform]
    with store.connect() as conn:
        store.insert_post(conn, {
            "id": pid, "owner_id": "local", "job_id": w["job"], "clip_id": w["clip"],
            "version_id": w["version"], "platform": platform, "method": "official",
            "status": status, "approval": approval, "holds": [], "slot_at": slot,
            "target_visibility": target, "meta": meta, "ai_meta": meta, "step": step,
            "next_action_at": due, "created_at": NOW, **(extra or {})})
    return pid


def uploaded(w, video_id, *, publish_at=NOW + 3 * H, privacy="private", extra=None) -> str:
    """A post whose video is on YouTube, private, scheduled to go public."""
    w["yt"].add_video(video_id, title="My clip", status={
        "privacyStatus": privacy, "publishAt": youtube_api.rfc3339(publish_at)})
    return seed_post(w, status="uploaded", slot=publish_at, extra={
        "platform_post_id": video_id, "url": f"https://www.youtube.com/shorts/{video_id}",
        "scheduled_publish_at": publish_at, **(extra or {})})


def post(pid) -> dict:
    with store.connect() as conn:
        return store.decode_row(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


def steps(pid) -> list[str]:
    with store.connect() as conn:
        return [r["step"] for r in conn.execute(
            "SELECT step FROM publish_attempts WHERE post_id=? ORDER BY finished_at, rowid",
            (pid,)).fetchall()]


def tick(w) -> dict:
    report = w["pub"].tick()
    w["pub"].wait()
    return report


def at(w, seconds: float) -> None:
    w["clock"]["t"] = NOW + seconds


def video_status(w, video_id) -> dict:
    return w["yt"].videos[video_id]["status"]


def _video(w):
    import paths

    return paths.output_dir() / w["job"] / "01-clip-vertical-v1.mp4"


def updates(w) -> int:
    return len(w["yt"].calls("PUT", "/youtube/v3/videos"))


def entries(kind, post_id=None) -> list[str]:
    query = {"kind": kind, **({"post_id": post_id} if post_id else {})}
    return [e["summary"] for e in activity.list_entries("local", query)]


# ── the switch ───────────────────────────────────────────────────────────

@pytest.mark.req("6.8")
def test_the_pause_switch_is_stored_logged_and_shown_with_the_posts(client):
    api = "/api/autopilot"
    first = client.post(f"{api}/pause", json={"paused": True})
    assert first.status_code == 200
    assert first.json() == {"paused": True, "changed": True, "holding": []}
    assert client.get(f"{api}/prefs").json()["paused"] is True
    assert client.get(f"{api}/posts").json()["paused"] is True      # shown where posts are listed
    again = client.post(f"{api}/pause", json={"paused": True}).json()
    assert again == {"paused": True, "changed": False}
    assert len(entries("pause.on")) == 1                           # setting it again logs nothing
    for bad in ({"paused": "yes"}, {}, {"paused": True, "extra": 1}):
        assert client.post(f"{api}/pause", json=bad).status_code == 400
    assert client.patch(f"{api}/prefs", json={"paused": False}).status_code == 400
    resumed = client.post(f"{api}/pause", json={"paused": False}).json()
    assert (resumed["paused"], resumed["changed"], resumed["replanned"]) == (False, True, [])
    assert client.get(f"{api}/posts").json()["paused"] is False
    assert entries("pause.off") == ["Resumed publishing."]


@pytest.mark.req("6.8", "15.3")
def test_each_owner_has_their_own_pause_both_ways(two_users):
    alice, bob = two_users
    api = "/api/autopilot"
    assert alice.post(f"{api}/pause", json={"paused": True}).json()["changed"] is True
    assert (alice.get(f"{api}/prefs").json()["paused"],
            bob.get(f"{api}/prefs").json()["paused"]) == (True, False)
    assert bob.post(f"{api}/pause", json={"paused": False}).json()["changed"] is False
    assert alice.get(f"{api}/posts").json()["paused"] is True
    bob.post(f"{api}/pause", json={"paused": True})
    alice.post(f"{api}/pause", json={"paused": False})
    assert (alice.get(f"{api}/prefs").json()["paused"],
            bob.get(f"{api}/prefs").json()["paused"]) == (False, True)


# ── while paused (6.9) ───────────────────────────────────────────────────

@pytest.mark.req("6.9")
def test_no_publish_starts_while_paused_and_everything_goes_on_after_resume(w):
    ig_due = seed_post(w, "instagram", slot=NOW)
    yt_private = seed_post(w, target="private", slot=NOW)
    yt_public = seed_post(w, slot=NOW + 3 * H)                 # would upload up to 24 h early
    pause.set_paused("local", True)
    for _ in range(2):
        report = tick(w)
        assert report["dispatched"] == []
    assert w["ig"].containers == {} and w["yt"].sessions == {}
    assert [post(p)["status"] for p in (ig_due, yt_private, yt_public)] == ["scheduled"] * 3
    assert steps(ig_due) == steps(yt_private) == steps(yt_public) == []

    pause.set_paused("local", False)
    tick(w)
    assert list(w["ig"].containers) == ["c1"]
    assert post(yt_private)["status"] == "private"
    assert post(yt_public)["status"] == "uploaded"


@pytest.mark.req("6.9")
def test_steps_under_way_wait_except_finishing_a_reel_already_published(w):
    from platforms.instagram_api import InstagramAPI

    with InstagramAPI("local", clock=lambda: NOW) as api:       # a Reel published earlier
        published = api.create_container("Dark mode is here #tips")
        api.upload(published, _video(w))
        media_id = api.publish(published["container_id"])
    resolving = seed_post(w, "instagram", status="publishing", step="ig_resolve", due=NOW,
                          slot=NOW - 600, extra={"step_data": {"container": published,
                                                               "media_id": media_id}})
    with InstagramAPI("local", clock=lambda: NOW) as api:       # one uploaded, not yet published
        waiting = api.create_container("Dark mode is here #tips")
        api.upload(waiting, _video(w))
    publishing = seed_post(w, "instagram", status="publishing", step="ig_publish", due=NOW,
                           slot=NOW - 300, extra={"step_data": {"container": waiting}})
    pause.set_paused("local", True)
    tick(w)
    assert post(resolving)["status"] == "live"                  # only recorded its link
    assert (post(publishing)["status"], post(publishing)["step"]) == ("publishing", "ig_publish")
    assert w["ig"].containers[waiting["container_id"]]["status_code"] == "FINISHED"
    assert len(w["ig"].media) == 1
    pause.set_paused("local", False)
    tick(w)
    assert post(publishing)["status"] == "live" and len(w["ig"].media) == 2


@pytest.mark.req("6.9", "6.10")
def test_pausing_holds_uploaded_videos_and_resuming_gives_their_times_back(w):
    later = uploaded(w, "vidA", publish_at=NOW + 3 * H)
    soon = uploaded(w, "vidB", publish_at=NOW + 300)
    made_public = uploaded(w, "vidD", publish_at=NOW + 2 * H, privacy="public")
    private_target = seed_post(w, status="private", target="private", slot=NOW - H,
                               extra={"platform_post_id": "vidP", "published_at": NOW - H})
    result = pause.set_paused("local", True)
    assert set(result["holding"]) == {later, soon, made_public}
    assert "3 uploaded YouTube videos with a publish time ahead are kept private" in \
        entries("pause.on")[0]
    tick(w)
    for pid, vid in ((later, "vidA"), (soon, "vidB")):
        p = post(pid)
        assert (p["status"], p["paused_hold"], p["scheduled_publish_at"]) == ("uploaded", 1, None)
        assert video_status(w, vid)["privacyStatus"] == "private"
        assert "publishAt" not in video_status(w, vid)
    assert (post(made_public)["status"], post(made_public)["paused_hold"]) == ("live", 0)
    assert post(private_target)["status"] == "private" and steps(private_target) == []
    sent = updates(w)
    at(w, 60)
    tick(w)
    assert updates(w) == sent                                   # held once, not again each tick

    at(w, 120)
    pause.set_paused("local", False)
    tick(w)
    assert video_status(w, "vidA")["publishAt"] == youtube_api.rfc3339(NOW + 3 * H)   # its slot
    assert video_status(w, "vidB")["publishAt"] == youtube_api.rfc3339(NOW + 120 + 900)  # +15 min
    assert [(post(p)["paused_hold"], post(p)["scheduled_publish_at"]) for p in (later, soon)] == [
        (0, NOW + 3 * H), (0, NOW + 120 + 900)]
    assert "2 uploaded YouTube videos held during the pause get their publish time back." in \
        entries("pause.off")[0]


@pytest.mark.req("6.9")
def test_pausing_in_the_middle_of_an_upload_still_holds_the_video(w, monkeypatch):
    fake, pid = w["yt"], seed_post(w, slot=NOW + 3 * H)
    original = fake._chunk

    def pause_on_first_chunk(request, sid):
        if not fake.sessions[sid]["data"]:
            pause.set_paused("local", True)                     # the user pauses mid-upload
        return original(request, sid)

    monkeypatch.setattr(fake, "_chunk", pause_on_first_chunk)
    tick(w)
    p = post(pid)
    assert (p["status"], p["platform_post_id"]) == ("uploaded", "vid_sess1")   # finished, recorded
    assert video_status(w, "vid_sess1")["publishAt"] == youtube_api.rfc3339(NOW + 3 * H)
    tick(w)
    p = post(pid)
    assert (p["status"], p["paused_hold"]) == ("uploaded", 1)
    assert "publishAt" not in video_status(w, "vid_sess1")
    assert steps(pid) == ["yt_init", "yt_upload", "yt_finalize", "yt_unschedule"]


# ── resuming (6.10) ──────────────────────────────────────────────────────

def windows(w, platform) -> dict:
    return (w["profile"]["settings"].get("windows") or profiles.default_windows())[platform]


@pytest.mark.req("6.10")
def test_resuming_gives_posts_whose_slots_passed_the_next_free_slots(w):
    locked = seed_post(w, "instagram", slot=NOW + H, extra={"user_locked": 1})
    other = seed_post(w, "instagram", slot=NOW + 5 * H)
    yt_private = seed_post(w, target="private", slot=NOW + 2 * H)
    ahead = seed_post(w, "instagram", slot=NOW + 30 * H)
    pending = seed_post(w, "instagram", slot=NOW + H, approval="pending")
    pause.set_paused("local", True)
    at(w, 6 * H)                                               # all but `ahead` passed meanwhile
    result = pause.set_paused("local", False)
    now = NOW + 6 * H
    assert set(result["replanned"]) == {locked, other, yt_private}

    ig_slots = sorted(post(p)["slot_at"] for p in (locked, other))
    candidates = planner.candidates(windows(w, "instagram"), TZ, now)
    assert ig_slots[0] == candidates[0] and ig_slots[1] in candidates
    assert ig_slots[1] - ig_slots[0] >= 4 * H                  # the minimum gap still holds
    assert post(yt_private)["slot_at"] == planner.candidates(windows(w, "youtube"), TZ, now)[0]
    assert [post(p)["status"] for p in (locked, other, yt_private)] == ["scheduled"] * 3
    assert post(locked)["user_locked"] == 0
    [moved] = entries("post.replanned", locked)
    assert "passed while publishing was paused" in moved
    assert "The time you chose for it was dropped." in moved
    assert post(ahead)["slot_at"] == NOW + 30 * H              # its slot hasn't come yet
    assert post(pending)["slot_at"] == NOW + H                 # not cleared: not re-planned
    assert "3 posts whose slot passed during the pause moved" in entries("pause.off")[0]

    tick(w)
    assert w["ig"].containers == {} and w["yt"].sessions == {}  # nothing goes out at once
    assert post(pending)["status"] == "waiting_approval"       # 6.3, as usual


@pytest.mark.req("6.10")
def test_held_videos_whose_slots_passed_are_spread_out_not_published_together(w):
    first = uploaded(w, "vidA", publish_at=NOW + H)
    second = uploaded(w, "vidB", publish_at=NOW + 2 * H)
    pause.set_paused("local", True)
    tick(w)
    assert post(first)["paused_hold"] == post(second)["paused_hold"] == 1
    at(w, 8 * H)
    pause.set_paused("local", False)
    tick(w)
    now = NOW + 8 * H
    candidates = planner.candidates(windows(w, "youtube"), TZ, now)
    times = sorted(post(p)["scheduled_publish_at"] for p in (first, second))
    assert times[0] == candidates[0] and times[1] in candidates
    assert times[1] - times[0] >= 4 * H                        # not both 15 minutes from now
    for pid, vid in ((first, "vidA"), (second, "vidB")):
        p = post(pid)
        assert (p["status"], p["paused_hold"], p["slot_at"]) == ("uploaded", 0,
                                                                 p["scheduled_publish_at"])
        assert video_status(w, vid)["publishAt"] == youtube_api.rfc3339(p["slot_at"])
    assert all("goes public at" in e for e in entries("post.rescheduled"))


@pytest.mark.req("6.10", "8.4")
def test_a_held_video_switched_to_private_during_the_pause_stays_private(w):
    pid = uploaded(w, "vidA", publish_at=NOW + 3 * H)
    pause.set_paused("local", True)
    tick(w)
    posts.patch("local", pid, {"target_visibility": "private"})
    assert post(pid)["remote_dirty"] == 1
    pause.set_paused("local", False)
    tick(w)                                                    # nothing to restore
    tick(w)                                                    # then the edit makes it private
    p = post(pid)
    assert (p["status"], p["paused_hold"], p["scheduled_publish_at"]) == ("private", 0, None)
    assert video_status(w, "vidA")["privacyStatus"] == "private"
    assert "publishAt" not in video_status(w, "vidA")


@pytest.mark.req("6.9", "6.10")
def test_a_replacement_asked_for_before_the_pause_runs_after_it(w):
    pid = uploaded(w, "vidOld", publish_at=NOW + 3 * H)
    _, newer = seed_version(w["job"], clip_id=w["clip"], number=2)
    posts.replace("local", pid)
    pause.set_paused("local", True)
    tick(w)
    p = post(pid)
    assert (p["paused_hold"], p["step"]) == (1, "yt_replace")  # held, and the replace kept
    assert "publishAt" not in video_status(w, "vidOld")
    tick(w)
    assert "vidOld" in w["yt"].videos and w["yt"].sessions == {}
    pause.set_paused("local", False)
    tick(w)
    p = post(pid)
    assert "vidOld" not in w["yt"].videos
    assert (p["status"], p["version_id"], p["platform_post_id"], p["paused_hold"]) == (
        "uploaded", newer, "vid_sess1", 0)
    assert video_status(w, "vid_sess1")["publishAt"] == youtube_api.rfc3339(NOW + 3 * H)


# ── pause and resume close together ──────────────────────────────────────

@pytest.mark.req("6.9")
def test_pausing_again_turns_a_pending_reschedule_into_a_hold(w):
    pid = uploaded(w, "vidA", publish_at=NOW + 3 * H, extra={
        "paused_hold": 1, "step": "yt_reschedule", "next_action_at": NOW + 600})
    # The reschedule's write reached YouTube, but its reply was lost: the
    # publish time is back on YouTube while the post still shows it held.
    result = pause.set_paused("local", True)
    assert pid in result["holding"]
    assert (post(pid)["step"], post(pid)["next_action_at"]) == ("yt_unschedule", NOW)
    tick(w)
    p = post(pid)
    assert (p["paused_hold"], p["step"]) == (1, None)
    assert "publishAt" not in video_status(w, "vidA")


@pytest.mark.req("6.10")
def test_a_hold_that_lands_after_the_resume_still_gets_its_time_back(w):
    pid = uploaded(w, "vidA", publish_at=NOW + 3 * H)
    pause.set_paused("local", True)
    pause.set_paused("local", False)                          # resumed before the publisher ran
    # ... and the hold already under way when the user resumed lands now.
    video_status(w, "vidA").pop("publishAt")
    with store.connect() as conn:
        store.update_post(conn, pid, {"paused_hold": 1, "scheduled_publish_at": None})
    tick(w)
    p = post(pid)
    assert (p["paused_hold"], p["scheduled_publish_at"]) == (0, NOW + 3 * H)
    assert video_status(w, "vidA")["publishAt"] == youtube_api.rfc3339(NOW + 3 * H)
    assert steps(pid) == ["yt_reschedule"]


@pytest.mark.req("6.10")
def test_resuming_drops_an_unschedule_still_waiting_to_retry(w):
    pid = uploaded(w, "vidA", publish_at=NOW + 3 * H)
    pause.set_paused("local", True)
    w["yt"].fail_next = (503, {"error": {"code": 503, "message": "Backend Error",
                                         "errors": [{"reason": "backendError"}]}})
    tick(w)
    p = post(pid)
    assert (p["step"], p["retry_count"], p["paused_hold"]) == ("yt_unschedule", 1, 0)
    pause.set_paused("local", False)
    p = post(pid)
    assert (p["step"], p["next_action_at"], p["retry_count"]) == (None, None, 0)
    at(w, 600)
    tick(w)
    assert video_status(w, "vidA")["publishAt"] == youtube_api.rfc3339(NOW + 3 * H)
    assert steps(pid) == ["yt_unschedule"]                     # the failed try only
