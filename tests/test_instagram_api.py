"""Meta sign-in and the Instagram API client (task 7.3, design §8.3, §9.2)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import stat

import pytest

from platforms import errors, http, meta_auth
from platforms.instagram_api import InstagramAPI
from tests.fakes.instagram import APP, IG_USER, PAGE_TOKEN, SHORT_TOKEN, USER_TOKEN, \
    FakeInstagram, page

NOW = 1_793_590_200.0                                      # Mon 2 Nov 2026, 03:30 UTC


@pytest.fixture
def ig(net, _isolated_data):
    fake = FakeInstagram(net)
    meta_auth.save_app_settings(APP["app_id"], APP["app_secret"], APP["config_id"])
    meta_auth.save_token("local", {"user_token": USER_TOKEN, "page_id": "p1",
                                   "page_token": PAGE_TOKEN, "ig_user_id": IG_USER,
                                   "ig_username": "manish"})
    return fake


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "clip-vertical.mp4"
    path.write_bytes(os.urandom(3500))
    return path


@pytest.mark.req("9.2", "10.2")
def test_rupload_sends_its_headers_and_resumes_from_bytes_transferred(ig, video):
    with InstagramAPI("local") as api:
        container = api.create_container("Dark mode is here #tips", thumb_offset_ms=3200)
        assert container == {"container_id": "c1", "started_at": container["started_at"],
                             "upload_uri": "https://rupload.facebook.com/ig-api-upload/v26.0/c1"}
        ig.rupload_accept = 1200                           # the connection drops after 1,200 bytes
        with pytest.raises(errors.PlatformError) as cut:
            api.upload(container, video)
        assert (cut.value.kind, cut.value.status) == (errors.TEMPORARY, 500)
        assert api.upload_progress(container) == 1200
        api.resume_upload(container, video)
        assert api.container_status("c1")["status_code"] == "FINISHED"
    first, second = ig.calls("POST", "/ig-api-upload/v26.0/c1")
    assert first.headers["authorization"] == f"OAuth {PAGE_TOKEN}"
    assert (first.headers["offset"], first.headers["file_size"]) == ("0", "3500")
    assert (second.headers["offset"], second.headers["file_size"]) == ("1200", "3500")
    assert second.headers["content-length"] == "2300"
    assert second.content == video.read_bytes()[1200:]
    assert bytes(ig.containers["c1"]["data"]) == video.read_bytes()
    reads = [r for r in ig.calls("GET", "/c1") if "video_status" in str(r.url)]
    assert len(reads) == 2                                 # mine, and resume_upload's own
    assert all(r.url.params["fields"] == "video_status" for r in reads)


@pytest.mark.req("N3.1")
def test_the_appsecret_proof_is_the_hmac_of_the_token(ig):
    # The standard HMAC-SHA256 example vector, independent of the code under test.
    assert meta_auth.appsecret_proof("key", "The quick brown fox jumps over the lazy dog") == \
        "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
    with InstagramAPI("local") as api:
        api.publishing_limit()
    [call] = ig.calls("GET", "/content_publishing_limit")
    assert call.url.params["appsecret_proof"] == hmac.new(
        APP["app_secret"].encode(), PAGE_TOKEN.encode(), hashlib.sha256).hexdigest()


def test_a_wrong_proof_is_refused_by_meta(ig, monkeypatch):
    monkeypatch.setattr(meta_auth, "appsecret_proof", lambda secret, token: "0" * 64)
    with InstagramAPI("local") as api:
        with pytest.raises(errors.PlatformError) as refused:
            api.publishing_limit()
    assert (refused.value.kind, refused.value.result.code) == (errors.PERMANENT, "100")


@pytest.mark.req("11.4")
def test_page_discovery_keeps_pages_that_can_publish_to_instagram(ig):
    ig.pages = [page("p1"), page("p2", tasks=("ANALYZE", "ADVERTISE")), page("p3", ig=False),
                page("p4", tasks=("CREATE_CONTENT",))]
    ig.page_size = 2                                       # two result pages, followed by cursor
    with http.client() as client:
        found = meta_auth.pages(client, APP, USER_TOKEN)
        assert [(p["page_id"], p["ig_user_id"], p["ig_username"]) for p in found] == [
            ("p1", "igp1", "userp1"), ("p4", "igp4", "userp4")]
        assert found[0]["page_token"] == "EAApagep1"
        assert [r.url.params.get("after") for r in ig.calls("GET", "/me/accounts")] == [None, "2"]
        ig.pages = [page("p2", tasks=("ANALYZE",)), page("p3", ig=False)]
        with pytest.raises(meta_auth.MetaSetupError) as none:
            meta_auth.pages(client, APP, USER_TOKEN)
    assert str(none.value) == meta_auth.NO_ACCOUNT_MESSAGE


@pytest.mark.req("9.1")
def test_the_page_token_falls_back_to_the_user_token_and_the_choice_is_recorded(ig):
    ig.refuse = {PAGE_TOKEN}
    with InstagramAPI("local") as api:
        assert api.create_container("Caption")["container_id"] == "c1"
    tried = [r.headers["authorization"] for r in ig.calls("POST", f"{IG_USER}/media")]
    assert tried == [f"OAuth {PAGE_TOKEN}", f"OAuth {USER_TOKEN}"]
    assert meta_auth.load_token("local")["publish_token"] == "user"
    with InstagramAPI("local") as api:                     # a new client starts with the user token
        api.create_container("Second caption")
    tried = [r.headers["authorization"] for r in ig.calls("POST", f"{IG_USER}/media")]
    assert tried[2:] == [f"OAuth {USER_TOKEN}"]
    ig.refuse = {PAGE_TOKEN, USER_TOKEN}
    with InstagramAPI("local") as api:
        with pytest.raises(errors.PlatformError) as both:
            api.create_container("Third caption")
    assert (both.value.kind, both.value.result.reason) == (errors.ACCESS, "permission")


def test_a_restricted_account_does_not_fall_back(ig):
    ig.fail_next = (400, {"error": {"code": 25, "error_subcode": 2207050,
                                    "message": "The user is restricted"}})
    with InstagramAPI("local") as api:
        with pytest.raises(errors.PlatformError) as restricted:
            api.publishing_limit()
    assert restricted.value.result.reason == "restricted"
    assert restricted.value.result.message == errors.RESTRICTED_MESSAGE
    assert len(ig.calls("GET", "/content_publishing_limit")) == 1


@pytest.mark.req("11.4", "10.6", "N3.1")
def test_the_connect_steps_keep_secrets_out_of_urls(ig):
    url = meta_auth.dialog_url(APP, "state123", "http://localhost:8000/api/connections/instagram/callback")
    query = dict(http.httpx.URL(url).params)
    assert url.startswith("https://www.facebook.com/v26.0/dialog/oauth?")
    assert query == {"client_id": APP["app_id"], "state": "state123", "config_id": APP["config_id"],
                     "redirect_uri": "http://localhost:8000/api/connections/instagram/callback",
                     "response_type": "code", "override_default_response_type": "true"}
    with http.client() as client:
        short = meta_auth.exchange_code(client, APP, "AQDcode", "http://localhost:8000/cb")
        assert short["access_token"] == SHORT_TOKEN
        long = meta_auth.long_lived(client, APP, SHORT_TOKEN, now=NOW)
        assert long == {"token": USER_TOKEN, "expires_at": NOW + 5_183_944}
        info = meta_auth.debug_token(client, APP, USER_TOKEN)
    assert info == {"is_valid": True, "expires_at": None, "data_access_expires_at": 1_800_000_000.0,
                    "scopes": ig.debug["scopes"], "user_id": "u1"}
    assert meta_auth.missing_scopes(info["scopes"]) == []
    assert meta_auth.missing_scopes(["instagram_basic"]) == ["instagram_content_publish"]
    exchanges = ig.calls("POST", "/oauth/access_token")
    assert [ig.form(r).get("grant_type") for r in exchanges] == [None, "fb_exchange_token"]
    assert all(str(r.url) == "https://graph.facebook.com/v26.0/oauth/access_token"
               for r in exchanges)
    [batch] = [r for r in ig.requests if r.url.path == "/v26.0/"]
    assert batch.headers["authorization"] == f"OAuth {APP['app_id']}|{APP['app_secret']}"
    assert "input_token" not in str(batch.url) and USER_TOKEN in batch.content.decode()


def test_debug_token_reports_an_invalid_token(ig):
    with http.client() as client:
        assert meta_auth.debug_token(client, APP, "EAArevoked")["is_valid"] is False


@pytest.mark.req("9.2", "9.4", "9.5", "10.4", "12.5", "4.8")
def test_the_publishing_steps(ig, video):
    with InstagramAPI("local", clock=lambda: NOW) as api:
        assert api.publishing_limit() == {"used": 3, "limit": 100, "window_s": 86400}
        plain = api.create_container("Plain caption", thumb_offset_ms=3200)
        labelled = api.create_container("Labelled caption", ai_generated=True)
        forms = [ig.containers[c]["form"] for c in ("c1", "c2")]
        assert {k: v for k, v in forms[0].items() if k != "appsecret_proof"} == {
            "media_type": "REELS", "upload_type": "resumable", "caption": "Plain caption",
            "thumb_offset": "3200"}
        assert forms[1]["is_ai_generated"] == "true" and "thumb_offset" not in forms[1]
        with pytest.raises(errors.PlatformError) as early:            # not uploaded yet
            api.publish("c1")
        assert early.value.kind == errors.PROCESSING
        api.upload(plain, video)
        media_id = api.publish("c1")
        assert media_id == "m1"
        assert api.media("m1")["permalink"] == "https://www.instagram.com/reel/m1/"
        assert api.find_media("Plain caption", NOW - 60)["id"] == "m1"
        assert api.find_media("Some other caption", NOW - 60) is None
        assert api.insights("m1") == {"views": 340, "likes": 12, "comments": 3}
    lookups = ig.calls("GET", f"{IG_USER}/media")
    assert len(lookups) == 2 and all(r.url.params["since"] == str(int(NOW - 60)) for r in lookups)
    assert labelled["container_id"] == "c2"
    ig.fail_next = (400, {"error": {"code": 9, "error_subcode": 2207042,
                                    "message": "You reached the publishing limit"}})
    with InstagramAPI("local") as api:
        with pytest.raises(errors.PlatformError) as limit:
            api.publishing_limit()
    assert (limit.value.kind, limit.value.result.resume) == (errors.DEFERRED, "publishing_limit")


@pytest.mark.req("N3.1")
def test_no_token_or_secret_ever_appears_in_a_url(ig, video):
    with http.client() as client:
        meta_auth.exchange_code(client, APP, "AQDsecretCode", "http://localhost:8000/cb")
        meta_auth.long_lived(client, APP, SHORT_TOKEN, now=NOW)
        meta_auth.pages(client, APP, USER_TOKEN)
        meta_auth.debug_token(client, APP, USER_TOKEN)
    ig.refuse = {PAGE_TOKEN}                                    # exercise both tokens
    with InstagramAPI("local") as api:
        container = api.create_container("Caption")
        api.upload(container, video)
        api.publish(container["container_id"])
        api.insights("m1")
    secrets = (USER_TOKEN, PAGE_TOKEN, SHORT_TOKEN, "EAApagep1", APP["app_secret"], "AQDsecretCode")
    # 4 connect calls, 2 container tries (Page token refused), upload, publish, insights.
    assert len(ig.requests) == 9
    for request in ig.requests:
        assert not any(s in str(request.url) for s in secrets), str(request.url)


@pytest.mark.req("N3.1")
def test_meta_tokens_never_reach_the_logs(ig, caplog):
    http.install_redaction()
    caplog.set_level(logging.DEBUG)
    with http.client() as client:
        meta_auth.debug_token(client, APP, USER_TOKEN)
        meta_auth.pages(client, APP, USER_TOKEN)
    logging.getLogger("clipmaker.test").warning(
        "headers were %s", {"Authorization": f"OAuth {APP['app_id']}|{APP['app_secret']}"})
    assert APP["app_secret"] not in caplog.text and USER_TOKEN not in caplog.text
    assert meta_auth.appsecret_proof(APP["app_secret"], USER_TOKEN) not in caplog.text
    assert "graph.facebook.com" in caplog.text


@pytest.mark.req("N3.1")
def test_meta_settings_and_tokens_are_private_files(ig):
    for name in ("meta_app.json", "meta_token.json"):
        path = meta_auth.paths.data_file(name)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert meta_auth.app_settings("local")["config_id"] == APP["config_id"]
    with pytest.raises(meta_auth.MetaSetupError):
        meta_auth.save_app_settings("not-a-number", "secret", "cfg")
    meta_auth.record_token_choice("local", "user")
    stored = json.loads(meta_auth.paths.data_file("meta_token.json").read_text())
    assert stored["publish_token"] == "user" and stored["page_token"] == PAGE_TOKEN
