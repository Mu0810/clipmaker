"""Instagram Reels upload via the unofficial private API (instagrapi).

This logs in with a username/password (and a 2FA code when required) and
publishes a local clip as a Reel. The authenticated session is cached in
`ig_session.json` so the password isn't stored and subsequent uploads are
silent.

⚠️  This uses Instagram's *private* API, which is against Instagram's Terms of
Service and can lead to temporary blocks or account bans. Use a personal/creator
account you're willing to risk, and don't upload content you don't own.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import paths


def _session_file() -> Path:
    return paths.data_file("ig_session.json")


def _meta_file() -> Path:
    """Small cache of username/display info."""
    return paths.data_file("ig_account.json")


class InstagramError(RuntimeError):
    pass


class TwoFactorRequired(InstagramError):
    """Raised when a login needs a 6-digit 2FA code."""


def is_connected() -> bool:
    return _session_file().exists()


def _client():
    """Build an instagrapi Client, loading a cached session if present."""
    try:
        from instagrapi import Client
    except Exception as e:  # not installed yet
        raise InstagramError(
            "Instagram support needs the 'instagrapi' package. Install it: "
            "pip install instagrapi"
        ) from e

    cl = Client()
    cl.delay_range = [1, 3]            # be gentle to avoid rate limits
    if _session_file().exists():
        try:
            cl.load_settings(str(_session_file()))
        except Exception:
            pass
    return cl


def _save_account(cl) -> dict:
    """Cache lightweight display info for the connected account."""
    try:
        acct = cl.account_info()
        info = {
            "username": acct.username,
            "full_name": getattr(acct, "full_name", "") or "",
            "profile_pic": str(getattr(acct, "profile_pic_url", "") or ""),
        }
    except Exception:
        info = {"username": getattr(cl, "username", "") or "", "full_name": "",
                "profile_pic": ""}
    try:
        _meta_file().write_text(json.dumps(info))
    except Exception:
        pass
    return info


def connect(username: str, password: str,
            verification_code: str = "") -> dict:
    """Log in and cache the session. Returns account info.

    Raises TwoFactorRequired if a 2FA code is needed, InstagramError otherwise.
    """
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        raise InstagramError("Username and password are required.")

    from instagrapi.exceptions import (
        BadPassword, ChallengeRequired, TwoFactorRequired as _TwoFactor,
    )

    cl = _client()
    try:
        if verification_code.strip():
            cl.login(username, password,
                     verification_code=verification_code.strip())
        else:
            cl.login(username, password)
    except _TwoFactor as e:
        raise TwoFactorRequired(
            "Two-factor is enabled — enter the 6-digit code and try again."
        ) from e
    except BadPassword as e:
        raise InstagramError("Incorrect username or password.") from e
    except ChallengeRequired as e:
        raise InstagramError(
            "Instagram flagged this login (challenge required). Approve the "
            "login in the Instagram app, then try again."
        ) from e
    except Exception as e:
        raise InstagramError(f"Login failed: {e}") from e

    cl.dump_settings(str(_session_file()))
    return _save_account(cl)


def disconnect() -> None:
    for f in (_session_file(), _meta_file()):
        if f.exists():
            f.unlink()


def account_status() -> dict:
    """{connected, username, full_name, profile_pic} for the UI."""
    if not _session_file().exists():
        return {"connected": False, "account": None}
    account = None
    if _meta_file().exists():
        try:
            account = json.loads(_meta_file().read_text())
        except Exception:
            account = None
    return {"connected": True, "account": account}


def _make_thumbnail(video: Path) -> Path | None:
    """Grab a cover frame with ffmpeg so instagrapi doesn't need MoviePy.

    Instagram requires a JPEG cover; pulling one frame ourselves avoids the
    heavy MoviePy dependency since we already rely on ffmpeg.
    """
    thumb = video.with_suffix(".cover.jpg")
    for ss in ("1", "0"):             # try ~1s in, fall back to first frame
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-ss", ss, "-i", str(video),
                 "-frames:v", "1", "-q:v", "2", str(thumb)],
                capture_output=True, text=True, timeout=60)
            if proc.returncode == 0 and thumb.exists() and thumb.stat().st_size:
                return thumb
        except Exception:
            pass
    return None


def upload_reel(file_path: Path, caption: str = "") -> dict:
    """Publish a local mp4 as a Reel. Returns {code, url}."""
    if not _session_file().exists():
        raise InstagramError("No Instagram account connected. Connect one first.")
    if not file_path.exists():
        raise InstagramError(f"Clip not found: {file_path}")

    cl = _client()
    try:
        cl.get_timeline_feed()        # validates the cached session
    except Exception as e:
        raise InstagramError(
            "Instagram session expired — reconnect your account."
        ) from e

    thumb = _make_thumbnail(file_path)
    try:
        kwargs = {"thumbnail": thumb} if thumb else {}
        media = cl.clip_upload(file_path, caption[:2200], **kwargs)
    except Exception as e:
        raise InstagramError(f"Reel upload failed: {e}") from e
    finally:
        if thumb and thumb.exists():
            thumb.unlink(missing_ok=True)

    code = getattr(media, "code", None)
    return {"code": code, "url": f"https://www.instagram.com/reel/{code}/"
            if code else "https://www.instagram.com/"}
