"""Scheduled uploads on the publisher (task 9.5, design §8.7, §13.2;
Requirements 11.6, 15.4, N6.1).

tests/fixtures/schedule_api.json was captured from today's /api/schedule
routes and scheduler.py before they changed (the capture script is kept
with that code in .attic/2026-10-16-scheduler-before-task-9.5.tar.gz). The
replay below sends the same requests to the new routes, runs the same
uploads through the publisher with the same stand-in upload functions, and
compares every reply and every upload call.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

import db
import instagram_upload
import paths
import youtube_pw
import youtube_upload
from autopilot import legacy, posts, publisher, store
from autopilot import owner as owner_mod

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "schedule_api.json").read_text())
FILES = ("01-clip-vertical.mp4", "02-clip-vertical.mp4")
RESULTS = {"youtube_upload.upload_video": {"video_id": "api1", "url": "https://youtu.be/api1"},
           "youtube_pw.upload_video": {"video_id": "pw1", "url": "https://youtu.be/pw1"},
           "instagram_upload.upload_reel": {"code": "R1",
                                            "url": "https://www.instagram.com/reel/R1/"}}


class Crash(BaseException):
    """The process dying: not an Exception, so nothing catches it."""


def normalize(value, users: dict):
    """The capture's own normalization: ids and clocks differ between runs."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "id" and isinstance(item, str):
                out[key] = "<id>"
            elif key in ("created_at", "now") and isinstance(item, (int, float)):
                out[key] = "<time>"
            elif key == "user_id" and item is not None:
                out[key] = users.get(item, "<user>")
            else:
                out[key] = normalize(item, users)
        return out
    if isinstance(value, list):
        return [normalize(item, users) for item in value]
    return value


def seed(job_id: str, owner=None) -> None:
    db.create(job_id, "https://youtu.be/source", user_id=owner)
    folder = paths.output_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        (folder / name).write_bytes(b"rendered clip")


def http(client, step, names, users) -> dict:
    response = client.request(step["method"], step["path"].format(**names), json=step.get("body"))
    data = response.json()
    if step.get("name") and response.status_code == 200:
        names[step["name"]] = data["id"]
    return {"status": response.status_code, "body": normalize(data, users)}


def stand_ins(monkeypatch, step, calls) -> None:
    """Today's upload functions, replaced as in the capture."""
    def recorder(name):
        def upload(path, **kwargs):
            calls.append([name, Path(path).name, kwargs])
            if step["do"] == "crash":
                raise Crash("the process died during the upload")
            if step.get("fail"):
                raise RuntimeError(step["fail"])
            return dict(RESULTS[name])
        return upload

    monkeypatch.setattr(youtube_pw, "is_connected", lambda: bool(step.get("browser")))
    monkeypatch.setattr(youtube_pw, "upload_video", recorder("youtube_pw.upload_video"))
    monkeypatch.setattr(youtube_upload, "upload_video", recorder("youtube_upload.upload_video"))
    monkeypatch.setattr(instagram_upload, "upload_reel", recorder("instagram_upload.upload_reel"))


@pytest.fixture
def clock():
    return {"t": time.time()}


def make(clock, holder):
    return publisher.Publisher(clock=lambda: clock["t"], holder=holder)


@pytest.mark.req("15.4", "N6.1", "11.6")
def test_todays_requests_get_todays_replies_and_uploads(client, monkeypatch, clock):
    seed("c0ffee000001")
    names, made = {}, []
    pub = make(clock, "A")
    made.append(pub)
    try:
        for i, recorded in enumerate(FIXTURE["single"]):
            step, expected = recorded["step"], recorded["result"]
            got: dict = {}
            if step["do"] == "http":
                got = http(client, step, names, {})
            elif step["do"] in ("run", "crash"):
                calls: list = []
                stand_ins(monkeypatch, step, calls)
                clock["t"] = step["at"]
                pub.tick()
                try:
                    pub.wait()
                except Crash:
                    pass
                got = {"calls": sorted(calls, key=json.dumps)}
            elif step["do"] == "remove_file":
                (paths.output_dir() / "c0ffee000001" / step["file"]).unlink()
            elif step["do"] == "restart":
                clock["t"] = step["at"]                    # the crashed process's lease ran out
                pub = make(clock, "B")
                made.append(pub)
                report = pub.tick()
                pub.wait()
                assert len(report["interrupted"]) == 1
            assert got == expected, f"step {i}: {step}"
    finally:
        for p in made:
            p.close()


@pytest.mark.req("N6.1", "15.3")
def test_todays_multi_user_replies_both_ways(two_users):
    alice, bob = two_users
    users = {db.get_user_by_email(f"{n}@example.com")["id"]: f"<{n}>" for n in ("alice", "bob")}
    seed("a1ce00000001", {v: k for k, v in users.items()}["<alice>"])
    names = {}
    for i, recorded in enumerate(FIXTURE["multi"]):
        step = recorded["step"]
        client = alice if step["as"] == "alice" else bob
        assert http(client, step, names, users) == recorded["result"], f"step {i}: {step}"


def test_the_fixture_covers_every_status_and_upload_route():
    rows = [r for e in FIXTURE["single"] if e["step"]["do"] == "http"
            for r in (e["result"]["body"].get("schedules") or [] if isinstance(
                e["result"]["body"], dict) else [])]
    assert {r["status"] for r in rows} == {"pending", "uploading", "done", "error"}
    routes = {c[0] for e in FIXTURE["single"] for c in e["result"].get("calls", [])}
    assert routes == set(RESULTS)


# ── the publisher's rules for scheduled uploads (§8.7) ───────────────────

def scheduled(job_id="c0ffee000001", *, platform="youtube", at=None, title="My clip") -> str:
    return legacy.create(None, job_id=job_id, file=FILES[0], platform=platform, title=title,
                         description="Desc", tags=["a"], privacy="unlisted",
                         scheduled_at=at if at is not None else time.time() + 3600)["id"]


def attempts(pid) -> list[tuple]:
    with store.connect() as conn:
        return [(r["step"], r["outcome"]) for r in conn.execute(
            "SELECT step, outcome FROM publish_attempts WHERE post_id=? ORDER BY finished_at, "
            "rowid", (pid,)).fetchall()]


def run(pub) -> None:
    pub.tick()
    pub.wait()


@pytest.fixture
def world(client, monkeypatch, clock):
    seed("c0ffee000001")
    calls: list = []
    behaviour = {"fail": None}

    def upload(path, **kwargs):
        calls.append(kwargs)
        if behaviour["fail"]:
            raise behaviour["fail"]
        return {"video_id": "v1", "url": "https://youtu.be/v1"}

    monkeypatch.setattr(youtube_pw, "is_connected", lambda: False)
    monkeypatch.setattr(youtube_upload, "upload_video", upload)
    made = []

    def publisher_(holder="A"):
        p = make(clock, holder)
        made.append(p)
        return p

    yield {"calls": calls, "behaviour": behaviour, "make": publisher_, "clock": clock}
    for p in made:
        p.close()


@pytest.mark.req("15.4", "10.3")
def test_a_failed_scheduled_upload_is_never_retried(world):
    pid = scheduled(at=world["clock"]["t"])
    world["behaviour"]["fail"] = RuntimeError("Upload failed: quota exceeded")
    pub = world["make"]()
    run(pub)
    for later in (3600, 86400):
        world["clock"]["t"] += later
        run(pub)
    assert len(world["calls"]) == 1
    assert (legacy.get(pid)["status"], legacy.get(pid)["error"]) == (
        "error", "Upload failed: quota exceeded")
    assert attempts(pid) == [("legacy_start", "ok"), ("legacy_upload", "failed")]


@pytest.mark.req("15.4", "10.1")
def test_an_upload_cut_off_by_a_restart_is_failed_not_sent_again(world, monkeypatch):
    pid = scheduled(at=world["clock"]["t"])

    def dies(path, **kwargs):
        world["calls"].append(kwargs)
        raise Crash("the process died during the upload")

    monkeypatch.setattr(youtube_upload, "upload_video", dies)
    a = world["make"]("A")
    a.tick()
    with pytest.raises(Crash):
        a.wait()
    assert legacy.get(pid)["status"] == "uploading"
    world["clock"]["t"] += 61                                 # A's lease runs out
    b = world["make"]("B")
    run(b)
    world["clock"]["t"] += 3600
    run(b)
    assert len(world["calls"]) == 1                           # never sent a second time
    assert (legacy.get(pid)["status"], legacy.get(pid)["error"]) == (
        "error", "Interrupted by a restart — please re-schedule.")


@pytest.mark.req("6.9", "15.4")
def test_the_pause_holds_scheduled_uploads_and_a_passed_one_goes_out_on_resume(world):
    from autopilot import pause

    due = world["clock"]["t"] + 600
    pid = scheduled(at=due)
    pause.set_paused("local", True)
    pub = world["make"]()
    world["clock"]["t"] = due + 3600
    run(pub)
    assert world["calls"] == [] and legacy.get(pid)["status"] == "pending"
    pause.set_paused("local", False, now=world["clock"]["t"])
    assert legacy.get(pid)["scheduled_at"] == due             # the time the user chose is kept
    run(pub)
    assert len(world["calls"]) == 1 and legacy.get(pid)["status"] == "done"


# ── Autopilot never creates one (11.6) ───────────────────────────────────

@pytest.mark.req("11.6")
def test_autopilot_code_paths_cannot_create_a_scheduled_upload(client):
    base = {"owner_id": "local", "platform": "youtube", "status": "scheduled",
            "approval": "approved", "holds": [], "meta": {}, "ai_meta": {}, "created_at": 1.0}
    with store.connect() as conn:
        with pytest.raises(ValueError, match="Only autopilot/legacy.py"):
            store.insert_post(conn, {**base, "id": "x1", "method": "legacy"})
        with pytest.raises(ValueError):
            store.insert_post(conn, {**base, "id": "x2", "method": "official"}, legacy=True)
    # The only caller allowed to create one is the scheduled-uploads module:
    # find every call passing legacy=True in the app's code.
    import ast

    root = Path(paths.REPO)
    sources = [p for p in root.glob("*.py")] + [p for d in ("autopilot", "platforms", "media")
                                               for p in (root / d).glob("*.py")]

    def passes_legacy(path: Path) -> bool:
        return any(isinstance(node, ast.Call) and any(
            k.arg == "legacy" and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in node.keywords) for node in ast.walk(ast.parse(path.read_text("utf-8"))))

    creators = sorted(str(p.relative_to(root)) for p in sources if passes_legacy(p))
    assert creators == ["autopilot/legacy.py"]


@pytest.mark.req("11.6", "N6.1")
def test_autopilot_posts_stay_out_of_the_scheduled_posts_list(client):
    owner_mod.set_prefs("local", timezone="Asia/Kolkata")
    seed("c0ffee000002")
    clip_id = uuid.uuid4().hex[:16]
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET autopilot=1, source_key='yt:abc' WHERE job_id=?",
                     ("c0ffee000002",))
        store.insert_clip(conn, {"id": clip_id, "owner_id": "local", "job_id": "c0ffee000002",
                                 "idx": 1, "src_start": 0.0, "src_end": 30.0, "score": 80,
                                 "meta": {"youtube": {"title": "T", "description": "D",
                                                      "tags": [], "category_id": "22"}},
                                 "created_at": 1.0})
    added = posts.add("local", clip_id, "youtube", time.time() + 7200)["post"]
    assert added["method"] == "official"
    assert client.get("/api/schedule").json()["schedules"] == []
    assert client.delete(f"/api/schedule/{added['id']}").status_code == 404
    assert posts.get("local", added["id"])["id"] == added["id"]          # still there


# ── schedules made before the upgrade (15.4) ─────────────────────────────

@pytest.mark.req("15.4")
def test_schedules_made_before_the_upgrade_read_back_unchanged(_isolated_data):
    from autopilot import migrations
    from tests.test_migrations import SCHEDULES, _build_todays_database

    _build_todays_database()
    migrations.run()
    rows = {r["id"]: r for r in legacy.list_schedules(None)}
    today = {"pending": ("pending", None), "uploading": ("error", legacy.INTERRUPTED),
             "done": ("done", None), "error": ("error", "Upload failed: quota")}
    assert set(rows) == {s[0] for s in SCHEDULES}
    for sid, status, uid, privacy, url, error in SCHEDULES:
        row = rows[sid]
        assert (row["status"], row["error"]) == today[status]
        assert row == {**row, "created_at": 10.0, "job_id": "job1", "file": "01-a-vertical.mp4",
                       "platform": "youtube", "title": f"Title {sid}", "description": "Desc",
                       "tags": ["a", "b"], "privacy": privacy, "scheduled_at": 1000.0,
                       "url": url, "user_id": uid}


@pytest.mark.req("N3.1", "N3.2")
def test_a_job_id_that_isnt_one_cant_reach_outside_the_job_folders(client):
    # Today's route accepted job_id ".." and scheduled data-folder files such as
    # token.json for upload. The job id must now be one (12 hex digits).
    (paths.data_dir() / "token.json").write_text("{}")
    response = client.post("/api/schedule", json={
        "job_id": "..", "file": "token.json", "platform": "youtube", "title": "x",
        "scheduled_at": time.time() + 3600})
    assert (response.status_code, response.json()) == (404, {"detail": "Clip not found."})
    assert legacy.clip_path("..", "token.json") is None
    assert legacy.clip_path("c0ffee000001", "../../token.json") == \
        paths.output_dir() / "c0ffee000001" / "token.json"
