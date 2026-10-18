"""YouTube Data API v3 client for Autopilot (design §8.2; Requirements 8.1-8.5,
8.9, 10.4, 12.5).

`YouTubeAPI(owner)` calls YouTube with the owner's Google credentials
(platforms/google_creds.py) through the guarded client (platforms/http.py).
Failures raise errors.PlatformError, classified by the §8.6 table. A call that
answers 401 is retried once after refreshing the token.

Uploads use YouTube's resumable protocol. `init_upload()` creates the session
and returns its URI without sending any bytes, so the caller can store it
first (§8.2); `upload()` sends 8 MiB chunks, and after an interruption asks
where the session got to (an empty PUT with `Content-Range: bytes */<size>`):
308 resumes after the reported `Range`, 200 or 201 means done, and 404 or 410
raise SessionGone (the publisher then looks the video up, `yt_lookup`).

Quota, per Google Cloud project (the OAuth client id), counted per Pacific
day (platforms/usage.py):
* `yt-uploads:<client id>`: at most 100 uploads a day (10.4); the 101st is
  refused before anything is sent;
* `yt-units:<client id>`: list calls 1 unit, update, delete and
  `thumbnails.set` 50. Statistics stop at 9,000 units a day, so publishing
  always keeps headroom; publishing calls are counted and left to YouTube's
  own `quotaExceeded`.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from platforms import errors, google_creds, http, usage

API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
CHUNK = 8 * 1024 * 1024                       # 8 MiB, a multiple of 256 KiB
UPLOADS_PER_DAY = 100
DAILY_UNITS = 10_000
METRICS_UNIT_CAP = 9_000
UNITS = {"list": 1, "update": 50, "delete": 50, "thumbnail": 50}
PUBLISH_LEAD_S = 15 * 60
SNIPPET_WRITABLE = ("title", "description", "tags", "categoryId", "defaultLanguage",
                    "defaultAudioLanguage")
STATUS_WRITABLE = ("privacyStatus", "publishAt", "license", "embeddable",
                   "publicStatsViewable", "selfDeclaredMadeForKids", "containsSyntheticMedia")


class SessionGone(errors.PlatformError):
    """The resumable session no longer exists (404 or 410): look the video up."""

    def __init__(self, status: int, request_id: str | None = None):
        super().__init__(errors.Classified(errors.TEMPORARY, str(status),
                                           "The YouTube upload session expired."),
                         status, request_id)


def rfc3339(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def parse_duration(value: str | None) -> float | None:
    """YouTube's ISO 8601 duration ('PT1M5S') in seconds."""
    m = _DURATION.match(value or "")
    if not m:
        return None
    d, h, mi, s = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def build_snippet(meta: dict, language: str | None = None) -> dict:
    snippet = {"title": meta.get("title") or "", "description": meta.get("description") or "",
               "tags": list(meta.get("tags") or []),
               "categoryId": str(meta.get("category_id") or "22")}
    if language:
        snippet["defaultLanguage"] = language
    return snippet


def build_status(*, target_visibility: str, slot_at: float | None, now: float,
                 made_for_kids: bool, synthetic: bool) -> dict:
    """§8.2: always uploaded private. A Public target gets YouTube's scheduled
    publish time, at the slot but at least 15 minutes from now (8.2); a
    Private target stays private (8.3). Made for kids comes from the channel
    audience the user confirmed (4.7), the synthetic flag from the post (4.8)."""
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": bool(made_for_kids),
              "containsSyntheticMedia": bool(synthetic)}
    if target_visibility == "public":
        status["publishAt"] = rfc3339(max(slot_at or now, now + PUBLISH_LEAD_S))
    return status


def _json(response: httpx.Response) -> dict:
    try:
        return response.json() if response.content else {}
    except ValueError:
        return {"_text": response.text}


class YouTubeAPI:
    def __init__(self, owner: str, *, client: httpx.Client | None = None, clock=time.time):
        self.owner, self.clock = owner, clock
        self._own = client is None
        self.client = client or http.client()

    def close(self) -> None:
        if self._own:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── plumbing ────────────────────────────────────────────────────────

    def _quota_key(self, kind: str) -> str:
        cid = google_creds.client_id(self.owner)
        if not cid:
            raise google_creds._not_connected()
        return f"yt-{kind}:{cid}"

    def _count_units(self, units: int) -> None:
        usage.reserve(self._quota_key("units"), units, 10 ** 9, self.clock())

    def _send(self, method: str, url: str, *, write: bool, units: int = 0,
              expect=(200,), **kwargs) -> httpx.Response:
        """One authorised call; raises PlatformError on anything not in `expect`."""
        headers = dict(kwargs.pop("headers", None) or {})
        if units:
            self._count_units(units)
        for attempt in (1, 2):
            token, _ = google_creds.access_token(self.owner, client=self.client,
                                                 now=self.clock(), force=attempt == 2)
            headers["Authorization"] = f"Bearer {token}"
            try:
                response = self.client.request(method, url, headers=headers, **kwargs)
            except http.RequestRefused:
                raise
            except Exception as exc:
                raise errors.PlatformError(errors.classify_exception("youtube", exc)) from exc
            if response.status_code == 401 and attempt == 1:
                continue                           # refresh once and try again
            if response.status_code in expect:
                return response
            raise errors.PlatformError(
                errors.classify("youtube", response.status_code, _json(response), write=write),
                response.status_code, http.request_id(response))
        raise AssertionError("unreachable")


    # ── resumable upload ────────────────────────────────────────────────

    def init_upload(self, path, snippet: dict, status: dict) -> dict:
        """Create a resumable session for the file; no video bytes are sent.
        Counts the day's upload first (refused at 100, 10.4) and gives the
        count back if YouTube didn't create a session. Returns {"uri",
        "size", "sha256", "started_at", "request_id"} for the caller to store
        before calling upload()."""
        path = Path(path)
        size = path.stat().st_size
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        key, now = self._quota_key("uploads"), self.clock()
        if not usage.reserve(key, 1, UPLOADS_PER_DAY, now):
            raise errors.PlatformError(errors.Classified(
                errors.DEFERRED, "uploadQuota",
                f"This Google Cloud project has used its {UPLOADS_PER_DAY} YouTube uploads "
                "for today; uploads resume after midnight Pacific time.",
                resume="pacific_midnight"))
        try:
            response = self._send(
                "POST", UPLOAD_URL, write=True,
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={"X-Upload-Content-Length": str(size),
                         "X-Upload-Content-Type": "video/mp4",
                         "Content-Type": "application/json; charset=UTF-8"},
                content=json.dumps({"snippet": snippet, "status": status}).encode("utf-8"))
        except errors.PlatformError:
            usage.release(key, 1, now)
            raise
        uri = response.headers.get("location")
        if not uri:
            usage.release(key, 1, now)
            raise errors.PlatformError(errors.Classified(
                errors.TEMPORARY, "noSession", "YouTube didn't return an upload session."),
                response.status_code, http.request_id(response))
        http.check_url(uri)                         # the session must stay on YouTube's host
        return {"uri": uri, "size": size, "sha256": digest.hexdigest(), "started_at": now,
                "request_id": http.request_id(response)}

    def _finished(self, response: httpx.Response) -> dict:
        body = _json(response)
        return {"done": True, "offset": None, "video": body, "video_id": body.get("id"),
                "request_id": http.request_id(response)}

    def upload_status(self, session: dict) -> dict:
        """Where the session got to: {"done", "offset", ...}. Raises SessionGone."""
        response = self._send("PUT", session["uri"], write=True, expect=(200, 201, 308, 404, 410),
                              headers={"Content-Range": f"bytes */{session['size']}",
                                       "Content-Length": "0"}, content=b"")
        if response.status_code in (404, 410):
            raise SessionGone(response.status_code, http.request_id(response))
        if response.status_code in (200, 201):
            return self._finished(response)
        return {"done": False, "offset": _next_offset(response), "video": None,
                "video_id": None, "request_id": http.request_id(response)}

    def upload(self, session: dict, path, *, resume: bool = False, progress=None) -> dict:
        """Send the file in 8 MiB chunks. With `resume`, ask where the session
        got to first. Returns the finished status (video id and resource)."""
        path, size = Path(path), session["size"]
        if path.stat().st_size != size:
            raise errors.PlatformError(errors.Classified(
                errors.PERMANENT, "fileChanged",
                "The video file changed since its upload started."))
        offset = 0
        if resume:
            state = self.upload_status(session)
            if state["done"]:
                return state
            offset = state["offset"]
        with path.open("rb") as f:
            while True:
                f.seek(offset)
                chunk = f.read(CHUNK) if offset < size else b""
                end = offset + len(chunk) - 1
                response = self._send(
                    "PUT", session["uri"], write=True, expect=(200, 201, 308, 404, 410),
                    headers={"Content-Range": f"bytes {offset}-{end}/{size}",
                             "Content-Length": str(len(chunk)),
                             "Content-Type": "video/mp4"},
                    content=chunk)
                if response.status_code in (404, 410):
                    raise SessionGone(response.status_code, http.request_id(response))
                if response.status_code in (200, 201):
                    if progress:
                        progress(1.0)
                    return self._finished(response)
                new_offset = _next_offset(response)
                if new_offset <= offset and chunk:
                    raise errors.PlatformError(errors.Classified(
                        errors.TEMPORARY, "noProgress",
                        "YouTube didn't accept any of the last chunk."),
                        response.status_code, http.request_id(response))
                offset = new_offset
                if progress:
                    progress(offset / size if size else 1.0)

    # ── videos ──────────────────────────────────────────────────────────

    def videos(self, ids, part: str = "snippet,status") -> list[dict]:
        """videos.list for up to 50 ids (1 unit)."""
        ids = [i for i in ids if i][:50]
        if not ids:
            return []
        response = self._send("GET", f"{API}/videos", write=False, units=UNITS["list"],
                              params={"part": part, "id": ",".join(ids), "maxResults": 50})
        return _json(response).get("items") or []

    def update_video(self, video_id: str, *, snippet: dict | None = None,
                     status: dict | None = None) -> dict:
        """videos.update with the FULL snippet and status: YouTube deletes any
        property an update leaves out, so the current values are read first and
        the changes applied on top. A change of None removes that property
        (publishAt: None unschedules the video)."""
        items = self.videos([video_id], "snippet,status")
        if not items:
            raise errors.PlatformError(errors.Classified(
                errors.PERMANENT, "videoNotFound", "The video is no longer on YouTube."), 404)
        current = items[0]
        merged_snippet = {k: v for k, v in (current.get("snippet") or {}).items()
                          if k in SNIPPET_WRITABLE}
        merged_status = {k: v for k, v in (current.get("status") or {}).items()
                         if k in STATUS_WRITABLE}
        for target, changes in ((merged_snippet, snippet), (merged_status, status)):
            for key, value in (changes or {}).items():
                if value is None:
                    target.pop(key, None)
                else:
                    target[key] = value
        response = self._send("PUT", f"{API}/videos", write=True, units=UNITS["update"],
                              params={"part": "snippet,status"},
                              json={"id": video_id, "snippet": merged_snippet,
                                    "status": merged_status})
        return _json(response)

    def delete_video(self, video_id: str) -> bool:
        """videos.delete. True when deleted, False when it was already gone."""
        response = self._send("DELETE", f"{API}/videos", write=True, units=UNITS["delete"],
                              expect=(200, 204, 404), params={"id": video_id})
        return response.status_code != 404


    def set_thumbnail(self, video_id: str, image_path) -> bool:
        """thumbnails.set with the 16:9 JPEG. False when YouTube refuses custom
        thumbnails for this channel (a 403): the caller then records
        thumbnail_capability = unavailable and shows that YouTube picks the
        frame (8.9). Quota and server errors still raise."""
        data = Path(image_path).read_bytes()
        try:
            self._send("POST", THUMBNAIL_URL, write=True, units=UNITS["thumbnail"],
                       params={"videoId": video_id, "uploadType": "media"},
                       headers={"Content-Type": "image/jpeg"}, content=data)
        except errors.PlatformError as exc:
            if exc.status == 403 and exc.kind not in (errors.DEFERRED, errors.TEMPORARY) \
                    and exc.result.code != "insufficientPermissions":
                return False
            raise
        return True

    # ── channel and uploads ─────────────────────────────────────────────

    def channel(self) -> dict:
        """The connected channel: {"id", "title", "avatar", "uploads_playlist"}."""
        cached = getattr(self, "_channel", None)
        if cached:
            return cached
        response = self._send("GET", f"{API}/channels", write=False, units=UNITS["list"],
                              params={"part": "snippet,contentDetails", "mine": "true"})
        items = _json(response).get("items") or []
        if not items:
            raise errors.PlatformError(errors.Classified(
                errors.ACCESS, "noChannel", "This Google account has no YouTube channel.",
                reason="reconnect"))
        item = items[0]
        snippet = item.get("snippet") or {}
        self._channel = {
            "id": item.get("id"), "title": snippet.get("title"),
            "avatar": ((snippet.get("thumbnails") or {}).get("default") or {}).get("url"),
            "uploads_playlist": ((item.get("contentDetails") or {}).get("relatedPlaylists")
                                 or {}).get("uploads")}
        return self._channel

    def recent_uploads(self, limit: int = 10) -> list[dict]:
        """The channel's newest uploads: [{"id", "title", "published_at", "duration"}]."""
        playlist = self.channel().get("uploads_playlist")
        if not playlist:
            return []
        response = self._send("GET", f"{API}/playlistItems", write=False, units=UNITS["list"],
                              params={"part": "contentDetails", "playlistId": playlist,
                                      "maxResults": limit})
        ids = [(i.get("contentDetails") or {}).get("videoId")
               for i in _json(response).get("items") or []]
        out = []
        for item in self.videos(ids, "snippet,contentDetails"):
            out.append({"id": item.get("id"),
                        "title": (item.get("snippet") or {}).get("title"),
                        "published_at": parse_time((item.get("snippet") or {}).get("publishedAt")),
                        "duration": parse_duration((item.get("contentDetails") or {}).get("duration"))})
        return out

    def find_upload(self, title: str, started_at: float, duration: float | None) -> str | None:
        """yt_lookup (§8.2): the id of a recent upload with the same title,
        uploaded no earlier than the session start minus 60 s, and lasting
        within 1 s of the clip (not checked when the duration isn't known).
        None when there's no such video."""
        for video in self.recent_uploads():
            if (video["title"] == title and video["published_at"] is not None
                    and video["published_at"] >= started_at - 60
                    and (duration is None or (video["duration"] is not None
                                              and abs(video["duration"] - duration) <= 1.0))):
                return video["id"]
        return None

    # ── statistics (12.5) ───────────────────────────────────────────────

    def statistics(self, ids) -> dict[str, dict]:
        """Views, likes and comments, 50 videos per call (1 unit each). Stops
        when the day's units would pass 9,000, keeping headroom for publishing."""
        ids = [i for i in dict.fromkeys(ids) if i]
        out: dict[str, dict] = {}
        key = self._quota_key("units") if ids else None
        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            if not usage.reserve(key, UNITS["list"], METRICS_UNIT_CAP, self.clock()):
                raise errors.PlatformError(errors.Classified(
                    errors.DEFERRED, "metricsCap",
                    "Stopped fetching YouTube view counts for today, to keep quota for "
                    "publishing.", resume="pacific_midnight"))
            response = self._send("GET", f"{API}/videos", write=False,
                                  params={"part": "statistics", "id": ",".join(batch),
                                          "maxResults": 50})
            for item in _json(response).get("items") or []:
                stats = item.get("statistics") or {}
                out[item.get("id")] = {k: int(stats[s]) if str(stats.get(s, "")).isdigit()
                                       else None for k, s in (("views", "viewCount"),
                                                              ("likes", "likeCount"),
                                                              ("comments", "commentCount"))}
        return out


def _next_offset(response: httpx.Response) -> int:
    """The next byte to send after a 308: one past the end of the `Range`
    YouTube reports (`bytes=0-12345`), or 0 when it has nothing yet."""
    match = re.match(r"bytes=(\d+)-(\d+)", response.headers.get("range", ""))
    return int(match.group(2)) + 1 if match else 0
