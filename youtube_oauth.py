"""Per-user YouTube OAuth (web flow) for public deployments.

This differs from youtube_upload.py (desktop/local-browser flow used by the
single-user localhost tool). Here the *deployment* registers one Google Cloud
OAuth **web** client; each logged-in user connects their own channel via a
standard redirect flow, and their tokens are stored per-user in user_settings.

Inert unless BOTH are set:
  - CLIPMAKER_GOOGLE_CLIENT_SECRET : path to the web client_secret JSON
  - CLIPMAKER_OAUTH_REDIRECT       : the callback URL registered in Google,
                                     e.g. https://yourhost/api/youtube/oauth/callback

Requires google-auth-oauthlib + google-api-python-client (already in
requirements.txt). Google imports are lazy so the app still boots without them.
"""
from __future__ import annotations

import os
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def client_secret_path() -> str:
    return os.environ.get("CLIPMAKER_GOOGLE_CLIENT_SECRET", "").strip()


def redirect_uri() -> str:
    return os.environ.get("CLIPMAKER_OAUTH_REDIRECT", "").strip()


def enabled() -> bool:
    cs = client_secret_path()
    return bool(cs) and bool(redirect_uri()) and Path(cs).exists()


def _flow(state: str | None = None, scopes: list[str] | None = None):
    from google_auth_oauthlib.flow import Flow
    # With include_granted_scopes Google may return more scopes than were
    # requested (earlier grants); oauthlib treats that as an error unless relaxed.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    return Flow.from_client_secrets_file(
        client_secret_path(), scopes=scopes or SCOPES,
        redirect_uri=redirect_uri(), state=state)


def authorization_url(state: str, scopes: list[str] | None = None) -> str:
    """The Google consent URL. Autopilot passes its wider scope set; the
    manual flow keeps today's two scopes."""
    flow = _flow(state=state, scopes=scopes)
    url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent")
    return url


def exchange_code(code: str, state: str | None = None,
                  scopes: list[str] | None = None) -> dict:
    flow = _flow(state=state, scopes=scopes)
    flow.fetch_token(code=code)
    return _creds_to_dict(flow.credentials)


def _creds_to_dict(creds) -> dict:
    granted = getattr(creds, "granted_scopes", None) or creds.scopes or []
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": sorted(set(granted)),
        "expiry": creds.expiry.isoformat() + "Z" if getattr(creds, "expiry", None) else None,
    }


def _creds_from_dict(d: dict):
    from google.oauth2.credentials import Credentials
    return Credentials(
        d.get("token"), refresh_token=d.get("refresh_token"),
        token_uri=d.get("token_uri"), client_id=d.get("client_id"),
        client_secret=d.get("client_secret"), scopes=d.get("scopes"))


def channel_info(token: dict) -> dict:
    """Lightweight channel display info for the connected account UI."""
    from googleapiclient.discovery import build
    yt = build("youtube", "v3", credentials=_creds_from_dict(token),
               cache_discovery=False)
    items = yt.channels().list(part="snippet,statistics", mine=True
                               ).execute().get("items", [])
    if not items:
        return {}
    sn = items[0].get("snippet", {})
    st = items[0].get("statistics", {})
    return {
        "title": sn.get("title"),
        "thumbnail": sn.get("thumbnails", {}).get("default", {}).get("url"),
        "subscribers": st.get("subscriberCount"),
    }


def upload(token: dict, file_path, title: str, description: str = "",
           tags: list[str] | None = None, privacy: str = "unlisted") -> dict:
    """Upload a video to the token owner's channel via the official Data API."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    yt = build("youtube", "v3", credentials=_creds_from_dict(token),
               cache_discovery=False)
    body = {
        "snippet": {"title": title[:100], "description": description,
                    "tags": tags or []},
        "status": {"privacyStatus":
                   privacy if privacy in ("public", "unlisted", "private")
                   else "unlisted"},
    }
    media = MediaFileUpload(str(file_path), chunksize=-1, resumable=True,
                            mimetype="video/*")
    resp = yt.videos().insert(part="snippet,status", body=body,
                              media_body=media).execute()
    vid = resp.get("id")
    return {"url": f"https://youtu.be/{vid}" if vid else
            "https://www.youtube.com/", "id": vid}
