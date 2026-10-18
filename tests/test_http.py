"""The platform HTTP client (task 7.1, design §16)."""
from __future__ import annotations

import logging

import httpx
import pytest

from platforms import http as H


@pytest.mark.req("N3.4")
@pytest.mark.parametrize("url", [
    "https://evil.example.com/upload",
    "https://www.googleapis.com.evil.example.com/youtube/v3/videos",
    "https://googleapis.com/youtube/v3/videos",
    "http://www.googleapis.com/youtube/v3/videos",            # not HTTPS
    "https://graph.facebook.com:8443/v26.0/me",                # not port 443
    "https://user:pw@graph.facebook.com/v26.0/me",             # credentials in the URL
])
def test_hosts_outside_the_list_are_refused(url):
    with pytest.raises(H.HostNotAllowed):
        H.check_url(url)


@pytest.mark.req("N3.1")
@pytest.mark.parametrize("key", ["access_token", "input_token", "refresh_token",
                                 "client_secret", "fb_exchange_token", "code", "ACCESS_TOKEN"])
def test_a_secret_never_goes_in_a_url(key):
    with pytest.raises(H.SecretInUrl):
        H.check_url(f"https://graph.facebook.com/v26.0/me?fields=id&{key}=abc123")


def test_the_platform_hosts_are_allowed():
    for url in ("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable",
                "https://oauth2.googleapis.com/token",
                "https://graph.facebook.com/v26.0/me?fields=id&appsecret_proof=abc",
                "https://rupload.facebook.com/ig-api-upload/v26.0/123"):
        H.check_url(url)


@pytest.mark.req("N3.4")
def test_the_client_refuses_before_anything_leaves_the_machine(net):
    net.register("evil.example.com", lambda request: httpx.Response(200))
    with H.client() as c:
        with pytest.raises(H.HostNotAllowed):
            c.post("https://evil.example.com/upload", content=b"video bytes")
        with pytest.raises(H.SecretInUrl):
            c.get("https://graph.facebook.com/v26.0/me", params={"access_token": "EAABx"})
    assert net.requests == []


@pytest.mark.req("N3.4")
def test_redirects_are_never_followed(net):
    net.register("www.googleapis.com", lambda request: httpx.Response(
        302, headers={"location": "https://evil.example.com/steal"}))
    with H.client() as c:
        response = c.get("https://www.googleapis.com/youtube/v3/videos")
        assert response.status_code == 302 and len(net.requests) == 1
        with pytest.raises(H.HostNotAllowed):                  # following it by hand fails too
            c.get(response.headers["location"])
    with pytest.raises(ValueError):
        H.client(follow_redirects=True)


def test_request_ids_are_captured(net):
    net.register("www.googleapis.com", lambda request: httpx.Response(
        200, headers={"X-GUploader-UploadID": "ABC-upload-id"}))
    net.register("graph.facebook.com", lambda request: httpx.Response(
        200, headers={"x-fb-trace-id": "FbTrace123"}, json={"id": "1"}))
    with H.client() as c:
        assert H.request_id(c.get("https://www.googleapis.com/upload/youtube/v3/videos")) == \
            "ABC-upload-id"
        assert H.request_id(c.get("https://graph.facebook.com/v26.0/me")) == "FbTrace123"
        assert H.request_id(httpx.Response(200)) is None


@pytest.mark.req("N3.1")
def test_redact_removes_tokens_in_every_shape():
    samples = {
        "Authorization: Bearer ya29.a0AfB_secret": "ya29.a0AfB_secret",
        "{'Authorization': 'Bearer EAABsecretvalue'}": "EAABsecretvalue",
        "headers={'authorization': 'OAuth EAABother'}": "EAABother",
        "GET /v26.0/me?fields=id&access_token=EAABquery&x=1": "EAABquery",
        '{"refresh_token": "1//0gRefreshSecret"}': "1//0gRefreshSecret",
        "client_secret=GOCSPX-abc&grant_type=x": "GOCSPX-abc",
        "?appsecret_proof=deadbeef1234": "deadbeef1234",
        "Authorization=plainsecret": "plainsecret",
    }
    for text, secret in samples.items():
        clean = H.redact(text)
        assert secret not in clean and "[redacted]" in clean, text
    assert H.redact("nothing secret here") == "nothing secret here"
    assert H.safe_url("https://graph.facebook.com/v26.0/me?fields=id&appsecret_proof=x") == \
        "https://graph.facebook.com/v26.0/me"


@pytest.mark.req("N3.1")
def test_captured_logs_are_redacted(net, caplog):
    H.install_redaction()
    caplog.set_level(logging.DEBUG)
    logging.getLogger("clipmaker.anything").warning(
        "Upload failed with headers %s", {"Authorization": "Bearer ya29.LeakyToken"})
    net.register("graph.facebook.com", lambda request: httpx.Response(200, json={"id": "1"}))
    with H.client() as c:
        # httpx logs the full URL of every request; the proof must not survive.
        c.get("https://graph.facebook.com/v26.0/me",
              params={"fields": "id", "appsecret_proof": "ProofValue987"},
              headers={"Authorization": "OAuth EAABHeaderToken"})
    text = caplog.text
    httpx_lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert httpx_lines and "[redacted]" in httpx_lines[-1]   # httpx's line had the query
    assert "LeakyToken" not in text and "ProofValue987" not in text
    assert "EAABHeaderToken" not in text and "[redacted]" in text
    assert "graph.facebook.com/v26.0/me" in text           # the request is still logged
