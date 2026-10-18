"""Connecting Instagram through Meta (task 8.2, design §9.2, §9.3)."""
from __future__ import annotations

import json
import os
import stat
import time

import httpx
import pytest

from autopilot import accounts, activity, connections_api, oauth_state, readiness, store
from platforms import meta_auth
from tests.fakes.instagram import APP, USER_TOKEN, FakeInstagram, page

BASE = "/api/connections/instagram"
CALLBACK_URI = "http://localhost/api/connections/instagram/callback"   # TestClient: no port
SECRETS = (USER_TOKEN, "EAApagep1", "EAApagep2", APP["app_secret"], "AQDcode")


@pytest.fixture
def meta(client, net):
    fake = FakeInstagram(net)
    r = client.post(f"{BASE}/app", json={"app_id": APP["app_id"], "app_secret": APP["app_secret"],
                                         "config_id": APP["config_id"]})
    assert r.status_code == 200, r.text
    return fake


def start(client) -> tuple[str, dict]:
    body = client.get(f"{BASE}/start").json()
    return httpx.URL(body["url"]).params["state"], body


def callback(client, **params):
    return client.get(f"{BASE}/callback", params=params, follow_redirects=False)


def view(client) -> dict:
    r = client.get(BASE)
    assert not any(s in r.text for s in SECRETS), "a token or secret leaked into the view"
    return r.json()


def stored() -> dict:
    return meta_auth.load_token("local") or {}


@pytest.mark.req("11.8", "N3.1")
def test_the_meta_app_settings_are_saved_privately(client, meta):
    v = view(client)
    assert v["configured"] is True and v["mode"] == "local"
    assert v["app"] == {"app_id": APP["app_id"], "config_id": APP["config_id"],
                        "secret_set": True}
    assert stat.S_IMODE(os.stat(meta_auth.paths.data_file("meta_app.json")).st_mode) == 0o600
    assert {p["scope"] for p in v["permissions"]} == {
        "instagram_content_publish", "instagram_basic", "instagram_manage_insights",
        "pages_show_list", "pages_read_engagement", "business_management"}
    assert all(p["why"] for p in v["permissions"]) and len(v["setup_steps"]) == 4
    bad = client.post(f"{BASE}/app", json={"app_id": "abc", "app_secret": "s", "config_id": "c"})
    assert bad.status_code == 400 and meta_auth.app_settings("local")["app_id"] == APP["app_id"]


@pytest.mark.req("9.1", "11.4", "11.5", "10.6")
def test_signing_in_with_one_page_connects_it(client, meta):
    before = time.time()
    state, body = start(client)
    params = dict(httpx.URL(body["url"]).params)
    assert body["url"].startswith("https://www.facebook.com/v26.0/dialog/oauth?")
    assert {k: params[k] for k in ("client_id", "config_id", "response_type",
                                   "override_default_response_type", "redirect_uri")} == {
        "client_id": APP["app_id"], "config_id": APP["config_id"], "response_type": "code",
        "override_default_response_type": "true", "redirect_uri": CALLBACK_URI}

    r = callback(client, code="AQDcode", state=state)
    assert (r.status_code, r.headers["location"]) == (303, "/?instagram=connected")
    exchange = meta.calls("POST", "/oauth/access_token")[0]
    assert meta.form(exchange)["redirect_uri"] == CALLBACK_URI     # the same as the dialog's

    data = stored()
    assert (data["user_token"], data["page_token"], data["ig_user_id"], data["page_id"]) == (
        USER_TOKEN, "EAApagep1", "igp1", "p1")
    assert "pending" not in data
    assert stat.S_IMODE(os.stat(meta_auth.paths.data_file("meta_token.json")).st_mode) == 0o600
    account = accounts.get("local", "instagram")
    assert (account["external_id"], account["page_id"], account["name"], account["status"],
            account["quota_key"]) == ("igp1", "p1", "userp1", "ok", "ig:igp1")
    assert before + 5_183_944 <= account["token_expires_at"] <= time.time() + 5_183_944
    assert account["data_access_expires_at"] == 1_800_000_000
    assert "instagram_content_publish" in account["scopes"]
    assert readiness.platform_ready("local", "instagram") == (True, [])

    v = view(client)
    assert v["connected"] and v["account"]["name"] == "userp1" and v["choose"] is None
    assert v["last_result"]["status"] == "connected" and v["last_result"]["warnings"] == []
    assert activity.list_entries("local", {"kind": "account.connected"})


@pytest.mark.req("N3.3")
def test_a_missing_reused_or_expired_state_is_rejected(client, meta):
    assert callback(client, code="AQDcode").status_code == 400                 # missing
    other = oauth_state.issue("local", "youtube", {})
    assert callback(client, code="AQDcode", state=other).status_code == 400   # another flow's
    state, _ = start(client)
    assert callback(client, code="AQDcode", state=state).status_code == 303
    exchanges = len(meta.calls("POST", "/oauth/access_token"))
    replay = callback(client, code="AQDcode", state=state)
    assert replay.status_code == 400                                          # single use
    old = oauth_state.issue("local", "instagram", {"redirect_uri": CALLBACK_URI},
                            now=time.time() - 601)
    expired = callback(client, code="AQDcode", state=old)
    assert expired.status_code == 400 and "expired" in expired.json()["detail"]
    assert len(meta.calls("POST", "/oauth/access_token")) == exchanges        # nothing exchanged


def test_a_cancelled_sign_in_returns_to_the_app_with_the_reason(client, meta):
    state, _ = start(client)
    r = callback(client, state=state, error="access_denied", error_reason="user_denied",
                 error_description="Permissions error")
    assert (r.status_code, r.headers["location"]) == (303, "/?instagram=error")
    result = view(client)["last_result"]
    assert result["status"] == "error" and "cancelled" in result["message"]
    assert meta.calls("POST", "/oauth/access_token") == []
    assert callback(client, code="AQDcode", state=state).status_code == 400   # state used up


def paste(client, token="EAAexplorerToken"):
    return client.post(f"{BASE}/token", json={"token": token})


@pytest.mark.req("11.4")
def test_a_missing_publish_permission_is_named_and_a_working_connection_kept(client, meta):
    state, _ = start(client)
    meta.debug["scopes"] = ["instagram_basic", "pages_show_list"]
    r = callback(client, code="AQDcode", state=state)
    assert r.headers["location"] == "/?instagram=error"
    assert "instagram_content_publish" in view(client)["last_result"]["message"]
    assert accounts.get("local", "instagram") is None and "ig_user_id" not in stored()

    meta.debug["scopes"].append("instagram_content_publish")
    assert paste(client).status_code == 200                       # now it connects
    meta.debug["scopes"].remove("instagram_content_publish")
    refused = paste(client)
    assert refused.status_code == 400 and "instagram_content_publish" in refused.json()["detail"]
    v = view(client)
    assert v["connected"] and v["account"]["status"] == "ok"      # the earlier connection stays
    assert v["last_result"]["status"] == "error"
    assert paste(client, token="has spaces").status_code == 422


@pytest.mark.req("11.4")
def test_no_professional_account_gives_the_fix(client, meta):
    meta.pages = [page("p1", ig=False), page("p2", tasks=("ANALYZE",))]
    r = paste(client)
    assert (r.status_code, r.json()["detail"]) == (400, meta_auth.NO_ACCOUNT_MESSAGE)
    meta.debug["scopes"] = ["instagram_basic", "instagram_content_publish"]   # no pages_show_list
    unseen = paste(client)
    assert unseen.status_code == 400 and "pages_show_list" in unseen.json()["detail"]
    assert view(client)["connected"] is False


def test_a_missing_insights_permission_is_a_warning_only(client, meta):
    meta.debug["scopes"] = ["instagram_basic", "instagram_content_publish", "pages_show_list"]
    r = paste(client)
    assert r.status_code == 200 and r.json()["connected"]
    assert r.json()["last_result"]["warnings"] == [connections_api.INSIGHTS_WARNING]


@pytest.mark.req("11.4")
def test_several_pages_lead_to_a_selection_step(client, meta):
    meta.pages = [page("p1"), page("p2")]
    state, _ = start(client)
    assert callback(client, code="AQDcode", state=state).headers["location"] == \
        "/?instagram=choose"
    v = view(client)                                              # checks no Page token leaks
    assert v["connected"] is False and v["account"] is None
    assert [p["page_id"] for p in v["choose"]["pages"]] == ["p1", "p2"]
    assert set(v["choose"]["pages"][0]) == {"page_id", "page_name", "ig_user_id",
                                            "ig_username", "ig_name", "ig_avatar"}
    assert v["last_result"]["status"] == "choose"

    unknown = client.post(f"{BASE}/select", json={"page_id": "p9"})
    assert unknown.status_code == 422 and unknown.json()["detail"][0]["field"] == "page_id"
    chosen = client.post(f"{BASE}/select", json={"page_id": "p2"}).json()
    assert chosen["outcome"] == "connected" and chosen["choose"] is None
    assert (chosen["account"]["external_id"], chosen["account"]["page_id"]) == ("igp2", "p2")
    assert stored()["page_token"] == "EAApagep2" and "pending" not in stored()
    assert client.post(f"{BASE}/select", json={"page_id": "p1"}).status_code == 409

    paste(client)                                                 # a new choice, left too long
    with pytest.raises(connections_api.HTTPException) as late:
        connections_api.select_page("local", "p1", now=time.time() + 31 * 60)
    assert late.value.status_code == 409


def seed_post(status, *, platform="instagram", slot=None) -> str:
    import uuid

    pid = uuid.uuid4().hex[:16]
    with store.connect() as conn:
        store.insert_post(conn, {"id": pid, "owner_id": "local", "job_id": "job1",
                                 "platform": platform, "method": "official", "status": status,
                                 "approval": "approved", "holds": [], "meta": {}, "ai_meta": {},
                                 "slot_at": slot, "created_at": time.time()})
    return pid


def status_of(pid) -> str:
    with store.connect() as conn:
        return conn.execute("SELECT status FROM posts WHERE id=?", (pid,)).fetchone()[0]


@pytest.mark.req("11.7", "11.6")
def test_disconnect_deletes_the_tokens_and_holds_unpublished_posts(client, meta):
    session = meta_auth.paths.data_file("ig_session.json")       # the password login
    session.write_text("{}")
    assert paste(client).status_code == 200
    later = time.time() + 5 * 86400
    ids = {"scheduled": seed_post("scheduled", slot=later), "retrying": seed_post("retrying"),
           "live": seed_post("live")}
    youtube = seed_post("scheduled", platform="youtube", slot=later)

    r = client.post(f"{BASE}/disconnect").json()
    assert (r["connected"], r["posts_waiting"]) == (False, 2)
    assert not meta_auth.paths.data_file("meta_token.json").exists()
    assert accounts.get("local", "instagram")["status"] == "needs_reconnect"
    assert {k: status_of(v) for k, v in ids.items()} == {
        "scheduled": "needs_reconnect", "retrying": "needs_reconnect", "live": "live"}
    assert status_of(youtube) == "scheduled"
    assert readiness.platform_ready("local", "instagram")[0] is False
    assert session.exists()                                       # manual uploads keep working
    manual = client.get("/api/instagram/status").json()
    assert manual["connected"] and manual["manual_only"] is True
    assert "manual uploads only" in manual["note"]
    assert activity.list_entries("local", {"kind": "account.disconnected"})

    assert paste(client).status_code == 200                      # reconnecting brings them back
    assert status_of(ids["scheduled"]) == "scheduled" and status_of(ids["retrying"]) == "scheduled"


@pytest.mark.req("11.6")
def test_the_youtube_browser_login_is_labelled_manual_only(client):
    status = client.get("/api/youtube/status").json()
    assert status["pw_manual_only"] is True and "manual uploads only" in status["pw_note"]
