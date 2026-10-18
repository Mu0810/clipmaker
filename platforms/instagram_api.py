"""Instagram API client for Autopilot: Reels through Graph API v26.0 (design
§8.3; Requirements 9.1-9.5, 10.4, 12.5).

`InstagramAPI(owner)` publishes to the Instagram professional account chosen
when connecting (platforms/meta_auth.py). Every call carries the token in an
`Authorization: OAuth …` header, never a URL, and Graph calls carry
`appsecret_proof`. Failures raise errors.PlatformError, classified by §8.6.

Token choice (§8.3): Meta's guides disagree on which token publishes Reels.
The Page token is tried first; on an access or permission error the user
token is tried once, and the one that worked is recorded, so later calls
start with it.

Steps for one post, matching the publisher's: `publishing_limit()` (10.4,
9.5), `create_container()` (store its id and upload URI before uploading),
`upload()` / `resume_upload()` to rupload.facebook.com (a resume starts at
`video_status.uploading_phase.bytes_transferred`), `container_status()`,
`publish()`, then `media()`, or `find_media()` by caption when the media id
was lost, and `insights()` for views, likes and comments (12.5).
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from platforms import errors, http, meta_auth

RUPLOAD = "https://rupload.facebook.com/ig-api-upload/{version}/{container}"
STREAM_BLOCK = 1 << 20


def _not_connected(message="Instagram isn't connected; connect it to continue.") -> errors.PlatformError:
    return errors.PlatformError(errors.Classified(errors.ACCESS, "not_connected", message,
                                                  reason="reconnect"))


def _token_problem(exc: errors.PlatformError) -> bool:
    return exc.kind == errors.ACCESS and exc.result.reason in ("permission", "reconnect")


class InstagramAPI:
    def __init__(self, owner: str, *, client: httpx.Client | None = None, clock=time.time):
        self.owner, self.clock = owner, clock
        self.app = meta_auth.app_settings(owner)
        if self.app is None:
            raise _not_connected("Add your Meta app's settings, then connect Instagram.")
        self.conn = meta_auth.load_token(owner)
        if not self.conn or not self.conn.get("ig_user_id"):
            raise _not_connected()
        self.ig = self.conn["ig_user_id"]
        self._own = client is None
        self.client = client or http.client(timeout=http.UPLOAD_TIMEOUT)

    def close(self) -> None:
        if self._own:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── tokens ──────────────────────────────────────────────────────────

    def _tokens(self) -> list[tuple[str, str]]:
        order = ["user", "page"] if self.conn.get("publish_token") == "user" else ["page", "user"]
        tokens = {"page": self.conn.get("page_token"), "user": self.conn.get("user_token")}
        found = [(kind, tokens[kind]) for kind in order if tokens.get(kind)]
        if not found:
            raise _not_connected()
        return found

    def _with_token(self, call):
        """Run `call(token)` with the preferred token, falling back to the
        other once on an access error, and record the one that worked."""
        tokens = self._tokens()
        for i, (kind, token) in enumerate(tokens):
            try:
                result = call(token)
            except errors.PlatformError as exc:
                if i == 0 and len(tokens) > 1 and _token_problem(exc):
                    continue
                raise
            if self.conn.get("publish_token") != kind:
                meta_auth.record_token_choice(self.owner, kind)
                self.conn["publish_token"] = kind
            return result
        raise AssertionError("unreachable")

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._with_token(lambda token: meta_auth.graph_get(
            self.client, self.app, token, path, params))

    def _post(self, path: str, data: dict | None = None) -> dict:
        return self._with_token(lambda token: meta_auth.graph_post(
            self.client, self.app, token, path, data))

    # ── publishing ──────────────────────────────────────────────────────

    def publishing_limit(self) -> dict:
        """{"used", "limit", "window_s"} from content_publishing_limit (10.4)."""
        body = self._get(f"{self.ig}/content_publishing_limit",
                         {"fields": "quota_usage,config"})
        item = (body.get("data") or [{}])[0]
        config = item.get("config") or {}
        return {"used": int(item.get("quota_usage") or 0),
                "limit": int(config.get("quota_total") or 0) or None,
                "window_s": int(config.get("quota_duration") or 86400)}

    def create_container(self, caption: str, *, thumb_offset_ms: int | None = None,
                         ai_generated: bool = False) -> dict:
        """A resumable REELS container (§8.3). The AI label is sent only when
        the post's disclosure is on (4.8); Instagram's default is no label.
        Returns {"container_id", "upload_uri", "started_at"} to store before
        uploading."""
        data = {"media_type": "REELS", "upload_type": "resumable", "caption": caption or ""}
        if thumb_offset_ms is not None:
            data["thumb_offset"] = str(max(0, int(thumb_offset_ms)))
        if ai_generated:
            data["is_ai_generated"] = "true"
        body = self._post(f"{self.ig}/media", data)
        container = body.get("id")
        if not container:
            raise errors.PlatformError(errors.Classified(
                errors.TEMPORARY, "noContainer", "Instagram didn't return a container."))
        uri = body.get("uri") or RUPLOAD.format(version=meta_auth.GRAPH_VERSION,
                                                container=container)
        http.check_url(uri)
        return {"container_id": str(container), "upload_uri": uri, "started_at": self.clock()}

    def upload(self, container: dict, path, *, offset: int = 0) -> dict:
        """Stream the file to rupload.facebook.com from `offset`."""
        path = Path(path)
        size = path.stat().st_size

        def blocks():
            with path.open("rb") as f:
                f.seek(offset)
                for block in iter(lambda: f.read(STREAM_BLOCK), b""):
                    yield block

        def send(token):
            try:
                response = self.client.post(
                    container["upload_uri"], content=blocks(),
                    headers={"Authorization": f"OAuth {token}", "offset": str(offset),
                             "file_size": str(size),
                             "Content-Length": str(max(0, size - offset)),
                             "Content-Type": "application/octet-stream"})
            except http.RequestRefused:
                raise
            except Exception as exc:
                raise errors.PlatformError(errors.classify_exception("instagram", exc)) from exc
            body = meta_auth._body(response)
            if response.status_code != 200 or body.get("success") is False:
                raise errors.PlatformError(errors.classify("instagram", response.status_code, body),
                                           response.status_code, http.request_id(response))
            return {"done": True, "request_id": http.request_id(response)}

        return self._with_token(send)

    def upload_progress(self, container: dict) -> int:
        """Bytes Instagram has received: video_status.uploading_phase.bytes_transferred."""
        body = self._get(container["container_id"], {"fields": "video_status"})
        phase = ((body.get("video_status") or {}).get("uploading_phase") or {})
        return int(phase.get("bytes_transferred") or 0)

    def resume_upload(self, container: dict, path) -> dict:
        """After an interruption: ask how much arrived, then send the rest."""
        offset = self.upload_progress(container)
        if offset >= Path(path).stat().st_size:
            return {"done": True, "request_id": None}
        return self.upload(container, path, offset=offset)

    def container_status(self, container_id: str) -> dict:
        """{"status_code", "status"}: IN_PROGRESS, FINISHED, PUBLISHED, ERROR or EXPIRED."""
        body = self._get(container_id, {"fields": "status_code,status"})
        return {"status_code": body.get("status_code"), "status": body.get("status")}

    def publish(self, container_id: str) -> str:
        """media_publish; returns the media id (9.4)."""
        body = self._post(f"{self.ig}/media_publish", {"creation_id": container_id})
        if not body.get("id"):
            raise errors.PlatformError(errors.Classified(
                errors.TEMPORARY, "noMediaId", "Instagram didn't return the published media id."))
        return str(body["id"])

    def media(self, media_id: str) -> dict:
        return self._get(media_id, {"fields": "id,permalink,timestamp,media_product_type"})

    def find_media(self, caption: str, since: float) -> dict | None:
        """The account's media published since `since` with this caption, for
        a publish whose media id was lost (§8.3 `ig_resolve`)."""
        body = self._get(f"{self.ig}/media", {"fields": "id,caption,timestamp,permalink",
                                              "since": str(int(since)), "limit": 25})
        want = (caption or "").strip()
        for item in body.get("data") or []:
            if (item.get("caption") or "").strip() == want:
                return item
        return None

    def insights(self, media_id: str) -> dict:
        """Views, likes and comments of a published Reel (12.5)."""
        body = self._get(f"{media_id}/insights", {"metric": "views,likes,comments"})
        out = {"views": None, "likes": None, "comments": None}
        for metric in body.get("data") or []:
            name = metric.get("name")
            if name not in out:
                continue
            value = (metric.get("total_value") or {}).get("value")
            if value is None and metric.get("values"):
                value = metric["values"][0].get("value")
            out[name] = int(value) if isinstance(value, (int, float)) else None
        return out
