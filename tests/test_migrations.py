"""Schema version 1 migration (task 2.3)."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SCHEDULES = [
    # id, status, user_id, privacy, url, error
    ("s-pending", "pending", None, "unlisted", None, None),
    ("s-uploading", "uploading", None, "public", None, None),
    ("s-done", "done", "u1", "private", "https://youtu.be/abc", None),
    ("s-error", "error", None, "unlisted", None, "Upload failed: quota"),
]


def _build_todays_database(seed_preset: bool = False) -> None:
    """A database in the shape it has on disk today (before Autopilot)."""
    import db
    import paths

    db.init()
    conn = sqlite3.connect(paths.db_path())
    conn.executescript((FIXTURES / "legacy_pro_schema.sql").read_text())
    conn.execute("INSERT INTO jobs(job_id, created_at, url, status, user_id) "
                 "VALUES('job1', 1.0, 'https://youtu.be/x', 'done', NULL)")
    conn.execute("INSERT INTO users(id, email, pw_hash, created_at) "
                 "VALUES('u1', 'a@example.com', 'hash', 1.0)")
    conn.execute("INSERT INTO user_settings(user_id, skey, value_json) "
                 "VALUES('u1', 'llm', '{\"model\": \"m\"}')")
    for sid, status, uid, privacy, url, error in SCHEDULES:
        conn.execute(
            "INSERT INTO schedules(id, created_at, job_id, file, platform, title, "
            "description, tags_json, privacy, scheduled_at, status, error, url, "
            "user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, 10.0, "job1", "01-a-vertical.mp4", "youtube", f"Title {sid}",
             "Desc", json.dumps(["a", "b"]), privacy, 1000.0, status, error, url,
             uid))
    if seed_preset:
        conn.execute("INSERT INTO presets(id, name, settings_json) "
                     "VALUES('p1', 'mine', '{}')")
    conn.commit()
    conn.close()


def _open():
    import paths

    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.mark.req("N6.2")
def test_backup_is_taken_first_and_is_private(_isolated_data):
    from autopilot import migrations

    _build_todays_database()
    result = migrations.run()
    backup = Path(result["backup"])
    assert backup.parent == _isolated_data
    assert backup.name.startswith("clipmaker.db.bak-")
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600
    old = sqlite3.connect(backup)
    try:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 0
        assert old.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 4
        assert old.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        old.close()


@pytest.mark.req("N2.1", "N6.2")
def test_schema_version_wal_and_new_tables(_isolated_data):
    from autopilot import migrations

    _build_todays_database()
    migrations.run()
    conn = _open()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.TARGET_VERSION
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert {"profiles", "clips", "clip_versions", "accounts", "posts",
                "publish_attempts", "post_metrics", "platform_usage", "activity",
                "music_clearance", "app_state"} <= _tables(conn)
        job_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert {name for name, _ in migrations.JOB_COLUMNS} <= job_cols
    finally:
        conn.close()


@pytest.mark.req("N6.2")
def test_existing_rows_are_unchanged(_isolated_data):
    from autopilot import migrations

    _build_todays_database()
    migrations.run()
    conn = _open()
    try:
        job = conn.execute("SELECT * FROM jobs WHERE job_id='job1'").fetchone()
        assert (job["url"], job["status"], job["autopilot"], job["attempts"],
                job["ai_calls"]) == ("https://youtu.be/x", "done", 0, 0, 0)
        assert conn.execute("SELECT email FROM users").fetchone()[0] == "a@example.com"
        assert conn.execute("SELECT value_json FROM user_settings").fetchone()[0] \
            == '{"model": "m"}'
        assert conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 4
    finally:
        conn.close()


@pytest.mark.req("15.4", "N6.2")
def test_schedules_are_copied_into_posts(_isolated_data):
    from autopilot import migrations

    _build_todays_database()
    migrations.run()
    conn = _open()
    try:
        posts = {r["id"]: r for r in conn.execute("SELECT * FROM posts")}
    finally:
        conn.close()
    assert set(posts) == {s[0] for s in SCHEDULES}
    expected = {"s-pending": ("scheduled", None),
                "s-uploading": ("failed", "Interrupted by a restart — please re-schedule."),
                "s-done": ("live", None),
                "s-error": ("failed", "Upload failed: quota")}
    assert {k: (p["status"], p["last_error"]) for k, p in posts.items()} == expected
    for post in posts.values():
        assert (post["method"], post["approval"], post["user_locked"]) == \
            ("legacy", "approved", 1)
        assert post["slot_at"] == 1000.0
        meta = json.loads(post["meta_json"])
        assert meta["title"] == f"Title {post['id']}"
        assert meta["tags"] == ["a", "b"] and meta["file"] == "01-a-vertical.mp4"
    assert posts["s-done"]["owner_id"] == "u1"
    assert posts["s-done"]["url"] == "https://youtu.be/abc"
    assert posts["s-pending"]["owner_id"] == "local"
    assert posts["s-done"]["target_visibility"] == "private"


@pytest.mark.req("15.2", "N6.2")
def test_empty_pro_tables_dropped_and_used_ones_kept(_isolated_data):
    from autopilot import migrations

    _build_todays_database(seed_preset=True)
    migrations.run()
    conn = _open()
    try:
        tables = _tables(conn)
    finally:
        conn.close()
    assert "legacy_presets" in tables and "presets" not in tables
    assert not (set(migrations.LEGACY_PRO_TABLES) & tables)
    assert "legacy_batch_queue" not in tables


@pytest.mark.req("N6.2")
def test_second_run_changes_nothing(_isolated_data):
    from autopilot import migrations

    _build_todays_database()
    migrations.run()
    backups_before = sorted(_isolated_data.glob("clipmaker.db.bak-*"))
    result = migrations.run()
    v = migrations.TARGET_VERSION
    assert result == {"from": v, "to": v, "backup": None}
    assert sorted(_isolated_data.glob("clipmaker.db.bak-*")) == backups_before
    conn = _open()
    try:
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 4
    finally:
        conn.close()


@pytest.mark.req("N2.1", "N6.2")
def test_failed_migration_leaves_database_unchanged(_isolated_data, monkeypatch):
    from autopilot import migrations

    _build_todays_database()

    def broken(conn):
        conn.execute("ALTER TABLE jobs ADD COLUMN autopilot INTEGER")
        raise RuntimeError("boom")

    monkeypatch.setitem(migrations.MIGRATIONS, 1, broken)
    with pytest.raises(RuntimeError, match="boom"):
        migrations.run()
    conn = _open()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        job_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert "autopilot" not in job_cols
        assert "posts" not in _tables(conn)
    finally:
        conn.close()


@pytest.mark.req("N6.2")
def test_fresh_install_needs_no_backup(_isolated_data):
    import db
    from autopilot import migrations

    db.init()
    result = migrations.run()
    assert result["backup"] is None
    assert list(_isolated_data.glob("clipmaker.db.bak-*")) == []


@pytest.mark.req("N6.1")
def test_app_start_migrates_and_existing_endpoints_still_work(_isolated_data):
    from fastapi.testclient import TestClient

    import app as app_module
    from autopilot import migrations

    _build_todays_database()
    with TestClient(app_module.app) as c:
        assert c.get("/api/jobs").json()["jobs"][0]["job_id"] == "job1"
        assert len(c.get("/api/schedule").json()["schedules"]) == 4
    conn = _open()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.TARGET_VERSION
    finally:
        conn.close()


@pytest.mark.req("N2.1")
def test_store_helpers_round_trip(_isolated_data):
    import db
    from autopilot import migrations, store

    db.init()
    migrations.run()
    store.state_set("publisher_lease", {"holder": "a", "expires_at": 5})
    assert store.state_get("publisher_lease") == {"holder": "a", "expires_at": 5}
    assert store.state_get("missing", {"x": 1}) == {"x": 1}
    with store.connect() as conn:
        row = conn.execute("SELECT '{\"a\": 1}' AS detail_json, 3 AS n").fetchone()
    assert store.decode_row(row) == {"detail": {"a": 1}, "n": 3}
