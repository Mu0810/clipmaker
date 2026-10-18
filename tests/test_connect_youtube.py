"""Connecting YouTube for Autopilot (task 8.1, design §9.1, §9.3)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from autopilot import accounts, activity, connections_api, oauth_state, readiness, store
from platforms import google_creds
from tests.fakes.youtube import FakeYouTube

CLIENT_ID = "123-abc.apps.googleusercontent.com"
MANAGE = connections_api.YT_AUTOPILOT_SCOPE


class FakeDesktopFlow:
    """Stands in for google_auth_oauthlib's InstalledAppFlow."""
    calls: list[dict] = []
    grant: list[str] = []

    def __init__(self, path, scopes):
        self.path, self.scopes = path, scopes

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        return cls(path, scopes)

    def run_local_server(self, **kwargs):
        from google.oauth2.credentials import Credentials

        type(self).calls.append({"scopes": list(self.scopes), **kwargs})
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
        return Credentials("ya29.connected", refresh_token="1//newRefresh",
                           token_uri="https://oauth2.googleapis.com/token", client_id=CLIENT_ID,
                           client_secret="GOCSPX-secret", scopes=list(self.scopes),
                           granted_scopes=list(type(self).grant or self.scopes), expiry=expiry)


@pytest.fixture
def desktop(client, net, monkeypatch):
    import google_auth_oauthlib.flow
    import youtube_upload

    youtube_upload.save_client_secret(json.dumps({"installed": {
        "client_id": CLIENT_ID, "client_secret": "GOCSPX-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token"}}))
    FakeDesktopFlow.calls, FakeDesktopFlow.grant = [], []
    monkeypatch.setattr(google_auth_oauthlib.flow, "InstalledAppFlow", FakeDesktopFlow)
    return FakeYouTube(net)


def item(owner, key):
    return next(i for i in readiness.check(owner)["items"] if i["key"] == key)


@pytest.mark.req("8.1", "11.8", "11.5", "N3.1")
def test_connecting_for_autopilot_fills_the_account_and_keeps_the_scopes(client, desktop):
    r = client.post("/api/connections/youtube/connect")
    assert r.status_code == 200, r.text
    [flow] = FakeDesktopFlow.calls
    assert flow["scopes"] == [*__import__("youtube_upload").SCOPES, MANAGE]
    assert (flow["include_granted_scopes"], flow["prompt"]) == ("true", "consent")

    account = accounts.get("local", "youtube")
    assert (account["external_id"], account["name"], account["status"]) == (
        "UCchannel123", "Manish Clips", "ok")
    assert account["avatar"] == "https://yt3.example/avatar.jpg"
    assert account["quota_key"] == f"yt:{CLIENT_ID}" and MANAGE in account["scopes"]
    assert MANAGE in json.loads(google_creds.token_file().read_text())["scopes"]

    view = r.json()
    assert view["connected"] and view["autopilot"] and view["mode"] == "desktop"
    assert view["account"]["name"] == "Manish Clips" and view["account"]["connected_at"]
    assert any(p["scope"] == MANAGE and "Manage your YouTube videos" in p["why"]
               for p in view["permissions"])
    assert "ya29" not in r.text and "1//" not in r.text and "GOCSPX" not in r.text
    assert item("local", "youtube_connected")["ok"] is True
    assert [e["kind"] for e in activity.list_entries("local", {"kind": "account."})] == [
        "account.connected"]


@pytest.mark.req("11.8")
def test_an_unticked_permission_is_not_recorded_as_granted(client, desktop):
    import youtube_upload

    FakeDesktopFlow.grant = list(youtube_upload.SCOPES)       # the user unticked "manage"
    view = client.post("/api/connections/youtube/connect").json()
    assert view["connected"] and view["autopilot"] is False
    assert MANAGE not in accounts.get("local", "youtube")["scopes"]
    assert item("local", "youtube_connected")["ok"] is False
    assert "manual uploads only" in item("local", "youtube_connected")["message"]


@pytest.mark.req("4.7", "11.1")
def test_declarations_are_saved(client, desktop):
    url = "/api/autopilot/youtube/declarations"
    assert client.post(url, json={"made_for_kids": False}).status_code == 409   # not connected
    client.post("/api/connections/youtube/connect")
    assert readiness.platform_ready("local", "youtube")[0] is False             # audience unset
    r = client.post(url, json={"made_for_kids": False, "audit_declared": True,
                               "oauth_production_declared": True})
    assert r.status_code == 200
    assert (r.json()["made_for_kids"], r.json()["audit_declared"],
            r.json()["oauth_production_declared"]) == (False, True, True)
    assert readiness.platform_ready("local", "youtube") == (True, [])
    assert item("local", "youtube_audit")["ok"] and item("local", "oauth_production")["ok"]
    bad = client.post(url, json={"made_for_kids": "maybe", "tokens": "x"})
    assert bad.status_code == 422
    assert {p["field"] for p in bad.json()["detail"]} == {"made_for_kids", "tokens"}
    unset = client.post(url, json={"made_for_kids": None}).json()
    assert unset["made_for_kids"] is None
    assert activity.list_entries("local", {"kind": "account.declared"})[0]["summary"] == \
        "YouTube: audience not confirmed"


def seed_post(status, *, platform="youtube", slot=None, owner="local") -> str:
    import time
    import uuid

    pid = uuid.uuid4().hex[:16]
    with store.connect() as conn:
        store.insert_post(conn, {"id": pid, "owner_id": owner, "job_id": "job1",
                                 "platform": platform, "method": "official", "status": status,
                                 "approval": "approved", "holds": [], "meta": {}, "ai_meta": {},
                                 "slot_at": slot, "created_at": time.time()})
    return pid


def post(pid) -> dict:
    with store.connect() as conn:
        return store.decode_row(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


@pytest.mark.req("11.7")
def test_disconnect_revokes_deletes_and_holds_unpublished_posts(client, desktop):
    import time

    client.post("/api/connections/youtube/connect")
    client.post("/api/autopilot/youtube/declarations", json={"made_for_kids": False})
    later = time.time() + 5 * 86400
    ids = {"scheduled": seed_post("scheduled", slot=later),
           "waiting_approval": seed_post("waiting_approval"),
           "uploaded": seed_post("uploaded", slot=later), "live": seed_post("live")}
    instagram = seed_post("scheduled", platform="instagram", slot=later)

    r = client.post("/api/connections/youtube/disconnect").json()
    assert (r["connected"], r["revoked"], r["posts_waiting"]) == (False, True, 2)
    assert desktop.revoked == ["1//newRefresh"]                    # the token went in the body
    assert all(str(q.url) == "https://oauth2.googleapis.com/revoke"
               for q in desktop.calls("POST", "/revoke"))
    assert not google_creds.token_file().exists()
    assert accounts.get("local", "youtube")["status"] == "needs_reconnect"
    assert {k: post(v)["status"] for k, v in ids.items()} == {
        "scheduled": "needs_reconnect", "waiting_approval": "needs_reconnect",
        "uploaded": "uploaded", "live": "live"}                     # out on YouTube already
    assert post(instagram)["status"] == "scheduled"
    assert item("local", "youtube_connected")["ok"] is False
    assert activity.list_entries("local", {"kind": "account.disconnected"})

    # Reconnecting the same channel keeps the audience and brings the posts back.
    client.post("/api/connections/youtube/connect")
    assert accounts.get("local", "youtube")["made_for_kids"] == 0
    assert (post(ids["scheduled"])["status"], post(ids["scheduled"])["slot_at"]) == (
        "scheduled", later)
    back = post(ids["waiting_approval"])
    assert back["status"] == "scheduled" and back["slot_at"] is not None


def test_a_failed_revoke_still_disconnects(client, desktop):
    client.post("/api/connections/youtube/connect")
    desktop.revoke_status = 500
    r = client.post("/api/connections/youtube/disconnect").json()
    assert r["revoked"] is False and not google_creds.token_file().exists()
    assert client.post("/api/youtube/disconnect").status_code == 200     # the old route too


@pytest.fixture
def web(two_users, net, monkeypatch):
    import youtube_oauth

    seen = {"auth": [], "exchange": []}

    def authorization_url(state, scopes=None):
        seen["auth"].append({"state": state, "scopes": scopes})
        return f"https://accounts.google.com/o/oauth2/auth?state={state}"

    def exchange_code(code, state=None, scopes=None):
        seen["exchange"].append({"code": code, "scopes": list(scopes or [])})
        return {"token": "ya29.web", "refresh_token": "1//webRefresh",
                "token_uri": "https://oauth2.googleapis.com/token", "client_id": CLIENT_ID,
                "client_secret": "GOCSPX-web", "scopes": list(scopes or []), "expiry": None}

    monkeypatch.setattr(youtube_oauth, "enabled", lambda: True)
    monkeypatch.setattr(youtube_oauth, "authorization_url", authorization_url)
    monkeypatch.setattr(youtube_oauth, "exchange_code", exchange_code)
    monkeypatch.setattr(youtube_oauth, "channel_info", lambda token: {"title": "Manual"})
    FakeYouTube(net)
    return two_users, seen


@pytest.mark.req("N3.3", "8.1", "N4.2")
def test_the_web_flow_checks_a_single_use_state(web):
    import time

    import db

    (alice, bob), seen = web
    alice_id = db.get_user_by_email("alice@example.com")["id"]
    start = alice.get("/api/connections/youtube/start").json()
    state = seen["auth"][-1]["state"]
    assert seen["auth"][-1]["scopes"] == connections_api.YT_AUTOPILOT_SCOPES
    assert start["url"].endswith(state)
    callback = "/api/youtube/oauth/callback"
    assert bob.get(callback, params={"code": "c", "state": state},
                   follow_redirects=False).status_code == 400            # not bob's state
    ok = alice.get(callback, params={"code": "c", "state": state}, follow_redirects=False)
    assert ok.status_code == 303
    assert ok.headers["location"] == "/?youtube=connected"              # the page opens Setup
    assert seen["exchange"][-1]["scopes"] == connections_api.YT_AUTOPILOT_SCOPES
    stored = db.settings_get(alice_id, "youtube")
    assert stored["token"]["token"] == "ya29.web" and stored["channel"]["title"] == "Manish Clips"
    assert accounts.get(alice_id, "youtube")["external_id"] == "UCchannel123"
    replay = alice.get(callback, params={"code": "c", "state": state}, follow_redirects=False)
    assert replay.status_code == 400                                     # single use
    old = oauth_state.issue(alice_id, "youtube", {"scopes": ["x"]}, now=time.time() - 601)
    expired = alice.get(callback, params={"code": "c", "state": old}, follow_redirects=False)
    assert expired.status_code == 400 and "expired" in expired.json()["detail"]
    assert alice.get(callback, params={"code": "c", "state": ""},
                     follow_redirects=False).status_code == 400          # missing


def test_the_manual_web_flow_keeps_todays_scopes(web):
    import db
    import youtube_oauth

    (alice, _), seen = web
    alice.get("/api/youtube/oauth/start")
    state = seen["auth"][-1]["state"]
    assert seen["auth"][-1]["scopes"] is None                            # today's default
    alice.get("/api/youtube/oauth/callback", params={"code": "c", "state": state},
              follow_redirects=False)
    assert seen["exchange"][-1]["scopes"] == youtube_oauth.SCOPES
    alice_id = db.get_user_by_email("alice@example.com")["id"]
    assert db.settings_get(alice_id, "youtube")["channel"] == {"title": "Manual"}


@pytest.mark.req("11.6")
def test_the_manual_upload_path_is_unchanged(client, desktop, monkeypatch):
    import db
    import paths
    import youtube_pw
    import youtube_upload

    job_dir = paths.output_dir() / "abcdef123456"
    job_dir.mkdir(parents=True)
    (job_dir / "01-clip-vertical.mp4").write_bytes(b"video")
    db.create("abcdef123456", "https://youtu.be/x")
    calls = []
    monkeypatch.setattr(youtube_pw, "is_connected", lambda: False)
    monkeypatch.setattr(youtube_upload, "upload_video", lambda path, **kw: calls.append(
        (path, kw)) or {"id": "vid9", "url": "https://youtu.be/vid9"})
    r = client.post("/api/upload", json={"job_id": "abcdef123456", "file": "01-clip-vertical.mp4",
                                         "title": "T", "description": "D", "tags": ["a"],
                                         "privacy": "private"})
    assert r.json() == {"id": "vid9", "url": "https://youtu.be/vid9"}
    assert calls == [((job_dir / "01-clip-vertical.mp4").resolve(),
                      {"title": "T", "description": "D", "tags": ["a"], "privacy": "private"})]
    client.post("/api/youtube/connect")                  # the manual connect button
    assert FakeDesktopFlow.calls[-1]["scopes"] == youtube_upload.SCOPES
