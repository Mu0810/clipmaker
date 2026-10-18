"""Readiness checklist for Autopilot (Requirement 11, design §9.4).

Required items gate publishing to a platform: a post for a platform that is
not ready is planned as 'blocked'. Advisory items never block; they explain a
consequence (for example, "YouTube uploads will stay private").
"""
from __future__ import annotations

import importlib.util
import json

import llm
import paths
from autopilot import store
from autopilot.owner import get_prefs
from platforms import meta_auth

YT_MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube"
IG_PUBLISH_SCOPE = "instagram_content_publish"
YT_AUDIT_FORM = "https://support.google.com/youtube/contact/yt_api_form"


def _account(owner: str, platform: str) -> dict | None:
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE owner_id=? AND platform=?",
                           (owner, platform)).fetchone()
    return store.decode_row(row)


def _scopes(account: dict | None) -> set[str]:
    raw = (account or {}).get("scopes") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = raw.split()
    return {str(s) for s in raw}


def _shaping_available() -> bool:
    try:
        from PIL import features
        return bool(features.check_feature("raqm") and features.check_feature("fribidi"))
    except Exception:
        return False


def _face_detector_available() -> bool:
    return importlib.util.find_spec("cv2") is not None and paths.face_model().is_file()


def _lost(name: str, reason: str | None) -> str:
    """The stored reason is a whole sentence when it names the platform
    ("YouTube access was lost: ...", "YouTube was disconnected."); a bare one
    gets the lead-in, so it never reads "access was lost" twice."""
    reason = (reason or "").strip()
    if not reason:
        return f"{name} access was lost: reconnect to continue."
    return reason if reason.startswith(name) else f"{name} access was lost: {reason}"


def _item(key, kind, ok, label, message, fix=None, platform=None):
    return {"key": key, "kind": kind, "platform": platform, "ok": bool(ok),
            "label": label, "message": message, "fix": fix}


def check(owner: str) -> dict:
    yt = _account(owner, "youtube")
    ig = _account(owner, "instagram")
    yt_ok = bool(yt and yt.get("status") == "ok" and YT_MANAGE_SCOPE in _scopes(yt))
    ig_ok = bool(ig and ig.get("status") == "ok" and IG_PUBLISH_SCOPE in _scopes(ig))
    audience_ok = bool(yt and yt.get("made_for_kids") is not None)
    tz = get_prefs(owner).get("timezone")

    if yt_ok:
        yt_msg = f"Connected to {yt.get('name') or 'your channel'}."
    elif yt and yt.get("status") == "needs_reconnect":
        yt_msg = _lost("YouTube", yt.get("status_reason"))
    elif yt:
        yt_msg = "Connected for manual uploads only; Autopilot needs permission to manage the videos it posts."
    else:
        yt_msg = "Connect your YouTube channel for Autopilot."

    if ig_ok:
        ig_msg = f"Connected to @{ig.get('name') or 'your account'}."
    elif ig and ig.get("status") == "needs_reconnect":
        ig_msg = _lost("Instagram", ig.get("status_reason"))
    elif ig:
        ig_msg = "Connected, but permission to publish content is missing."
    elif meta_auth.app_settings(owner) is None:
        ig_msg = ("First add your own Meta app below, a one-time setup in Meta for Developers; "
                  "then connect your Instagram professional account.")
    else:
        ig_msg = "Connect your Instagram professional account through Meta."

    items = [
        _item("youtube_connected", "required", yt_ok, "YouTube connected for Autopilot",
              yt_msg, None if yt_ok else "Connect for Autopilot", "youtube"),
        _item("youtube_audience", "required", audience_ok, "Channel audience confirmed",
              "Audience confirmed." if audience_ok else
              "Tell YouTube whether your videos are made for kids. Autopilot never guesses this.",
              None if audience_ok else "Choose your channel's audience",
              "youtube"),
        _item("instagram_connected", "required", ig_ok, "Instagram connected through Meta",
              ig_msg, None if ig_ok else "Connect Instagram", "instagram"),
        _item("ai_engine", "advisory", llm.is_configured(), "AI engine",
              "AI engine connected." if llm.is_configured() else
              "Without it, titles and captions come from simple rules and every post waits for your approval.",
              None if llm.is_configured() else "Add your AI engine key"),
        _item("oauth_production", "advisory", bool(yt and yt.get("oauth_production_declared")),
              "Google Cloud app published (In production)",
              "Declared." if yt and yt.get("oauth_production_declared") else
              "Google sign-in expires every 7 days while your Google Cloud app is in \"Testing\".",
              None if yt and yt.get("oauth_production_declared") else
              "Publish the app in Google Cloud, then confirm here", "youtube"),
        _item("youtube_audit", "advisory", bool(yt and yt.get("audit_declared")),
              "YouTube API audit passed",
              "Declared." if yt and yt.get("audit_declared") else
              "YouTube uploads will stay private until your Google Cloud project passes YouTube's API audit.",
              None if yt and yt.get("audit_declared") else YT_AUDIT_FORM, "youtube"),
        _item("text_shaping", "advisory", _shaping_available(), "Text shaping for complex scripts",
              "Hindi, Arabic, Bengali and similar scripts render correctly." if _shaping_available() else
              "Subtitles in Hindi, Arabic, Bengali and similar scripts will wait for your approval.",
              None if _shaping_available() else "brew install fribidi"),
        _item("face_detector", "advisory", _face_detector_available(), "Face detector",
              "Fill layout keeps faces in frame." if _face_detector_available() else
              "Clips use the Fit layout (blurred background) instead of Fill.",
              None if _face_detector_available() else "scripts/fetch_models.sh"),
        _item("timezone", "setting", tz is not None, "Time zone",
              f"Posting times use {tz}." if tz else "Not set yet; your browser's time zone is offered.",
              None if tz else "Choose your time zone"),
    ]
    platforms = {}
    for platform in ("youtube", "instagram"):
        missing = [i for i in items if i["kind"] == "required" and i["platform"] == platform
                   and not i["ok"]]
        platforms[platform] = {"ready": not missing,
                               "reasons": [i["message"] for i in missing]}
    return {"items": items, "platforms": platforms}


def platform_ready(owner: str, platform: str) -> tuple[bool, list[str]]:
    state = check(owner)["platforms"].get(platform)
    if state is None:
        return False, [f"Unknown platform: {platform}"]
    return state["ready"], state["reasons"]
