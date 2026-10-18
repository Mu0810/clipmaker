"""HTTP for the platform clients (design §8.6, §16; Requirements N3.1, N3.4).

Every YouTube and Instagram call goes through `client()`, which:

* refuses any host except the four platform hosts, over HTTPS on the
  standard port, with no user name in the URL, and never follows a redirect
  (the guard runs on every request the client sends, so a redirect followed
  by hand is checked too). Media and metadata therefore only ever reach the
  connected platforms (N3.4);
* refuses a URL that carries a token or secret in its query: tokens travel
  only in headers, and secrets in form bodies, so they never land in a
  logged URL (N3.1);
* sets timeouts, and logs each response at debug level with its request id
  (`X-GUploader-UploadID` on YouTube uploads, `x-fb-trace-id` on Graph calls)
  and without the query string.

`install_redaction()` (run at start-up) redacts `Authorization` values, bearer
and OAuth tokens and `access_token=`-style values from every log record, from
any logger, including httpx's and httpcore's own request lines.
"""
from __future__ import annotations

import logging
import re

import httpx

ALLOWED_HOSTS = frozenset({"www.googleapis.com", "oauth2.googleapis.com",
                           "graph.facebook.com", "rupload.facebook.com"})
REQUEST_ID_HEADERS = ("x-guploader-uploadid", "x-fb-trace-id")
# Query keys that would put a credential in a URL. appsecret_proof is not one:
# it is an HMAC that is useless without the token, and Graph GETs can only
# carry it in the query.
SECRET_QUERY_KEYS = frozenset({"access_token", "input_token", "refresh_token",
                               "client_secret", "fb_exchange_token", "id_token", "code"})
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
UPLOAD_TIMEOUT = httpx.Timeout(300.0, connect=10.0)     # one chunk of a resumable upload

log = logging.getLogger(__name__)


class RequestRefused(RuntimeError):
    """A platform request was stopped before it left the machine."""


class HostNotAllowed(RequestRefused):
    pass


class SecretInUrl(RequestRefused):
    pass


def check_url(url) -> httpx.URL:
    """The URL, if a platform client may call it; raises RequestRefused."""
    u = httpx.URL(str(url))
    host = (u.host or "").lower().rstrip(".")
    if u.scheme != "https":
        raise HostNotAllowed(f"Platform calls must use HTTPS, not {u.scheme or 'no scheme'}.")
    if host not in ALLOWED_HOSTS:
        raise HostNotAllowed(f"{host or 'That address'} isn't one of the platform hosts "
                             "Clip Maker may call.")
    if u.userinfo:
        raise HostNotAllowed("A platform URL can't carry a user name or password.")
    if u.port not in (None, 443):
        raise HostNotAllowed(f"Platform calls use port 443, not {u.port}.")
    leaked = sorted({k.lower() for k, _ in u.params.multi_items()} & SECRET_QUERY_KEYS)
    if leaked:
        raise SecretInUrl(f"Refused to put {', '.join(leaked)} in a URL; tokens go in "
                          "headers and secrets in the request body.")
    return u


def _guard(request: httpx.Request) -> None:
    check_url(request.url)


def request_id(response: httpx.Response) -> str | None:
    """The platform's id for this request, for the attempt record (§8.6)."""
    for name in REQUEST_ID_HEADERS:
        value = response.headers.get(name)
        if value:
            return value
    return None


def safe_url(url) -> str:
    """A URL for logs and messages: no query string, anything token-shaped redacted."""
    u = httpx.URL(str(url))
    port = f":{u.port}" if u.port else ""
    return redact(f"{u.scheme}://{u.host}{port}{u.path}")


def _log_response(response: httpx.Response) -> None:
    rid = request_id(response)
    log.debug("%s %s -> %s%s", response.request.method, safe_url(response.request.url),
              response.status_code, f" (request id {rid})" if rid else "")


def client(*, timeout: httpx.Timeout = DEFAULT_TIMEOUT, **kwargs) -> httpx.Client:
    """An httpx client for platform calls, guarded as described above. Extra
    keyword arguments go to httpx.Client; redirects are never followed."""
    if kwargs.pop("follow_redirects", False):
        raise ValueError("Platform clients never follow redirects.")
    hooks = kwargs.pop("event_hooks", None) or {}
    return httpx.Client(
        timeout=timeout, follow_redirects=False,
        event_hooks={"request": [_guard, *hooks.get("request", [])],
                     "response": [_log_response, *hooks.get("response", [])]},
        **kwargs)


# ── redaction (N3.1) ─────────────────────────────────────────────────────

_REDACTED = "[redacted]"
_PATTERNS = (
    # "Bearer ya29.…", "OAuth EAAB…", "OAuth <app id>|<app secret>" wherever they appear.
    (re.compile(r"(?i)\b(bearer|oauth)(\s+)[A-Za-z0-9._~+/=|\-]{6,}"), rf"\1\2{_REDACTED}"),
    # "Authorization: xyz" / "'authorization': 'xyz'" without a scheme word.
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)"
                r"(?!(?:bearer|oauth)\b|\[redacted\])[^'\"\s,;}]+"), rf"\1{_REDACTED}"),
    # access_token=…, "refresh_token": "…", client_secret=… and the like. A
    # YouTube upload_id is redacted too: the session URL is enough to upload.
    (re.compile(r"(?i)\b(access_token|input_token|refresh_token|client_secret|"
                r"fb_exchange_token|id_token|appsecret_proof|upload_id)(['\"]?\s*[=:]\s*['\"]?)"
                r"[^&\s'\",;}]+"), rf"\1\2{_REDACTED}"),
)


def redact(text) -> str:
    """`text` with credentials replaced by [redacted]."""
    out = str(text)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


class RedactingFilter(logging.Filter):
    """A logging filter that rewrites a record's message with `redact()`.
    Attach it to a handler, or let install_redaction() apply it to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


_installed = False


def install_redaction() -> None:
    """Redact every log record created from now on, whichever logger makes it.
    A filter on one logger misses records from its children (httpcore logs on
    httpcore.http11, for example), so the record factory does the work."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()
    redactor = RedactingFilter()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        redactor.filter(record)
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
