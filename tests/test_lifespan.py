"""Start-up work runs in the lifespan hook, not at import (task 1.2)."""
from __future__ import annotations

import sqlite3
import threading

import pytest

BACKGROUND_THREADS = {"publisher", "job-worker", "maintenance", "render-worker"}


def _thread_names() -> set[str]:
    return {t.name for t in threading.enumerate()}


@pytest.mark.req("N7.1")
def test_import_has_no_database_side_effect(tmp_path):
    """Importing the app in a fresh process must not create the database."""
    import os
    import subprocess
    import sys

    import paths

    data = tmp_path / "fresh"
    data.mkdir()
    env = dict(os.environ, CLIPMAKER_DATA_DIR=str(data), CLIPMAKER_NO_BACKGROUND="1")
    result = subprocess.run([sys.executable, "-c", "import app"], cwd=paths.REPO,
                            env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert not (data / "clipmaker.db").exists()
    assert not (data / "output").exists()


@pytest.mark.req("N7.1", "N6.1")
def test_lifespan_initialises_the_database(client, _isolated_data):
    import paths

    assert paths.output_dir().is_dir()
    with sqlite3.connect(paths.db_path()) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "schedules", "users", "sessions", "user_settings"} <= tables


@pytest.mark.req("N7.1")
def test_background_threads_are_skipped_in_tests(client):
    assert not (_thread_names() & BACKGROUND_THREADS)
