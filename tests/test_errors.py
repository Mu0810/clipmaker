"""Platform error classes (task 7.1, design §8.6)."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from platforms import errors as E
from platforms.http import HostNotAllowed


def yt(status, *reasons, message="YouTube says no"):
    return {"error": {"code": status, "message": message,
                      "errors": [{"domain": "youtube", "reason": r, "message": message}
                                 for r in reasons]}}


def graph(code=None, subcode=None, message="Graph says no", transient=None):
    err = {"message": message, "type": "OAuthException"}
    if code is not None:
        err["code"] = code
    if subcode is not None:
        err["error_subcode"] = subcode
    if transient is not None:
        err["is_transient"] = transient
    return {"error": err}


YOUTUBE_CASES = [
    # (status, body, kind, code, extra)
    (None, None, E.TEMPORARY, None, {}),
    (500, yt(500, "backendError"), E.TEMPORARY, "backendError", {}),
    (503, "<html>Service Unavailable</html>", E.TEMPORARY, "503", {}),
    (403, yt(403, "rateLimitExceeded"), E.TEMPORARY, "rateLimitExceeded", {}),
    (403, yt(403, "userRateLimitExceeded"), E.TEMPORARY, "userRateLimitExceeded", {}),
    (429, yt(429), E.TEMPORARY, "429", {}),
    (403, yt(403, "quotaExceeded"), E.DEFERRED, "quotaExceeded", {"resume": "pacific_midnight"}),
    (400, yt(400, "uploadLimitExceeded"), E.DEFERRED, "uploadLimitExceeded", {"resume": "24h"}),
    (401, yt(401, "authError"), E.ACCESS, "authError", {"reason": "reconnect"}),
    (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
     E.ACCESS, "invalid_grant", {"reason": "reconnect"}),
    (403, yt(403, "insufficientPermissions"), E.ACCESS, "insufficientPermissions",
     {"reason": "permission"}),
    (403, yt(403, "forbidden"), E.ACCESS, "forbidden", {"reason": "permission"}),
    (400, yt(400, "invalidTitle"), E.PERMANENT, "invalidTitle", {}),
    (400, yt(400, "invalidCategoryId"), E.PERMANENT, "invalidCategoryId", {}),
    (403, yt(403, "forbiddenPrivacySetting"), E.PERMANENT, "forbiddenPrivacySetting", {}),
    (404, yt(404, "videoNotFound"), E.PERMANENT, "videoNotFound", {}),
]

INSTAGRAM_CASES = [
    (None, None, E.TEMPORARY, None, {}),
    (500, graph(1, message="An unknown error occurred"), E.TEMPORARY, "1", {}),
    (502, "Bad Gateway", E.TEMPORARY, "502", {}),
    (400, graph(2), E.TEMPORARY, "2", {}),
    (400, graph(4), E.TEMPORARY, "4", {}),
    (400, graph(17), E.TEMPORARY, "17", {}),
    (400, graph(32), E.TEMPORARY, "32", {}),
    (400, graph(341), E.TEMPORARY, "341", {}),
    (400, graph(613), E.TEMPORARY, "613", {}),
    (400, graph(999, transient=True), E.TEMPORARY, "999", {}),
    # Meta's error-code reference (task 7.1 table)
    (400, graph(9, 2207042), E.DEFERRED, "9/2207042", {"resume": "publishing_limit"}),
    (400, graph(4, 2207051), E.PERMANENT, "4/2207051", {}),
    (400, graph(25, 2207050), E.ACCESS, "25/2207050", {"reason": "restricted",
                                                        "message": E.RESTRICTED_MESSAGE}),
    (400, graph(24, 2207006), E.TEMPORARY, "24/2207006", {"new_container": True}),
    (400, graph(24, 2207008), E.TEMPORARY, "24/2207008", {"new_container": True}),
    (400, graph(-2, 2207020), E.TEMPORARY, "-2/2207020", {"new_container": True}),
    (400, graph(-1, 2207001), E.TEMPORARY, "-1/2207001", {"new_container": True}),
    (400, graph(-1, 2207032), E.TEMPORARY, "-1/2207032", {"new_container": True}),
    (400, graph(-1, 2207053), E.TEMPORARY, "-1/2207053", {"new_container": True}),
    (400, graph(9007, 2207027), E.PROCESSING, "9007/2207027", {}),
    (400, graph(352, 2207026), E.PERMANENT, "352/2207026", {}),
    # access and the rest of §8.6
    (400, graph(190, 460), E.ACCESS, "190/460", {"reason": "reconnect"}),
    (400, graph(102), E.ACCESS, "102", {"reason": "reconnect"}),
    (401, {}, E.ACCESS, "401", {"reason": "reconnect"}),
    (403, graph(10), E.ACCESS, "10", {"reason": "permission"}),
    (403, graph(200, message="(#200) Requires instagram_content_publish"), E.ACCESS, "200",
     {"reason": "permission"}),
    (403, graph(299), E.ACCESS, "299", {"reason": "permission"}),
    (400, graph(100, message="Invalid parameter"), E.PERMANENT, "100", {}),
    (400, {"debug_info": {"retriable": False, "type": "ProcessingFailedError",
                          "message": "Bad file"}}, E.PERMANENT, "ProcessingFailedError", {}),
    (500, {"debug_info": {"retriable": True, "type": "TransientError",
                          "message": "Try again"}}, E.TEMPORARY, "TransientError", {}),
]


@pytest.mark.req("10.2", "10.3")
@pytest.mark.parametrize("status,body,kind,code,extra", YOUTUBE_CASES)
def test_youtube_errors_are_classified(status, body, kind, code, extra):
    result = E.classify("youtube", status, body)
    assert (result.kind, result.code) == (kind, code)
    for key, value in extra.items():
        assert getattr(result, key) == value


@pytest.mark.req("10.2", "10.3")
@pytest.mark.parametrize("status,body,kind,code,extra", INSTAGRAM_CASES)
def test_instagram_errors_are_classified(status, body, kind, code, extra):
    result = E.classify("instagram", status, body)
    assert (result.kind, result.code) == (kind, code)
    for key, value in extra.items():
        assert getattr(result, key) == value


def test_forbidden_on_a_read_is_permanent_but_missing_permissions_never_are():
    assert E.classify("youtube", 403, yt(403, "forbidden"), write=False).kind == E.PERMANENT
    assert E.classify("youtube", 403, yt(403, "insufficientPermissions"),
                      write=False).kind == E.ACCESS


def test_bodies_may_be_json_text_or_bytes():
    raw = '{"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}}'
    assert E.classify("youtube", 403, raw).kind == E.DEFERRED
    assert E.classify("youtube", 403, raw.encode()).kind == E.DEFERRED
    assert E.classify("instagram", 400, b'{"error": {"code": 190}}').reason == "reconnect"


@pytest.mark.req("N3.1")
def test_messages_never_carry_a_token():
    result = E.classify("instagram", 400, graph(100, message=(
        "Bad request for https://graph.facebook.com/me?access_token=EAABsecret123")))
    assert "EAABsecret123" not in result.message and "[redacted]" in result.message


@pytest.mark.req("10.2")
def test_deferrals_and_processing_dont_use_up_a_retry():
    assert not E.classify("youtube", 403, yt(403, "quotaExceeded")).counts_as_retry
    assert not E.classify("instagram", 400, graph(9007, 2207027)).counts_as_retry
    assert E.classify("youtube", 500, yt(500, "backendError")).counts_as_retry
    assert [E.retry_delay(n) for n in range(4)] == [120.0, 600.0, 1800.0, None]


@pytest.mark.req("10.2")
def test_instagram_container_status():
    assert E.classify_container("ERROR", 1).new_container
    assert E.classify_container("EXPIRED", 1).kind == E.TEMPORARY
    assert E.classify_container("IN_PROGRESS", 2) is None
    late = E.classify_container("IN_PROGRESS", 5)
    assert (late.kind, late.new_container) == (E.TEMPORARY, True)
    assert E.classify_container("FINISHED", 9) is None


def test_exceptions_without_a_response():
    timeout = E.classify_exception("youtube", httpx.ConnectTimeout("timed out"))
    assert (timeout.kind, timeout.code) == (E.TEMPORARY, "ConnectTimeout")
    refused = E.classify_exception("instagram", HostNotAllowed("evil.example.com isn't allowed"))
    assert refused.kind == E.PERMANENT
    with pytest.raises(KeyError):
        E.classify_exception("youtube", KeyError("a bug"))


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


@pytest.mark.req("10.2")
def test_quota_resumes_at_the_next_pacific_midnight_across_daylight_saving():
    # Fall back on 1 Nov 2026: midnight is PDT (UTC-7) before, PST (UTC-8) after.
    assert E.next_pacific_midnight(utc(2026, 11, 1, 3)) == utc(2026, 11, 1, 7)    # 20:00 PDT
    assert E.next_pacific_midnight(utc(2026, 11, 1, 20)) == utc(2026, 11, 2, 8)   # 12:00 PST
    # Spring forward on 14 Mar 2027.
    assert E.next_pacific_midnight(utc(2027, 3, 14, 6)) == utc(2027, 3, 14, 8)    # 22:00 PST
    assert E.next_pacific_midnight(utc(2027, 3, 14, 20)) == utc(2027, 3, 15, 7)   # 13:00 PDT
    quota = E.classify("youtube", 403, yt(403, "quotaExceeded"))
    assert E.resume_at(quota, utc(2026, 11, 1, 20)) == utc(2026, 11, 2, 8)
    daily = E.classify("youtube", 400, yt(400, "uploadLimitExceeded"))
    assert E.resume_at(daily, 1000.0) == 1000.0 + 86400
    assert E.resume_at(E.classify("instagram", 400, graph(9, 2207042)), 1000.0) is None
