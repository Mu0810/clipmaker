"""Hourly maintenance (task 10.1, design §8.9, §11; Requirements 5.4, 10.6, 10.7,
11.2, 12.1, 12.5), with a fake clock, the fake YouTube and the fake Instagram."""
from __future__ import annotations

import json
import os
import uuid

import pytest

import paths
from autopilot import accounts, activity, maintenance, planner, posts, profiles, readiness
from autopilot import store
from autopilot import owner as owner_mod
from platforms import errors, google_creds, meta_auth, usage
from tests.fakes.instagram import APP, IG_USER, PAGE_TOKEN, USER_TOKEN, FakeInstagram
from tests.fakes.youtube import FakeYouTube

NOW = 1_793_590_200.0                      # Mon 2 Nov 2026, 03:30 UTC (09:00 in Kolkata)
H, D = 3600, 86400
TZ = "Asia/Kolkata"
CLIENT_ID = "123-abc.apps.googleusercontent.com"
INSIGHTS = "instagram_manage_insights"
YT_META = {"title": "My clip", "description": "D", "tags": [], "category_id": "26"}


def connect_youtube(*, connected_at=NOW - 2 * D, audience=False) -> None:
    google_creds.token_file().write_text(json.dumps({
        "token": "ya29.valid", "refresh_token": "1//refresh", "client_id": CLIENT_ID,
        "client_secret": "GOCSPX-secret", "scopes": [readiness.YT_MANAGE_SCOPE],
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": google_creds._format_expiry(NOW + 30 * D)}))
    accounts.upsert("local", "youtube", external_id="UCchannel123", name="Manish Clips",
                    avatar=None, scopes=[readiness.YT_MANAGE_SCOPE],
                    quota_key=f"yt:{CLIENT_ID}", now=connected_at)
    if audience is not None:
        accounts.set_declarations("local", "youtube", made_for_kids=audience)


def connect_instagram(*, scopes=(readiness.IG_PUBLISH_SCOPE, INSIGHTS)) -> None:
    meta_auth.save_app_settings(APP["app_id"], APP["app_secret"], APP["config_id"])
    meta_auth.save_token("local", {"user_token": USER_TOKEN, "page_id": "p1",
                                   "page_token": PAGE_TOKEN, "ig_user_id": IG_USER,
                                   "ig_username": "manish"})
    accounts.upsert("local", "instagram", external_id=IG_USER, name="manish", avatar=None,
                    scopes=list(scopes), quota_key=f"ig:{IG_USER}", page_id="p1",
                    now=NOW - 2 * D)


@pytest.fixture
def w(client, net, monkeypatch):
    owner_mod.set_prefs("local", timezone=TZ)
    profile = profiles.ensure_default("local")
    yt, ig = FakeYouTube(net), FakeInstagram(net)
    clock = {"t": NOW}
    monkeypatch.setattr(posts, "_now", lambda: clock["t"])
    made = []

    def make(holder="A"):
        m = maintenance.Maintenance(clock=lambda: clock["t"], holder=holder)
        made.append(m)
        return m

    state = {"yt": yt, "ig": ig, "clock": clock, "profile": profile, "make": make,
             "client": client}
    state["job"] = seed_job(profile)
    state["clip"], state["version"] = seed_clip(state["job"])
    yield state
    for m in made:
        m.close()


def seed_job(profile) -> str:
    job_id = uuid.uuid4().hex[:12]
    with store.connect() as conn:
        conn.execute("INSERT INTO jobs(job_id, created_at, url, status, autopilot, profile_id, "
                     "profile_snapshot_json, source_key, rights_basis, language) "
                     "VALUES(?,?,?,?,1,?,?,?,?,?)",
                     (job_id, NOW - H, "https://youtu.be/x", "done", profile["id"],
                      json.dumps({**profile["settings"], "mode": "full_auto"}), "yt:x",
                      "confirmed", "en"))
    return job_id


def seed_clip(job_id, *, idx=1, start=0.0):
    folder = paths.output_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{idx:02d}-clip-vertical.mp4").write_bytes(os.urandom(64))
    clip_id, version_id = uuid.uuid4().hex[:16], uuid.uuid4().hex[:16]
    with store.connect() as conn:
        store.insert_clip(conn, {"id": clip_id, "owner_id": "local", "job_id": job_id,
                                 "idx": idx, "src_start": start, "src_end": start + 10.0,
                                 "score": 80, "current_version_id": version_id,
                                 "meta": {"youtube": YT_META,
                                          "instagram": {"caption": "Dark mode #tips"}},
                                 "created_at": NOW})
        store.insert_clip_version(conn, {
            "id": version_id, "clip_id": clip_id, "version": 1, "spec": {}, "status": "ready",
            "files": {"vertical": f"{idx:02d}-clip-vertical.mp4"}})
    return clip_id, version_id


def seed_post(w, platform="youtube", *, status="scheduled", slot=NOW + 5 * H, method="official",
              extra=None) -> str:
    pid = uuid.uuid4().hex[:16]
    meta = YT_META if platform == "youtube" else {"caption": "Dark mode #tips"}
    with store.connect() as conn:
        store.insert_post(conn, {
            "id": pid, "owner_id": "local", "job_id": w["job"], "clip_id": w["clip"],
            "version_id": w["version"], "platform": platform, "method": method,
            "status": status, "approval": "approved", "holds": [], "slot_at": slot,
            "meta": meta, "ai_meta": meta, "created_at": NOW, **(extra or {})},
            legacy=method == "legacy")
    return pid


def live(w, platform="youtube", *, media_id, published=NOW - 2 * D, method="official",
         status="live") -> str:
    return seed_post(w, platform, status=status, slot=published, method=method, extra={
        "platform_post_id": media_id, "published_at": published})


def post(pid) -> dict:
    with store.connect() as conn:
        return store.decode_row(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


def entries(kind) -> list[str]:
    return [e["summary"] for e in activity.list_entries("local", {"kind": kind}, limit=200)]


def at(w, seconds: float) -> None:
    w["clock"]["t"] = NOW + seconds


def debug_calls(ig) -> int:
    return sum(1 for r in ig.requests if r.method == "POST" and b"debug_token" in r.content)


def stats_calls(yt) -> list:
    return [r for r in yt.calls("GET", "/youtube/v3/videos")
            if r.url.params.get("part") == "statistics"]


# ── account checks (§8.9) ────────────────────────────────────────────────

@pytest.mark.req("10.6")
def test_each_account_is_checked_at_most_once_a_day(w):
    connect_youtube()
    connect_instagram()
    a = w["make"]("A")
    report = a.tick()
    assert sorted((r["platform"], r["outcome"]) for r in report["accounts"]) == [
        ("instagram", "ok"), ("youtube", "ok")]
    assert (w["yt"].tokens_issued, debug_calls(w["ig"])) == (1, 1)
    saved = json.loads(google_creds.token_file().read_text())
    assert saved["token"] == "ya29.fresh1"                       # refreshed and saved
    for hours in (1, 12):
        at(w, hours * H)
        assert a.tick()["accounts"] == []
    at(w, 23 * H)                                                  # A's lease ran out at 14 h:
    b = w["make"]("B")                                             # a restart, say
    assert b.tick()["accounts"] == []                              # the day's checks are kept
    at(w, 24 * H)
    assert len(b.tick()["accounts"]) == 2
    assert (w["yt"].tokens_issued, debug_calls(w["ig"])) == (2, 2)


def test_only_the_lease_holder_runs_maintenance(w):
    connect_youtube()
    a, b = w["make"]("A"), w["make"]("B")
    assert a.tick()["lease"] is True
    assert b.tick() == {"lease": False}
    at(w, 2 * H + 1)                                               # A stopped renewing
    assert b.tick()["lease"] is True
    assert w["yt"].tokens_issued == 1


@pytest.mark.req("10.7")
def test_a_refused_google_sign_in_holds_the_accounts_posts_with_the_reason(w):
    connect_youtube()
    connect_instagram()
    waiting = seed_post(w)
    uploaded = seed_post(w, status="uploaded", extra={"platform_post_id": "vidU"})
    out = live(w, media_id="vidL")
    ig_post = seed_post(w, "instagram")
    w["yt"].token_error = {"error": "invalid_grant",
                           "error_description": "Token has been expired or revoked."}
    report = w["make"]().tick()
    assert {r["platform"]: r["outcome"] for r in report["accounts"]} == {
        "youtube": "needs_reconnect", "instagram": "ok"}
    reason = ("YouTube access was lost: Google refused the saved sign-in (Token has been "
              "expired or revoked.). Reconnect YouTube to keep publishing.")
    account = accounts.get("local", "youtube")
    assert (account["status"], account["status_reason"]) == ("needs_reconnect", reason)
    assert (post(waiting)["status"], post(waiting)["last_error"]) == ("needs_reconnect", reason)
    assert post(uploaded)["status"] == "uploaded" and post(out)["status"] == "live"
    assert post(ig_post)["status"] == "scheduled"
    assert entries("account.needs_reconnect") == [
        reason + " 1 unpublished post waits until you reconnect."]
    item = {i["key"]: i for i in readiness.check("local")["items"]}["youtube_connected"]
    assert item["message"] == reason                               # said once, not twice
    at(w, 24 * H)
    assert [r["platform"] for r in w["make"]("B").tick()["accounts"]] == ["instagram"]


@pytest.mark.req("10.7", "11.1")
@pytest.mark.parametrize("days,testing", [(7, True), (6, True), (8, True), (5, False), (20, False)])
def test_a_sign_in_ended_about_a_week_after_connecting_points_to_testing_mode(w, days, testing):
    connect_youtube(connected_at=NOW - days * D)
    w["yt"].token_error = {"error": "invalid_grant", "error_description": "Token has been "
                                                                          "expired or revoked."}
    w["make"]().tick()
    reason = accounts.get("local", "youtube")["status_reason"]
    assert ("\"Testing\"" in reason) is testing
    if testing:
        assert f"Google ended the sign-in {days} days after you connected" in reason


@pytest.mark.req("10.6")
def test_a_check_with_no_answer_is_tried_again_next_hour(w, monkeypatch):
    connect_youtube()
    connect_instagram()
    fake, original = w["yt"], w["yt"]._token
    down = {"left": 1}

    def token(request):
        if down["left"]:
            down["left"] -= 1
            return fake._json(503, {"error": "backend_error"})
        return original(request)

    monkeypatch.setattr(fake, "_token", token)
    w["ig"].fail_next = (500, {"error": {"message": "Please retry", "code": 2,
                                         "is_transient": True}})
    m = w["make"]()
    assert {r["platform"]: r["outcome"] for r in m.tick()["accounts"]} == {
        "youtube": "retry", "instagram": "retry"}
    assert accounts.get("local", "youtube")["status"] == "ok"
    assert accounts.get("local", "instagram")["status"] == "ok"
    at(w, H)
    assert {r["platform"]: r["outcome"] for r in m.tick()["accounts"]} == {
        "youtube": "ok", "instagram": "ok"}
    at(w, 2 * H)
    assert m.tick()["accounts"] == []


def set_debug(w, **values) -> None:
    w["ig"].debug = {**w["ig"].debug, **values}


@pytest.mark.req("10.6")
@pytest.mark.parametrize("field", ["data_access_expires_at", "expires_at"])
def test_meta_access_ending_within_7_days_asks_for_a_reconnect_first(w, field):
    connect_instagram()
    ends = NOW + 5 * D
    set_debug(w, **{field: int(ends)})
    m = w["make"]()
    assert m.tick()["accounts"] == [{"owner": "local", "platform": "instagram",
                                     "outcome": "reminded"}]
    label = planner.local_label(ends, TZ)
    assert entries("account.reconnect_reminder") == [
        f"Reconnect Instagram before {label}: Meta ends Clip Maker's access to your account "
        "then, and Instagram posts would stop until you reconnect."]
    account = accounts.get("local", "instagram")
    column = {"expires_at": "token_expires_at"}.get(field, field)
    assert account["status"] == "ok" and account[column] == ends
    at(w, H)
    m.tick()
    assert len(entries("account.reconnect_reminder")) == 1      # once a day, with the check
    at(w, 24 * H)
    m.tick()
    assert len(entries("account.reconnect_reminder")) == 2


@pytest.mark.req("10.6")
def test_meta_access_far_off_needs_nothing_and_updates_the_account(w):
    connect_instagram()
    set_debug(w, expires_at=0, data_access_expires_at=int(NOW + 60 * D))
    assert w["make"]().tick()["accounts"][0]["outcome"] == "ok"
    account = accounts.get("local", "instagram")
    assert (account["token_expires_at"], account["data_access_expires_at"]) == (None, NOW + 60 * D)
    assert entries("account.reconnect_reminder") == []


@pytest.mark.req("10.7")
@pytest.mark.parametrize("debug,says", [
    ({"is_valid": False}, "isn't valid any more"),
    ({"scopes": ["instagram_basic", INSIGHTS]}, "the instagram_content_publish permission was "
                                                "removed"),
    ({"data_access_expires_at": int(NOW - H)}, "Meta's access to your account ended on"),
])
def test_meta_access_that_is_gone_holds_the_accounts_posts(w, debug, says):
    connect_instagram()
    waiting = seed_post(w, "instagram")
    set_debug(w, **debug)
    assert w["make"]().tick()["accounts"][0]["outcome"] == "needs_reconnect"
    account = accounts.get("local", "instagram")
    assert account["status"] == "needs_reconnect"
    assert account["status_reason"].startswith("Instagram access was lost: ")
    assert says in account["status_reason"]
    assert (post(waiting)["status"], post(waiting)["last_error"]) == (
        "needs_reconnect", account["status_reason"])
    if "scopes" in debug:
        assert account["scopes"] == sorted(debug["scopes"])        # what Meta says now


# ── metrics (12.5) ───────────────────────────────────────────────────────

def snapshots(pid) -> list[dict]:
    with store.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT fetched_at, age_hours, views, likes, comments FROM post_metrics "
            "WHERE post_id=? ORDER BY fetched_at", (pid,)).fetchall()]


@pytest.mark.req("12.5", "5.4")
def test_metrics_are_fetched_once_a_day_for_30_days_after_publishing(w):
    connect_youtube()
    connect_instagram()
    for vid in ("vid1", "vid2", "vidOld", "vidYoung", "vidM"):
        w["yt"].add_video(vid)
    yt1 = live(w, media_id="vid1", published=NOW - 49 * H)
    yt2 = live(w, media_id="vid2", published=NOW - 49 * H, status="kept_private")
    reel = live(w, "instagram", media_id="m77", published=NOW - 49 * H)
    old = live(w, media_id="vidOld", published=NOW - 31 * D)          # past the 30 days
    young = live(w, media_id="vidYoung", published=NOW - 30 * 60)     # not yet an hour old
    manual = live(w, media_id="vidM", method="legacy")                 # a manual upload
    report = w["make"]().tick()
    assert sorted(report["metrics"]["fetched"]) == sorted([yt1, yt2, reel])
    [call] = stats_calls(w["yt"])                                       # one batch, both ids
    assert sorted(call.url.params["id"].split(",")) == ["vid1", "vid2"]
    assert snapshots(yt1) == [{"fetched_at": NOW, "age_hours": 49.0, "views": 120, "likes": 7,
                               "comments": 2}]
    assert snapshots(reel) == [{"fetched_at": NOW, "age_hours": 49.0, "views": 340, "likes": 12,
                                "comments": 3}]
    assert snapshots(old) == snapshots(young) == snapshots(manual) == []
    assert entries("metrics.fetched") == [
        "Fetched views, likes and comments for 2 YouTube posts and 1 Instagram post."]

    m = w["make"]("A")
    at(w, H)
    assert m.tick()["metrics"]["fetched"] == [young]                    # now an hour old
    at(w, 22 * H)
    assert m.tick()["metrics"]["fetched"] == []                         # not a day yet
    at(w, 23 * H)
    assert sorted(m.tick()["metrics"]["fetched"]) == sorted([yt1, yt2, reel])
    assert [s["age_hours"] for s in snapshots(yt1)] == [49.0, 72.0]
    # 5.4: a snapshot at 72 h, which the planner reads for the account's best hours.
    assert planner.views_at_72h(snapshots(yt1)) == 120


@pytest.mark.req("12.5")
def test_youtube_metrics_stop_at_the_daily_unit_cap_and_go_on_after_the_reset(w):
    connect_youtube()
    ids = [live(w, media_id=f"v{i:02d}") for i in range(60)]
    for i in range(60):
        w["yt"].add_video(f"v{i:02d}")
    key = f"yt-units:{CLIENT_ID}"
    assert usage.reserve(key, 8999, 10 ** 9, NOW)                      # publishing used the rest
    m = w["make"]()
    report = m.tick()["metrics"]
    assert (len(report["fetched"]), len(report["waiting"])) == (50, 10)
    assert [len(c.url.params["id"].split(",")) for c in stats_calls(w["yt"])] == [50]
    assert usage.used(key, NOW) == 9000
    assert entries("metrics.fetched") == [
        "Fetched views, likes and comments for 50 YouTube posts. YouTube's daily quota for "
        "view counts is used up, so 10 more wait until midnight Pacific time."]
    at(w, H)
    assert m.tick()["metrics"]["waiting"] == report["waiting"]
    assert len(stats_calls(w["yt"])) == 1                              # no call made at the cap
    assert len(entries("metrics.fetched")) == 1
    w["clock"]["t"] = errors.next_pacific_midnight(NOW) + 60
    assert sorted(m.tick()["metrics"]["fetched"]) == sorted(report["waiting"])
    assert all(snapshots(pid) for pid in ids)


@pytest.mark.req("12.5")
def test_instagram_insights_need_the_permission_and_skip_a_reel_that_is_gone(w, monkeypatch):
    connect_instagram(scopes=(readiness.IG_PUBLISH_SCOPE,))
    set_debug(w, scopes=["instagram_basic", "instagram_content_publish", "pages_show_list"])
    first = live(w, "instagram", media_id="m1")
    second = live(w, "instagram", media_id="m2")
    m = w["make"]()
    assert sorted(m.tick()["metrics"]["skipped"]) == sorted([first, second])
    assert w["ig"].calls("GET", "/insights") == []                     # the connect screen warns
    accounts.set_scopes("local", "instagram", [readiness.IG_PUBLISH_SCOPE, INSIGHTS])
    original, gone = w["ig"]._graph, {"left": 1}

    def graph(request, rest, form, token):
        if rest.endswith("/insights") and gone["left"]:
            gone["left"] -= 1
            return w["ig"].error(400, 100, "Object with ID does not exist")
        return original(request, rest, form, token)

    monkeypatch.setattr(w["ig"], "_graph", graph)
    at(w, H)
    assert len(m.tick()["metrics"]["fetched"]) == 1                    # the other one still is
    assert len(snapshots(first)) + len(snapshots(second)) == 1


# ── purge (12.1) ─────────────────────────────────────────────────────────

@pytest.mark.req("12.1")
def test_activity_older_than_400_days_is_purged_once_a_day(w):
    for days in (500, 401):
        activity.log("local", "system", "source.done", f"{days} days ago", at=NOW - days * D)
    activity.log("local", "system", "source.done", "399 days ago", at=NOW - 399 * D)
    m = w["make"]()
    assert m.tick()["purged"] == 2
    assert entries("source.done") == ["399 days ago"]
    assert entries("maintenance.purged") == ["Deleted 2 activity entries older than 400 days."]
    activity.log("local", "system", "source.done", "401 days ago", at=NOW + H - 401 * D)
    at(w, H)
    assert m.tick()["purged"] is None                                  # once a day
    at(w, 24 * H)
    assert m.tick()["purged"] == 1
    assert entries("maintenance.purged")[0] == "Deleted 1 activity entry older than 400 days."


# ── blocked posts (11.2) ─────────────────────────────────────────────────

@pytest.mark.req("11.2")
def test_blocked_posts_get_slots_once_a_ready_account_is_inserted(w):
    connect_instagram()
    created = posts.create_posts_for_job("local", w["job"])["created"]
    by_platform = {post(p)["platform"]: p for p in created}
    waiting, planned = by_platform["youtube"], by_platform["instagram"]
    assert (post(waiting)["status"], post(waiting)["last_error_class"]) == ("blocked", "setup")
    assert post(planned)["status"] == "scheduled"
    ahead = seed_post(w, status="blocked", slot=NOW + 6 * H,
                      extra={"user_locked": 1, "last_error_class": "setup", "last_error": "x"})
    passed = seed_post(w, status="blocked", slot=NOW - H,
                       extra={"user_locked": 1, "last_error_class": "setup", "last_error": "x"})
    too_long = seed_post(w, status="blocked", extra={
        "last_error_class": "format", "last_error": "YouTube Shorts must be at most 180 s long"})
    m = w["make"]()
    assert m.tick()["replanned"] == {}                                 # YouTube isn't ready
    connect_youtube(audience=None)                                     # connected, no audience
    at(w, H)
    assert m.tick()["replanned"] == {}
    accounts.set_declarations("local", "youtube", made_for_kids=False)
    at(w, 2 * H)
    result = m.tick()["replanned"]["local:youtube"]
    assert result == {"kept": [ahead], "replanned": sorted(result["replanned"])} and \
        set(result["replanned"]) == {waiting, passed}
    for pid in (waiting, passed):
        p = post(pid)
        assert (p["status"], p["user_locked"], p["last_error"], p["last_error_class"]) == (
            "scheduled", 0, None, None)
        assert p["slot_at"] >= NOW + 2 * H + planner.LEAD_S
    assert (post(ahead)["status"], post(ahead)["slot_at"], post(ahead)["user_locked"]) == (
        "scheduled", NOW + 6 * H, 1)
    assert post(too_long)["status"] == "blocked"                       # the user has to act
    notes = [e["summary"] for e in activity.list_entries("local", {"post_id": passed})]
    assert any("was dropped and the post moved to the next free slot" in n for n in notes)


@pytest.mark.req("11.2")
def test_confirming_the_audience_plans_waiting_posts_at_once(w):
    connect_youtube(audience=None)
    created = posts.create_posts_for_job("local", w["job"])["created"]
    [waiting] = [p for p in created if post(p)["platform"] == "youtube"]
    assert post(waiting)["status"] == "blocked"
    response = w["client"].post("/api/autopilot/youtube/declarations",
                                json={"made_for_kids": False})
    assert response.status_code == 200
    assert post(waiting)["status"] == "scheduled" and post(waiting)["slot_at"]
