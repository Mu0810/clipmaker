"""SQLite persistence for jobs so history survives a server restart.

A single `jobs` table stores each run plus its final result (as JSON). Live
progress (pct/message) stays in app.py's in-memory store; only durable fields
are written here, on creation and at terminal states.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import paths

# Re-entrant: Autopilot helpers (autopilot/store.py) share this lock, and a
# nested call from the same thread must not deadlock the process.
_LOCK = threading.RLock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(paths.db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init() -> None:
    with _LOCK, _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS jobs(
                job_id            TEXT PRIMARY KEY,
                created_at        REAL,
                url               TEXT,
                status            TEXT,
                source_title      TEXT,
                method            TEXT,
                transcript_source TEXT,
                error             TEXT,
                result_json       TEXT,
                user_id           TEXT,
                pct               REAL,
                message           TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS schedules(
                id            TEXT PRIMARY KEY,
                created_at    REAL,
                job_id        TEXT,
                file          TEXT,
                platform      TEXT,
                title         TEXT,
                description   TEXT,
                tags_json     TEXT,
                privacy       TEXT,
                scheduled_at  REAL,
                status        TEXT,
                error         TEXT,
                url           TEXT,
                user_id       TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS users(
                id          TEXT PRIMARY KEY,
                email       TEXT UNIQUE,
                pw_hash     TEXT,
                created_at  REAL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS sessions(
                token       TEXT PRIMARY KEY,
                user_id     TEXT,
                created_at  REAL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS user_settings(
                user_id     TEXT,
                skey        TEXT,
                value_json  TEXT,
                PRIMARY KEY(user_id, skey)
            )"""
        )
        # Migrate older DBs that predate the user_id columns.
        for tbl in ("jobs", "schedules"):
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({tbl})")}
            if "user_id" not in cols:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id TEXT")
        jobcols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        if "pct" not in jobcols:
            c.execute("ALTER TABLE jobs ADD COLUMN pct REAL")
        if "message" not in jobcols:
            c.execute("ALTER TABLE jobs ADD COLUMN message TEXT")


def create(job_id: str, url: str, user_id: str | None = None) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO jobs(job_id, created_at, url, status, "
            "user_id, pct, message) VALUES(?,?,?,?,?,?,?)",
            (job_id, time.time(), url, "queued", user_id, 0, "Queued"),
        )


def update_progress(job_id: str, status: str | None = None,
                    pct: float | None = None, message: str | None = None,
                    error: str | None = None) -> None:
    """Persist live progress so state survives restarts and is readable by any
    process. Only the provided fields are written."""
    sets, params = [], []
    for col, val in (("status", status), ("pct", pct),
                     ("message", message), ("error", error)):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if not sets:
        return
    params.append(job_id)
    with _LOCK, _conn() as c:
        c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", params)


def reconcile_jobs() -> int:
    """Mark jobs left 'queued'/'running' by a crashed/restarted process as
    errored (their worker is gone). Returns how many were reconciled."""
    with _LOCK, _conn() as c:
        cur = c.execute(
            "UPDATE jobs SET status='error', "
            "message='Interrupted by a restart.', "
            "error='Interrupted by a restart.' "
            "WHERE status IN ('queued','running')")
        return cur.rowcount


def job_owner(job_id: str) -> str | None:
    """The user_id that owns a job (None if unowned, unknown or deleted)."""
    with _LOCK, _conn() as c:
        row = c.execute("SELECT user_id FROM jobs WHERE job_id=? AND deleted_at IS NULL",
                        (job_id,)).fetchone()
    return row["user_id"] if row else None


def finish(job_id: str, status: str, result: dict | None = None,
           error: str | None = None) -> None:
    """Persist a terminal state ('done' or 'error') with its result/error.
    A failure has no result, so the title stored at submission is kept."""
    result = result or {}
    with _LOCK, _conn() as c:
        c.execute(
            "UPDATE jobs SET status=?, source_title=COALESCE(?, source_title), method=?, "
            "transcript_source=?, error=?, result_json=? WHERE job_id=?",
            (
                status,
                result.get("source_title"),
                result.get("method"),
                result.get("transcript_source"),
                error,
                json.dumps(result) if result else None,
                job_id,
            ),
        )


def get(job_id: str) -> dict | None:
    """Return a job in the same shape the status endpoint serves, or None
    (also for a deleted source, task 9.6)."""
    with _LOCK, _conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE job_id=? AND deleted_at IS NULL",
                        (job_id,)).fetchone()
    if not row:
        return None
    result = json.loads(row["result_json"]) if row["result_json"] else None
    pct = row["pct"]
    if pct is None:
        pct = 100 if row["status"] == "done" else 0
    return {
        "status": row["status"],
        "pct": pct,
        "message": row["message"] or row["error"] or row["status"],
        "error": row["error"],
        "result": result,
        "stage": row["stage"] if "stage" in row.keys() else None,
    }


def list_jobs(user_id: str | None = None, limit: int = 100) -> list[dict]:
    """Summaries for the Library, newest first. When user_id is given, only
    that user's jobs are returned; when None (single-user/local), all jobs.
    Deleted sources (task 9.6) aren't listed."""
    where = "WHERE deleted_at IS NULL " + ("AND user_id=? " if user_id is not None else "")
    params: tuple = (user_id, limit) if user_id is not None else (limit,)
    with _LOCK, _conn() as c:
        rows = c.execute(
            "SELECT job_id, created_at, url, status, source_title, "
            "transcript_source, method, result_json FROM jobs "
            f"{where}ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()

    out = []
    for r in rows:
        result = json.loads(r["result_json"]) if r["result_json"] else None
        clips = (result or {}).get("clips", []) if result else []
        out.append({
            "job_id": r["job_id"],
            "created_at": r["created_at"],
            "url": r["url"],
            "status": r["status"],
            "source_title": r["source_title"] or "Untitled video",
            "transcript_source": r["transcript_source"],
            "method": r["method"],
            "clip_count": len(clips),
        })
    return out


# ── scheduled uploads ────────────────────────────────────────────────────
# Task 9.5 moved scheduled uploads onto `posts` (method 'legacy',
# autopilot/legacy.py). The `schedules` table stays, unused, for the
# migration that copies it (design §13.1).


# ── users & sessions (optional auth) ─────────────────────────────────────

def create_user(uid: str, email: str, pw_hash: str) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO users(id, email, pw_hash, created_at) VALUES(?,?,?,?)",
            (uid, email, pw_hash, time.time()),
        )


def get_user_by_email(email: str) -> dict | None:
    with _LOCK, _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return dict(row) if row else None


def get_user(uid: str) -> dict | None:
    with _LOCK, _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


def user_count() -> int:
    with _LOCK, _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_session(token: str, user_id: str) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO sessions(token, user_id, created_at) "
            "VALUES(?,?,?)",
            (token, user_id, time.time()),
        )


def session_user(token: str) -> dict | None:
    """Return {id, email} for a valid session token, or None."""
    with _LOCK, _conn() as c:
        row = c.execute(
            "SELECT u.id AS id, u.email AS email FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token=?",
            (token,),
        ).fetchone()
    return {"id": row["id"], "email": row["email"]} if row else None


def delete_session(token: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


# ── per-user settings (AI key, monetization, …) ──────────────────────────

def settings_get(user_id: str, skey: str) -> dict | None:
    with _LOCK, _conn() as c:
        row = c.execute(
            "SELECT value_json FROM user_settings WHERE user_id=? AND skey=?",
            (user_id, skey),
        ).fetchone()
    if not row or not row["value_json"]:
        return None
    try:
        return json.loads(row["value_json"])
    except Exception:
        return None


def settings_set(user_id: str, skey: str, value: dict) -> None:
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO user_settings(user_id, skey, value_json) "
            "VALUES(?,?,?)",
            (user_id, skey, json.dumps(value or {})),
        )


def settings_delete(user_id: str, skey: str) -> None:
    with _LOCK, _conn() as c:
        c.execute("DELETE FROM user_settings WHERE user_id=? AND skey=?",
                  (user_id, skey))
