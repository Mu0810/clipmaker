"""Posts and the review-queue API (task 6.4, §6.1, §6.4, §7, §14)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from autopilot import activity, posts, profiles, readiness, store
from autopilot import owner as owner_mod

KOL = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=KOL).timestamp()      # Mon 2 Nov 2026, 09:00 IST
API = "/api/autopilot/posts"
# Default windows (besttime.py): YouTube Mon 15, 18 and Tue 12, 18; Instagram Mon 11, 19.


def at(day: int, hour: int) -> float:
    return datetime(2026, 11, 2 + day, hour, 0, tzinfo=KOL).timestamp()


@pytest.fixture
def clock(monkeypatch):
    now = {"t": NOW}
    monkeypatch.setattr(posts, "_now", lambda: now["t"])
    return now


@pytest.fixture
def ready(monkeypatch):
    state = {"youtube": (True, []), "instagram": (True, [])}
    monkeypatch.setattr(readiness, "platform_ready", lambda owner, platform: state[platform])
    return state


def seed_job(owner="local", *, mode="review", platforms=("youtube", "instagram"),
             source_key="yt:abc123", **settings) -> str:
    owner_mod.set_prefs(owner, timezone="Asia/Kolkata")
    profile = profiles.ensure_default(owner)
    snapshot = {**profile["settings"], "mode": mode, "platforms": list(platforms), **settings}
    job_id = uuid.uuid4().hex[:12]
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id, created_at, url, status, user_id, autopilot, profile_id, "
            "profile_snapshot_json, source_key) VALUES(?,?,?,?,?,1,?,?,?)",
            (job_id, NOW - 60, "https://youtu.be/abc123", "done",
             None if owner == "local" else owner, profile["id"], json.dumps(snapshot),
             source_key))
    return job_id


def seed_clip(job_id, idx, owner="local", *, score=80, start=None, end=None, holds=(),
              **extra) -> str:
    clip_id = uuid.uuid4().hex[:16]
    start = idx * 60.0 if start is None else start
    meta = {"youtube": {"title": f"Clip {idx} title", "description": f"Clip {idx} description",
                        "tags": ["clip", f"n{idx}"], "category_id": "22"},
            "instagram": {"caption": f"Clip {idx} caption #clip"},
            "hook": "Watch this", "holds": list(holds), **extra}
    with store.connect() as conn:
        store.insert_clip(conn, {
            "id": clip_id, "owner_id": owner, "job_id": job_id, "idx": idx,
            "src_start": start, "src_end": start + 30 if end is None else end,
            "score": score, "score_reason": "Strong hook", "method": "ai:test",
            "meta": meta, "created_at": NOW})
    return clip_id


def queue(client, **params) -> dict:
    return {p["clip_id"] + ":" + p["platform"]: p
            for p in client.get(API, params=params).json()["posts"]}


@pytest.mark.req("6.1", "6.4", "13.3")
def test_a_held_post_stays_pending_in_full_auto(client, clock, ready):
    job = seed_job(mode="full_auto", platforms=("youtube",))
    held = seed_clip(job, 1, score=90, holds=["sensitive"],
                     sensitive_categories=["health claims"])
    clean = seed_clip(job, 2, score=80)
    assert len(posts.create_posts_for_job("local", job)["created"]) == 2
    q = queue(client)
    h, c = q[held + ":youtube"], q[clean + ":youtube"]
    assert (h["status"], h["approval"], h["cleared"]) == ("scheduled", "pending", False)
    assert h["holds"][0]["code"] == "sensitive" and "health claims" in h["holds"][0]["message"]
    assert (c["approval"], c["cleared"]) == ("auto", True)
    # The held post's slot comes: it isn't published and waits for approval.
    clock["t"] = h["slot_at"] + 1
    assert posts.release_unapproved() == [h["id"]]
    assert client.get(f"{API}/{h['id']}").json()["status"] == "waiting_approval"
    assert client.get(f"{API}/{c['id']}").json()["status"] == "scheduled"
    held_log = activity.list_entries("local", {"kind": "post.held"})
    assert held_log and held_log[0]["post_id"] == h["id"]


@pytest.mark.req("6.2", "6.3", "5.6")
def test_a_review_post_past_its_slot_waits_then_gets_the_next_free_slot(client, clock, ready):
    job = seed_job(platforms=("youtube",))
    seed_clip(job, 1)
    [pid] = posts.create_posts_for_job("local", job)["created"]
    post = client.get(f"{API}/{pid}").json()
    assert (post["status"], post["approval"], post["slot_at"]) == ("scheduled", "pending", at(0, 15))
    assert post["slot_basis"] == "defaults"
    assert post["slot_label"] == "Mon 2 Nov 2026, 15:00 IST (Asia/Kolkata)"
    clock["t"] = at(0, 16)
    assert posts.release_unapproved() == [pid]
    waiting = client.get(f"{API}/{pid}").json()
    assert (waiting["status"], waiting["slot_at"], waiting["cleared"]) == ("waiting_approval",
                                                                           None, False)
    approved = client.post(f"{API}/{pid}/approve")
    assert approved.status_code == 200
    body = approved.json()
    # 18:00 is the next window at least 30 minutes away.
    assert (body["status"], body["approval"], body["slot_at"]) == ("scheduled", "approved",
                                                                   at(0, 18))
    assert body["cleared"] is True
    kinds = {e["kind"] for e in activity.list_entries("local", {"post_id": pid})}
    assert {"post.planned", "post.waiting_approval", "post.approved"} <= kinds
    again = client.post(f"{API}/{pid}/approve")
    assert again.status_code == 409 and again.json()["detail"] == "Already approved."


@pytest.mark.req("6.5", "6.7", "4.3")
def test_reset_restores_the_ai_text_and_a_limit_violation_names_the_field(client, clock, ready):
    job = seed_job()
    clip = seed_clip(job, 1)
    posts.create_posts_for_job("local", job)
    q = queue(client)
    yt, ig = q[clip + ":youtube"]["id"], q[clip + ":instagram"]["id"]

    edited = client.patch(f"{API}/{yt}", json={"meta": {"title": "My own title"}})
    assert edited.status_code == 200
    post = edited.json()["post"]
    assert (post["meta"]["title"], post["ai_meta"]["title"]) == ("My own title", "Clip 1 title")
    assert post["edited_fields"] == ["title"]
    reset = client.post(f"{API}/{yt}/reset", json={"field": "title"}).json()
    assert reset["meta"]["title"] == "Clip 1 title" and reset["edited_fields"] == []

    too_long = client.patch(f"{API}/{yt}", json={"meta": {"title": "x" * 101}})
    assert too_long.status_code == 422
    assert [p["field"] for p in too_long.json()["detail"]] == ["meta.title"]
    tags = client.patch(f"{API}/{yt}", json={"meta": {"tags": ["a" * 60] * 9}})  # 548 as counted
    assert tags.status_code == 422 and tags.json()["detail"][0]["field"] == "meta.tags"
    caption = client.patch(f"{API}/{ig}", json={"meta": {"caption": " ".join(
        f"#t{i}" for i in range(31))}})
    assert caption.status_code == 422 and caption.json()["detail"][0]["field"] == "meta.caption"
    status = client.patch(f"{API}/{yt}", json={"status": "live"})
    assert status.status_code == 422 and status.json()["detail"][0]["field"] == "status"
    assert client.get(f"{API}/{yt}").json()["meta"]["title"] == "Clip 1 title"   # nothing stored
    unknown = client.post(f"{API}/{ig}/reset", json={"field": "title"})
    assert unknown.status_code == 422 and unknown.json()["detail"][0]["field"] == "field"


@pytest.mark.req("6.6")
def test_a_bulk_action_reports_each_post(client, clock, ready):
    job = seed_job()
    clip = seed_clip(job, 1)
    posts.create_posts_for_job("local", job)
    q = queue(client)
    yt, ig = q[clip + ":youtube"]["id"], q[clip + ":instagram"]["id"]
    assert client.post(f"{API}/{ig}/reject").status_code == 200
    r = client.post(f"{API}/bulk", json={"ids": [yt, ig, "nope"], "action": "approve"})
    results = {x["id"]: x for x in r.json()["results"]}
    assert results[yt]["ok"] is True and results[yt]["status"] == "scheduled"
    assert (results[ig]["ok"], results[ig]["error"], results[ig]["status"]) == (
        False, "You already decided not to post it.", "rejected")
    assert results["nope"] == {"id": "nope", "ok": False, "error": "Not found."}
    assert client.get(f"{API}/{yt}").json()["approval"] == "approved"
    bad = client.post(f"{API}/bulk", json={"ids": [yt], "action": "publish"})
    assert bad.status_code == 422 and bad.json()["detail"][0]["field"] == "action"


@pytest.mark.req("5.5", "14.4")
def test_a_moved_slot_is_locked_and_a_replan_leaves_it_alone(client, clock, ready):
    job = seed_job(platforms=("youtube",))
    a, b = seed_clip(job, 1, score=90), seed_clip(job, 2, score=80)
    posts.create_posts_for_job("local", job)
    q = queue(client)
    pa, pb = q[a + ":youtube"], q[b + ":youtube"]
    assert (pa["slot_at"], pb["slot_at"]) == (at(0, 15), at(1, 12))   # 18:00 is within 4 h
    moved = client.patch(f"{API}/{pb['id']}", json={"slot_at": at(0, 17)}).json()
    post = moved["post"]
    assert (post["slot_at"], post["user_locked"], post["slot_basis"]) == (at(0, 17), 1, "user")
    assert any(w.startswith("Less than 4 h") for w in moved["warnings"])

    every_day_20 = {str(d): [20] for d in range(7)}
    profile = profiles.ensure_default("local")
    profiles.update("local", profile["id"], settings_patch={
        "windows": {"youtube": every_day_20, "instagram": every_day_20}})
    r = client.post(f"/api/autopilot/profiles/{profile['id']}/replan")
    assert r.json() == {"replanned": 1, "scheduled": 1, "unscheduled": 0}
    after = queue(client)
    assert after[b + ":youtube"]["slot_at"] == at(0, 17)              # the user's slot stays
    assert after[a + ":youtube"]["slot_at"] == at(1, 20)              # Mon 20:00 is 3 h after it
    assert after[a + ":youtube"]["slot_basis"] == "defaults"
    removed = client.patch(f"{API}/{pa['id']}", json={"slot_at": None}).json()["post"]
    assert (removed["status"], removed["slot_at"], removed["user_locked"]) == ("unscheduled",
                                                                               None, 1)
    past = client.patch(f"{API}/{pb['id']}", json={"slot_at": NOW - 60})
    assert past.status_code == 422 and past.json()["detail"][0]["field"] == "slot_at"


@pytest.mark.req("5.5", "5.6", "6.5")
def test_the_queue_gets_each_posts_source_account_local_slot_and_lasting_warnings(
        client, clock, ready):
    job = seed_job(platforms=("youtube", "instagram"), min_gap_hours=4, daily_max=2)
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET source_title='Creator Economy Podcast' WHERE job_id=?",
                     (job,))
        conn.execute("INSERT INTO accounts(id, owner_id, platform, external_id, name, status) "
                     "VALUES('acc-yt', 'local', 'youtube', 'UC1', 'Manish Clips', 'ok')")
    a, b = seed_clip(job, 1, score=90), seed_clip(job, 2, score=80)
    posts.create_posts_for_job("local", job)
    q = queue(client)
    pa, pb = q[a + ":youtube"], q[b + ":youtube"]
    assert (pa["source_title"], pa["account_name"]) == ("Creator Economy Podcast", "Manish Clips")
    assert q[a + ":instagram"]["account_name"] is None                # not connected
    assert (pa["slot_day"], pa["slot_time"], pa["slot_zone"]) == ("2026-11-02", "15:00", "IST")
    assert pa["posting"] == {"min_gap_hours": 4.0, "daily_max": 2}
    assert pa["warnings"] == [] and pb["warnings"] == []              # the planner's slots

    client.patch(f"{API}/{pb['id']}", json={"slot_at": at(0, 17)})   # 2 h after clip 1's
    again = queue(client)                                             # read afresh, as on reload
    assert again[b + ":youtube"]["warnings"] == [
        "Less than 4 h from another YouTube post at Mon 2 Nov 2026, 15:00 IST (Asia/Kolkata)."]
    assert again[a + ":youtube"]["warnings"] == []                    # the AI's slot isn't flagged
    unslotted = client.patch(f"{API}/{pa['id']}", json={"slot_at": None}).json()["post"]
    assert (unslotted["slot_day"], unslotted["slot_time"], unslotted["warnings"]) == (None, None, [])


@pytest.mark.req("13.4")
def test_a_clip_already_posted_to_the_account_is_skipped(client, clock, ready):
    old_job = seed_job(platforms=("youtube",), source_key="yt:same")
    seed_clip(old_job, 1, start=100, end=140)
    [old_post] = posts.create_posts_for_job("local", old_job)["created"]
    with store.connect() as conn:
        store.update_post(conn, old_post, {"status": "live", "published_at": NOW - 86400})

    new_job = seed_job(source_key="yt:same")
    overlapping = seed_clip(new_job, 1, start=110, end=150)   # 30 s of 40 shared: over half
    neighbour = seed_clip(new_job, 2, start=125, end=165)     # 15 s of 40 shared: not over half
    out = posts.create_posts_for_job("local", new_job)
    assert [(s["clip_id"], s["platform"]) for s in out["skipped"]] == [(overlapping, "youtube")]
    assert sorted(queue(client, source=new_job)) == sorted(
        [overlapping + ":instagram", neighbour + ":youtube", neighbour + ":instagram"])
    skipped = activity.list_entries("local", {"kind": "post.skipped"})
    assert len(skipped) == 1 and skipped[0]["summary"].startswith("Skipped: already posted")
    again = client.post(API, json={"clip_id": overlapping, "platform": "youtube"})
    assert again.status_code == 409 and "already posted" in again.json()["detail"]


@pytest.mark.req("5.1", "11.2")
def test_a_post_waits_blocked_while_its_platform_is_not_ready(client, clock, ready):
    ready["instagram"] = (False, ["Connect Instagram through Meta."])
    job = seed_job()
    good = seed_clip(job, 1, score=90)
    seed_clip(job, 2, score=40)                                 # under the minimum score of 60
    assert len(posts.create_posts_for_job("local", job)["created"]) == 2
    q = queue(client)
    assert set(q) == {good + ":youtube", good + ":instagram"}
    ig = q[good + ":instagram"]
    assert (ig["status"], ig["slot_at"], ig["last_error"]) == ("blocked", None,
                                                               "Connect Instagram through Meta.")
    assert q[good + ":youtube"]["status"] == "scheduled"


@pytest.mark.req("6.5")
def test_reject_frees_the_slot_and_marks_an_upload_for_deletion(client, clock, ready):
    job = seed_job(platforms=("youtube",))
    a, b = seed_clip(job, 1, score=90), seed_clip(job, 2, score=80)
    posts.create_posts_for_job("local", job)
    q = queue(client)
    first, second = q[a + ":youtube"], q[b + ":youtube"]
    with store.connect() as conn:
        store.update_post(conn, first["id"], {"status": "uploaded", "platform_post_id": "vid1",
                                              "scheduled_publish_at": first["slot_at"]})
    gone = client.post(f"{API}/{first['id']}/reject").json()
    assert (gone["status"], gone["approval"], gone["slot_at"], gone["step"]) == (
        "rejected", "rejected", None, "yt_delete")
    plain = client.post(f"{API}/{second['id']}/reject").json()
    assert (plain["status"], plain["step"]) == ("rejected", None)
    again = client.post(f"{API}/{second['id']}/reject")
    assert again.status_code == 409 and again.json()["detail"] == "You already decided not to post it."


@pytest.mark.req("6.5", "6.8", "5.6", "N3.2")
def test_the_queue_filters_and_carries_the_limits_and_pause_state(client, clock, ready):
    job = seed_job()
    seed_clip(job, 1)
    posts.create_posts_for_job("local", job)
    data = client.get(API, params={"platform": "instagram"}).json()
    assert [p["platform"] for p in data["posts"]] == ["instagram"]
    assert data["limits"]["youtube"]["title_chars"] == 100
    assert data["limits"]["instagram"]["hashtags"] == 30
    assert (data["paused"], data["timezone"]) == (False, "Asia/Kolkata")
    assert "owner_id" not in data["posts"][0] and "step_data" not in data["posts"][0]
    morning = client.get(API, params={"from": at(0, 0), "to": at(0, 12)}).json()["posts"]
    assert [p["platform"] for p in morning] == ["instagram"]    # Instagram 11:00, YouTube 15:00
    assert client.get(API, params={"status": "nonsense"}).status_code == 422


@pytest.mark.req("5.5")
def test_adding_a_post_with_or_without_a_slot(client, clock, ready):
    job = seed_job(platforms=("youtube",))
    clip = seed_clip(job, 1)
    posts.create_posts_for_job("local", job)
    dup = client.post(API, json={"clip_id": clip, "platform": "youtube"})
    assert dup.status_code == 409 and "already has" in dup.json()["detail"]
    r = client.post(API, json={"clip_id": clip, "platform": "instagram", "slot_at": at(1, 9)})
    assert r.status_code == 201
    added = r.json()["post"]
    assert (added["status"], added["slot_at"], added["user_locked"], added["slot_basis"]) == (
        "scheduled", at(1, 9), 1, "user")
    assert added["meta"]["caption"] == "Clip 1 caption #clip" and added["approval"] == "pending"
    other = seed_clip(job, 2)
    planned = client.post(API, json={"clip_id": other, "platform": "instagram"}).json()["post"]
    assert (planned["slot_at"], planned["user_locked"]) == (at(0, 11), 0)
    assert client.post(API, json={"clip_id": "nope", "platform": "youtube"}).status_code == 404
    bad = client.post(API, json={"clip_id": other, "platform": "youtube", "slot_at": NOW - 1})
    assert bad.status_code == 422 and bad.json()["detail"][0]["field"] == "slot_at"


@pytest.mark.req("N4.1", "N4.2", "N3.2")
def test_owners_are_isolated_both_ways(two_users, clock, ready):
    import db

    alice, bob = two_users
    ids = {name: db.get_user_by_email(f"{name}@example.com")["id"] for name in ("alice", "bob")}
    mine = {}
    for name, owner in ids.items():
        job = seed_job(owner, platforms=("youtube",))
        clip = seed_clip(job, 1, owner)
        [mine[name]] = posts.create_posts_for_job(owner, job)["created"]
        mine[name + "_clip"] = clip
    for me, other, client in (("alice", "bob", alice), ("bob", "alice", bob)):
        theirs = mine[other]
        assert [p["id"] for p in client.get(API).json()["posts"]] == [mine[me]]
        assert client.patch(f"{API}/{mine[me]}",
                            json={"meta": {"title": f"{me}'s title"}}).status_code == 200
        assert client.get(f"{API}/{theirs}").status_code == 404
        assert client.patch(f"{API}/{theirs}", json={"meta": {"title": "x"}}).status_code == 404
        for action in ("approve", "reject", "retry"):
            assert client.post(f"{API}/{theirs}/{action}").status_code == 404
        assert client.post(f"{API}/{theirs}/reset", json={"field": "title"}).status_code == 404
        assert client.post(f"{API}/bulk", json={"ids": [theirs], "action": "reject"}).json() == {
            "results": [{"id": theirs, "ok": False, "error": "Not found."}]}
        assert client.post(API, json={"clip_id": mine[other + "_clip"],
                                      "platform": "instagram"}).status_code == 404
    for name, client in (("alice", alice), ("bob", bob)):
        post = client.get(f"{API}/{mine[name]}").json()
        assert (post["meta"]["title"], post["status"]) == (f"{name}'s title", "scheduled")


def failed(pid: str, **fields) -> None:
    """The post was approved, then its publish failed (10.2)."""
    with store.connect() as conn:
        store.update_post(conn, pid, {
            "approval": "approved", "status": "failed", "retry_count": 3,
            "last_error": "Gave up after 3 retries: Service Unavailable",
            "last_error_class": "temporary", **fields})


@pytest.mark.req("10.10")
def test_a_failed_post_with_nothing_sent_goes_back_into_the_plan(client, clock, ready):
    job = seed_job(platforms=("youtube",))
    seed_clip(job, 1)
    [pid] = posts.create_posts_for_job("local", job)["created"]
    failed(pid)                                          # at 09:00, before its 15:00 slot
    assert client.post(f"{API}/{pid}/approve").json()["detail"] == (
        "Publishing failed; choose Try again, or Don't post.")

    reply = client.post(f"{API}/{pid}/retry")
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert (body["status"], body["approval"], body["slot_at"], body["cleared"]) == (
        "scheduled", "approved", at(0, 15), True)       # its own time, still ahead
    assert (body["retry_count"], body["step"], body["last_error"]) == (0, None, None)
    [entry] = activity.list_entries("local", {"kind": "post.retried"})
    assert (entry["actor"], entry["post_id"]) == ("user", pid)
    assert entry["summary"] == ("Tried the YouTube post for clip 1 again; it's planned for "
                                "Mon 2 Nov 2026, 15:00 IST (Asia/Kolkata).")

    again = client.post(f"{API}/{pid}/retry")
    assert (again.status_code, again.json()["detail"]) == (
        409, "It isn't failed any more, so there's nothing to try again.")


@pytest.mark.req("10.10", "5.5")
def test_a_failed_post_whose_time_passed_gets_the_next_free_slot(client, clock, ready):
    job = seed_job(platforms=("youtube",))
    seed_clip(job, 1)
    [pid] = posts.create_posts_for_job("local", job)["created"]
    failed(pid, user_locked=1)                          # the user had chosen 15:00
    clock["t"] = at(0, 16)
    body = client.post(f"{API}/{pid}/retry").json()
    # 18:00 is the next window at least 30 minutes away, as for an approval.
    assert (body["status"], body["slot_at"], body["user_locked"]) == ("scheduled", at(0, 18), 0)
    kinds = [e["kind"] for e in activity.list_entries("local", {"post_id": pid})][:2]
    assert kinds == ["post.planned", "post.retried"]


@pytest.mark.req("10.10", "10.1")
def test_a_failed_post_the_platform_holds_part_of_carries_on_from_its_step(client, clock, ready):
    job = seed_job(platforms=("youtube",))
    seed_clip(job, 1)
    [pid] = posts.create_posts_for_job("local", job)["created"]
    session = {"uri": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=s1"}
    failed(pid, step="yt_upload", step_data={"session": session, "file": "01-clip.mp4"})
    body = client.post(f"{API}/{pid}/retry").json()
    assert (body["status"], body["step"], body["next_action_at"], body["retry_count"]) == (
        "retrying", "yt_upload", NOW, 0)               # the publisher resumes that session
    assert body["slot_at"] == at(0, 15) and "step_data" not in body
    with store.connect() as conn:
        assert store.get_post(conn, "local", pid)["step_data"]["session"] == session
    [entry] = activity.list_entries("local", {"kind": "post.retried"})
    assert entry["summary"] == ("Tried the YouTube post for clip 1 again; it carries on from "
                                "where publishing stopped.")


@pytest.mark.req("10.10")
def test_only_a_failed_autopilot_post_can_be_tried_again(client, clock, ready):
    job = seed_job(platforms=("youtube",))
    seed_clip(job, 1)
    [pid] = posts.create_posts_for_job("local", job)["created"]
    reply = client.post(f"{API}/{pid}/retry")                   # scheduled, not failed
    assert (reply.status_code, reply.json()["detail"]) == (
        409, "It isn't failed any more, so there's nothing to try again.")
    legacy_id = uuid.uuid4().hex[:16]
    with store.connect() as conn:
        store.insert_post(conn, {"id": legacy_id, "owner_id": "local", "job_id": job,
                                 "platform": "youtube", "method": "legacy", "status": "failed",
                                 "approval": "approved", "holds": [], "slot_at": NOW - 60,
                                 "meta": {"title": "Scheduled by hand"}, "ai_meta": {},
                                 "last_error": "Interrupted by a restart — please re-schedule.",
                                 "created_at": NOW}, legacy=True)
    reply = client.post(f"{API}/{legacy_id}/retry")
    assert (reply.status_code, reply.json()["detail"]) == (409, posts.LEGACY_ONCE)
    with store.connect() as conn:
        assert store.get_post(conn, "local", legacy_id)["status"] == "failed"
    assert client.post(f"{API}/ffffffffffffffff/retry").status_code == 404
