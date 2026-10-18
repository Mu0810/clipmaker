"""The publisher loop (task 9.1, design §8.1, §8.5, §8.8)."""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from autopilot import accounts, activity, posts, profiles, publisher, readiness, store
from autopilot import owner as owner_mod
from platforms import errors, limits

KOL = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=KOL).timestamp()      # Mon 2 Nov 2026, 09:00 IST
GOOD = {"duration": 30.0, "bytes": 5_000_000, "format": "mov,mp4,m4a,3gp,3g2,mj2",
        "bitrate": 3_000_000, "width": 1080, "height": 1920, "fps": 30.0,
        "video_codec": "h264", "pix_fmt": "yuv420p", "field_order": "progressive",
        "video_bitrate": 2_800_000, "audio_codec": "aac", "sample_rate": 48000,
        "channels": 2, "moov_before_mdat": True, "has_edit_list": False}


@pytest.fixture
def world(_isolated_data, monkeypatch):
    import db
    from autopilot import migrations

    db.init()
    migrations.run()
    owner_mod.set_prefs("local", timezone="Asia/Kolkata")
    profile = profiles.ensure_default("local")
    accounts.upsert("local", "youtube", external_id="UC1", name="Chan", avatar=None,
                    scopes=[readiness.YT_MANAGE_SCOPE], quota_key="yt:client")
    accounts.set_declarations("local", "youtube", made_for_kids=False)
    accounts.upsert("local", "instagram", external_id="ig1", name="ig", avatar=None,
                    scopes=[readiness.IG_PUBLISH_SCOPE], quota_key="ig:ig1", page_id="p1")
    monkeypatch.setattr(publisher, "_REGISTRY", {})
    clock = {"t": NOW}
    monkeypatch.setattr(posts, "_now", lambda: clock["t"])
    made: list[publisher.Publisher] = []

    def make(holder="A"):
        p = publisher.Publisher(clock=lambda: clock["t"], holder=holder)
        made.append(p)
        return p

    state = {"clock": clock, "make": make, "job": seed_job(profile)}
    state["version"] = seed_version(state["job"], GOOD)
    yield state
    for p in made:
        p.close()


def seed_job(profile, *, rights="confirmed") -> str:
    job_id = uuid.uuid4().hex[:12]
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id, created_at, url, status, autopilot, profile_id, "
            "profile_snapshot_json, source_key, rights_basis) VALUES(?,?,?,?,1,?,?,?,?)",
            (job_id, NOW - 3600, "https://youtu.be/x", "done", profile["id"],
             json.dumps(profile["settings"]), "yt:x", rights))
    return job_id


def seed_version(job_id, probe, *, name="01-clip-vertical.mp4") -> str:
    import paths

    clip_id, version_id = uuid.uuid4().hex[:16], uuid.uuid4().hex[:16]
    folder = paths.output_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"rendered video")
    with store.connect() as conn:
        store.insert_clip(conn, {"id": clip_id, "owner_id": "local", "job_id": job_id, "idx": 1,
                                 "src_start": 0.0, "src_end": 30.0, "score": 80,
                                 "current_version_id": version_id, "created_at": NOW})
        store.insert_clip_version(conn, {"id": version_id, "clip_id": clip_id, "version": 1,
                                         "spec": {}, "status": "ready",
                                         "files": {"vertical": name}, "probe": probe})
    return version_id


def seed_post(w, *, platform="instagram", status="scheduled", slot=NOW, approval="approved",
              target="public", locked=0, step=None, due=None, method="official", version=None,
              holds=(), job=None, extra=None) -> str:
    pid = uuid.uuid4().hex[:16]
    with store.connect() as conn:
        store.insert_post(conn, {
            "id": pid, "owner_id": "local", "job_id": job or w["job"],
            "version_id": version or w["version"], "platform": platform, "method": method,
            "status": status, "approval": approval, "holds": list(holds), "slot_at": slot,
            "user_locked": locked, "target_visibility": target, "meta": {}, "ai_meta": {},
            "step": step, "next_action_at": due, "created_at": NOW, **(extra or {})},
            legacy=method == "legacy")
    return pid


def post(pid) -> dict:
    with store.connect() as conn:
        return store.decode_row(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


def attempts(pid) -> list[dict]:
    with store.connect() as conn:
        return store.decode_rows(conn.execute(
            "SELECT * FROM publish_attempts WHERE post_id=? ORDER BY finished_at, rowid",
            (pid,)).fetchall())


def stub(name, platform, calls, result=None, raises=None):
    """Register a step that records (post id, step) and returns or raises."""
    def handler(ctx):
        calls.append((ctx.post["id"], name))
        if raises is not None:
            raise raises(ctx) if callable(raises) else raises
        return result(ctx) if callable(result) else (result or publisher.StepResult(
            status="live"))
    publisher.register(name, platform, handler)


def run(pub, now=None) -> dict:
    report = pub.tick(now)
    pub.wait()
    return report


# ── the lease (KD5) ──────────────────────────────────────────────────────

@pytest.mark.req("N2.1")
def test_only_the_lease_holder_acts(world):
    calls = []
    stub("ig_limit", "instagram", calls)
    a, b = world["make"]("A"), world["make"]("B")
    pid = seed_post(world)
    assert run(a)["dispatched"] == [(pid, "ig_limit")]
    assert run(b) == {"lease": False, "missed": None, "interrupted": [], "released": [],
                      "dispatched": []}
    world["clock"]["t"] += 30
    assert a.tick()["lease"] is True and b.tick()["lease"] is False      # A renews in time
    world["clock"]["t"] += 61                                            # A stops ticking
    assert b.tick()["lease"] is True and a.tick()["lease"] is False      # B takes over
    assert publisher.lease_holder()["holder"] == "B"
    assert calls == [(pid, "ig_limit")]
    b.close()
    assert a.tick()["lease"] is True                                     # released at shutdown


# ── the format check (§8.8, 10.8) ────────────────────────────────────────

@pytest.mark.req("10.8")
def test_a_rendered_file_passes_both_platforms():
    assert limits.check_file(GOOD, "youtube") == [] and limits.check_file(GOOD, "instagram") == []


@pytest.mark.req("10.8")
@pytest.mark.parametrize("platform,change,expected", [
    ("youtube", {"duration": 200.0}, "at most 180 s long; this clip is 200.0 s"),
    ("youtube", {"width": 1920, "height": 1080}, "vertical or square"),
    ("youtube", {"video_codec": "vp9"}, "must be H.264 for YouTube"),
    ("youtube", {"audio_codec": "opus"}, "audio must be AAC"),
    ("instagram", {"duration": 2.0}, "3 s to 15 min long; this clip is 2.0 s"),
    ("instagram", {"duration": 901.0}, "3 s to 15 min"),
    ("instagram", {"bytes": 350_000_000}, "at most 300 MB; this file is 350 MB"),
    ("instagram", {"width": 2160, "height": 3840}, "at most 1920 px wide"),
    ("instagram", {"width": 1000, "height": 50}, "between 0.01:1 and 10:1"),
    ("instagram", {"video_codec": "vp9"}, "H.264 or HEVC"),
    ("instagram", {"field_order": "tt"}, "progressive"),
    ("instagram", {"pix_fmt": "yuv444p"}, "4:2:0"),
    ("instagram", {"fps": 15.0}, "23 to 60 frames per second"),
    ("instagram", {"video_bitrate": 30_000_000}, "at most 25 Mb/s; this video is 30.0 Mb/s"),
    ("instagram", {"sample_rate": 96000}, "at most 48 kHz"),
    ("instagram", {"channels": 6}, "mono or stereo"),
    ("instagram", {"moov_before_mdat": False}, "moov atom"),
    ("instagram", {"has_edit_list": True}, "edit lists"),
])
def test_each_broken_limit_is_named(platform, change, expected):
    problems = limits.check_file({**GOOD, **change}, platform)
    assert len(problems) == 1 and expected in problems[0], problems
    assert limits.check_file({**GOOD, "video_codec": None}, platform) == [
        "The file has no video stream."]


@pytest.mark.req("10.8")
def test_a_format_block_names_the_limit(world):
    calls = []
    stub("yt_init", "youtube", calls)
    stub("ig_limit", "instagram", calls)
    long_version = seed_version(world["job"], {**GOOD, "duration": 200.0}, name="02-long.mp4")
    short_version = seed_version(world["job"], {**GOOD, "duration": 2.0}, name="03-short.mp4")
    yt = seed_post(world, platform="youtube", version=long_version, slot=NOW + 3600)
    ig = seed_post(world, version=short_version)
    run(world["make"]())
    assert calls == []
    blocked = post(yt)
    assert blocked["status"] == "blocked" and "at most 180 s long; this clip is 200.0 s" in \
        blocked["last_error"]
    assert "3 s to 15 min long; this clip is 2.0 s" in post(ig)["last_error"]
    [check] = attempts(yt)
    assert (check["step"], check["outcome"]) == ("check", "blocked")
    assert "180 s" in check["message"]
    assert activity.list_entries("local", {"kind": "post.blocked", "post_id": yt})


def test_rights_and_the_account_are_checked_before_publishing(world):
    calls = []
    stub("ig_limit", "instagram", calls)
    stub("yt_init", "youtube", calls)
    no_rights = seed_job(profiles.ensure_default("local"), rights=None)
    unsure = seed_post(world, job=no_rights, version=seed_version(no_rights, GOOD))
    with store.connect() as conn:
        conn.execute("DELETE FROM accounts WHERE platform='youtube'")
    orphan = seed_post(world, platform="youtube", slot=NOW + 60)
    missing_file = seed_version(world["job"], GOOD, name="04-gone.mp4")
    import paths
    (paths.output_dir() / world["job"] / "04-gone.mp4").unlink()
    gone = seed_post(world, version=missing_file)
    run(world["make"]())
    assert calls == []
    assert post(unsure)["status"] == "blocked" and "rights" in post(unsure)["last_error"]
    assert post(orphan)["status"] == "needs_reconnect"
    assert "isn't connected" in post(orphan)["last_error"]
    assert post(gone)["status"] == "blocked" and "missing" in post(gone)["last_error"]


# ── missed slots (§8.5, 10.5) ────────────────────────────────────────────

@pytest.mark.req("10.5")
def test_missed_slots_publish_only_the_latest_under_two_hours(world):
    calls = []
    stub("ig_limit", "instagram", calls)
    stub("yt_init", "youtube", calls)
    store.state_set(publisher.HEARTBEAT_KEY, {"at": NOW - 6 * 3600, "holder": "old"})
    recent = seed_post(world, slot=NOW - 30 * 60)
    locked = seed_post(world, slot=NOW - 3 * 3600, locked=1)
    older = seed_post(world, slot=NOW - 5 * 3600)
    # Private target: uploads only at its (new) slot. A Public one would start
    # uploading at once if the new slot is within 24 h (8.2), which is correct
    # but not what this test is about.
    stale = seed_post(world, platform="youtube", slot=NOW - 3 * 3600, target="private")
    uploaded = seed_post(world, platform="youtube", status="uploaded", slot=NOW - 3600,
                         extra={"platform_post_id": "vid1"})
    pending = seed_post(world, slot=NOW - 3 * 3600, approval="pending")     # not cleared

    pub = world["make"]()
    report = run(pub)
    assert report["missed"]["publishing"] == [recent]
    assert set(report["missed"]["replanned"]) == {locked, older, stale}
    assert calls == [(recent, "ig_limit")]                    # only the recent one started
    for pid in (locked, older, stale):
        p = post(pid)
        assert p["status"] in ("scheduled", "unscheduled") and p["user_locked"] == 0
        assert p["slot_at"] is None or p["slot_at"] >= NOW + planner_lead()
    missed = {e["post_id"]: e["summary"]
              for e in activity.list_entries("local", {"kind": "publish.missed"})}
    assert set(missed) == {recent, locked, older, stale}
    assert "publishing now" in missed[recent]
    assert "The time you chose for it was dropped." in missed[locked]
    assert "dropped" not in missed[older]
    assert "was last running at" in missed[older]
    up = post(uploaded)                                        # YouTube publishes it itself
    assert (up["status"], up["slot_at"]) == ("uploaded", NOW - 3600)
    assert post(pending)["status"] == "waiting_approval"       # released, not re-planned
    assert pub.tick()["missed"] is None                        # the pass runs once
    assert world["make"]("B").tick()["lease"] is False         # A still holds the lease


def planner_lead() -> float:
    from autopilot import planner
    return planner.LEAD_S


# ── one step per account (10.9) ──────────────────────────────────────────

@pytest.mark.req("10.9")
def test_one_step_per_account_at_a_time(world):
    order, release, first_started, yt_done = [], threading.Event(), threading.Event(), \
        threading.Event()

    def ig(ctx):
        order.append(ctx.post["id"])
        if len(order) == 1:
            first_started.set()
            assert release.wait(5)
        return publisher.StepResult(status="live")

    def yt(ctx):
        yt_done.set()
        return publisher.StepResult(status="uploaded")

    publisher.register("ig_limit", "instagram", ig)
    publisher.register("yt_init", "youtube", yt)
    a, b = seed_post(world, slot=NOW - 20), seed_post(world, slot=NOW - 10)
    other = seed_post(world, platform="youtube", slot=NOW + 3600)
    pub = world["make"]()
    assert {w for w, _ in pub.tick()["dispatched"]} == {a, b, other}
    assert first_started.wait(5) and yt_done.wait(5)            # the other account isn't held up
    assert order == [a]                                         # b waits behind a
    assert pub.tick()["dispatched"] == []                       # nothing dispatched twice
    release.set()
    pub.wait()
    assert order == [a, b] and post(a)["status"] == post(b)["status"] == "live"
    assert post(other)["status"] == "uploaded"


# ── pause (6.8, 6.9) ─────────────────────────────────────────────────────

@pytest.mark.req("6.9")
def test_a_pause_blocks_new_starts_but_not_cleanup(world):
    calls = []
    stub("ig_limit", "instagram", calls)
    stub("ig_container", "instagram", calls)
    stub("yt_delete", "youtube", calls, result=publisher.StepResult(status="rejected"))
    owner_mod.set_prefs("local", paused=True)
    due = seed_post(world)
    retrying = seed_post(world, status="retrying", step="ig_container", due=NOW - 5)
    rejected = seed_post(world, platform="youtube", status="rejected", step="yt_delete",
                         due=NOW - 5, approval="rejected")
    pub = world["make"]()
    assert run(pub)["dispatched"] == [(rejected, "yt_delete")]
    assert calls == [(rejected, "yt_delete")] and post(rejected)["step"] is None
    assert (post(due)["status"], post(retrying)["status"]) == ("scheduled", "retrying")
    owner_mod.set_prefs("local", paused=False)
    run(pub)
    assert {c for c in calls} == {(rejected, "yt_delete"), (due, "ig_limit"),
                                  (retrying, "ig_container")}


# ── access errors (10.7) ─────────────────────────────────────────────────

@pytest.mark.req("10.7")
def test_an_access_error_moves_every_post_of_the_account(world):
    calls = []
    revoked = errors.PlatformError(errors.classify("instagram", 400, {"error": {
        "code": 190, "message": "Error validating access token"}}), 400, "TraceX")
    stub("ig_limit", "instagram", calls, raises=revoked)
    failing = seed_post(world)
    later = seed_post(world, slot=NOW + 86400)
    retrying = seed_post(world, status="retrying", step="ig_upload", due=NOW + 600)
    live = seed_post(world, status="live", slot=NOW - 86400)
    youtube = seed_post(world, platform="youtube", slot=NOW + 3 * 86400)   # outside the 24 h window
    pub = world["make"]()
    run(pub)
    for pid in (failing, later, retrying):
        assert post(pid)["status"] == "needs_reconnect", pid
        assert "Instagram access was lost" in post(pid)["last_error"]
    assert post(retrying)["step"] == "ig_upload"                # resumes after a reconnect
    assert post(live)["status"] == "live" and post(youtube)["status"] == "scheduled"
    account = accounts.get("local", "instagram")
    assert account["status"] == "needs_reconnect" and "access was lost" in account["status_reason"]
    [attempt] = attempts(failing)
    assert (attempt["step"], attempt["outcome"], attempt["http_status"],
            attempt["platform_code"], attempt["request_id"]) == ("ig_limit", "access", 400,
                                                                 "190", "TraceX")
    assert activity.list_entries("local", {"kind": "account.needs_reconnect"})
    assert pub.tick()["dispatched"] == []                        # nothing runs while disconnected


# ── retries and deferrals (10.2, 10.3) ───────────────────────────────────

def temporary():
    return errors.PlatformError(errors.classify("youtube", 503, "Service Unavailable"), 503)


@pytest.mark.req("10.2")
def test_temporary_errors_retry_after_2_10_and_30_minutes_then_fail(world):
    calls = []
    stub("yt_init", "youtube", calls, raises=lambda ctx: temporary())
    pid = seed_post(world, platform="youtube", target="private", slot=NOW - 10)
    pub = world["make"]()
    run(pub)
    for retries, delay in ((1, 120), (2, 600), (3, 1800)):
        p = post(pid)
        assert (p["status"], p["step"], p["retry_count"]) == ("retrying", "yt_init", retries)
        assert p["next_action_at"] == world["clock"]["t"] + delay
        world["clock"]["t"] = p["next_action_at"]
        run(pub)
    p = post(pid)
    assert p["status"] == "failed" and p["next_action_at"] is None
    assert p["last_error"].startswith("Gave up after 3 retries")
    assert [a["outcome"] for a in attempts(pid)] == ["retry", "retry", "retry", "failed"]
    assert len(calls) == 4
    assert activity.list_entries("local", {"kind": "publish.failed", "post_id": pid})


@pytest.mark.req("10.3")
def test_a_permanent_error_fails_at_once(world):
    stub("ig_limit", "instagram", [], raises=errors.PlatformError(errors.classify(
        "instagram", 400, {"error": {"code": 4, "error_subcode": 2207051,
                                     "message": "Spam suspected"}}), 400))
    pid = seed_post(world)
    run(world["make"]())
    p = post(pid)
    assert (p["status"], p["last_error"], p["last_error_class"]) == ("failed", "Spam suspected",
                                                                     "permanent")


@pytest.mark.req("10.2")
def test_a_quota_deferral_waits_for_pacific_midnight_without_using_a_retry(world):
    quota = errors.PlatformError(errors.classify("youtube", 403, {"error": {
        "code": 403, "errors": [{"reason": "quotaExceeded"}], "message": "Quota"}}), 403)
    stub("yt_init", "youtube", [], raises=quota)
    pid = seed_post(world, platform="youtube", target="private", slot=NOW - 10)
    run(world["make"]())
    p = post(pid)
    assert (p["status"], p["retry_count"], p["last_error_class"]) == ("retrying", 0, "deferred")
    assert p["next_action_at"] == errors.next_pacific_midnight(NOW)
    assert activity.list_entries("local", {"kind": "publish.deferred", "post_id": pid})


def test_a_new_container_retry_restarts_at_the_named_step(world):
    gone = errors.PlatformError(errors.classify_container("ERROR", 1), retry_step="ig_container")
    stub("ig_processing", "instagram", [], raises=gone)
    pid = seed_post(world, status="publishing", step="ig_processing", due=NOW - 1)
    run(world["make"]())
    p = post(pid)
    assert (p["status"], p["step"], p["next_action_at"]) == ("retrying", "ig_container", NOW + 120)


# ── chaining and the single transaction (§8.1 step 5, N2.1) ──────────────

@pytest.mark.req("N2.1", "10.1")
def test_steps_chain_and_each_commits_with_its_attempt_row(world, monkeypatch):
    calls = []
    stub("yt_init", "youtube", calls, result=lambda ctx: publisher.StepResult(
        status="publishing", next_step="yt_upload", fields={"step_data": {"session": "s1"}}))
    stub("yt_upload", "youtube", calls, result=lambda ctx: publisher.StepResult(
        status="uploaded", fields={"platform_post_id": "vid1",
                                   "step_data": {**ctx.step_data, "done": True}},
        account_fields={"last_publish_at": NOW}, activity=[("publish.uploaded", "Uploaded", None)]))
    pid = seed_post(world, platform="youtube", slot=NOW + 3600)

    real_update = store.update_post
    broken = {"on": True}

    def failing_update(conn, post_id, fields):
        if broken["on"] and fields.get("status") == "uploaded":
            raise RuntimeError("disk full")
        return real_update(conn, post_id, fields)

    monkeypatch.setattr(store, "update_post", failing_update)
    pub = world["make"]()
    pub.tick()
    with pytest.raises(RuntimeError, match="disk full"):
        pub.wait()
    p = post(pid)
    assert (p["status"], p["step"], p["step_data"]) == ("publishing", "yt_upload", {"session": "s1"})
    assert [a["step"] for a in attempts(pid)] == ["yt_init"]     # yt_upload's row rolled back
    assert not activity.list_entries("local", {"kind": "publish.uploaded"})

    broken["on"] = False
    run(pub)                                                     # resumes at yt_upload
    p = post(pid)
    assert (p["status"], p["step"], p["platform_post_id"]) == ("uploaded", None, "vid1")
    assert p["step_data"] == {"session": "s1", "done": True}
    assert [a["step"] for a in attempts(pid)] == ["yt_init", "yt_upload"]
    assert calls == [(pid, "yt_init"), (pid, "yt_upload"), (pid, "yt_upload")]
    assert accounts.get("local", "youtube")["last_publish_at"] == NOW
    assert activity.list_entries("local", {"kind": "publish.started", "post_id": pid})


@pytest.mark.req("10.7", "10.1")
def test_a_reconnect_resumes_an_interrupted_publish(world):
    interrupted = seed_post(world, platform="youtube", status="needs_reconnect",
                            step="yt_upload", due=None, extra={"step_data": {"session": "s1"}})
    verifying = seed_post(world, platform="youtube", status="uploaded", step="yt_verify",
                          due=None)
    waiting = seed_post(world, platform="youtube", status="needs_reconnect", slot=NOW + 86400)
    out = posts.restore_after_reconnect("local", "youtube")
    assert out["resumed"] == [interrupted] and out["kept"] == [waiting]
    p = post(interrupted)
    assert (p["status"], p["step"], p["next_action_at"]) == ("retrying", "yt_upload", NOW)
    assert p["step_data"] == {"session": "s1"}                   # the session isn't lost
    assert post(verifying)["next_action_at"] == NOW               # re-armed
    assert post(waiting)["status"] == "scheduled"


def test_autopilot_rules_leave_legacy_posts_alone(world, monkeypatch):
    from autopilot import legacy

    calls = []
    stub("yt_init", "youtube", calls)
    monkeypatch.setattr(legacy, "upload", lambda post, path: calls.append(post["id"]) or {})
    legacy_post = seed_post(world, platform="youtube", method="legacy", slot=NOW - 3 * 3600,
                            target="private", extra={"meta": {"file": "missing.mp4"}})
    report = run(world["make"]())
    # No missed-slot move, no official steps, no reconnect hold: task 9.5's
    # scheduled-upload runner handles it (its file is missing here, so it fails).
    assert report["missed"] == {"publishing": [], "replanned": []} and calls == []
    assert post(legacy_post)["status"] == "failed"
    assert posts.hold_for_reconnect("local", "youtube", "gone") == 0


def test_a_missing_handler_waits_without_failing_the_post(world):
    pid = seed_post(world)
    run(world["make"]())
    p = post(pid)
    assert (p["status"], p["step"], attempts(pid)) == ("scheduled", None, [])


# ── the background thread ─────────────────────────────────────────────────

def test_the_background_thread_ticks_and_stops(world):
    pub = publisher.start_background(interval=0.05)
    try:
        deadline = time.time() + 5
        while store.state_get(publisher.HEARTBEAT_KEY) is None and time.time() < deadline:
            time.sleep(0.02)
        heartbeat = store.state_get(publisher.HEARTBEAT_KEY)
        assert heartbeat and heartbeat["holder"] == pub.holder
        assert publisher.start_background() is pub                 # one per process
    finally:
        publisher.stop_background()
    assert publisher._background is None
    assert publisher.lease_holder()["expires_at"] == 0           # released at shutdown
