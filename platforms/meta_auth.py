"""Meta sign-in for Instagram: Facebook Login for Business (design §9.2, §8.9;
Requirements 9.1, 10.6, 11.4, N3.1).

Building blocks for the connect flow (task 8 wires them to routes):

* `dialog_url()`  - the browser URL, with `config_id`, `response_type=code`
                    and `override_default_response_type=true`;
* `exchange_code()`, `long_lived()` - the code exchange and the 60-day token,
                    as form POSTs so the app secret never sits in a URL;
* `pages()`       - `/me/accounts`, keeping Pages with a linked Instagram
                    professional account and a MANAGE or CREATE_CONTENT task;
* `debug_token()` - validity, expiry and scopes, checked with the app token
                    through Graph's batch endpoint, so the token being
                    inspected travels in the request body, not the URL;
* `appsecret_proof()` - HMAC-SHA256 of a token keyed with the app secret,
                    sent with every Graph call.

App settings live in `meta_app.json` (single-user, mode 600) or the
CLIPMAKER_META_* variables (multi-user). Tokens live in `meta_token.json`
(single-user, mode 600) or `user_settings.instagram` (multi-user). Tokens
travel only in `Authorization` headers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlencode

import db
import paths
from platforms import errors, http

GRAPH_VERSION = "v26.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
DIALOG = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
PUBLISH_TASKS = frozenset({"MANAGE", "CREATE_CONTENT"})
REQUIRED_SCOPE = "instagram_content_publish"
SETTINGS_KEY = "instagram"
LOCAL = "local"
MAX_PAGES = 10                         # /me/accounts result pages followed
NO_ACCOUNT_MESSAGE = ("No Instagram professional account is linked to a Facebook Page you "
                      "manage. In Instagram, switch to a professional account, link it to a "
                      "Facebook Page, then reconnect.")
_lock = threading.Lock()


class MetaSetupError(ValueError):
    """The Meta app or account isn't set up for API publishing (11.4)."""


# ── app settings ─────────────────────────────────────────────────────────

def _app_file() -> Path:
    return paths.data_file("meta_app.json")


def _write_private(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def app_settings(owner: str) -> dict | None:
    """{"app_id", "app_secret", "config_id", "redirect_uri"} or None."""
    if owner == LOCAL:
        try:
            data = json.loads(_app_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    else:
        data = {"app_id": os.environ.get("CLIPMAKER_META_APP_ID", ""),
                "app_secret": os.environ.get("CLIPMAKER_META_APP_SECRET", ""),
                "config_id": os.environ.get("CLIPMAKER_META_CONFIG_ID", ""),
                "redirect_uri": os.environ.get("CLIPMAKER_META_REDIRECT", "")}
    data = {k: str(data.get(k) or "").strip() for k in
            ("app_id", "app_secret", "config_id", "redirect_uri")}
    return data if data["app_id"] and data["app_secret"] else None


def save_app_settings(app_id: str, app_secret: str, config_id: str) -> None:
    """Single-user setup screen: store the Meta app's id, secret and
    configuration id, readable by this account only."""
    values = {"app_id": str(app_id).strip(), "app_secret": str(app_secret).strip(),
              "config_id": str(config_id).strip()}
    if not values["app_id"].isdigit():
        raise MetaSetupError("The app id is the number shown in your Meta app's settings.")
    if not values["app_secret"] or not values["config_id"]:
        raise MetaSetupError("Enter the app secret and the Login configuration id.")
    _write_private(_app_file(), values)


# ── tokens ───────────────────────────────────────────────────────────────

def _token_file() -> Path:
    return paths.data_file("meta_token.json")


def load_token(owner: str) -> dict | None:
    """The stored connection: user_token, page_id, page_token, ig_user_id,
    ig_username, scopes, expiry dates and publish_token ("page" | "user").
    While the user is choosing between several Pages it also holds `pending`
    (that sign-in's tokens and Page list), and `last_result` says how the last
    connect attempt ended. All of it stays on the server."""
    if owner == LOCAL:
        try:
            return json.loads(_token_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    stored = db.settings_get(owner, SETTINGS_KEY) or {}
    token = stored.get("meta")
    return token if isinstance(token, dict) else None


def save_token(owner: str, data: dict) -> None:
    with _lock:
        if owner == LOCAL:
            _write_private(_token_file(), data)
        else:
            stored = db.settings_get(owner, SETTINGS_KEY) or {}
            db.settings_set(owner, SETTINGS_KEY, {**stored, "meta": data})


def delete_token(owner: str) -> None:
    """Forget the Meta connection, its pending Page choice and last result
    (disconnect, 11.7). The password login for manual uploads lives in its own
    files and is left alone (11.6)."""
    with _lock:
        if owner == LOCAL:
            _token_file().unlink(missing_ok=True)
            return
        stored = db.settings_get(owner, SETTINGS_KEY)
        if stored is None:
            return
        rest = {k: v for k, v in stored.items() if k != "meta"}
        if rest:
            db.settings_set(owner, SETTINGS_KEY, rest)
        else:
            db.settings_delete(owner, SETTINGS_KEY)


def record_token_choice(owner: str, kind: str) -> None:
    """Remember which token publishing works with (§8.3, "Token choice")."""
    data = load_token(owner) or {}
    if data.get("publish_token") != kind:
        save_token(owner, {**data, "publish_token": kind})


def appsecret_proof(app_secret: str, token: str) -> str:
    return hmac.new(app_secret.encode("utf-8"), token.encode("utf-8"),
                    hashlib.sha256).hexdigest()


# ── Graph helpers ────────────────────────────────────────────────────────

def _body(response) -> dict:
    try:
        return response.json() if response.content else {}
    except ValueError:
        return {"_text": response.text}


def _check(response) -> dict:
    body = _body(response)
    if response.status_code != 200:
        raise errors.PlatformError(errors.classify("instagram", response.status_code, body),
                                   response.status_code, http.request_id(response))
    return body


def _call(client, method: str, url: str, **kwargs):
    try:
        return client.request(method, url, **kwargs)
    except http.RequestRefused:
        raise
    except Exception as exc:
        raise errors.PlatformError(errors.classify_exception("instagram", exc)) from exc


def graph_get(client, app: dict, token: str, path: str, params: dict | None = None) -> dict:
    """A Graph GET with the token in the header and the proof in the query."""
    query = {**(params or {}), "appsecret_proof": appsecret_proof(app["app_secret"], token)}
    return _check(_call(client, "GET", f"{GRAPH}/{path.lstrip('/')}", params=query,
                        headers={"Authorization": f"OAuth {token}"}))


def graph_post(client, app: dict, token: str, path: str, data: dict | None = None) -> dict:
    """A Graph POST with the token in the header and the fields (proof included)
    in the form body."""
    form = {**(data or {}), "appsecret_proof": appsecret_proof(app["app_secret"], token)}
    return _check(_call(client, "POST", f"{GRAPH}/{path.lstrip('/')}", data=form,
                        headers={"Authorization": f"OAuth {token}"}))


# ── the connect flow's steps ─────────────────────────────────────────────

def dialog_url(app: dict, state: str, redirect_uri: str) -> str:
    """Step 1 (§9.2): where the browser goes to sign in with Meta."""
    if not app.get("config_id"):
        raise MetaSetupError("Add the Facebook Login for Business configuration id first.")
    return DIALOG + "?" + urlencode({
        "client_id": app["app_id"], "redirect_uri": redirect_uri, "state": state,
        "config_id": app["config_id"], "response_type": "code",
        "override_default_response_type": "true"})


def _token_exchange(client, form: dict) -> dict:
    body = _check(_call(client, "POST", f"{GRAPH}/oauth/access_token", data=form))
    if not body.get("access_token"):
        raise errors.PlatformError(errors.Classified(
            errors.ACCESS, "noToken", "Meta didn't return an access token.", reason="reconnect"))
    return body


def exchange_code(client, app: dict, code: str, redirect_uri: str) -> dict:
    """Step 2: the code from the callback, exchanged server-side."""
    return _token_exchange(client, {"client_id": app["app_id"], "client_secret": app["app_secret"],
                                    "redirect_uri": redirect_uri, "code": code})


def long_lived(client, app: dict, short_token: str, *, now: float) -> dict:
    """Step 3: the 60-day user token. Returns {"token", "expires_at"}."""
    body = _token_exchange(client, {"grant_type": "fb_exchange_token",
                                    "client_id": app["app_id"],
                                    "client_secret": app["app_secret"],
                                    "fb_exchange_token": short_token})
    expires_in = body.get("expires_in")
    return {"token": body["access_token"],
            "expires_at": now + float(expires_in) if expires_in else None}


def pages(client, app: dict, user_token: str) -> list[dict]:
    """Step 4: Pages the user can publish for that have a linked Instagram
    professional account. Raises MetaSetupError with the 11.4 message when
    there are none. Result pages are followed by cursor, never by the `next`
    URL (it carries the token in its query)."""
    fields = ("id,name,access_token,tasks,"
              "instagram_business_account{id,username,name,profile_picture_url}")
    found, after = [], None
    for _ in range(MAX_PAGES):
        params = {"fields": fields, "limit": 100}
        if after:
            params["after"] = after
        body = graph_get(client, app, user_token, "me/accounts", params)
        for page in body.get("data") or []:
            ig = page.get("instagram_business_account") or {}
            if ig.get("id") and PUBLISH_TASKS & set(page.get("tasks") or []):
                found.append({"page_id": page.get("id"), "page_name": page.get("name"),
                              "page_token": page.get("access_token"), "ig_user_id": ig["id"],
                              "ig_username": ig.get("username"), "ig_name": ig.get("name"),
                              "ig_avatar": ig.get("profile_picture_url")})
        paging = body.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not after or not paging.get("next"):
            break
    if not found:
        raise MetaSetupError(NO_ACCOUNT_MESSAGE)
    return found


def debug_token(client, app: dict, token: str) -> dict:
    """Step 5 and token health (§8.9): {"is_valid", "expires_at" (None =
    never), "data_access_expires_at", "scopes", "user_id"}, checked with the
    app access token. Graph's batch endpoint carries the inspected token in
    the POST body, so it never appears in a URL."""
    app_token = f"{app['app_id']}|{app['app_secret']}"
    batch = [{"method": "GET", "relative_url": "debug_token?" + urlencode({"input_token": token})}]
    replies = graph_post(client, app, app_token, "", {"batch": json.dumps(batch),
                                                      "include_headers": "false"})
    reply = replies[0] if isinstance(replies, list) and replies else {}
    body = _body_text(reply.get("body"))
    if reply.get("code") != 200:
        raise errors.PlatformError(errors.classify("instagram", reply.get("code") or 500, body),
                                   reply.get("code"))
    data = body.get("data") or {}

    def when(value):
        return None if not value else float(value)       # 0 means never

    return {"is_valid": bool(data.get("is_valid")), "expires_at": when(data.get("expires_at")),
            "data_access_expires_at": when(data.get("data_access_expires_at")),
            "scopes": [str(s) for s in data.get("scopes") or []],
            "user_id": data.get("user_id")}


def _body_text(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {"_text": str(value)}


def missing_scopes(scopes) -> list[str]:
    """The permissions Autopilot still needs (named in the 11.4 message)."""
    return [] if REQUIRED_SCOPE in set(scopes or ()) else [REQUIRED_SCOPE]
