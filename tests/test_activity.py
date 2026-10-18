"""Activity log (task 3.1)."""
from __future__ import annotations

import pytest


@pytest.fixture
def migrated(_isolated_data):
    import db
    from autopilot import migrations

    db.init()
    migrations.run()


@pytest.mark.req("12.1")
def test_log_and_list_newest_first(migrated):
    from autopilot import activity

    first = activity.log("local", "ai", "clips.chosen", "Chose 6 clips", job_id="j1",
                         detail={"scores": [91, 80]})
    second = activity.log("local", "user", "post.approved", "Approved", post_id="p1")
    entries = activity.list_entries("local")
    assert [e["id"] for e in entries] == [second, first]
    assert entries[1]["detail"] == {"scores": [91, 80]}
    assert entries[0]["actor"] == "user" and entries[0]["post_id"] == "p1"


@pytest.mark.req("12.1")
def test_unknown_kind_and_actor_are_rejected(migrated):
    from autopilot import activity

    with pytest.raises(activity.ActivityError):
        activity.log("local", "ai", "made.up", "x")
    with pytest.raises(activity.ActivityError):
        activity.log("local", "robot", "post.approved", "x")
    with pytest.raises(activity.ActivityError):
        activity.log("local", "ai", "post.approved", "   ")


@pytest.mark.req("12.1")
def test_filters_and_paging(migrated):
    from autopilot import activity

    ids = [activity.log("local", "system", "publish.step", f"step {i}", post_id="p1")
           for i in range(5)]
    activity.log("local", "system", "post.held", "held", post_id="p2")
    assert len(activity.list_entries("local", {"kind": "publish."})) == 5
    assert len(activity.list_entries("local", {"kind": "post."})) == 1
    assert len(activity.list_entries("local", {"post_id": "p2"})) == 1
    page1 = activity.list_entries("local", {"post_id": "p1"}, limit=2)
    page2 = activity.list_entries("local", {"post_id": "p1"}, before_id=page1[-1]["id"],
                                  limit=2)
    assert [e["id"] for e in page1 + page2] == ids[::-1][:4]


@pytest.mark.req("12.1", "N4.1")
def test_entries_are_owner_scoped(migrated):
    from autopilot import activity

    activity.log("alice", "user", "post.approved", "alice did this")
    activity.log("bob", "user", "post.approved", "bob did this")
    assert [e["summary"] for e in activity.list_entries("alice")] == ["alice did this"]
    assert [e["summary"] for e in activity.list_entries("bob")] == ["bob did this"]


@pytest.mark.req("12.1")
def test_purge_keeps_400_days(migrated):
    from autopilot import activity

    now = 1_800_000_000.0
    activity.log("local", "system", "maintenance.purged", "old", at=now - 401 * 86400)
    activity.log("local", "system", "maintenance.purged", "recent", at=now - 399 * 86400)
    assert activity.purge(now=now) == 1
    assert [e["summary"] for e in activity.list_entries("local")] == ["recent"]


@pytest.mark.req("12.1")
def test_oversized_detail_is_truncated(migrated):
    from autopilot import activity

    activity.log("local", "system", "publish.step", "big", detail={"blob": "x" * 50_000})
    entry = activity.list_entries("local")[0]
    assert entry["detail"]["truncated"] is True
