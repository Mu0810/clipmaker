"""Readiness checklist (task 3.3)."""
from __future__ import annotations

import json
import time

import pytest

YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube.readonly",
             "https://www.googleapis.com/auth/youtube"]


def _account(platform, owner="local", **fields):
    from autopilot import store

    row = {"id": f"acc-{platform}-{owner}", "owner_id": owner, "platform": platform,
           "name": "Channel" if platform == "youtube" else "creator",
           "status": "ok", "scopes_json": json.dumps(fields.pop("scopes", [])),
           "connected_at": time.time(), **fields}
    cols = ", ".join(row)
    with store.connect() as conn:
        conn.execute(f"INSERT OR REPLACE INTO accounts({cols}) VALUES"
                     f"({', '.join('?' * len(row))})", tuple(row.values()))


def _items(client):
    data = client.get("/api/autopilot/readiness").json()
    return {i["key"]: i for i in data["items"]}, data["platforms"]


@pytest.mark.req("11.1", "11.2")
def test_nothing_connected_blocks_both_platforms(client):
    items, platforms = _items(client)
    assert not items["youtube_connected"]["ok"] and not items["instagram_connected"]["ok"]
    assert platforms == {
        "youtube": {"ready": False, "reasons": [items["youtube_connected"]["message"],
                                                items["youtube_audience"]["message"]]},
        "instagram": {"ready": False, "reasons": [items["instagram_connected"]["message"]]}}


@pytest.mark.req("11.1", "11.2")
def test_youtube_needs_manage_scope_and_audience(client):
    _account("youtube", scopes=YT_SCOPES[:2])          # manual-upload scopes only
    items, platforms = _items(client)
    assert not items["youtube_connected"]["ok"]
    assert "manual uploads only" in items["youtube_connected"]["message"]

    _account("youtube", scopes=YT_SCOPES)
    items, platforms = _items(client)
    assert items["youtube_connected"]["ok"] and not items["youtube_audience"]["ok"]
    assert platforms["youtube"]["ready"] is False

    _account("youtube", scopes=YT_SCOPES, made_for_kids=0)
    items, platforms = _items(client)
    assert items["youtube_audience"]["ok"] and platforms["youtube"]["ready"] is True


@pytest.mark.req("11.1", "11.2")
def test_instagram_needs_publish_permission(client):
    _account("instagram", scopes=["instagram_basic"])
    items, platforms = _items(client)
    assert "permission to publish" in items["instagram_connected"]["message"]
    _account("instagram", scopes=["instagram_basic", "instagram_content_publish"])
    assert _items(client)[1]["instagram"]["ready"] is True


@pytest.mark.req("10.7", "11.2")
def test_needs_reconnect_is_not_ready(client):
    _account("youtube", scopes=YT_SCOPES, made_for_kids=0, status="needs_reconnect",
             status_reason="Access was removed in your Google account.")
    items, platforms = _items(client)
    assert platforms["youtube"]["ready"] is False
    assert "Access was removed" in items["youtube_connected"]["message"]


@pytest.mark.req("11.3")
def test_advisory_items_never_block(client, monkeypatch):
    import llm
    from autopilot import readiness

    monkeypatch.setattr(llm, "is_configured", lambda: False)
    monkeypatch.setattr(readiness, "_shaping_available", lambda: False)
    monkeypatch.setattr(readiness, "_face_detector_available", lambda: False)
    _account("youtube", scopes=YT_SCOPES, made_for_kids=0)
    items, platforms = _items(client)
    assert platforms["youtube"]["ready"] is True
    assert "stay private" in items["youtube_audit"]["message"]
    assert "every 7 days" in items["oauth_production"]["message"]
    assert "waits for your approval" in items["ai_engine"]["message"]
    assert "wait for your approval" in items["text_shaping"]["message"]
    assert "Fit layout" in items["face_detector"]["message"]
    assert all(items[k]["kind"] == "advisory" for k in
               ("ai_engine", "oauth_production", "youtube_audit", "text_shaping",
                "face_detector"))


@pytest.mark.req("11.1")
def test_declarations_turn_advisories_green(client):
    _account("youtube", scopes=YT_SCOPES, made_for_kids=0, audit_declared=1,
             oauth_production_declared=1)
    items, _ = _items(client)
    assert items["youtube_audit"]["ok"] and items["oauth_production"]["ok"]


@pytest.mark.req("11.1", "5.6")
def test_timezone_item_follows_prefs(client):
    assert _items(client)[0]["timezone"]["ok"] is False
    client.patch("/api/autopilot/prefs", json={"timezone": "Asia/Kolkata"})
    item = _items(client)[0]["timezone"]
    assert item["ok"] and "Asia/Kolkata" in item["message"]


@pytest.mark.req("11.2", "N4.1")
def test_platform_ready_is_per_owner(client):
    from autopilot import readiness

    _account("instagram", owner="alice", scopes=["instagram_content_publish"])
    assert readiness.platform_ready("alice", "instagram")[0] is True
    assert readiness.platform_ready("bob", "instagram")[0] is False


@pytest.mark.req("3.2", "3.5", "11.3")
def test_installed_model_and_shaping_are_reported():
    import paths
    from autopilot import readiness

    if not paths.face_model().is_file():
        pytest.skip("face model not downloaded (run scripts/fetch_models.sh)")
    assert readiness._face_detector_available() is True
    assert readiness._shaping_available() is True
