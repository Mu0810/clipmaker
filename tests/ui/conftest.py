"""Browser tests for the screens (task 12.2), run with `pytest -m ui`.

- `live_server`: Clip Maker served by uvicorn in a thread, on a free port
  bound to 127.0.0.1, with this test's temporary data folder (the autouse
  `_isolated_data` fixture) and the background threads off. Its start-up runs
  the app's own bootstrap, so the database is created and migrated.
- `seed`: test data written straight into that database.
- `browser` and `page`: Playwright driving the installed Google Chrome,
  headless. The tests skip when Playwright or Chrome isn't available.
- Every request the page makes to another host (the Google Fonts stylesheet,
  say) is answered by `page.route` with an empty reply and recorded in
  `page_log["outside"]`, so no test reaches the network. A JavaScript error
  on the page fails the test.
"""
from __future__ import annotations

import socket
import threading
import time
import uuid

import pytest


@pytest.fixture
def live_server(_isolated_data):
    import uvicorn

    import app as app_module

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app_module.app, log_level="warning", lifespan="on",
                                           ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True,
                              name="ui-test-server")
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("The test server stopped while starting.")
        if time.monotonic() > deadline:
            raise RuntimeError("The test server didn't start within 20 s.")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(15)
        sock.close()


class Seed:
    """Test data written straight into the live server's database."""

    def post(self, *, status: str = "scheduled", approval: str = "pending",
             platform: str = "youtube", method: str = "official", owner: str = "local",
             **extra) -> str:
        from autopilot import store

        pid = uuid.uuid4().hex[:16]
        meta = ({"title": "A clip", "description": "", "tags": [], "category_id": "22"}
                if platform == "youtube" else {"caption": "A clip #tips"})
        with store.connect() as conn:
            store.insert_post(conn, {
                "id": pid, "owner_id": owner, "platform": platform, "method": method,
                "status": status, "approval": approval, "holds": [], "meta": meta,
                "ai_meta": meta, "created_at": time.time(), **extra},
                legacy=method == "legacy")
        return pid

    def posts(self, *, waiting: int = 0, failed: int = 0, reconnect: int = 0) -> list[str]:
        """Posts the counts badge counts: waiting for approval, failed, and
        waiting for a reconnect."""
        ids = [self.post() for _ in range(waiting)]
        ids += [self.post(status="failed", approval="approved") for _ in range(failed)]
        ids += [self.post(status="needs_reconnect", approval="approved") for _ in range(reconnect)]
        return ids

    def update(self, post_id: str, **fields) -> None:
        from autopilot import store

        with store.connect() as conn:
            store.update_post(conn, post_id, fields)

    def paused(self, value: bool = True) -> None:
        from autopilot import owner as owner_mod

        owner_mod.set_prefs("local", paused=value)

    def source(self, *, title: str | None = "Podcast ep 1", status: str = "done", age: float = 0,
               autopilot: int = 1, settings: dict | None = None, request: dict | None = None,
               deleted: bool = False, **fields) -> str:
        """An Autopilot source (a `jobs` row) of the local user. `age` orders
        sources: a larger age is newer. Other `fields` are jobs columns."""
        import json

        from autopilot import profiles, store

        job_id = uuid.uuid4().hex[:12]
        profile = profiles.ensure_default("local")
        row = {"job_id": job_id, "created_at": time.time() - 1000 + age,
               "url": "https://www.youtube.com/watch?v=seeded", "status": status,
               "autopilot": autopilot, "profile_id": profile["id"],
               "profile_snapshot_json": json.dumps(settings or profile["settings"]),
               "request_json": json.dumps(request or {"kind": "url", "url": "https://youtu.be/x"}),
               "source_title": title, "rights_basis": "confirmed",
               "deleted_at": time.time() if deleted else None, **fields}
        with store.connect() as conn:
            conn.execute(f"INSERT INTO jobs({', '.join(row)}) VALUES({', '.join('?' * len(row))})",
                         tuple(row.values()))
        return job_id

    def stage(self, job_id: str, **fields) -> None:
        """Change a source's columns: status, stage, pct, message and so on."""
        from autopilot import store

        with store.connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE job_id=?",
                         (*fields.values(), job_id))

    def job(self, job_id: str) -> dict:
        from autopilot import store

        with store.connect() as conn:
            return store.decode_row(conn.execute("SELECT * FROM jobs WHERE job_id=?",
                                                 (job_id,)).fetchone())

    def jobs(self) -> list[dict]:
        """The Autopilot sources, oldest first."""
        from autopilot import store

        with store.connect() as conn:
            return store.decode_rows(conn.execute(
                "SELECT * FROM jobs WHERE autopilot=1 ORDER BY created_at").fetchall())

    def profile(self, name: str, **settings) -> dict:
        from autopilot import profiles

        return profiles.create("local", name, settings)

    def account(self, platform: str, external_id: str, name: str = "My Channel") -> None:
        from autopilot import store

        with store.connect() as conn:
            conn.execute("INSERT INTO accounts(id, owner_id, platform, external_id, name, status) "
                         "VALUES(?,?,?,?,?,?)",
                         (f"local-{platform}", "local", platform, external_id, name, "ok"))

    def clip(self, job_id: str, idx: int, *, score: int = 80,
             reason: str = "Strong hook in the first 3 s") -> str:
        """A clip of a source, with its score and the reason it was picked."""
        from autopilot import store

        clip_id = uuid.uuid4().hex[:16]
        with store.connect() as conn:
            store.insert_clip(conn, {"id": clip_id, "owner_id": "local", "job_id": job_id, "idx": idx,
                                     "src_start": idx * 60.0, "src_end": idx * 60.0 + 30,
                                     "score": score, "score_reason": reason, "method": "ai:test",
                                     "created_at": time.time()})
        return clip_id

    def timezone(self, zone: str = "Asia/Kolkata") -> None:
        from autopilot import owner as owner_mod

        owner_mod.set_prefs("local", timezone=zone)

    def read_post(self, post_id: str) -> dict:
        from autopilot import store

        with store.connect() as conn:
            return store.get_post(conn, "local", post_id)

    def prefs(self) -> dict:
        from autopilot import owner as owner_mod

        return owner_mod.get_prefs("local")

    # ── connections (task 12.5) ──────────────────────────────────────────

    YT_MANAGE = "https://www.googleapis.com/auth/youtube"
    YT_MANUAL = ["https://www.googleapis.com/auth/youtube.upload",
                 "https://www.googleapis.com/auth/youtube.readonly"]

    def youtube(self, *, autopilot: bool = True, name: str = "Manish Clips",
                made_for_kids: bool | None = None, **account) -> None:
        """A YouTube connection on this Mac: the token file and the account
        row, with the Autopilot permission or (autopilot=False) only today's two."""
        import json

        from autopilot import accounts
        from platforms import google_creds

        scopes = [*self.YT_MANUAL, *([self.YT_MANAGE] if autopilot else [])]
        google_creds.token_file().write_text(json.dumps({
            "token": "ya29.test", "refresh_token": "1//test", "client_id": "123-abc.apps.googleusercontent.com",
            "client_secret": "GOCSPX-test", "scopes": scopes, "token_uri": "https://oauth2.googleapis.com/token",
            "expiry": google_creds._format_expiry(time.time() + 30 * 86400)}))
        accounts.upsert("local", "youtube", external_id="UCchannel123", name=name, avatar=None, scopes=scopes,
                        quota_key="yt:123-abc.apps.googleusercontent.com", **account)
        if made_for_kids is not None:
            accounts.set_declarations("local", "youtube", made_for_kids=made_for_kids)

    def instagram(self, *, publish: bool = True, name: str = "manish.makes", **account) -> None:
        """An Instagram connection through Meta: the stored token and the account row."""
        from autopilot import accounts
        from platforms import meta_auth

        scopes = ["instagram_basic", "pages_show_list", *(["instagram_content_publish"] if publish else [])]
        meta_auth.save_token("local", {"user_token": "EAAtest", "page_id": "p1", "page_token": "EAApage",
                                       "ig_user_id": "1784", "ig_username": name, "scopes": scopes})
        accounts.upsert("local", "instagram", external_id="1784", name=name, avatar=None, scopes=scopes,
                        quota_key="ig:1784", page_id="p1", **account)

    def meta_app(self) -> None:
        from platforms import meta_auth
        from tests.fakes.instagram import APP

        meta_auth.save_app_settings(APP["app_id"], APP["app_secret"], APP["config_id"])

    def track(self, name: str = "beat.mp3") -> None:
        """An audio file in the music folder (not cleared for publishing)."""
        import paths

        paths.music_dir().mkdir(parents=True, exist_ok=True)
        (paths.music_dir() / name).write_bytes(b"ID3")

    def metric(self, post_id: str, *, views: int, likes: int, comments: int, age_hours: float = 72) -> None:
        from autopilot import store

        with store.connect() as conn:
            conn.execute("INSERT INTO post_metrics(post_id, fetched_at, age_hours, views, likes, comments) "
                         "VALUES(?,?,?,?,?,?)", (post_id, time.time(), age_hours, views, likes, comments))

    def activity(self, kind: str, summary: str, *, actor: str = "system", at: float | None = None) -> int:
        from autopilot import activity

        return activity.log("local", actor, kind, summary, at=at)


@pytest.fixture
def seed(live_server) -> Seed:
    return Seed()


@pytest.fixture(scope="session")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright isn't installed.")
    manager = sync_playwright().start()
    try:
        chrome = manager.chromium.launch(channel="chrome", headless=True)
    except Exception as exc:                      # no Chrome on this machine
        manager.stop()
        pytest.skip(f"Google Chrome isn't available: {str(exc).splitlines()[0]}")
    yield chrome
    chrome.close()
    manager.stop()


@pytest.fixture
def page_log() -> dict:
    return {"outside": [], "errors": []}


@pytest.fixture
def page(browser, live_server, page_log):
    context = browser.new_context(base_url=live_server, viewport={"width": 1440, "height": 900},
                                  locale="en-GB", timezone_id="Asia/Kolkata")

    def answer(route) -> None:
        url = route.request.url
        if url == live_server or url.startswith(live_server + "/"):
            route.continue_()
            return
        page_log["outside"].append(url)
        css = route.request.resource_type == "stylesheet"
        route.fulfill(status=200, body="", content_type="text/css" if css else "text/plain")

    context.route("**/*", answer)
    tab = context.new_page()
    tab.on("pageerror", lambda exc: page_log["errors"].append(str(exc)))
    yield tab
    context.close()
    assert not page_log["errors"], f"JavaScript errors on the page: {page_log['errors']}"
