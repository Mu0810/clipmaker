"""The unfinished /api/pro endpoints are gone (task 2.1)."""
from __future__ import annotations

import importlib.util
import subprocess

import pytest

REMOVED_MODULES = ("api_pro", "presets", "batch", "analytics",
                   "scheduler_advanced", "editor")


@pytest.mark.req("15.2")
def test_no_pro_routes_are_registered():
    import app

    paths = [getattr(route, "path", "") for route in app.app.routes]
    pro = [p for p in paths if p == "/api/pro" or p.startswith("/api/pro/")]
    assert pro == []
    assert "/api/process" in paths          # the core endpoint is still there


@pytest.mark.req("15.1", "15.2")
def test_pro_modules_are_removed():
    assert [m for m in REMOVED_MODULES if importlib.util.find_spec(m)] == []


@pytest.mark.req("15.1", "N3.2")
def test_editor_trim_with_file_paths_runs_nothing(client, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a))
    response = client.post("/api/pro/editor/trim", json={
        "input_path": "/etc/hosts", "output_path": "/tmp/owned.mp4",
        "start_time": 0, "end_time": 1})
    assert not 200 <= response.status_code < 300
    assert calls == []


@pytest.mark.req("15.3")
@pytest.mark.parametrize("path", ["/api/pro/presets?user_id=someone",
                                  "/api/pro/analytics/summary?user_id=someone",
                                  "/api/pro/schedule/list?user_id=someone"])
def test_query_string_user_ids_reach_no_handler(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert "presets" not in response.text
