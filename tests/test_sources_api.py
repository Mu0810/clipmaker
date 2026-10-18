"""GET /api/autopilot/sources, the list behind the Sources view (task 12.3,
design §14; Requirements 1.3, 1.5, 1.7, N8.1, N4), the probed title a
queued source keeps, and trying a failed source again (1.10, design §15)."""
from __future__ import annotations

import json
import time
import uuid

import pytest

import db
import paths
from autopilot import activity, intake, profiles, sources, store
from autopilot import owner as owner_mod
from tests.test_intake import YT, runner_for

NO_CLIPS = ("No clip reached your minimum score of 60 (best: 48), so no posts were planned. "
            "Lower the minimum score in the preset, or try another video.")


def seed_source(*, owner: str | None = None, title: str | None = "Podcast ep 1", status="done",
                autopilot=1, age=0.0, attempts=0, stage=None, pct=0, message=None, ai_calls=0,
                best_score=None, settings=None, profile_id=None, request=None, channel=None,
                duration=None, rights_basis="confirmed", deleted=False, error=None) -> str:
    job_id = uuid.uuid4().hex[:12]
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id, created_at, url, status, user_id, autopilot, profile_id, "
            "profile_snapshot_json, request_json, attempts, stage, pct, message, ai_calls, "
            "best_score, source_title, source_channel_title, source_duration, rights_basis, "
            "deleted_at, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, time.time() - 1000 + age, "https://youtu.be/x", status, owner, autopilot,
             profile_id, json.dumps(settings if settings is not None else {"min_score": 60}),
             json.dumps(request or {"kind": "url", "url": "https://youtu.be/x"}), attempts,
             stage, pct, message, ai_calls, best_score, title, channel, duration, rights_basis,
             time.time() if deleted else None, error))
    return job_id


def seed_post(job_id: str, owner: str = "local") -> None:
    with store.connect() as conn:
        store.insert_post(conn, {"id": uuid.uuid4().hex[:16], "owner_id": owner, "job_id": job_id,
                                 "platform": "youtube", "method": "official", "status": "scheduled",
                                 "approval": "pending", "holds": [], "meta": {}, "ai_meta": {},
                                 "created_at": time.time()})


@pytest.mark.req("1.3", "1.5", "1.7", "N8.1")
def test_the_list_shows_each_source_with_its_stage_outcome_and_ai_calls(client):
    profile = profiles.ensure_default("local")
    owner_mod.set_prefs("local", timezone="Asia/Kolkata")
    failed = seed_source(age=1, status="error", message="Error: Couldn't download the video.",
                         error="Couldn't download the video.", ai_calls=2)
    planned = seed_source(age=2, title="Talk with posts", message="Done: 2 clips, 2 posts planned.",
                          ai_calls=7, best_score=82, profile_id=profile["id"])
    seed_post(planned)
    seed_post(planned)
    below = seed_source(age=3, title="Quiet talk", message=NO_CLIPS, ai_calls=9, best_score=48)
    upload = seed_source(age=4, title=None, status="queued", message="Queued",
                         request={"kind": "file", "path": "/private/staging/upload-1a2b.mp4",
                                  "filename": "talk.mp4"})
    running = seed_source(age=5, title="Long interview", status="running", stage="rendering", pct=70,
                          message="Rendering clip 2 of 6", attempts=1, channel="Other Channel",
                          duration=3725.5, profile_id=profile["id"])
    seed_source(age=6, autopilot=0, title="Made by hand")              # a manual job: not listed
    seed_source(age=7, title="Deleted talk", deleted=True)             # deleted: not listed

    reply = client.get("/api/autopilot/sources")
    assert reply.status_code == 200
    body = reply.json()
    assert body["timezone"] == "Asia/Kolkata"
    listed = body["sources"]
    assert [s["id"] for s in listed] == [running, upload, below, planned, failed]   # newest first
    by_id = {s["id"]: s for s in listed}

    assert by_id[running] == {
        "id": running, "title": "Long interview", "kind": "url", "url": "https://youtu.be/x",
        "channel": "Other Channel", "own_channel": False, "duration": 3725.5,
        "profile": {"id": profile["id"], "name": "Default"},
        "created_at": by_id[running]["created_at"], "status": "running", "stage": "rendering",
        "pct": 70, "message": "Rendering clip 2 of 6", "error": None, "restarted": True,
        "ai_calls": 0, "best_score": None, "min_score": 60, "posts": 0, "retryable": False}
    assert by_id[below]["message"] == NO_CLIPS                          # 1.7, with the best score
    assert (by_id[below]["best_score"], by_id[below]["min_score"]) == (48, 60)
    assert by_id[below]["ai_calls"] == 9                                # N8.1
    assert (by_id[planned]["posts"], by_id[planned]["ai_calls"]) == (2, 7)
    assert by_id[failed]["status"] == "error" and by_id[failed]["restarted"] is False
    assert [s["id"] for s in listed if s["retryable"]] == [failed]     # 1.10: only the failed one
    assert by_id[upload]["kind"] == "file" and by_id[upload]["title"] == "talk.mp4"
    assert by_id[upload]["url"] is None
    assert "/private/staging" not in reply.text                          # the server's path stays put
    assert by_id[below]["profile"] == {"id": None, "name": None}


@pytest.mark.req("1.3")
def test_a_queued_source_shows_its_probed_title_and_keeps_it_when_it_fails(client):
    profiles.ensure_default("local")
    intake.set_enqueuer(lambda job_id: None)
    [queued] = intake.submit_urls(["https://youtu.be/vid2"], "local", rights_confirmed=True,
                                  runner=runner_for(YT))
    [listed] = sources.list_sources(None)
    assert (listed["id"], listed["title"], listed["channel"], listed["duration"]) == (
        queued["job_id"], "Second talk", "Other Channel", 1200)
    assert (listed["status"], listed["message"], listed["pct"]) == ("queued", "Queued", 0)

    db.finish(queued["job_id"], "error", error="Download failed")      # a failure has no result
    [listed] = sources.list_sources(None)
    assert (listed["status"], listed["title"]) == ("error", "Second talk")
    db.finish(queued["job_id"], "done", result={"source_title": "Second talk (full)"})
    assert sources.list_sources(None)[0]["title"] == "Second talk (full)"


@pytest.mark.req("1.3")
def test_an_uploaded_file_is_named_by_its_file_name(client, tmp_path, monkeypatch):
    from media import probe as probe_mod

    profiles.ensure_default("local")
    intake.set_enqueuer(lambda job_id: None)
    monkeypatch.setattr(probe_mod, "probe", lambda path: {"duration": 600.0})
    staged = tmp_path / "upload-0123.mp4"
    staged.write_bytes(b"not really a video")
    job = intake.submit_file(staged, "My talk.mp4", "local", rights_confirmed=True)
    [listed] = sources.list_sources(None)
    assert (listed["id"], listed["title"], listed["kind"]) == (job["job_id"], "My talk.mp4", "file")


@pytest.mark.req("1.5")
def test_a_source_a_restart_re_ran_says_so(client):
    job_id = seed_source(status="running", stage="transcribing", pct=20, message="Transcribing")
    intake.set_enqueuer(lambda job_id: None)
    assert intake.requeue_interrupted()["requeued"] == [job_id]
    [listed] = sources.list_sources(None)
    assert (listed["status"], listed["restarted"], listed["message"]) == (
        "queued", True, "Queued (resumed after a restart)")


@pytest.mark.req("N4.1", "N4.2")
def test_each_user_lists_only_their_own_sources(two_users, monkeypatch):
    alice, bob = two_users
    monkeypatch.setattr(intake, "_default_runner", runner_for(YT))     # the route's own yt-dlp
    added = alice.post("/api/autopilot/sources",
                       json={"urls": ["https://youtu.be/vid1"], "rights_confirmed": True})
    assert added.status_code == 200, added.text
    alice_job = added.json()["jobs"][0]["job_id"]
    added = bob.post("/api/autopilot/sources",
                     json={"urls": ["https://youtu.be/vid2"], "rights_confirmed": True})
    bob_job = added.json()["jobs"][0]["job_id"]

    alice_list = alice.get("/api/autopilot/sources").json()["sources"]
    bob_list = bob.get("/api/autopilot/sources").json()["sources"]
    assert [s["id"] for s in alice_list] == [alice_job]
    assert [s["id"] for s in bob_list] == [bob_job]
    assert alice_list[0]["title"] == "First talk" and bob_list[0]["title"] == "Second talk"
    assert alice_list[0]["profile"]["name"] == "Default"


def job_row(job_id: str) -> dict:
    with store.connect() as conn:
        return store.decode_row(conn.execute("SELECT * FROM jobs WHERE job_id=?",
                                             (job_id,)).fetchone())


@pytest.mark.req("1.10", "N8.1")
def test_a_failed_video_is_queued_again_and_keeps_its_ai_calls(client, monkeypatch):
    enqueued: list[str] = []
    monkeypatch.setattr(intake, "_enqueue_fn", enqueued.append)
    job_id = seed_source(title="Failed talk", status="error", attempts=1, ai_calls=2, pct=40,
                         message="Error: Interrupted twice by a restart.",
                         error="Interrupted twice by a restart.", best_score=77)

    reply = client.post(f"/api/autopilot/sources/{job_id}/retry")
    assert reply.status_code == 200, reply.text
    source = reply.json()["source"]                    # as the list shows it, for the row
    assert (source["id"], source["status"], source["message"], source["pct"]) == (
        job_id, "queued", "Queued to try again", 0)
    assert (source["error"], source["best_score"], source["restarted"]) == (None, None, False)
    assert (source["ai_calls"], source["retryable"]) == (2, False)     # the next run adds to them
    assert enqueued == [job_id]
    job = job_row(job_id)
    assert (job["status"], job["attempts"], job["stage"], job["result"]) == ("queued", 0, None, None)
    [entry] = activity.list_entries("local", {"kind": "source.retried"})
    assert (entry["actor"], entry["job_id"]) == ("user", job_id)
    assert entry["summary"] == ("Tried “Failed talk” again. "
                                "It had failed: Interrupted twice by a restart.")

    again = client.post(f"/api/autopilot/sources/{job_id}/retry")       # a second press
    assert (again.status_code, again.json()["detail"]) == (
        409, "It's already queued or being processed.")
    assert enqueued == [job_id]


@pytest.mark.req("1.10")
def test_a_video_with_posts_or_without_its_file_isnt_tried_again(client, monkeypatch):
    enqueued: list[str] = []
    monkeypatch.setattr(intake, "_enqueue_fn", enqueued.append)
    out = paths.output_dir()
    out.mkdir(parents=True, exist_ok=True)
    staged = out / "upload-2222.mp4"
    staged.write_bytes(b"waiting to be processed")

    def upload(name: str) -> dict:
        return {"kind": "file", "path": str(out / name), "filename": "talk.mp4"}

    done = seed_source(title="Done talk", age=1)
    posted = seed_source(title="Posted talk", status="error", age=2, error="Database is locked")
    seed_post(posted)                     # a run stopped between planning and finishing
    lost = seed_source(title="Lost upload", status="error", age=3, request=upload("upload-0000.mp4"),
                       error="The uploaded video is missing; submit it again.")
    moved = seed_source(title="Moved upload", status="error", age=4, request=upload("upload-1111.mp4"),
                        error="Whisper ran out of memory")
    (out / moved).mkdir()
    (out / moved / "source.mp4").write_bytes(b"moved in by the first try")
    waiting = seed_source(title="Waiting upload", status="error", age=5,
                          request=upload("upload-2222.mp4"), error="Couldn't process the video")
    manual = seed_source(title="Made by hand", status="error", age=6, autopilot=0)
    deleted = seed_source(title="Deleted talk", status="error", age=7, deleted=True)

    assert {s["title"]: s["retryable"] for s in sources.list_sources(None)} == {
        "Done talk": False, "Posted talk": False, "Lost upload": False,
        "Moved upload": True, "Waiting upload": True}

    def refusal(job_id: str) -> tuple[int, str]:
        reply = client.post(f"/api/autopilot/sources/{job_id}/retry")
        return reply.status_code, reply.json()["detail"]

    assert refusal(done) == (409, "It didn't fail, so there's nothing to try again.")
    assert refusal(posted) == (409, "It already has posts, so trying again could post the same "
                                    "clips twice. Delete it and add the video again.")
    assert refusal(lost) == (409, "The uploaded file is gone, so it can't be tried again. "
                                  "Upload the video again.")
    for job_id in (manual, deleted, "ffffffffffff", "not-a-job"):
        assert client.post(f"/api/autopilot/sources/{job_id}/retry").status_code == 404
    assert enqueued == []
    assert job_row(posted)["status"] == "error"

    for job_id in (moved, waiting):
        assert client.post(f"/api/autopilot/sources/{job_id}/retry").status_code == 200
    assert enqueued == [moved, waiting]


@pytest.mark.req("1.10", "N4.1", "N4.2")
def test_only_the_owner_can_try_a_failed_video_again(two_users, monkeypatch):
    alice, bob = two_users
    monkeypatch.setattr(intake, "_default_runner", runner_for(YT))
    added = alice.post("/api/autopilot/sources",
                       json={"urls": ["https://youtu.be/vid1"], "rights_confirmed": True})
    assert added.status_code == 200, added.text
    job_id = added.json()["jobs"][0]["job_id"]
    db.finish(job_id, "error", error="Couldn't download the video.")
    enqueued: list[str] = []
    monkeypatch.setattr(intake, "_enqueue_fn", enqueued.append)

    assert bob.post(f"/api/autopilot/sources/{job_id}/retry").status_code == 404
    assert job_row(job_id)["status"] == "error" and enqueued == []
    assert alice.post(f"/api/autopilot/sources/{job_id}/retry").status_code == 200
    assert job_row(job_id)["status"] == "queued" and enqueued == [job_id]
