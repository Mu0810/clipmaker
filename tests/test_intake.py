"""Autopilot source intake and restart recovery (task 6.1)."""
from __future__ import annotations

import subprocess

import pytest

from autopilot import intake, profiles, store


@pytest.fixture
def db_ready(_isolated_data):
    """An isolated database with the Autopilot schema and a default profile,
    plus a captured enqueuer so no real worker runs."""
    from autopilot import migrations
    import db

    db.init()
    migrations.run()
    profiles.ensure_default("local")
    enqueued: list[str] = []
    intake.set_enqueuer(enqueued.append)
    return enqueued


def runner_for(table: dict):
    """A fake yt-dlp: maps a URL (by its last path/query segment) to a
    (id, title, channel, channel_id, duration) tuple, an Exception to raise,
    or returns a non-zero result for an unknown URL."""
    def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
        url = cmd[-1]
        key = next((k for k in table if k in url), None)
        value = table.get(key)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: not available")
        line = "\t".join(str(v) for v in value)
        return subprocess.CompletedProcess(cmd, 0, line + "\n", "")
    return run


def account(owner: str, platform: str, external_id: str) -> None:
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO accounts(id, owner_id, platform, external_id, status) "
            "VALUES(?,?,?,?,?)",
            (f"{owner}-{platform}", owner, platform, external_id, "connected"))


YT = {"vid1": ("vid1", "First talk", "My Channel", "UCmine", 600),
      "vid2": ("vid2", "Second talk", "Other Channel", "UCother", 1200)}


@pytest.mark.req("1.6")
def test_a_source_over_three_hours_is_refused(db_ready):
    table = {"long": ("long", "Marathon", "Chan", "UCx", 3 * 3600 + 1)}
    [result] = intake.probe_urls(["https://youtu.be/long"], "local",
                                 runner=runner_for(table))
    assert "error" in result and "3 hours" in result["error"]
    assert db_ready == []


@pytest.mark.req("1.2")
def test_at_most_twenty_urls(db_ready):
    with pytest.raises(intake.IntakeError, match="20"):
        intake.probe_urls([f"https://youtu.be/v{k}" for k in range(21)], "local",
                          runner=runner_for({}))


@pytest.mark.req("13.1")
def test_a_missing_rights_confirmation_is_refused(db_ready):
    with pytest.raises(intake.IntakeError, match="rights"):
        intake.submit_urls(["https://youtu.be/vid2"], "local",
                           rights_confirmed=False, runner=runner_for(YT))
    assert db_ready == []
    jobs = intake.submit_urls(["https://youtu.be/vid2"], "local",
                              rights_confirmed=True, runner=runner_for(YT))
    assert [j["job_id"] for j in jobs] == db_ready
    assert len(db_ready) == 1


@pytest.mark.req("13.2")
def test_the_users_own_channel_needs_no_confirmation(db_ready):
    account("local", "youtube", "UCmine")
    probed = intake.probe_urls(["https://youtu.be/vid1"], "local", runner=runner_for(YT))
    assert probed[0]["own_channel"] is True
    jobs = intake.submit_urls(["https://youtu.be/vid1"], "local",
                              rights_confirmed=False, runner=runner_for(YT))
    assert len(jobs) == 1 and db_ready == [jobs[0]["job_id"]]
    with store.connect() as conn:
        row = conn.execute("SELECT rights_basis, autopilot, source_key, source_duration "
                           "FROM jobs WHERE job_id=?", (jobs[0]["job_id"],)).fetchone()
    assert (row["rights_basis"], row["autopilot"], row["source_key"]) == (
        "own_channel", 1, "yt:vid1")
    assert row["source_duration"] == 600


@pytest.mark.req("1.2")
def test_submission_order_is_kept_and_bad_urls_reported(db_ready):
    account("local", "youtube", "UCmine")       # vid1 own, vid2 other, bad fails
    urls = ["https://youtu.be/vid1", "https://youtu.be/bad", "https://youtu.be/vid2"]
    jobs = intake.submit_urls(urls, "local", rights_confirmed=True, runner=runner_for(YT))
    assert "error" in jobs[1]                   # the bad URL is reported in place
    queued = [j["job_id"] for j in jobs if "job_id" in j]
    assert queued == db_ready                   # queued in submission order
    with store.connect() as conn:
        rows = conn.execute("SELECT job_id, source_key FROM jobs ORDER BY created_at").fetchall()
    assert [r["source_key"] for r in rows] == ["yt:vid1", "yt:vid2"]


def queued_job(url="https://youtu.be/vid1", attempts=0, autopilot=1,
               request='{"kind":"url","url":"https://youtu.be/vid1"}', status="running"):
    import uuid
    jid = uuid.uuid4().hex[:12]
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO jobs(job_id, created_at, url, status, autopilot, attempts, "
            "request_json) VALUES(?, strftime('%s','now'), ?, ?, ?, ?, ?)",
            (jid, url, status, autopilot, attempts, request))
    return jid


@pytest.mark.req("1.5")
def test_an_interrupted_job_is_requeued_once_then_fails(db_ready):
    jid = queued_job(attempts=0)
    first = intake.requeue_interrupted()
    assert first == {"requeued": [jid], "failed": []} and db_ready == [jid]
    with store.connect() as conn:
        row = conn.execute("SELECT status, attempts FROM jobs WHERE job_id=?",
                           (jid,)).fetchone()
    assert (row["status"], row["attempts"]) == ("queued", 1)

    # A second restart: now at the limit, so it fails instead of re-queueing.
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET status='running', attempts=? WHERE job_id=?",
                     (intake.MAX_ATTEMPTS, jid))
    db_ready.clear()
    second = intake.requeue_interrupted()
    assert second == {"requeued": [], "failed": [jid]} and db_ready == []
    with store.connect() as conn:
        row = conn.execute("SELECT status, message FROM jobs WHERE job_id=?",
                           (jid,)).fetchone()
    assert row["status"] == "error" and row["message"] == intake.INTERRUPTED_TWICE


@pytest.mark.req("1.5")
def test_a_job_with_no_stored_request_cannot_be_replayed(db_ready):
    jid = queued_job(request=None)
    result = intake.requeue_interrupted()
    assert result == {"requeued": [], "failed": [jid]}
    with store.connect() as conn:
        row = conn.execute("SELECT status, message FROM jobs WHERE job_id=?",
                           (jid,)).fetchone()
    assert row["status"] == "error" and row["message"] == intake.INTERRUPTED_TWICE


@pytest.mark.req("1.5", "13.2")
def test_a_manual_jobs_request_is_stored(_isolated_data):
    from fastapi.testclient import TestClient

    import app as app_module

    with TestClient(app_module.app) as c:
        # The real yt-dlp isn't called for /api/process (it only validates the
        # URL and queues); the worker is a no-op in test mode. We just check the
        # request was persisted for replay.
        r = c.post("/api/process", json={"url": "https://youtu.be/abc", "max_clips": 3})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
    with store.connect() as conn:
        row = conn.execute("SELECT request_json FROM jobs WHERE job_id=?",
                           (job_id,)).fetchone()
    import json
    assert row["request_json"]
    assert json.loads(row["request_json"])["max_clips"] == 3


@pytest.mark.req("1.6")
def test_a_file_source_reads_duration_and_hashes(db_ready, tmp_path):
    from tests.helpers.media import make_video

    video = make_video(tmp_path / "clip.mp4", seconds=4, size=(320, 240))
    with pytest.raises(intake.IntakeError, match="rights"):
        intake.submit_file(video, "clip.mp4", "local", rights_confirmed=False)
    assert db_ready == []
    out = intake.submit_file(video, "clip.mp4", "local", rights_confirmed=True)
    assert db_ready == [out["job_id"]] and 3.5 < out["duration"] < 4.5
    with store.connect() as conn:
        row = conn.execute("SELECT source_key, rights_basis, autopilot FROM jobs "
                           "WHERE job_id=?", (out["job_id"],)).fetchone()
    assert row["source_key"].startswith("file:") and len(row["source_key"]) == len("file:") + 64
    assert (row["rights_basis"], row["autopilot"]) == ("confirmed", 1)
