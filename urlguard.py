"""URL validation / SSRF guard for the video-URL input.

The /api/process URL is handed to yt-dlp, so without validation a user could
point it at internal addresses (e.g. cloud metadata 169.254.169.254) or
arbitrary hosts. This restricts input to YouTube by default and always blocks
private/loopback/link-local/reserved targets.

Set CLIPMAKER_ALLOW_ANY_SITE=1 to lift the YouTube-only restriction for a
trusted local install (the internal-address blocks still apply).
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
}


class UrlError(ValueError):
    pass


def _allow_any() -> bool:
    return os.environ.get("CLIPMAKER_ALLOW_ANY_SITE", "").strip().lower() in (
        "1", "true", "yes", "on")


def _blocked_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _is_youtube(host: str) -> bool:
    return host in _YOUTUBE_HOSTS or host.endswith(".youtube.com")


def validate(url: str) -> str:
    """Return the cleaned URL if allowed, else raise UrlError."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UrlError("Only http(s) links are allowed.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UrlError("That URL has no host.")

    # Reject internal IP-literal hosts outright.
    if _blocked_ip(host):
        raise UrlError("That address is not allowed.")

    if not _allow_any() and not _is_youtube(host):
        raise UrlError("Only YouTube links are allowed.")

    # Defense-in-depth: block hosts that resolve to internal networks.
    try:
        for info in socket.getaddrinfo(host, None):
            if _blocked_ip(info[4][0]):
                raise UrlError("That host resolves to a blocked network.")
    except socket.gaierror:
        pass  # let yt-dlp surface genuine DNS failures

    return url
