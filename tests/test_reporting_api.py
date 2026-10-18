"""Counts, activity and history API (task 10.2, design §11, §14; Requirements
12.1, 12.2, 12.4, 12.5, 15.4)."""
from __future__ import annotations

import uuid

import pytest

import db
from autopilot import accounts, activity, pause, planner, posts, reporting, sources, store
from autopilot import owner as owner_mod

API = "/api/autopilot"
T = 1_793_590_200.0                      # Mon 2 Nov 2026, 03:30 UTC
H, D = 3600, 86400
TZ = "Asia/Kolkata"


def seed_post(owner="local", platform="youtube", *, status="live", approval="approved",
              method="official", job=None, published=None, slot=None, meta=None,
              extra=None) -> str:
    pid = uuid.uuid4().hex[:16]
    if meta is None:
        meta = ({"title": f"Clip {pid[:4]}", "description": "", "tags": [], "category_id": "22"}
                if platform == "youtube" else {"caption": f"Reel {pid[:4]}\nsecond line #tips"})
    with store.connect() as conn:
        store.insert_post(conn, {
            "id": pid, "owner_id": owner, "job_id": job, "platform": platform,
            "method": method, "status": status, "approval": approval, "holds": [],
            "slot_at": slot, "meta": meta, "ai_meta": meta, "published_at": published,
            "created_at": T - 10 * D, **(extra or {})}, legacy=method == "legacy")
    return pid


def snapshot(pid, fetched, views, *, likes=1, comments=0, age=24.0) -> None:
    with store.connect() as conn:
        conn.execute("INSERT INTO post_metrics(post_id, fetched_at, age_hours, views, likes, "
                     "comments) VALUES(?,?,?,?,?,?)", (pid, fetched, age, views, likes, comments))


def counts(client) -> dict:
    return client.get(f"{API}/counts").json()


# ── counts (12.2) ────────────────────────────────────────────────────────

@pytest.mark.req("12.2")
def test_counts_follow_post_statuses(client):
    assert counts(client) == {"failed": 0, "waiting_approval": 0, "needs_reconnect": 0,
                              "paused": False}
    failed = seed_post(status="failed")
    seed_post(status="failed", method="legacy")                    # a manual upload: its own list
    pending = seed_post(status="scheduled", approval="pending", slot=T + H)   # Review mode
    seed_post(status="waiting_approval", approval="pending")        # its slot passed
    seed_post("local", "instagram", status="blocked", approval="pending")     # held, and blocked
    seed_post(status="scheduled", approval="auto", slot=T + 2 * H)  # cleared in Full auto
    seed_post(status="rejected", approval="rejected")
    reconnect = seed_post("local", "instagram", status="needs_reconnect", slot=T + 3 * H)
    seed_post(status="live")
    assert counts(client) == {"failed": 1, "waiting_approval": 3, "needs_reconnect": 1,
                              "paused": False}

    posts.reject("local", failed)                                   # how a failure is cleared
    posts.approve("local", pending)
    with store.connect() as conn:
        store.update_post(conn, reconnect, {"status": "scheduled"})
    pause.set_paused("local", True)
    assert counts(client) == {"failed": 0, "waiting_approval": 2, "needs_reconnect": 0,
                              "paused": True}


# ── activity (12.1) ──────────────────────────────────────────────────────

@pytest.mark.req("12.1")
def test_the_activity_log_filters_and_pages_newest_first(client):
    owner_mod.set_prefs("local", timezone=TZ)
    job, other, pid = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12], uuid.uuid4().hex[:16]
    made = [activity.log("local", "system", kind, f"entry {i}", job_id=src, post_id=post,
                         at=T + i, detail={"n": i})
            for i, (kind, src, post) in enumerate([
                ("source.submitted", job, None), ("clips.chosen", job, None),
                ("post.planned", job, pid), ("publish.started", job, pid),
                ("publish.live", job, pid), ("source.done", other, None)])]
    page = client.get(f"{API}/activity", params={"source": job}).json()
    assert [e["summary"] for e in page["entries"]] == [f"entry {i}" for i in (4, 3, 2, 1, 0)]
    assert page["entries"][0] == {
        "id": made[4], "at": T + 4, "at_label": planner.local_label(T + 4, TZ),
        "actor": "system", "kind": "publish.live", "summary": "entry 4", "detail": {"n": 4},
        "job_id": job, "clip_id": None, "post_id": pid}
    assert (page["next_before_id"], page["timezone"]) == (None, TZ)

    def kinds(**query) -> list[str]:
        return [e["kind"] for e in client.get(f"{API}/activity", params=query).json()["entries"]]

    assert kinds(kind="publish.", source=job) == ["publish.live", "publish.started"]
    assert kinds(kind="post.planned", source=job) == ["post.planned"]
    assert kinds(post=pid) == ["publish.live", "publish.started", "post.planned"]
    assert kinds(source=other) == ["source.done"]

    pages, before = [], None
    while True:
        query = {"source": job, "limit": 2, **({"before_id": before} if before else {})}
        body = client.get(f"{API}/activity", params=query).json()
        pages.append([e["summary"] for e in body["entries"]])
        before = body["next_before_id"]
        if before is None:
            break
    assert pages == [["entry 4", "entry 3"], ["entry 2", "entry 1"], ["entry 0"]]

    for bad in ({"kind": "nonsense"}, {"kind": "nothing."}):
        response = client.get(f"{API}/activity", params=bad)
        assert response.status_code == 422 and response.json()["detail"][0]["field"] == "kind"
    assert client.get(f"{API}/activity", params={"limit": 0}).status_code == 422


# ── history (12.4, 12.5) ─────────────────────────────────────────────────

@pytest.mark.req("12.4", "12.5", "15.4")
def test_history_lists_published_and_failed_posts_with_links_and_latest_metrics(client):
    owner_mod.set_prefs("local", timezone=TZ)
    accounts.upsert("local", "youtube", external_id="UC1", name="Manish Clips",
                    avatar="https://yt3.example/a.jpg", scopes=[], quota_key="yt:c")
    accounts.upsert("local", "instagram", external_id="ig1", name="manish", avatar=None,
                    scopes=[], quota_key="ig:ig1")
    yt_live = seed_post(published=T - 3 * H, extra={
        "url": "https://www.youtube.com/shorts/v1", "platform_post_id": "v1"})
    yt_private = seed_post(status="private", published=T - 2 * H,
                           extra={"url": "https://www.youtube.com/shorts/v2"})
    kept = seed_post(status="kept_private", slot=T - D, extra={
        "scheduled_publish_at": T - D, "last_error": "YouTube kept this video private."})
    ig_failed = seed_post("local", "instagram", status="failed", slot=T - 5 * H,
                          extra={"last_error": "Gave up after 3 retries: Instagram is down."})
    ig_live = seed_post("local", "instagram", published=T - H,
                        extra={"url": "https://www.instagram.com/reel/m1/"})
    manual = seed_post(method="legacy", published=T - 4 * H, extra={"url": "https://youtu.be/m"},
                       meta={"title": "By hand", "description": "", "tags": [],
                             "privacy": "unlisted", "file": "01-a.mp4"})
    for status in ("scheduled", "uploaded", "rejected", "cancelled", "publishing",
                   "waiting_approval", "needs_reconnect", "blocked"):
        seed_post(status=status, slot=T + H)                        # not history
    snapshot(yt_live, T - 2 * H, 50, age=1.0)
    snapshot(yt_live, T - H, 120, likes=7, comments=2, age=2.0)

    body = client.get(f"{API}/history").json()
    assert [p["id"] for p in body["posts"]] == [ig_live, yt_private, yt_live, manual, ig_failed,
                                                kept]
    assert (body["next"], body["timezone"]) == (None, TZ)
    item = {p["id"]: p for p in body["posts"]}
    yt = item[yt_live]
    assert (yt["platform"], yt["status"], yt["url"], yt["publish_time"]) == (
        "youtube", "live", "https://www.youtube.com/shorts/v1", T - 3 * H)
    assert yt["publish_time_label"] == planner.local_label(T - 3 * H, TZ)
    assert yt["account"] == {"name": "Manish Clips", "avatar": "https://yt3.example/a.jpg",
                             "external_id": "UC1"}
    assert yt["metrics"] == {"fetched_at": T - H, "age_hours": 2.0, "views": 120, "likes": 7,
                             "comments": 2}                             # the latest snapshot
    assert yt["title"] == f"Clip {yt_live[:4]}" and yt["source_deleted"] is False
    assert "owner_id" not in yt and "step_data" not in yt
    ig = item[ig_live]
    assert (ig["title"], ig["account"]["name"], ig["metrics"]) == (
        f"Reel {ig_live[:4]}", "manish", None)                          # the caption's first line
    assert (item[manual]["account"], item[manual]["method"], item[manual]["title"]) == (
        None, "legacy", "By hand")
    assert item[ig_failed]["last_error"] == "Gave up after 3 retries: Instagram is down."
    assert item[kept]["publish_time"] == T - D

    def ids(**query) -> list[str]:
        return [p["id"] for p in client.get(f"{API}/history", params=query).json()["posts"]]

    assert ids(status="failed") == [ig_failed]
    assert ids(platform="instagram") == [ig_live, ig_failed]
    for field, value in (("status", "scheduled"), ("platform", "tiktok")):
        response = client.get(f"{API}/history", params={field: value})
        assert response.status_code == 422 and response.json()["detail"][0]["field"] == field


@pytest.mark.req("12.4")
def test_history_pages_never_skip_posts_listed_at_the_same_time(client):
    same = T - 2 * H
    made = [seed_post(published=T - H), seed_post(published=same),
            seed_post("local", "instagram", published=same), seed_post(published=same),
            seed_post(published=T - 3 * H)]
    seen, query = [], {"limit": 2}
    for _ in range(5):
        page = client.get(f"{API}/history", params=query).json()
        seen += [p["id"] for p in page["posts"]]
        if not page["next"]:
            break
        query = {"limit": 2, **page["next"]}
    assert len(seen) == len(set(seen)) == 5
    assert seen[0] == made[0] and seen[-1] == made[-1]
    assert seen[1:4] == sorted(made[1:4], reverse=True)              # same time: by id


@pytest.mark.req("1.9", "12.4")
def test_a_deleted_sources_published_posts_stay_in_the_history(client):
    job = uuid.uuid4().hex[:12]
    db.create(job, "https://youtu.be/x")
    db.finish(job, "done")
    pid = seed_post(job=job, published=T - H)
    snapshot(pid, T - 30 * 60, 9)
    sources.delete(job, None)
    [item] = client.get(f"{API}/history").json()["posts"]
    assert (item["id"], item["source_deleted"], item["metrics"]["views"]) == (pid, True, 9)


@pytest.mark.req("15.4")
def test_scheduled_uploads_from_before_the_upgrade_are_in_the_history(_isolated_data):
    from autopilot import migrations
    from tests.test_migrations import _build_todays_database

    _build_todays_database()
    migrations.run()
    local = {p["id"]: p for p in reporting.history("local")["posts"]}
    assert set(local) == {"s-uploading", "s-error"}
    assert local["s-error"]["last_error"] == "Upload failed: quota"
    assert local["s-uploading"]["last_error"] == "Interrupted by a restart — please re-schedule."
    [done] = reporting.history("u1")["posts"]
    assert (done["id"], done["status"], done["url"], done["method"]) == (
        "s-done", "live", "https://youtu.be/abc", "legacy")
    assert reporting.counts("local")["failed"] == 0                 # their own list, not the badge


# ── each owner's own (15.3, N3.2) ────────────────────────────────────────

@pytest.mark.req("15.3", "N3.2", "12.1", "12.2", "12.4")
def test_counts_activity_and_history_are_each_owners_own_both_ways(two_users):
    alice, bob = two_users
    owners = {n: db.get_user_by_email(f"{n}@example.com")["id"] for n in ("alice", "bob")}
    mine = {}
    for name, owner in owners.items():
        live_id = seed_post(owner, published=T - H, extra={"url": f"https://youtu.be/{name}"})
        failed_id = seed_post(owner, status="failed")
        if name == "alice":
            seed_post(owner, status="scheduled", approval="pending", slot=T + H)
        entry = activity.log(owner, "system", "publish.live", f"{name}'s post is live",
                             post_id=live_id)
        mine[name] = {"posts": {live_id, failed_id}, "live": live_id, "entry": entry}
    for me, other, client in (("alice", "bob", alice), ("bob", "alice", bob)):
        assert counts(client) == {"failed": 1, "waiting_approval": 1 if me == "alice" else 0,
                                  "needs_reconnect": 0, "paused": False}
        history = client.get(f"{API}/history").json()["posts"]
        assert {p["id"] for p in history} == mine[me]["posts"]
        entries = client.get(f"{API}/activity", params={"kind": "publish.live"}).json()["entries"]
        assert [e["id"] for e in entries] == [mine[me]["entry"]]
        theirs = client.get(f"{API}/activity", params={"post": mine[other]["live"]}).json()
        assert theirs["entries"] == []
