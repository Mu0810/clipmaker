"""A stateful fake of Google's token endpoint and the YouTube Data API v3,
for the `net` fixture: `FakeYouTube(net)` registers itself on
oauth2.googleapis.com and www.googleapis.com.

It keeps resumable sessions (with the bytes received), videos with full
snippet and status objects, a channel with an uploads playlist, and every
request it served. Knobs for tests: `token_error`, `expire_session`,
`accept_limit` (bytes accepted from the next chunk), `fail_next` (status,
body) and `thumbnail_status`.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

import httpx

CHANNEL = {"id": "UCchannel123", "snippet": {"title": "Manish Clips", "thumbnails": {
    "default": {"url": "https://yt3.example/avatar.jpg"}}},
    "contentDetails": {"relatedPlaylists": {"uploads": "UUchannel123"}}}


class FakeYouTube:
    def __init__(self, net):
        self.requests: list[httpx.Request] = []
        self.sessions: dict[str, dict] = {}
        self.videos: dict[str, dict] = {}
        self.playlist: list[str] = []
        self.tokens_issued = 0
        self.token_error: dict | None = None
        self.accept_limit: int | None = None
        self.fail_next: tuple[int, dict] | None = None
        self.thumbnail_status = 200
        self.thumbnails: dict[str, bytes] = {}
        self.unauthorised_once = False
        self.revoked: list[str] = []
        self.revoke_status = 200
        net.register("oauth2.googleapis.com", self.handle)
        net.register("www.googleapis.com", self.handle)

    # ── helpers ─────────────────────────────────────────────────────────

    def add_video(self, video_id: str, *, title="A clip", published="2026-11-02T03:30:00Z",
                  duration="PT10S", status=None, snippet=None) -> dict:
        # A given status replaces the default schedule: an upload sent without
        # publishAt has none.
        base_status = {"uploadStatus": "uploaded", "privacyStatus": "private",
                       "publishAt": "2026-11-03T12:30:00Z", "license": "youtube",
                       "embeddable": True, "publicStatsViewable": True, "madeForKids": False,
                       "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False}
        if status is not None:
            base_status.pop("publishAt")
            base_status.update(status)
        video = {"id": video_id,
                 "snippet": {"title": title, "description": "Old description",
                             "tags": ["a", "b"], "categoryId": "26", "defaultLanguage": "en",
                             "publishedAt": published, "channelId": CHANNEL["id"],
                             "thumbnails": {"default": {"url": "x"}}, "channelTitle": "Manish",
                             "localized": {"title": title}, **(snippet or {})},
                 "status": base_status,
                 "contentDetails": {"duration": duration},
                 "statistics": {"viewCount": "120", "likeCount": "7", "commentCount": "2"}}
        self.videos[video_id] = video
        self.playlist.insert(0, video_id)
        return video

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    @staticmethod
    def _json(status: int, body=None, headers=None) -> httpx.Response:
        return httpx.Response(status, json=body if body is not None else {}, headers=headers)

    # ── routing ─────────────────────────────────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return self._token(request)
        if self.fail_next:
            status, body = self.fail_next
            self.fail_next = None
            return self._json(status, body)
        if self.unauthorised_once and request.url.path.startswith("/youtube/"):
            self.unauthorised_once = False
            return self._json(401, {"error": {"code": 401, "errors": [{"reason": "authError"}]}})
        path, q = request.url.path, request.url.params
        if path == "/upload/youtube/v3/videos" and request.method == "POST":
            return self._init(request)
        if path == "/upload/youtube/v3/videos" and request.method == "PUT":
            return self._chunk(request, q.get("upload_id"))
        if path == "/upload/youtube/v3/thumbnails/set":
            if self.thumbnail_status != 200:
                return self._json(self.thumbnail_status, {"error": {
                    "code": self.thumbnail_status, "errors": [{"reason": "forbidden"}]}})
            self.thumbnails[q.get("videoId")] = request.content
            return self._json(200, {"items": [{"default": {"url": "thumb"}}]})
        if path == "/youtube/v3/videos" and request.method == "GET":
            ids = (q.get("id") or "").split(",")
            parts = (q.get("part") or "").split(",")
            items = [{"id": v["id"], **{p: v[p] for p in parts if p in v}}
                     for i in ids if (v := self.videos.get(i))]
            return self._json(200, {"items": items})
        if path == "/youtube/v3/videos" and request.method == "PUT":
            body = json.loads(request.content)
            video = self.videos[body["id"]]
            # Like YouTube: the snippet and status are replaced as sent.
            keep_snippet = {k: video["snippet"][k] for k in ("publishedAt", "channelId",
                                                             "thumbnails", "channelTitle")}
            video["snippet"] = {**keep_snippet, **body["snippet"]}
            video["status"] = {"uploadStatus": "uploaded", "madeForKids": False,
                               **body["status"]}
            return self._json(200, video)
        if path == "/youtube/v3/videos" and request.method == "DELETE":
            gone = self.videos.pop(q.get("id"), None)
            return httpx.Response(204 if gone else 404)
        if path == "/youtube/v3/channels":
            return self._json(200, {"items": [CHANNEL]})
        if path == "/youtube/v3/playlistItems":
            limit = int(q.get("maxResults") or 5)
            return self._json(200, {"items": [{"contentDetails": {"videoId": v}}
                                              for v in self.playlist[:limit]]})
        return self._json(404, {"error": {"code": 404, "errors": [{"reason": "notFound"}]}})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.url.path == "/revoke":
            self.revoked.append(form.get("token"))
            return httpx.Response(self.revoke_status)
        if self.token_error:
            return self._json(400, self.token_error)
        if form.get("grant_type") != "refresh_token" or not form.get("refresh_token"):
            return self._json(400, {"error": "invalid_request"})
        self.tokens_issued += 1
        return self._json(200, {"access_token": f"ya29.fresh{self.tokens_issued}",
                                "expires_in": 3599, "token_type": "Bearer",
                                "scope": "https://www.googleapis.com/auth/youtube "
                                         "https://www.googleapis.com/auth/youtube.upload"})

    def _init(self, request: httpx.Request) -> httpx.Response:
        sid = f"sess{len(self.sessions) + 1}"
        self.sessions[sid] = {"size": int(request.headers["x-upload-content-length"]),
                              "meta": json.loads(request.content), "data": bytearray(),
                              "gone": None}
        return httpx.Response(200, headers={
            "Location": f"https://www.googleapis.com/upload/youtube/v3/videos"
                        f"?uploadType=resumable&upload_id={sid}",
            "X-GUploader-UploadID": f"guploader-{sid}"})

    def expire_session(self, sid: str, status: int = 404) -> None:
        self.sessions[sid]["gone"] = status

    def _chunk(self, request: httpx.Request, sid: str) -> httpx.Response:
        session = self.sessions.get(sid)
        if session is None or session["gone"]:
            return httpx.Response(session["gone"] if session else 404)
        size = session["size"]
        m = re.match(r"bytes (\*|(\d+)-(\d+))/(\d+)", request.headers.get("content-range", ""))
        assert m and int(m.group(4)) == size, request.headers.get("content-range")
        if m.group(1) != "*":
            start = int(m.group(2))
            assert start == len(session["data"]), (start, len(session["data"]))
            data = request.content
            if self.accept_limit is not None:
                data, self.accept_limit = data[:self.accept_limit], None
            session["data"].extend(data)
        received = len(session["data"])
        if received >= size:
            video_id = f"vid_{sid}"
            meta = session["meta"]
            self.add_video(video_id, title=meta["snippet"]["title"], status=meta["status"],
                           snippet=meta["snippet"])
            return httpx.Response(200, json=self.videos[video_id],
                                  headers={"X-GUploader-UploadID": f"guploader-{sid}"})
        headers = {"Range": f"bytes=0-{received - 1}"} if received else {}
        return httpx.Response(308, headers=headers)
