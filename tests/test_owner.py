"""Owner resolution and Autopilot preferences (task 3.1)."""
from __future__ import annotations

import pytest


@pytest.mark.req("15.3", "N3.2")
def test_single_user_mode_resolves_to_local(client):
    import db

    response = client.patch("/api/autopilot/prefs", json={"timezone": "Asia/Kolkata"})
    assert response.status_code == 200
    assert db.settings_get("local", "autopilot_prefs")["timezone"] == "Asia/Kolkata"


@pytest.mark.req("5.6")
def test_prefs_defaults_and_update(client):
    assert client.get("/api/autopilot/prefs").json() == {
        "timezone": None, "paused": False, "last_profile_id": None,
        "autopilot_default": False}
    updated = client.patch("/api/autopilot/prefs",
                           json={"timezone": "America/New_York", "autopilot_default": True})
    assert updated.json()["timezone"] == "America/New_York"
    assert updated.json()["autopilot_default"] is True


@pytest.mark.req("5.6")
@pytest.mark.parametrize("body", [{"timezone": "Mars/Olympus"}, {"timezone": ""},
                                  {"autopilot_default": "yes"}, {"colour": "red"}])
def test_invalid_prefs_are_400(client, body):
    assert client.patch("/api/autopilot/prefs", json=body).status_code == 400


@pytest.mark.req("6.8")
def test_pause_is_not_settable_through_prefs(client):
    response = client.patch("/api/autopilot/prefs", json={"paused": True})
    assert response.status_code == 400
    assert client.get("/api/autopilot/prefs").json()["paused"] is False


@pytest.mark.req("5.6")
def test_last_profile_must_exist(client):
    assert client.patch("/api/autopilot/prefs",
                        json={"last_profile_id": "nope"}).status_code == 404
    pid = client.get("/api/autopilot/profiles").json()["profiles"][0]["id"]
    assert client.patch("/api/autopilot/prefs",
                        json={"last_profile_id": pid}).json()["last_profile_id"] == pid


@pytest.mark.req("15.3", "N3.2")
def test_signed_out_request_is_401(monkeypatch):
    from fastapi.testclient import TestClient

    import app as app_module

    monkeypatch.setenv("CLIPMAKER_AUTH", "1")
    with TestClient(app_module.app) as c:
        assert c.get("/api/autopilot/prefs").status_code == 401


@pytest.mark.req("15.3")
def test_query_string_user_id_is_ignored(two_users):
    alice, bob = two_users
    alice.patch("/api/autopilot/prefs", json={"timezone": "Asia/Kolkata"})
    import db
    alice_id = db.get_user_by_email("alice@example.com")["id"]
    leaked = bob.get(f"/api/autopilot/prefs?user_id={alice_id}&owner={alice_id}").json()
    assert leaked["timezone"] is None


@pytest.mark.req("N4.1", "N4.2")
def test_prefs_are_isolated_both_ways(two_users):
    alice, bob = two_users
    assert alice.patch("/api/autopilot/prefs",
                       json={"timezone": "Asia/Kolkata"}).status_code == 200
    assert bob.patch("/api/autopilot/prefs",
                     json={"timezone": "Europe/Berlin"}).status_code == 200
    assert alice.get("/api/autopilot/prefs").json()["timezone"] == "Asia/Kolkata"
    assert bob.get("/api/autopilot/prefs").json()["timezone"] == "Europe/Berlin"
