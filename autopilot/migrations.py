"""Database migrations for Autopilot (design §3, §13).

`run()` is called at start-up right after `db.init()`. It moves the database to
the latest schema version (`PRAGMA user_version`):

1. Before changing anything, it copies the whole database to
   `clipmaker.db.bak-<YYYYmmdd-HHMMSS>` (readable by this account only).
2. It switches the database to WAL mode.
3. It applies each pending version inside one transaction, so a failure leaves
   the database exactly as it was.

Running it again on an up-to-date database does nothing (no new backup).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import db
import paths

TARGET_VERSION = 4

LEGACY_PRO_TABLES = (
    "presets", "batch_queue", "upload_queue", "clip_analytics",
    "analytics_snapshots", "hashtag_performance", "title_patterns",
    "time_slot_performance",
)

JOB_COLUMNS = (
    ("autopilot", "INTEGER NOT NULL DEFAULT 0"),
    ("profile_id", "TEXT"),
    ("profile_snapshot_json", "TEXT"),
    ("request_json", "TEXT"),
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("stage", "TEXT"),
    ("source_key", "TEXT"),
    ("source_channel_id", "TEXT"),
    ("source_channel_title", "TEXT"),
    ("source_duration", "REAL"),
    ("language", "TEXT"),
    ("rights_basis", "TEXT"),
    ("rights_confirmed_at", "REAL"),
    ("ai_calls", "INTEGER NOT NULL DEFAULT 0"),
    ("best_score", "INTEGER"),
)

V1_TABLES = """
CREATE TABLE IF NOT EXISTS profiles(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
  settings_json TEXT NOT NULL, is_default INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 1, created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS ix_profiles_owner ON profiles(owner_id);

CREATE TABLE IF NOT EXISTS clips(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, job_id TEXT NOT NULL,
  idx INTEGER NOT NULL, src_start REAL NOT NULL, src_end REAL NOT NULL,
  score INTEGER, score_reason TEXT, method TEXT, sensitive_json TEXT,
  current_version_id TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS ix_clips_job ON clips(job_id);

CREATE TABLE IF NOT EXISTS clip_versions(
  id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, version INTEGER NOT NULL,
  spec_json TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
  files_json TEXT, probe_json TEXT, created_at REAL, finished_at REAL,
  UNIQUE(clip_id, version));

CREATE TABLE IF NOT EXISTS accounts(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, platform TEXT NOT NULL,
  external_id TEXT, name TEXT, avatar TEXT, page_id TEXT,
  scopes_json TEXT, token_expires_at REAL, data_access_expires_at REAL,
  status TEXT NOT NULL, status_reason TEXT, made_for_kids INTEGER,
  audit_declared INTEGER, oauth_production_declared INTEGER,
  quota_key TEXT, thumbnail_capability TEXT,
  connected_at REAL, last_publish_at REAL,
  UNIQUE(owner_id, platform));

CREATE TABLE IF NOT EXISTS posts(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
  job_id TEXT, clip_id TEXT, version_id TEXT,
  platform TEXT NOT NULL, account_id TEXT,
  method TEXT NOT NULL, status TEXT NOT NULL, approval TEXT NOT NULL,
  holds_json TEXT NOT NULL DEFAULT '[]',
  slot_at REAL, slot_basis TEXT,
  user_locked INTEGER NOT NULL DEFAULT 0,
  target_visibility TEXT NOT NULL DEFAULT 'public',
  meta_json TEXT NOT NULL, ai_meta_json TEXT NOT NULL,
  disclosure_ai INTEGER NOT NULL DEFAULT 0,
  step TEXT, step_data_json TEXT,
  next_action_at REAL, retry_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, last_error_class TEXT,
  remote_dirty INTEGER NOT NULL DEFAULT 0,
  paused_hold INTEGER NOT NULL DEFAULT 0,
  platform_post_id TEXT, url TEXT, scheduled_publish_at REAL, published_at REAL,
  created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS ix_posts_due ON posts(status, next_action_at);
CREATE INDEX IF NOT EXISTS ix_posts_owner_slot ON posts(owner_id, slot_at);
CREATE INDEX IF NOT EXISTS ix_posts_account_slot ON posts(account_id, slot_at);

CREATE TABLE IF NOT EXISTS publish_attempts(
  id TEXT PRIMARY KEY, post_id TEXT NOT NULL, step TEXT NOT NULL,
  started_at REAL, finished_at REAL, outcome TEXT,
  http_status INTEGER, platform_code TEXT, request_id TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS ix_attempts_post ON publish_attempts(post_id);

CREATE TABLE IF NOT EXISTS post_metrics(
  post_id TEXT NOT NULL, fetched_at REAL NOT NULL, age_hours REAL,
  views INTEGER, likes INTEGER, comments INTEGER,
  PRIMARY KEY(post_id, fetched_at));

CREATE TABLE IF NOT EXISTS platform_usage(
  key TEXT NOT NULL, window_start REAL NOT NULL, count INTEGER NOT NULL,
  PRIMARY KEY(key, window_start));

CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, at REAL NOT NULL,
  actor TEXT NOT NULL, kind TEXT NOT NULL,
  job_id TEXT, clip_id TEXT, post_id TEXT,
  summary TEXT NOT NULL, detail_json TEXT);
CREATE INDEX IF NOT EXISTS ix_activity_owner_at ON activity(owner_id, at);

CREATE TABLE IF NOT EXISTS music_clearance(
  owner_id TEXT NOT NULL, track TEXT NOT NULL, cleared_at REAL NOT NULL,
  PRIMARY KEY(owner_id, track));

CREATE TABLE IF NOT EXISTS app_state(
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
"""

# schedules.status -> (posts.status, error to record or None)
SCHEDULE_STATUS = {
    "pending": ("scheduled", None),
    "uploading": ("failed", "Interrupted by a restart — please re-schedule."),
    "done": ("live", None),
    "error": ("failed", None),
}


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _has_rows(conn: sqlite3.Connection) -> bool:
    """True when any user table holds a row (a fresh database needs no backup)."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]
    return any(conn.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone()
               for t in tables)


def backup_database(conn: sqlite3.Connection) -> Path:
    """Full copy of the database, made with SQLite's online backup API."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = paths.data_dir() / f"clipmaker.db.bak-{stamp}"
    n = 1
    while dest.exists():                 # two runs in the same second
        dest = paths.data_dir() / f"clipmaker.db.bak-{stamp}-{n}"
        n += 1
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    os.chmod(dest, 0o600)
    return dest


def _copy_schedules(conn: sqlite3.Connection) -> int:
    """Copy legacy scheduled uploads into posts as method='legacy' (§13.1)."""
    if not _table_exists(conn, "schedules"):
        return 0
    copied = 0
    for row in conn.execute("SELECT * FROM schedules").fetchall():
        status, error = SCHEDULE_STATUS.get(row["status"] or "", ("failed", None))
        # 15.4: the content is unchanged; only an interrupted upload gets
        # today's restart message (what scheduler.py would have written).
        error = error if error is not None else row["error"]
        try:
            tags = json.loads(row["tags_json"]) if row["tags_json"] else []
        except (TypeError, ValueError):
            tags = []
        meta = {"title": row["title"] or "", "description": row["description"] or "",
                "tags": tags, "privacy": row["privacy"] or "unlisted",
                "file": row["file"] or ""}
        meta_json = json.dumps(meta)
        created = row["created_at"]
        conn.execute(
            "INSERT OR IGNORE INTO posts(id, owner_id, job_id, platform, method, "
            "status, approval, holds_json, slot_at, slot_basis, user_locked, "
            "target_visibility, meta_json, ai_meta_json, last_error, url, "
            "published_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["user_id"] or "local", row["job_id"],
             row["platform"] or "youtube", "legacy", status, "approved", "[]",
             row["scheduled_at"], "user", 1,
             "private" if (row["privacy"] == "private") else "public",
             meta_json, meta_json, error, row["url"],
             row["scheduled_at"] if status == "live" else None,
             created, created))
        copied += 1
    return copied


def _retire_legacy_pro_tables(conn: sqlite3.Connection) -> dict[str, str]:
    """Drop empty pro tables; keep non-empty ones as legacy_<name>."""
    outcome: dict[str, str] = {}
    # analytics_snapshots references clip_analytics, so handle it first.
    for name in ("analytics_snapshots",) + tuple(
            t for t in LEGACY_PRO_TABLES if t != "analytics_snapshots"):
        if not _table_exists(conn, name):
            continue
        has_rows = conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
        if has_rows:
            conn.execute(f"ALTER TABLE {name} RENAME TO legacy_{name}")
            outcome[name] = f"renamed to legacy_{name}"
        else:
            conn.execute(f"DROP TABLE {name}")
            outcome[name] = "dropped"
    return outcome


def _apply_v1(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "jobs")
    for name, decl in JOB_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
    for statement in V1_TABLES.split(";"):
        if statement.strip():
            conn.execute(statement)
    _copy_schedules(conn)
    _retire_legacy_pro_tables(conn)


MIGRATIONS = {1: _apply_v1}


def _apply_v2(conn: sqlite3.Connection) -> None:
    """Task 6.4: keep each clip's AI metadata for both platforms
    ({youtube, instagram, hook, title_suggestions, holds}) on the clip, so a
    post added later for any platform starts from the AI's text."""
    if "meta_json" not in _columns(conn, "clips"):
        conn.execute("ALTER TABLE clips ADD COLUMN meta_json TEXT")


MIGRATIONS[2] = _apply_v2


def _apply_v3(conn: sqlite3.Connection) -> None:
    """Task 9.3: keep the cover each render chose ({t, face, chosen}) on its
    version, so Instagram's thumb_offset uses the clip's cover frame (9.2)
    even when the cover was picked automatically (spec cover.t is null)."""
    if "cover_json" not in _columns(conn, "clip_versions"):
        conn.execute("ALTER TABLE clip_versions ADD COLUMN cover_json TEXT")


MIGRATIONS[3] = _apply_v3


def _apply_v4(conn: sqlite3.Connection) -> None:
    """Task 9.6: a deleted source keeps its row, marked `deleted_at`, so posts
    it already published keep their history and 13.4 still recognises their
    clips if the same video is submitted again. Its files are deleted."""
    if "deleted_at" not in _columns(conn, "jobs"):
        conn.execute("ALTER TABLE jobs ADD COLUMN deleted_at REAL")


MIGRATIONS[4] = _apply_v4


def run() -> dict:
    """Bring the database to TARGET_VERSION. Returns what was done."""
    conn = db._conn()
    try:
        version = _user_version(conn)
        if version >= TARGET_VERSION:
            return {"from": version, "to": version, "backup": None}
        backup = backup_database(conn) if _has_rows(conn) else None
        conn.execute("PRAGMA journal_mode=WAL")
        conn.isolation_level = None      # explicit transaction control below
        conn.execute("BEGIN IMMEDIATE")
        try:
            for step in range(version + 1, TARGET_VERSION + 1):
                MIGRATIONS[step](conn)
            conn.execute(f"PRAGMA user_version = {TARGET_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"from": version, "to": TARGET_VERSION,
                "backup": str(backup) if backup else None}
    finally:
        conn.close()
