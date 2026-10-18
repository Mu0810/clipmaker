"""Instagram publishing steps, against the fake Instagram (task 9.3, design §8.3, §8.4)."""
from __future__ import annotations

import json
import os
import uuid

import pytest

from autopilot import accounts, activity, planner, posts, profiles, publish_instagram, publisher
from autopilot import owner as owner_mod
from autopilot import readiness, store
from platforms import meta_auth
from tests.fakes.instagram import APP, IG_USER, PAGE_TOKEN, USER_TOKEN, FakeInstagram

NOW = 1_793_590_200.0                      # Mon 2 Nov 2026, 03:30 UTC (09:00 in Kolkata)
TZ = "Asia/Kolkata"
CAPTION = "Dark mode is here #tips"
PROBE = {"duration": 10.0, "bytes": 3500, "format": "mov,mp4,m4a,3gp,3g2,mj2", "bitrate": 2_000_000,
         "width": 1080, "height": 1920, "fps": 30.0, "video_codec": "h264", "pix_fmt": "yuv420p",
         "field_order": "progressive", "video_bitrate": 1_900_000, "audio_codec": "aac",
         "sample_rate": 48000, "channels": 2, "moov_before_mdat": True, "has_edit_list": False}


class Crash(BaseException):
    """Stands in for the process dying: not an Exception, so nothing catches it."""


@pytest.fixture
def ig(_isolated_data, net, monkeypatch):
    import db
    from autopilot import migrations

    db.init()
    migrations.run()
    owner_mod.set_prefs("local", timezone=TZ)
    profile = profiles.ensure_default("local")
    fake = FakeInstagram(net)
    fake.media_time = "2026-11-02T03:31:00+0000"                 # NOW + 60 s
    meta_auth.save_app_settings(APP["app_id"], APP["app_secret"], APP["config_id"])
    meta_auth.save_token("local", {"user_token": USER_TOKEN, "page_id": "p1",
                                   "page_token": PAGE_TOKEN, "ig_user_id": IG_USER,
                                   "ig_username": "manish"})
    accounts.upsert("local", "instagram", external_id=IG_USER, name="manish", avatar=None,
                    scopes=[readiness.IG_PUBLISH_SCOPE], quota_key=f"ig:{IG_USER}",
                    page_id="p1")
    monkeypatch.setattr(publisher, "_REGISTRY", {})
    publish_instagram.register_all()
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


def seed_version(job_id, *, cover={"t": 3.2, "face": True, "chosen": "auto"}, spec_cover=None):
    import paths

    folder = paths.output_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "01-clip-vertical.mp4"
    video.write_bytes(os.urandom(3500))
    clip_id, version_id = uuid.uuid4().hex[:16], uuid.uuid4().hex[:16]
    with store.connect() as conn:
        store.insert_clip(conn, {"id": clip_id, "owner_id": "local", "job_id": job_id, "idx": 1,
                                 "src_start": 0.0, "src_end": 10.0, "score": 80,
                                 "current_version_id": version_id, "created_at": NOW})
        store.insert_clip_version(conn, {
            "id": version_id, "clip_id": clip_id, "version": 1,
            "spec": {"cover": {"t": spec_cover}}, "status": "ready", "cover": cover,
            "files": {"vertical": video.name}, "probe": PROBE})
    return clip_id, version_id, video


def seed_post(w, *, status="scheduled", slot=NOW, step=None, due=None, extra=None) -> str:
    pid = uuid.uuid4().hex[:16]
    meta = {"caption": CAPTION}
    with store.connect() as conn:
        store.insert_post(conn, {
            "id": pid, "owner_id": "local", "job_id": w["job"], "clip_id": w["clip"],
            "version_id": w["version"], "platform": "instagram", "method": "official",
            "status": status, "approval": "approved", "holds": [], "slot_at": slot,
            "meta": meta, "ai_meta": meta, "step": step, "next_action_at": due,
            "created_at": NOW, **(extra or {})})
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


def at(w, seconds: float) -> None:
    w["clock"]["t"] = NOW + seconds


def status_reads(fake, cid: str) -> int:
    return sum(1 for r in fake.calls("GET", f"/{cid}")
               if r.url.params.get("fields") == "status_code,status")


def publishes(fake) -> int:
    return len(fake.calls("POST", "/media_publish"))


def fail_once(fake, monkeypatch, suffix: str, response) -> None:
    """The next Graph call whose path ends with `suffix` gets `response`."""
    original, done = fake._graph, {"hit": False}

    def graph(request, rest, form, token):
        if not done["hit"] and rest.endswith(suffix):
            done["hit"] = True
            return response
        return original(request, rest, form, token)

    monkeypatch.setattr(fake, "_graph", graph)


@pytest.mark.req("9.1", "9.2", "9.4", "10.4", "4.8")
def test_a_reel_publishes_with_its_caption_cover_and_ai_label(ig, monkeypatch):
    fake = ig["fake"]
    pid = seed_post(ig, extra={"disclosure_ai": 1})
    seen, original = {}, fake._rupload

    def first_upload(request, token):
        seen.setdefault("step_data", post(pid)["step_data"])      # what was stored before bytes
        return original(request, token)

    monkeypatch.setattr(fake, "_rupload", first_upload)
    pub = ig["make"]()
    run(pub)
    container = seen["step_data"]["container"]
    assert (container["container_id"], container["upload_uri"]) == (
        "c1", "https://rupload.facebook.com/ig-api-upload/v26.0/c1")
    form = fake.containers["c1"]["form"]
    assert (form["media_type"], form["upload_type"], form["caption"], form["thumb_offset"],
            form["is_ai_generated"]) == ("REELS", "resumable", CAPTION, "3200", "true")
    assert len(fake.calls("GET", "/content_publishing_limit")) == 1
    p = post(pid)
    assert (p["status"], p["step"], p["next_action_at"]) == ("publishing", "ig_processing",
                                                             NOW + 60)   # first check in a minute
    assert status_reads(fake, "c1") == 0 and publishes(fake) == 0

    at(ig, 60)
    run(pub)
    p = post(pid)
    assert (p["status"], p["step"], p["platform_post_id"]) == ("live", None, "m1")
    assert p["url"] == "https://www.instagram.com/reel/m1/"
    assert p["published_at"] == NOW + 60                          # Instagram's own timestamp
    assert bytes(fake.containers["c1"]["data"]) == ig["video"].read_bytes()
    assert list(fake.media) == ["m1"] and fake.media["m1"]["caption"] == CAPTION
    assert [a["step"] for a in attempts(pid)] == ["ig_limit", "ig_container", "ig_upload",
                                                  "ig_processing", "ig_publish", "ig_resolve"]
    assert accounts.get("local", "instagram")["last_publish_at"] == NOW + 60
    assert activity.list_entries("local", {"kind": "publish.live", "post_id": pid})


@pytest.mark.req("9.2", "4.8")
def test_without_disclosure_no_ai_label_is_sent_and_a_cover_set_in_the_edit_is_used(ig):
    _, version, _ = seed_version(ig["job"], cover=None, spec_cover=1.5)
    pid = seed_post(ig, extra={"version_id": version})
    run(ig["make"]())
    form = ig["fake"].containers["c1"]["form"]
    assert form["thumb_offset"] == "1500"
    assert "is_ai_generated" not in form                          # Instagram's default: no label
    assert post(pid)["step"] == "ig_processing"


@pytest.mark.req("N2.2", "10.1")
def test_a_crash_between_container_and_publish_resumes_without_a_duplicate(ig, monkeypatch):
    fake = ig["fake"]
    pid = seed_post(ig)
    original, crashed = fake._rupload, {"done": False}

    def dies_after_the_upload(request, token):
        response = original(request, token)
        if not crashed["done"]:
            crashed["done"] = True
            raise Crash("the process died once Instagram had the whole file")
        return response

    monkeypatch.setattr(fake, "_rupload", dies_after_the_upload)
    a = ig["make"]("A")
    a.tick()
    with pytest.raises(Crash):
        a.wait()
    p = post(pid)
    assert (p["status"], p["step"]) == ("publishing", "ig_upload")     # the container was stored
    assert p["step_data"]["container"]["container_id"] == "c1"

    at(ig, 61)                                                       # A's lease runs out
    b = ig["make"]("B")
    assert run(b)["lease"] is True
    assert len(fake.calls("POST", "/ig-api-upload/v26.0/c1")) == 1   # nothing sent twice
    assert post(pid)["step"] == "ig_processing"
    at(ig, 121)
    run(b)
    p = post(pid)
    assert (p["status"], p["platform_post_id"]) == ("live", "m1")
    assert list(fake.containers) == ["c1"] and list(fake.media) == ["m1"]
    assert publishes(fake) == 1


def published_elsewhere(ig) -> dict:
    """A container created, uploaded and published, as if by a run that died
    before it could store the media id."""
    from platforms.instagram_api import InstagramAPI

    with InstagramAPI("local", clock=lambda: NOW) as api:
        container = api.create_container(CAPTION, thumb_offset_ms=3200)
        api.upload(container, ig["video"])
        api.publish(container["container_id"])
    return container


@pytest.mark.req("10.1", "9.4")
@pytest.mark.parametrize("step", ["ig_container", "ig_processing", "ig_publish"])
def test_a_resumed_step_asks_first_and_never_publishes_twice(ig, step):
    fake = ig["fake"]
    container = published_elsewhere(ig)
    pid = seed_post(ig, status="publishing", step=step, due=NOW,
                    extra={"step_data": {"container": container, "caption": CAPTION,
                                         "file": ig["video"].name}})
    run(ig["make"]())
    p = post(pid)
    assert (p["status"], p["platform_post_id"], p["url"]) == (
        "live", "m1", "https://www.instagram.com/reel/m1/")
    assert publishes(fake) == 1 and list(fake.media) == ["m1"]       # only the earlier publish
    assert list(fake.containers) == ["c1"]                           # and no new container
    lookup = [r for r in fake.calls("GET", f"/{IG_USER}/media")]
    assert lookup and lookup[-1].url.params["since"] == str(int(container["started_at"] - 60))


@pytest.mark.req("10.1", "9.4")
def test_a_lost_publish_reply_with_the_container_published_resolves_to_live(ig):
    fake = ig["fake"]
    fake.drop_publish_reply = True
    pid = seed_post(ig)
    pub = ig["make"]()
    run(pub)
    at(ig, 60)
    run(pub)
    p = post(pid)
    assert (p["status"], p["platform_post_id"], p["retry_count"]) == ("live", "m1", 0)
    assert publishes(fake) == 1 and list(fake.media) == ["m1"]
    notes = [e["summary"] for e in activity.list_entries("local", {"kind": "publish.step",
                                                                   "post_id": pid})]
    assert any("wasn't published again" in n for n in notes)
    assert [a["step"] for a in attempts(pid)][-2:] == ["ig_publish", "ig_resolve"]


@pytest.mark.req("10.1", "10.2")
def test_a_failed_publish_with_the_container_finished_publishes_once_on_the_retry(ig, monkeypatch):
    fake = ig["fake"]
    fail_once(fake, monkeypatch, "/media_publish", fake.error(
        500, 2, "Service temporarily unavailable"))
    pid = seed_post(ig)
    pub = ig["make"]()
    run(pub)
    at(ig, 60)
    run(pub)
    p = post(pid)
    assert (p["status"], p["step"], p["retry_count"], p["next_action_at"]) == (
        "retrying", "ig_publish", 1, NOW + 60 + 120)
    assert fake.media == {} and fake.containers["c1"]["status_code"] == "FINISHED"
    at(ig, 180)
    run(pub)
    p = post(pid)
    assert (p["status"], p["platform_post_id"], p["retry_count"]) == ("live", "m1", 0)
    assert publishes(fake) == 2 and list(fake.media) == ["m1"]


@pytest.mark.req("10.2", "9.2")
def test_an_interrupted_upload_resumes_from_the_bytes_instagram_has(ig):
    fake = ig["fake"]
    fake.rupload_accept = 1200                         # the connection drops after 1,200 bytes
    pid = seed_post(ig)
    pub = ig["make"]()
    run(pub)
    p = post(pid)
    assert (p["status"], p["step"], p["retry_count"]) == ("retrying", "ig_upload", 1)
    at(ig, 120)
    run(pub)
    first, second = fake.calls("POST", "/ig-api-upload/v26.0/c1")
    assert (first.headers["offset"], second.headers["offset"]) == ("0", "1200")
    assert bytes(fake.containers["c1"]["data"]) == ig["video"].read_bytes()
    assert post(pid)["step"] == "ig_processing" and list(fake.containers) == ["c1"]


@pytest.mark.req("9.3", "10.2")
def test_processing_is_checked_once_a_minute_at_most_five_times_then_a_new_container(ig):
    fake = ig["fake"]
    fake.processing_checks = 100                           # c1 never finishes processing
    pid = seed_post(ig)
    pub = ig["make"]()
    run(pub)                                               # uploaded at NOW
    at(ig, 30)
    run(pub)
    assert status_reads(fake, "c1") == 0                   # not before a minute has passed
    for k in range(1, 5):
        at(ig, 60 * k)
        run(pub)
        p = post(pid)
        assert status_reads(fake, "c1") == k
        assert (p["status"], p["step"], p["next_action_at"]) == (
            "publishing", "ig_processing", NOW + 60 * (k + 1))
    at(ig, 300)
    run(pub)
    p = post(pid)
    assert status_reads(fake, "c1") == 5
    assert (p["status"], p["step"], p["retry_count"], p["next_action_at"]) == (
        "retrying", "ig_container", 1, NOW + 300 + 120)    # a temporary failure (10.2)
    assert "after 5 checks" in p["last_error"]
    assert (p["step_data"]["container_failed"], p["step_data"]["status_checks"]) == (True, 0)

    fake.processing_checks = 0
    at(ig, 420)
    run(pub)
    p = post(pid)
    assert list(fake.containers) == ["c1", "c2"]
    assert p["step_data"]["container"]["container_id"] == "c2"
    assert p["step_data"]["replaced_containers"] == ["c1"]
    assert (p["step"], p["retry_count"]) == ("ig_processing", 1)   # the retry still counts
    at(ig, 480)
    run(pub)
    p = post(pid)
    assert (p["status"], p["platform_post_id"], p["retry_count"]) == ("live", "m1", 0)
    assert publishes(fake) == 1                            # c1 was never published


@pytest.mark.req("9.3", "10.2", "N2.2")
@pytest.mark.parametrize("outcome", ["ERROR", "EXPIRED"])
def test_a_container_error_or_expiry_gets_a_new_container_on_the_retry(ig, outcome):
    fake = ig["fake"]
    fake.outcomes = [outcome]
    pid = seed_post(ig)
    pub = ig["make"]()
    run(pub)
    at(ig, 60)
    run(pub)
    p = post(pid)
    assert (p["status"], p["step"], p["retry_count"]) == ("retrying", "ig_container", 1)
    assert f"container {outcome}" in p["last_error"] and "Instagram said: " in p["last_error"]
    at(ig, 180)
    run(pub)
    at(ig, 240)
    run(pub)
    p = post(pid)
    assert (p["status"], p["platform_post_id"]) == ("live", "m1")
    assert list(fake.containers) == ["c1", "c2"] and publishes(fake) == 1
    notes = [e["summary"] for e in activity.list_entries("local", {"kind": "publish.step",
                                                                   "post_id": pid})]
    assert any(f"container {outcome}" in n and "uploaded again" in n for n in notes)


@pytest.mark.req("10.2")
def test_a_container_that_keeps_failing_gives_up_after_three_retries(ig):
    fake = ig["fake"]
    fake.outcomes = ["ERROR"] * 10
    pid = seed_post(ig)
    pub = ig["make"]()
    for _ in range(12):
        run(pub)
        if post(pid)["status"] == "failed":
            break
        ig["clock"]["t"] += 3600
    p = post(pid)
    assert p["status"] == "failed" and p["last_error"].startswith("Gave up after 3 retries")
    assert len(fake.containers) == 4 and fake.media == {}  # the first try and three retries
    assert activity.list_entries("local", {"kind": "publish.failed", "post_id": pid})


@pytest.mark.req("9.3")
def test_media_not_ready_at_publish_goes_back_to_processing_without_a_failure(ig, monkeypatch):
    fake = ig["fake"]
    fail_once(fake, monkeypatch, "/media_publish", fake.error(
        400, 9007, "Media is not ready to be published.", 2207027))
    pid = seed_post(ig)
    pub = ig["make"]()
    run(pub)
    at(ig, 60)
    run(pub)
    p = post(pid)
    assert (p["status"], p["step"], p["retry_count"], p["next_action_at"]) == (
        "publishing", "ig_processing", 0, NOW + 120)
    assert attempts(pid)[-1]["outcome"] == "processing"
    at(ig, 120)
    run(pub)
    assert post(pid)["status"] == "live" and publishes(fake) == 2


def seed_live(ig, published_at: float) -> str:
    return seed_post(ig, status="live", slot=published_at,
                     extra={"published_at": published_at, "platform_post_id": "old"})


def instagram_windows(ig) -> dict:
    return (ig["profile"]["settings"].get("windows") or profiles.default_windows())["instagram"]


@pytest.mark.req("9.5", "10.4")
def test_a_reached_limit_moves_the_post_past_the_estimated_reset_and_says_so(ig):
    fake = ig["fake"]
    fake.limit_used = 100                                  # of 100
    for hours in (20, 10, 2):
        seed_live(ig, NOW - hours * 3600)
    pid = seed_post(ig, extra={"user_locked": 1})
    run(ig["make"]())
    p = post(pid)
    reset = NOW - 20 * 3600 + 86400                        # the oldest publish, plus 24 h
    expected = min(t for t in planner.candidates(instagram_windows(ig), TZ, NOW) if t >= reset)
    assert (p["status"], p["step"], p["slot_at"], p["user_locked"]) == (
        "scheduled", None, expected, 0)
    assert fake.containers == {}
    [entry] = activity.list_entries("local", {"kind": "publish.deferred", "post_id": pid})
    assert "limit for this account is reached (100 of 100 posts in 24 h)" in entry["summary"]
    assert planner.local_label(reset, TZ) in entry["summary"]
    assert planner.local_label(expected, TZ) in entry["summary"]
    assert "The time you chose for it was dropped." in entry["summary"]
    assert (p["last_error"], p["last_error_class"]) == (entry["summary"], "deferred")   # the queue's note


@pytest.mark.req("9.5", "10.4")
def test_a_limit_our_records_dont_explain_is_checked_again_hourly_with_one_notice(ig):
    fake = ig["fake"]
    fake.limit_used = 100
    pid = seed_post(ig)
    pub = ig["make"]()
    run(pub)
    p = post(pid)
    assert (p["status"], p["step"], p["next_action_at"], p["retry_count"]) == (
        "retrying", "ig_limit", NOW + 3600, 0)
    assert "isn't known" in p["last_error"] and fake.containers == {}
    at(ig, 3600)
    run(pub)                                               # still reached
    assert post(pid)["next_action_at"] == NOW + 7200
    assert len(activity.list_entries("local", {"kind": "publish.deferred", "post_id": pid})) == 1
    fake.limit_used = 99
    at(ig, 7200)
    run(pub)
    p = post(pid)
    assert (p["step"], p["step_data"].get("limit_wait")) == ("ig_processing", None)
    assert list(fake.containers) == ["c1"]


@pytest.mark.req("9.5", "N2.2", "10.1")
@pytest.mark.parametrize("left_as,containers", [("FINISHED", ["c1"]), ("EXPIRED", ["c1", "c2"])])
def test_metas_limit_error_at_publish_moves_the_post_and_keeps_its_container(
        ig, monkeypatch, left_as, containers):
    fake = ig["fake"]
    seed_live(ig, NOW - 23 * 3600)                         # so the limit resets in an hour
    fail_once(fake, monkeypatch, "/media_publish", fake.error(
        400, 9, "Application request limit reached", 2207042))
    pid = seed_post(ig)
    pub = ig["make"]()
    run(pub)
    fake.limit_used = 100                                  # reached between upload and publish
    at(ig, 60)
    run(pub)
    p = post(pid)
    assert (p["status"], p["step"]) == ("scheduled", None)
    assert p["slot_at"] >= NOW + 3600                      # after the estimated reset
    assert p["step_data"]["container"]["container_id"] == "c1" and fake.media == {}

    fake.limit_used = 5
    fake.containers["c1"]["status_code"] = left_as
    slot = p["slot_at"]
    ig["clock"]["t"] = slot
    run(pub)
    if left_as == "EXPIRED":                               # a new container, uploaded again
        ig["clock"]["t"] = slot + 60
        run(pub)
    p = post(pid)
    assert (p["status"], p["platform_post_id"]) == ("live", "m1")
    assert list(fake.containers) == containers and publishes(fake) == 2
    assert len(fake.calls("POST", "/ig-api-upload/v26.0/c1")) == 1


@pytest.mark.req("9.4", "10.1")
def test_a_published_reel_whose_link_cant_be_found_ends_live_not_failed(ig):
    fake = ig["fake"]
    container = published_elsewhere(ig)
    fake.media["m1"]["caption"] = "Changed by Instagram"   # the caption lookup can't match
    pid = seed_post(ig, status="publishing", step="ig_publish", due=NOW,
                    extra={"step_data": {"container": container, "caption": CAPTION}})
    pub = ig["make"]()
    run(pub)
    p = post(pid)
    assert (p["status"], p["step"], p["retry_count"]) == ("retrying", "ig_resolve", 1)
    for wait in (120, 600, 1800):                          # the three retries (10.2)
        ig["clock"]["t"] += wait
        run(pub)
    p = post(pid)
    assert (p["status"], p["url"]) == ("live", None)
    assert "link couldn't be read" in p["last_error"] and publishes(fake) == 1


def test_schema_version_3_keeps_the_cover_on_each_version(ig):
    from autopilot import migrations

    with store.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.TARGET_VERSION >= 3
        columns = {r[1] for r in conn.execute("PRAGMA table_info(clip_versions)")}
        version = store.decode_row(conn.execute("SELECT * FROM clip_versions WHERE id=?",
                                                (ig["version"],)).fetchone())
    assert "cover_json" in columns
    assert version["cover"] == {"t": 3.2, "face": True, "chosen": "auto"}


@pytest.mark.req("10.10", "10.1")
def test_a_failed_reel_tried_again_carries_on_and_publishes_once(ig):
    fake = ig["fake"]
    fake.outcomes = ["ERROR"] * 4                        # the first container and three retries
    pid = seed_post(ig)
    pub = ig["make"]()
    for _ in range(12):
        run(pub)
        if post(pid)["status"] == "failed":
            break
        ig["clock"]["t"] += 3600
    assert post(pid)["status"] == "failed" and len(fake.containers) == 4

    tried = posts.retry("local", pid)                     # Instagram processes the next one
    assert (tried["status"], tried["retry_count"]) == ("retrying", 0)
    for _ in range(12):
        run(pub)
        if post(pid)["status"] == "live":
            break
        ig["clock"]["t"] += 120
    p = post(pid)
    assert p["status"] == "live" and len(fake.containers) == 5 and len(fake.media) == 1
    assert publishes(fake) == 1
