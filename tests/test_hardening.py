"""Cross-site request check and private file permissions (task 2.2)."""
from __future__ import annotations

import os
import stat

import pytest

OWN = "http://testserver"


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.req("N3.1")
def test_cross_site_post_is_blocked(client):
    response = client.post("/api/process", json={"url": ""},
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-site request blocked."}


@pytest.mark.req("N3.1")
def test_cross_site_multipart_upload_is_blocked(client):
    response = client.post("/api/process-file",
                           files={"file": ("a.mp4", b"x", "video/mp4")},
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


@pytest.mark.req("N3.1")
@pytest.mark.parametrize("headers", [
    {"Origin": "null"},
    {"Origin": "http://testserver:8001"},           # other local port
    {"Origin": "http://localhost"},                 # other host name
    {"Referer": "https://evil.example/page"},       # no Origin, foreign Referer
])
def test_other_cross_site_shapes_are_blocked(client, headers):
    response = client.post("/api/process", json={"url": ""}, headers=headers)
    assert response.status_code == 403


@pytest.mark.req("N3.1")
@pytest.mark.parametrize("headers", [
    {"Origin": OWN},
    {"Referer": OWN + "/"},
    {},                                             # curl or a script
])
def test_same_site_requests_reach_the_handler(client, headers):
    response = client.post("/api/process", json={"url": ""}, headers=headers)
    assert response.status_code == 400             # "A YouTube URL is required."


@pytest.mark.req("N3.1")
def test_get_requests_are_not_checked(client):
    response = client.get("/api/jobs", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200


@pytest.mark.req("N3.1")
def test_non_api_paths_are_not_checked(client):
    response = client.post("/not-an-api", headers={"Origin": "https://evil.example"})
    assert response.status_code != 403


@pytest.mark.req("N3.1")
def test_secret_files_and_profile_folder_are_restricted(_isolated_data):
    from fastapi.testclient import TestClient

    import app as app_module

    secrets = ["token.json", "client_secret.json", "ig_session.json",
               "llm_config.json", "monetize_config.json"]
    for name in secrets:
        path = _isolated_data / name
        path.write_text("{}")
        path.chmod(0o644)
    profile = _isolated_data / "yt_pw_profile"
    profile.mkdir()
    profile.chmod(0o755)

    with TestClient(app_module.app):
        pass

    assert {n: _mode(_isolated_data / n) for n in secrets} == {n: 0o600 for n in secrets}
    assert _mode(_isolated_data / "clipmaker.db") == 0o600
    assert _mode(profile) == 0o700


@pytest.mark.req("N3.1")
def test_symlinks_are_not_followed(_isolated_data, tmp_path):
    from fastapi.testclient import TestClient

    import app as app_module

    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    outside.chmod(0o644)
    (_isolated_data / "token.json").symlink_to(outside)

    with TestClient(app_module.app):
        pass

    assert _mode(outside) == 0o644


@pytest.mark.req("N3.1")
def test_files_created_after_start_are_private(client, _isolated_data):
    new_file = _isolated_data / "created-later.json"
    new_file.write_text("{}")
    assert _mode(new_file) == 0o600
