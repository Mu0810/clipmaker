"""YouTube Data API client and Google credentials (task 7.2, design §8.2, §9.1)."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone

import httpx
import pytest

from platforms import errors, google_creds, usage
from platforms import youtube_api as Y
from tests.fakes.youtube import FakeYouTube

CLIENT_ID = "123-abc.apps.googleusercontent.com"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/youtube"]


def utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


NOW = utc(2026, 11, 2, 3, 30)               # Mon 2 Nov 2026, 09:00 IST


@pytest.fixture
def yt(net, _isolated_data):
    import db
    from autopilot import migrations

    db.init()
    migrations.run()
    fake = FakeYouTube(net)
    write_token(expiry=NOW + 3600)
    return fake


def write_token(*, expiry: float | None, token="ya29.valid", scopes=SCOPES) -> dict:
    data = {"token": token, "refresh_token": "1//refreshSecret", "client_id": CLIENT_ID,
            "client_secret": "GOCSPX-secret", "scopes": list(scopes),
            "token_uri": "https://oauth2.googleapis.com/token", "universe_domain": "googleapis.com"}
    if expiry is not None:
        data["expiry"] = google_creds._format_expiry(expiry)
    google_creds.token_file().write_text(json.dumps(data))
    return data


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(os.urandom(3500))
    return path


def api(clock=NOW) -> Y.YouTubeAPI:
    now = {"t": clock}
    instance = Y.YouTubeAPI("local", clock=lambda: now["t"])
    instance.now = now
    return instance


def session_for(client, video, *, title="My clip"):
    snippet = Y.build_snippet({"title": title, "description": "D", "tags": ["x"],
                               "category_id": "26"}, "en")
    status = Y.build_status(target_visibility="public", slot_at=NOW + 3600, now=NOW,
                            made_for_kids=False, synthetic=True)
    return client.init_upload(video, snippet, status)


@pytest.mark.req("8.1", "8.2", "4.7", "4.8")
def test_init_sends_the_headers_and_body_and_no_video_bytes(yt, video):
    with api() as client:
        session = session_for(client, video)
    [req] = yt.calls("POST", "/upload/youtube/v3/videos")
    assert dict(req.url.params) == {"uploadType": "resumable", "part": "snippet,status"}
    assert req.headers["x-upload-content-length"] == "3500"
    assert req.headers["x-upload-content-type"] == "video/mp4"
    assert req.headers["content-type"].startswith("application/json")
    assert req.headers["authorization"] == "Bearer ya29.valid"
    assert json.loads(req.content) == {
        "snippet": {"title": "My clip", "description": "D", "tags": ["x"], "categoryId": "26",
                    "defaultLanguage": "en"},
        "status": {"privacyStatus": "private", "publishAt": "2026-11-02T04:30:00Z",
                   "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}}
    assert session["uri"].endswith("upload_id=sess1") and session["size"] == 3500
    assert session["sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()
    assert session["request_id"] == "guploader-sess1"
    assert yt.calls("PUT", "/upload/youtube/v3/videos") == []        # stored before any bytes
    assert usage.used(f"yt-uploads:{CLIENT_ID}", NOW) == 1


@pytest.mark.req("8.2", "8.3")
def test_publish_time_is_the_slot_but_at_least_15_minutes_away():
    soon = Y.build_status(target_visibility="public", slot_at=NOW + 60, now=NOW,
                          made_for_kids=True, synthetic=False)
    assert soon["publishAt"] == Y.rfc3339(NOW + 15 * 60) and soon["selfDeclaredMadeForKids"]
    private = Y.build_status(target_visibility="private", slot_at=NOW + 3600, now=NOW,
                             made_for_kids=False, synthetic=False)
    assert private == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": False}


@pytest.mark.req("8.2", "10.2")
def test_an_interrupted_upload_resumes_from_the_reported_range(yt, video, monkeypatch):
    monkeypatch.setattr(Y, "CHUNK", 1000)
    original, puts = yt._chunk, {"n": 0}

    def flaky(request, sid):
        puts["n"] += 1
        return httpx.Response(503) if puts["n"] == 2 else original(request, sid)

    monkeypatch.setattr(yt, "_chunk", flaky)
    with api() as client:
        session = session_for(client, video)
        with pytest.raises(errors.PlatformError) as lost:
            client.upload(session, video)
        assert lost.value.kind == errors.TEMPORARY and lost.value.status == 503
        done = client.upload(session, video, resume=True)
    puts_sent = yt.calls("PUT", "/upload/youtube/v3/videos")
    query = puts_sent[2]                                   # after chunk 1 and the 503
    assert query.headers["content-range"] == "bytes */3500" and query.content == b""
    assert [r.headers["content-range"] for r in puts_sent[3:]] == [
        "bytes 1000-1999/3500", "bytes 2000-2999/3500", "bytes 3000-3499/3500"]
    assert done["done"] and done["video_id"] == "vid_sess1"
    assert bytes(yt.sessions["sess1"]["data"]) == video.read_bytes()


def test_a_partly_accepted_chunk_continues_where_youtube_says(yt, video, monkeypatch):
    monkeypatch.setattr(Y, "CHUNK", 1000)
    yt.accept_limit = 600                                  # only 600 of the first 1,000 bytes
    with api() as client:
        done = client.upload(session_for(client, video), video)
    ranges = [r.headers["content-range"] for r in yt.calls("PUT", "/upload/youtube/v3/videos")]
    assert ranges[:2] == ["bytes 0-999/3500", "bytes 600-1599/3500"]
    assert done["done"] and bytes(yt.sessions["sess1"]["data"]) == video.read_bytes()


@pytest.mark.req("8.2")
@pytest.mark.parametrize("status", [404, 410])
def test_a_gone_session_is_reported(yt, video, status):
    with api() as client:
        session = session_for(client, video)
        yt.expire_session("sess1", status)
        with pytest.raises(Y.SessionGone) as gone:
            client.upload(session, video, resume=True)
        assert gone.value.status == status
        with pytest.raises(Y.SessionGone):
            client.upload(session, video)


@pytest.mark.req("8.4")
def test_update_sends_the_full_snippet_and_status(yt):
    yt.add_video("v1")
    with api() as client:
        client.update_video("v1", snippet={"title": "New title"})
        client.update_video("v1", status={"publishAt": None})          # unschedule
    first, second = (json.loads(r.content) for r in yt.calls("PUT", "/youtube/v3/videos"))
    assert first["snippet"] == {"title": "New title", "description": "Old description",
                                "tags": ["a", "b"], "categoryId": "26", "defaultLanguage": "en"}
    assert first["status"] == {"privacyStatus": "private", "publishAt": "2026-11-03T12:30:00Z",
                               "license": "youtube", "embeddable": True,
                               "publicStatsViewable": True, "selfDeclaredMadeForKids": False,
                               "containsSyntheticMedia": False}
    assert yt.calls("PUT", "/youtube/v3/videos")[0].url.params["part"] == "snippet,status"
    assert "publishAt" not in second["status"] and second["snippet"]["title"] == "New title"
    assert "publishAt" not in yt.videos["v1"]["status"]
    assert usage.used(f"yt-units:{CLIENT_ID}", NOW) == 2 * (1 + 50)
    with api() as client:
        assert client.delete_video("v1") is True and client.delete_video("v1") is False


@pytest.mark.req("10.4")
def test_upload_counters_reset_at_pacific_midnight_across_a_dst_change(yt, video):
    # 1 Nov 2026 is 25 hours long in Pacific time: it starts at 07:00 UTC (PDT)
    # and the next day starts at 08:00 UTC (PST).
    assert usage.day_start(utc(2026, 11, 1, 7)) == utc(2026, 11, 1, 7)
    assert usage.day_start(utc(2026, 11, 2, 7, 30)) == utc(2026, 11, 1, 7)    # 23:30 PST
    assert usage.day_start(utc(2026, 11, 2, 8)) == utc(2026, 11, 2, 8)
    late = utc(2026, 11, 2, 7)                                              # 23:00 PST
    with api(late) as client:
        for _ in range(100):
            session_for(client, video)
        with pytest.raises(errors.PlatformError) as refused:
            session_for(client, video)
        assert (refused.value.kind, refused.value.result.code) == (errors.DEFERRED, "uploadQuota")
        assert refused.value.result.resume == "pacific_midnight"
        assert len(yt.calls("POST", "/upload/youtube/v3/videos")) == 100   # nothing sent
        client.now["t"] = utc(2026, 11, 2, 8)                              # 00:00 PST
        session_for(client, video)
    assert usage.used(f"yt-uploads:{CLIENT_ID}", utc(2026, 11, 2, 8)) == 1


def test_a_failed_init_gives_the_upload_back(yt, video):
    yt.fail_next = (500, {"error": {"code": 500, "errors": [{"reason": "backendError"}]}})
    with api() as client:
        with pytest.raises(errors.PlatformError):
            session_for(client, video)
    assert usage.used(f"yt-uploads:{CLIENT_ID}", NOW) == 0


@pytest.mark.req("10.6", "N3.1")
def test_a_token_is_refreshed_before_it_expires_and_keeps_its_scopes(yt):
    write_token(expiry=NOW + 60, scopes=SCOPES[:2] + ["https://www.googleapis.com/auth/youtube"])
    with api() as client:
        client.videos(["x"])
    [refresh] = yt.calls("POST", "/token")
    assert str(refresh.url) == "https://oauth2.googleapis.com/token"      # nothing in the URL
    form = dict(httpx.QueryParams(refresh.content.decode()))
    assert form == {"grant_type": "refresh_token", "refresh_token": "1//refreshSecret",
                    "client_id": CLIENT_ID, "client_secret": "GOCSPX-secret"}
    [call] = yt.calls("GET", "/youtube/v3/videos")
    assert call.headers["authorization"] == "Bearer ya29.fresh1"
    saved = json.loads(google_creds.token_file().read_text())
    assert saved["token"] == "ya29.fresh1" and saved["universe_domain"] == "googleapis.com"
    assert set(SCOPES) <= set(saved["scopes"])                             # never narrowed
    assert abs(google_creds._parse_expiry(saved["expiry"]) - (NOW + 3599)) < 1
    assert stat.S_IMODE(os.stat(google_creds.token_file()).st_mode) == 0o600
    assert "https://www.googleapis.com/auth/youtube" in google_creds.granted_scopes("local")
    assert all("access_token" not in str(r.url) and "secret" not in str(r.url)
               for r in yt.requests)


@pytest.mark.req("10.7")
def test_revoked_access_asks_for_a_reconnect(yt):
    write_token(expiry=NOW - 10)
    yt.token_error = {"error": "invalid_grant",
                      "error_description": "Token has been expired or revoked."}
    with api() as client:
        with pytest.raises(errors.PlatformError) as revoked:
            client.videos(["x"])
    assert (revoked.value.kind, revoked.value.result.reason) == (errors.ACCESS, "reconnect")


def test_a_401_refreshes_once_and_retries(yt):
    yt.unauthorised_once = True
    with api() as client:
        assert client.videos(["missing"]) == []
    assert len(yt.calls("POST", "/token")) == 1 and len(yt.calls("GET", "/youtube/v3/videos")) == 2


def test_multi_user_tokens_live_in_user_settings(yt):
    import db

    token = write_token(expiry=NOW - 10)
    google_creds.token_file().unlink()
    db.settings_set("u1", "youtube", {"token": token, "channel": {"title": "Theirs"}})
    access, creds = google_creds.access_token("u1", now=NOW)
    stored = db.settings_get("u1", "youtube")
    assert access == "ya29.fresh1" and stored["token"]["token"] == "ya29.fresh1"
    assert stored["channel"] == {"title": "Theirs"} and creds.client_id == CLIENT_ID
    assert google_creds.load("local") is None


def test_the_manual_uploader_keeps_the_broader_grant(yt):
    import youtube_upload

    write_token(expiry=NOW + 10 ** 7)        # far in the future, so it isn't refreshed
    creds = youtube_upload._credentials_from_cache(refresh=False)
    assert set(creds.scopes) == set(SCOPES)  # not narrowed to today's two scopes


@pytest.mark.req("8.9")
def test_thumbnails_set_or_report_youtube_picks_the_frame(yt, tmp_path):
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8 jpeg bytes")
    yt.add_video("v1")
    with api() as client:
        assert client.set_thumbnail("v1", image) is True
        yt.thumbnail_status = 403
        assert client.set_thumbnail("v1", image) is False
    first = yt.calls("POST", "/upload/youtube/v3/thumbnails/set")[0]
    assert first.headers["content-type"] == "image/jpeg" and yt.thumbnails["v1"] == image.read_bytes()


@pytest.mark.req("8.2")
def test_the_channel_and_a_lost_upload_are_found(yt):
    started = utc(2026, 11, 2, 3, 0)
    yt.add_video("v_other", title="Something else", published="2026-11-02T03:05:00Z")
    yt.add_video("v_early", title="My clip", published="2026-11-02T02:58:00Z")    # 2 min early
    yt.add_video("v_match", title="My clip", published="2026-11-02T03:01:00Z", duration="PT10S")
    with api() as client:
        assert client.channel() == {"id": "UCchannel123", "title": "Manish Clips",
                                    "avatar": "https://yt3.example/avatar.jpg",
                                    "uploads_playlist": "UUchannel123"}
        assert client.find_upload("My clip", started, 10.4) == "v_match"
        assert client.find_upload("My clip", started, 12.0) is None              # 2 s off
    assert Y.parse_duration("PT1M5.5S") == 65.5 and Y.parse_duration("P1DT2H") == 93600


@pytest.mark.req("12.5")
def test_statistics_come_in_batches_of_50_within_the_metrics_cap(yt):
    ids = [f"v{k}" for k in range(120)]
    for vid in ids:
        yt.add_video(vid)
    with api() as client:
        stats = client.statistics(ids)
    calls = yt.calls("GET", "/youtube/v3/videos")
    assert [len(c.url.params["id"].split(",")) for c in calls] == [50, 50, 20]
    assert len(stats) == 120 and stats["v7"] == {"views": 120, "likes": 7, "comments": 2}

    usage.reserve(f"yt-units:{CLIENT_ID}", 8999 - 3, 10 ** 9, NOW)        # 3 used above
    with api() as client:
        with pytest.raises(errors.PlatformError) as capped:
            client.statistics(ids[:51])                                   # the 2nd batch passes 9,000
    assert (capped.value.kind, capped.value.result.code) == (errors.DEFERRED, "metricsCap")
    assert usage.used(f"yt-units:{CLIENT_ID}", NOW) == 9000
