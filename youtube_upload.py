"""YouTube Data API v3 upload via OAuth.

Requires a `client_secret.json` (OAuth Desktop client) in this directory. It can
be created in Google Cloud or pasted/uploaded straight from the web UI via
`save_client_secret`. Connecting an account (`connect`) opens a browser once for
consent; the resulting token is cached in `token.json` for silent uploads and
channel reads afterwards.
"""
from __future__ import annotations

import json
from pathlib import Path

import paths


def _client_secret() -> Path:
    return paths.data_file("client_secret.json")


def _token_file() -> Path:
    return paths.data_file("token.json")

# Upload + read-only so we can show which channel is connected.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


class UploadError(RuntimeError):
    pass


def client_configured() -> bool:
    return _client_secret().exists()


# Backwards-compatible alias used elsewhere.
def credentials_present() -> bool:
    return client_configured()


def is_connected() -> bool:
    """An account is connected once a usable token is cached."""
    return _token_file().exists()


def save_client_secret(raw: str) -> None:
    """Validate and persist a pasted/uploaded OAuth client_secret.json."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UploadError(f"That isn't valid JSON: {e}") from e

    # A Desktop OAuth client file has either an "installed" or "web" root key.
    root = data.get("installed") or data.get("web")
    if not isinstance(root, dict) or "client_id" not in root:
        raise UploadError(
            "This doesn't look like an OAuth client file. Download the "
            "'Desktop app' OAuth client JSON from Google Cloud and use that."
        )
    _client_secret().write_text(json.dumps(data, indent=2))


def _credentials_from_cache(refresh: bool = True):
    """Return cached credentials, refreshing if needed. None if not connected."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not _token_file().exists():
        return None
    # Use the scopes stored with the token: passing today's narrow list here
    # would make the refreshed token.json forget a broader Autopilot grant.
    creds = Credentials.from_authorized_user_file(str(_token_file()))
    if creds and creds.valid:
        return creds
    if refresh and creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _token_file().write_text(creds.to_json())
        return creds
    return creds if creds else None


def connect() -> dict:
    """Run the OAuth consent flow (opens a browser once) and cache the token.

    Returns the connected channel info. Raises UploadError on failure.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not _client_secret().exists():
        raise UploadError(
            "Add your Google OAuth client first (client_secret.json)."
        )
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(_client_secret()), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
    except Exception as e:  # consent denied, port issue, bad client, etc.
        raise UploadError(f"Could not connect: {e}") from e

    _token_file().write_text(creds.to_json())
    info = _channel_info(creds)
    if info is None:
        # Connected, but channel read failed — still usable for uploads.
        return {"connected": True, "channel": None}
    return {"connected": True, "channel": info}


def disconnect() -> None:
    """Forget the connected account (delete the cached token)."""
    if _token_file().exists():
        _token_file().unlink()


def _channel_info(creds) -> dict | None:
    from googleapiclient.discovery import build

    try:
        youtube = build("youtube", "v3", credentials=creds,
                        cache_discovery=False)
        resp = youtube.channels().list(
            part="snippet,statistics", mine=True).execute()
        items = resp.get("items") or []
        if not items:
            return None
        it = items[0]
        snip = it.get("snippet", {})
        stats = it.get("statistics", {})
        thumbs = snip.get("thumbnails", {})
        thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url")
        return {
            "id": it.get("id"),
            "title": snip.get("title"),
            "custom_url": snip.get("customUrl"),
            "thumbnail": thumb,
            "subscribers": stats.get("subscriberCount"),
            "videos": stats.get("videoCount"),
        }
    except Exception:
        return None


def account_status() -> dict:
    """Snapshot for the UI: is a client configured, and who's connected."""
    if not client_configured():
        return {"client_configured": False, "connected": False, "channel": None}
    if not is_connected():
        return {"client_configured": True, "connected": False, "channel": None}
    creds = _credentials_from_cache()
    if not creds:
        return {"client_configured": True, "connected": False, "channel": None}
    return {
        "client_configured": True,
        "connected": True,
        "channel": _channel_info(creds),
    }


def _get_credentials():
    """Credentials for an upload; runs consent if not yet connected."""
    if not _client_secret().exists():
        raise UploadError(
            "client_secret.json is missing. Add your Google OAuth client from "
            "the YouTube panel (or place the file in the clipmaker folder)."
        )
    creds = _credentials_from_cache()
    if creds and creds.valid:
        return creds
    # Not connected yet — fall back to the interactive flow.
    return connect() and _credentials_from_cache()


def upload_video(file_path: Path, title: str, description: str = "",
                 tags: list[str] | None = None, privacy: str = "unlisted",
                 progress=None) -> dict:
    """Resumable upload. privacy in {private, unlisted, public}.
    Returns {video_id, url}."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    if privacy not in {"private", "unlisted", "public"}:
        privacy = "unlisted"
    if not file_path.exists():
        raise UploadError(f"Clip not found: {file_path}")

    creds = _credentials_from_cache()
    if not creds or not creds.valid:
        raise UploadError("No YouTube account connected. Connect one first.")
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": title[:100] or "Clip",
            "description": description[:4900],
            "tags": (tags or [])[:30],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(str(file_path), chunksize=1024 * 1024 * 4,
                            resumable=True, mimetype="video/*")
    request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status and progress:
            progress(int(status.progress() * 100))

    vid = response["id"]
    return {"video_id": vid, "url": f"https://youtu.be/{vid}"}
