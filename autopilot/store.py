"""Data access helpers for the Autopilot tables.

Uses the same connection settings and process-wide lock as db.py, so new and
old code never interleave writes inside one process.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import Any, Iterator

import db


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """A locked connection; commits on success, rolls back on error."""
    with db._LOCK:
        conn = db._conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()


def decode_row(row: sqlite3.Row | None) -> dict | None:
    """Row to dict, decoding every `<name>_json` column into `<name>`."""
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key.endswith("_json"):
            name = key[: -len("_json")]
            try:
                out[name] = json.loads(value) if value else None
            except (TypeError, ValueError):
                out[name] = None
        else:
            out[key] = value
    return out


def decode_rows(rows) -> list[dict]:
    return [decode_row(r) for r in rows]


def state_get(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value_json FROM app_state WHERE key=?",
                           (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value_json"])
    except (TypeError, ValueError):
        return default


def state_set(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_state(key, value_json) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (key, json.dumps(value)))


def set_request(job_id: str, request: dict) -> None:
    """Persist a job's replayable request so a restart can re-enqueue it (1.5)."""
    with connect() as conn:
        conn.execute("UPDATE jobs SET request_json=? WHERE job_id=?",
                     (json.dumps(request), job_id))


# ── clips, clip versions and posts (task 6.4) ────────────────────────────
# Callers pass decoded names ("meta", "holds"); the `_json` columns are encoded
# here. Table and column names always come from code, never from a request.

JSON_COLUMNS = {
    "clips": ("sensitive", "meta"),
    "clip_versions": ("spec", "files", "probe", "cover"),
    "posts": ("holds", "meta", "ai_meta", "step_data"),
}
_NAME = __import__("re").compile(r"^[a-z_]+$")


def _encode(table: str, row: dict) -> dict:
    if table not in JSON_COLUMNS:
        raise ValueError(f"Unknown table: {table}")
    out = {}
    for key, value in row.items():
        if not _NAME.match(key):
            raise ValueError(f"Bad column name: {key!r}")
        if key in JSON_COLUMNS[table]:
            out[f"{key}_json"] = None if value is None else json.dumps(value, ensure_ascii=False)
        else:
            out[key] = value
    return out


def insert_row(conn: sqlite3.Connection, table: str, row: dict) -> None:
    data = _encode(table, row)
    conn.execute(f"INSERT INTO {table}({', '.join(data)}) "
                 f"VALUES({', '.join('?' for _ in data)})", tuple(data.values()))


def update_row(conn: sqlite3.Connection, table: str, row_id: str, fields: dict) -> None:
    data = _encode(table, fields)
    if data:
        conn.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in data)} WHERE id=?",
                     (*data.values(), row_id))


def insert_clip(conn, clip: dict) -> None:
    insert_row(conn, "clips", clip)


def insert_clip_version(conn, version: dict) -> None:
    insert_row(conn, "clip_versions", version)


def insert_post(conn, post: dict, *, legacy: bool = False) -> None:
    """Insert a post. A manually scheduled upload (method 'legacy') is created
    only by autopilot/legacy.py, which passes legacy=True (the migration
    copies today's schedules with its own SQL, §13.1); Autopilot can't create
    one (11.6)."""
    if (post.get("method") == "legacy") != legacy:
        raise ValueError("Only autopilot/legacy.py creates scheduled uploads (method 'legacy'); "
                         "Autopilot posts use method 'official'.")
    insert_row(conn, "posts", post)


def update_post(conn, post_id: str, fields: dict) -> None:
    update_row(conn, "posts", post_id, fields)


def get_clip(conn, owner: str, clip_id: str) -> dict | None:
    return decode_row(conn.execute("SELECT * FROM clips WHERE id=? AND owner_id=?",
                                   (clip_id, owner)).fetchone())


def get_post(conn, owner: str, post_id: str) -> dict | None:
    return decode_row(conn.execute("SELECT * FROM posts WHERE id=? AND owner_id=?",
                                   (post_id, owner)).fetchone())


def owner_posts(conn, owner: str, where: str = "", params: tuple = ()) -> list[dict]:
    """The owner's posts, oldest first; `where` is an extra SQL condition
    written in code (values go in `params`)."""
    sql = "SELECT * FROM posts WHERE owner_id=?" + (f" AND ({where})" if where else "")
    return decode_rows(conn.execute(sql + " ORDER BY created_at, id",
                                    (owner, *params)).fetchall())
