"""A stateful fake of Meta's Graph API v26.0 and rupload.facebook.com, for the
`net` fixture: `FakeInstagram(net)` registers itself on both hosts.

It checks what a careful server would: the token arrives in an
`Authorization: OAuth …` header, `appsecret_proof` matches that token, and
the app secret matches in token exchanges. Knobs for tests: `pages` (the
/me/accounts list), `page_size`, `refuse` (tokens refused with code 10 on
publishing calls), `rupload_accept` (bytes accepted before a retriable
failure), `debug` (the debug_token reply) and `fail_next` (status, body).
For publishing (task 9.3): `processing_checks` (status reads a finished
upload answers IN_PROGRESS before its outcome), `outcomes` (each finished
upload's status_code in turn, default FINISHED), `drop_publish_reply`
(media_publish publishes, then the reply is lost) and `media_time` (the
published media's timestamp).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import httpx

APP = {"app_id": "1234567890", "app_secret": "metaAppSecret", "config_id": "cfg-987"}
USER_TOKEN, PAGE_TOKEN, SHORT_TOKEN = "EAAuserLongLived", "EAApageToken", "EAAshortLived"
IG_USER = "17841400000000001"
STATUS_TEXT = {"IN_PROGRESS": "In Progress: Media is still being processed.",
               "FINISHED": "Finished: Media has been uploaded and it is ready to be published.",
               "PUBLISHED": "Published: Media has been published.",
               "ERROR": "Error: Media upload has failed with error code 2207026.",
               "EXPIRED": "Expired: Media was not published within 24 hours."}


def page(pid, *, ig=True, tasks=("MANAGE", "CREATE_CONTENT", "ANALYZE")) -> dict:
    entry = {"id": pid, "name": f"Page {pid}", "access_token": f"EAApage{pid}",
             "tasks": list(tasks)}
    if ig:
        entry["instagram_business_account"] = {
            "id": f"ig{pid}", "username": f"user{pid}", "name": f"IG {pid}",
            "profile_picture_url": f"https://scontent.example/{pid}.jpg"}
    return entry


class FakeInstagram:
    def __init__(self, net, app=APP):
        self.app, self.requests = app, []
        self.app_token = f"{app['app_id']}|{app['app_secret']}"
        self.tokens = {USER_TOKEN, PAGE_TOKEN, self.app_token}
        self.pages = [page("p1")]
        self.page_size = 100
        self.refuse: set[str] = set()
        self.rupload_accept: int | None = None
        self.fail_next: tuple[int, dict] | None = None
        self.containers: dict[str, dict] = {}
        self.media: dict[str, dict] = {}
        self.limit_used = 3
        self.processing_checks = 0
        self.outcomes: list[str] = []
        self.drop_publish_reply = False
        self.media_time = "2026-11-02T06:00:00+0000"
        self.debug = {"is_valid": True, "expires_at": 0, "data_access_expires_at": 1_800_000_000,
                      "scopes": ["instagram_basic", "instagram_content_publish",
                                 "instagram_manage_insights", "pages_show_list"],
                      "user_id": "u1"}
        net.register("graph.facebook.com", self.handle)
        net.register("rupload.facebook.com", self.handle)

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def error(status, code, message, subcode=None):
        err = {"message": message, "type": "OAuthException", "code": code,
               "fbtrace_id": "TraceFake"}
        if subcode is not None:
            err["error_subcode"] = subcode
        return httpx.Response(status, json={"error": err}, headers={"x-fb-trace-id": "TraceFake"})

    @staticmethod
    def ok(body, status=200):
        return httpx.Response(status, json=body, headers={"x-fb-trace-id": "TraceFake"})

    def proof(self, token: str) -> str:
        return hmac.new(self.app["app_secret"].encode(), token.encode(),
                        hashlib.sha256).hexdigest()

    def calls(self, method: str, path_suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests
                if r.method == method and r.url.path.endswith(path_suffix)]

    def form(self, request: httpx.Request) -> dict:
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    # ── routing ─────────────────────────────────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if self.fail_next:
            status, body = self.fail_next
            self.fail_next = None
            return self.ok(body, status)
        auth = request.headers.get("authorization", "")
        token = auth[len("OAuth "):] if auth.startswith("OAuth ") else None
        if request.url.host == "rupload.facebook.com":
            return self._rupload(request, token)
        rest = request.url.path.removeprefix("/v26.0").strip("/")
        form = self.form(request) if request.method == "POST" else {}
        if rest == "oauth/access_token":
            return self._exchange(form)
        if token not in self.tokens:
            return self.error(400, 190, "Invalid OAuth access token.")
        given = form.get("appsecret_proof") or request.url.params.get("appsecret_proof")
        if given != self.proof(token):
            return self.error(400, 100, "Invalid appsecret_proof provided in the API argument")
        if rest == "" and request.method == "POST":
            return self._batch(form, token)
        publishing = rest.endswith(("/media", "/media_publish")) or "rupload" in rest
        if token in self.refuse and publishing:
            return self.error(400, 10, "(#10) Application does not have permission for this action")
        return self._graph(request, rest, form, token)

    def _exchange(self, form: dict) -> httpx.Response:
        if form.get("client_secret") != self.app["app_secret"]:
            return self.error(400, 1, "Error validating client secret.")
        if form.get("code"):
            return self.ok({"access_token": SHORT_TOKEN, "token_type": "bearer"})
        if form.get("grant_type") == "fb_exchange_token" and form.get("fb_exchange_token"):
            return self.ok({"access_token": USER_TOKEN, "token_type": "bearer",
                            "expires_in": 5_183_944})
        return self.error(400, 100, "Missing parameters")

    def _batch(self, form: dict, token: str) -> httpx.Response:
        replies = []
        for op in json.loads(form["batch"]):
            url = urlparse(op["relative_url"])
            if url.path == "debug_token" and token == self.app_token:
                input_token = parse_qs(url.query)["input_token"][0]
                data = dict(self.debug, is_valid=self.debug["is_valid"]
                            and input_token in self.tokens)
                replies.append({"code": 200, "body": json.dumps({"data": data})})
            else:
                replies.append({"code": 400, "body": json.dumps(
                    {"error": {"code": 100, "message": "Unsupported"}})})
        return self.ok(replies)

    def _graph(self, request, rest: str, form: dict, token: str) -> httpx.Response:
        q = request.url.params
        if rest == "me/accounts":
            start = int(q.get("after", "0") or 0)
            chunk = self.pages[start:start + self.page_size]
            body = {"data": chunk}
            end = start + len(chunk)
            if end < len(self.pages):
                body["paging"] = {"cursors": {"after": str(end)},
                                  "next": f"https://graph.facebook.com/v26.0/me/accounts"
                                          f"?access_token={token}&after={end}"}
            return self.ok(body)
        if rest == f"{IG_USER}/content_publishing_limit":
            return self.ok({"data": [{"quota_usage": self.limit_used,
                                      "config": {"quota_total": 100, "quota_duration": 86400}}]})
        if rest == f"{IG_USER}/media" and request.method == "POST":
            cid = f"c{len(self.containers) + 1}"
            self.containers[cid] = {"form": form, "data": bytearray(), "size": None,
                                    "status_code": "IN_PROGRESS", "token": token}
            return self.ok({"id": cid, "uri": f"https://rupload.facebook.com/ig-api-upload/"
                                              f"v26.0/{cid}"})
        if rest == f"{IG_USER}/media_publish":
            container = self.containers[form["creation_id"]]
            if container["status_code"] != "FINISHED":
                return self.error(400, 9007, "Media is not ready to be published.", 2207027)
            mid = f"m{len(self.media) + 1}"
            container["status_code"] = "PUBLISHED"
            self.media[mid] = {"id": mid, "caption": container["form"].get("caption"),
                               "timestamp": self.media_time,
                               "permalink": f"https://www.instagram.com/reel/{mid}/",
                               "media_product_type": "REELS"}
            if self.drop_publish_reply:
                self.drop_publish_reply = False
                raise httpx.ReadTimeout("The read operation timed out", request=request)
            return self.ok({"id": mid})
        if rest == f"{IG_USER}/media":
            return self.ok({"data": list(reversed(self.media.values()))})
        if rest.endswith("/insights"):
            return self.ok({"data": [
                {"name": "views", "period": "lifetime", "values": [{"value": 340}]},
                {"name": "likes", "period": "lifetime", "total_value": {"value": 12}},
                {"name": "comments", "period": "lifetime", "values": [{"value": 3}]}]})
        if rest in self.containers:
            c = self.containers[rest]
            if "video_status" in q.get("fields", ""):
                return self.ok({"id": rest, "video_status": {"uploading_phase": {
                    "status": "in_progress", "bytes_transferred": len(c["data"])}}})
            code = c["status_code"]
            if c.get("pending"):                    # still processing for this many reads
                code, c["pending"] = "IN_PROGRESS", c["pending"] - 1
                if not c["pending"]:
                    c["status_code"] = c["outcome"]
            return self.ok({"id": rest, "status_code": code, "status": STATUS_TEXT[code]})
        if rest in self.media:
            return self.ok(self.media[rest])
        return self.error(404, 803, "Some of the aliases you requested do not exist")

    def _rupload(self, request, token) -> httpx.Response:
        if token not in self.tokens:
            return self.ok({"debug_info": {"retriable": False, "type": "AuthorizationError",
                                           "message": "Invalid OAuth access token."}}, 401)
        cid = request.url.path.rsplit("/", 1)[-1]
        container = self.containers[cid]
        if token in self.refuse:
            return self.ok({"debug_info": {"retriable": False, "type": "PermissionError",
                                           "message": "Not allowed"}}, 403)
        offset, size = int(request.headers["offset"]), int(request.headers["file_size"])
        if offset != len(container["data"]):
            return self.ok({"debug_info": {"retriable": False, "type": "OffsetMismatch",
                                           "message": "Offset mismatch"}}, 400)
        container["size"] = size
        data = request.content
        if self.rupload_accept is not None:
            container["data"].extend(data[:self.rupload_accept])
            self.rupload_accept = None
            return self.ok({"debug_info": {"retriable": True, "type": "TransientError",
                                           "message": "Connection reset"}}, 500)
        container["data"].extend(data)
        if len(container["data"]) == size:
            container["outcome"] = self.outcomes.pop(0) if self.outcomes else "FINISHED"
            container["pending"] = self.processing_checks
            container["status_code"] = ("IN_PROGRESS" if self.processing_checks
                                        else container["outcome"])
        return self.ok({"success": True, "message": "Upload successful."})
