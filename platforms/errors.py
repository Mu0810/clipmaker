"""How a platform error is handled (design §8.6, Requirements 10.2, 10.3).

`classify(platform, status, body)` sorts one failed YouTube or Instagram call
into:

* `temporary`  - retry after about 2, 10 and 30 minutes, then fail (10.2);
* `deferred`   - wait (quota, daily upload limit, publishing limit) without
                 counting a retry; `resume` says until when;
* `access`     - the account must be reconnected or a permission granted;
                 every post of the account waits (10.7); `reason` says which;
* `permanent`  - never retried; the post fails with the platform's message (10.3);
* `processing` - Instagram's "media not ready" (9007 / 2207027): check again,
                 not a failure.

`code` is the platform's own code ("quotaExceeded", "190", "9/2207042") and
`message` the platform's text with anything token-shaped removed. The
Instagram codes and subcodes come from Meta's error-code reference (updated
2 Jun 2026).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from platforms.http import RequestRefused, redact

TEMPORARY, DEFERRED, ACCESS, PERMANENT, PROCESSING = (
    "temporary", "deferred", "access", "permanent", "processing")
RETRY_DELAYS_S = (2 * 60, 10 * 60, 30 * 60)          # 10.2
PACIFIC = ZoneInfo("America/Los_Angeles")             # YouTube's quota day
MAX_MESSAGE = 500

RESTRICTED_MESSAGE = ("Instagram restricted the account: open the Instagram app and "
                      "complete the steps it asks for")

_YT_TEMPORARY = frozenset({"backendError", "rateLimitExceeded", "userRateLimitExceeded"})
_YT_ACCESS = frozenset({"insufficientPermissions", "forbidden"})
_GRAPH_TEMPORARY_CODES = frozenset({1, 2, 4, 17, 32, 341, 613})
# The retry needs a new container (the old one can't be published).
_IG_NEW_CONTAINER = frozenset({2207001, 2207006, 2207008, 2207020, 2207032, 2207053})


@dataclass(frozen=True)
class Classified:
    kind: str
    code: str | None
    message: str
    reason: str | None = None        # access: reconnect | permission | restricted
    resume: str | None = None        # deferred: pacific_midnight | 24h | publishing_limit
    new_container: bool = False      # Instagram: retry with a fresh container

    @property
    def retryable(self) -> bool:
        return self.kind == TEMPORARY

    @property
    def counts_as_retry(self) -> bool:
        """Deferrals and "still processing" don't use up a retry (§8.6)."""
        return self.kind not in (DEFERRED, PROCESSING)


class PlatformError(RuntimeError):
    """A failed platform call: its classification, HTTP status (None when
    there was no response) and the platform's request id, for the attempt row."""

    def __init__(self, result: Classified, status: int | None = None,
                 request_id: str | None = None, *, retry_step: str | None = None,
                 step_data: dict | None = None):
        super().__init__(result.message or result.kind)
        self.result, self.status, self.request_id = result, status, request_id
        # The step a retry should start from (None: the step that failed).
        self.retry_step = retry_step
        # Step data to store with the retry (None: leave it as stored), e.g.
        # an Instagram container marked unusable, so the retry makes a new one.
        self.step_data = step_data

    @property
    def kind(self) -> str:
        return self.result.kind


def _body(body) -> dict:
    if isinstance(body, dict):
        return body
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str) and body.strip():
        try:
            parsed = json.loads(body)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {"_text": body}
    return {}


def _message(*candidates) -> str:
    for text in candidates:
        if text:
            return redact(" ".join(str(text).split()))[:MAX_MESSAGE]
    return ""


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify(platform: str, status: int | None, body=None, *, write: bool = True) -> Classified:
    """Classify one failed call. `status` None means no response (network
    error or timeout). `write` says whether the call changed something on the
    platform: a YouTube `forbidden` on a write is a missing permission, on a
    read it's permanent."""
    data = _body(body)
    if platform == "youtube":
        return _youtube(status, data, write)
    if platform == "instagram":
        return _instagram(status, data)
    raise ValueError(f"Unknown platform: {platform}")


def _youtube(status, data: dict, write: bool) -> Classified:
    if status is None:
        return Classified(TEMPORARY, None, "The connection to YouTube failed.")
    err = data.get("error")
    if isinstance(err, str):                     # OAuth token endpoint
        message = _message(data.get("error_description"), err)
        if err == "invalid_grant":
            return Classified(ACCESS, err, message, reason="reconnect")
        kind = TEMPORARY if status >= 500 or status == 429 else (
            ACCESS if status == 401 else PERMANENT)
        return Classified(kind, err, message, reason="reconnect" if kind == ACCESS else None)
    err = err if isinstance(err, dict) else {}
    reasons = [e.get("reason") for e in err.get("errors") or [] if isinstance(e, dict)]
    reason = next((r for r in reasons if r), None)
    message = _message(err.get("message"), data.get("_text"), f"YouTube returned {status}.")
    code = reason or str(status)
    if "quotaExceeded" in reasons:
        return Classified(DEFERRED, "quotaExceeded", message, resume="pacific_midnight")
    if "uploadLimitExceeded" in reasons:
        return Classified(DEFERRED, "uploadLimitExceeded", message, resume="24h")
    if _YT_TEMPORARY & set(reasons):
        return Classified(TEMPORARY, code, message)
    if status == 401:
        return Classified(ACCESS, code, message, reason="reconnect")
    if _YT_ACCESS & set(reasons):
        if write or "insufficientPermissions" in reasons:
            return Classified(ACCESS, code, message, reason="permission")
        return Classified(PERMANENT, code, message)
    if status >= 500 or status == 429:
        return Classified(TEMPORARY, code, message)
    return Classified(PERMANENT, code, message)          # invalid…, forbiddenPrivacySetting, 4xx


def _instagram(status, data: dict) -> Classified:
    if status is None:
        return Classified(TEMPORARY, None, "The connection to Instagram failed.")
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    code, sub = _int(err.get("code")), _int(err.get("error_subcode"))
    label = f"{code}/{sub}" if code is not None and sub is not None else (
        str(code) if code is not None else str(status))
    message = _message(err.get("error_user_msg"), err.get("message"),
                       (data.get("debug_info") or {}).get("message"), data.get("_text"),
                       f"Instagram returned {status}.")
    if (code, sub) == (9, 2207042):
        return Classified(DEFERRED, label, message, resume="publishing_limit")
    if (code, sub) == (4, 2207051):                      # suspected spam: never retried
        return Classified(PERMANENT, label, message)
    if (code, sub) == (25, 2207050):
        return Classified(ACCESS, label, RESTRICTED_MESSAGE, reason="restricted")
    if (code, sub) == (9007, 2207027):
        return Classified(PROCESSING, label, message)
    if (code, sub) == (352, 2207026):                    # unsupported format
        return Classified(PERMANENT, label, message)
    if sub in _IG_NEW_CONTAINER:
        return Classified(TEMPORARY, label, message, new_container=True)
    if code in (190, 102) or status == 401:
        return Classified(ACCESS, label, message, reason="reconnect")
    if code == 10 or (code is not None and 200 <= code <= 299):
        return Classified(ACCESS, label, message, reason="permission")
    if err.get("is_transient") is True or code in _GRAPH_TEMPORARY_CODES:
        return Classified(TEMPORARY, label, message)
    if code == 100:
        return Classified(PERMANENT, label, message)
    debug = data.get("debug_info") if isinstance(data.get("debug_info"), dict) else None
    if debug is not None and "retriable" in debug:       # rupload.facebook.com
        kind = TEMPORARY if debug.get("retriable") is True else PERMANENT
        return Classified(kind, str(debug.get("type") or status), message)
    if status >= 500 or status == 429:
        return Classified(TEMPORARY, label, message)
    return Classified(PERMANENT, label, message)


def classify_container(status_code: str | None, checks: int, *, max_checks: int = 5) -> Classified | None:
    """An Instagram container's status_code after `checks` status queries:
    None while it's fine to keep waiting or once it's done, else a temporary
    error whose retry uses a new container (§8.6)."""
    if status_code in ("ERROR", "EXPIRED"):
        return Classified(TEMPORARY, status_code,
                          f"Instagram couldn't process the video (container {status_code}).",
                          new_container=True)
    if status_code not in ("FINISHED", "PUBLISHED") and checks >= max_checks:
        return Classified(TEMPORARY, status_code or "IN_PROGRESS",
                          f"Instagram still hadn't processed the video after {checks} checks.",
                          new_container=True)
    return None


def classify_exception(platform: str, exc: BaseException) -> Classified:
    """A call that got no usable response."""
    if isinstance(exc, RequestRefused):                   # our own guard: a bug, not the network
        return Classified(PERMANENT, "refused", _message(str(exc)))
    if isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError)):
        name = "YouTube" if platform == "youtube" else "Instagram"
        return Classified(TEMPORARY, exc.__class__.__name__,
                          f"The connection to {name} failed: {_message(str(exc)) or 'no response'}.")
    raise exc


def retry_delay(retries_done: int) -> float | None:
    """Seconds before the next retry of a temporary error, or None when the
    three retries are spent (10.2)."""
    return float(RETRY_DELAYS_S[retries_done]) if 0 <= retries_done < len(RETRY_DELAYS_S) else None


def next_pacific_midnight(now: float) -> float:
    """The next 00:00 in America/Los_Angeles, when YouTube's quota resets."""
    local = datetime.fromtimestamp(now, PACIFIC)
    tomorrow = (local + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=PACIFIC).timestamp()


def resume_at(result: Classified, now: float) -> float | None:
    """When a deferred call may be tried again. None for the publishing limit,
    whose reset time comes from Instagram's content_publishing_limit (9.5)."""
    if result.resume == "pacific_midnight":
        return next_pacific_midnight(now)
    if result.resume == "24h":
        return now + 24 * 3600
    return None
