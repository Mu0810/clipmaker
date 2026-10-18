"""Every data path resolves inside the data folder (task 1.1)."""
from __future__ import annotations

import pytest


def _data_paths():
    import instagram_upload
    import llm
    import monetize
    import paths
    import youtube_pw
    import youtube_upload

    return {
        "database": paths.db_path(),
        "output": paths.output_dir(),
        "music": paths.music_dir(),
        "llm config": llm._config_file(),
        "monetize config": monetize._config_file(),
        "youtube client": youtube_upload._client_secret(),
        "youtube saved login": youtube_upload._token_file(),
        "instagram session": instagram_upload._session_file(),
        "instagram account cache": instagram_upload._meta_file(),
        "studio browser profile": youtube_pw._profile_dir(),
        "studio account cache": youtube_pw._meta_file(),
    }


@pytest.mark.req("N7.1", "N6.1")
def test_all_data_paths_follow_the_data_folder(_isolated_data):
    root = _isolated_data.resolve()
    outside = {name: str(p) for name, p in _data_paths().items()
               if root not in p.parents}
    assert outside == {}


@pytest.mark.req("N6.1")
def test_default_data_folder_is_the_repository(monkeypatch):
    import paths

    monkeypatch.delenv("CLIPMAKER_DATA_DIR")
    assert paths.data_dir() == paths.REPO
    assert paths.db_path() == paths.REPO / "clipmaker.db"
    assert paths.models_dir() == paths.REPO / "models"


@pytest.mark.req("N6.1")
def test_bundled_assets_stay_in_the_repository(_isolated_data):
    import paths

    assert paths.models_dir().parent == paths.REPO
    assert paths.fonts_dir().parent == paths.REPO


@pytest.mark.req("N7.1")
def test_data_file_rejects_paths():
    import paths

    for bad in ("", "../x.json", "sub/x.json"):
        with pytest.raises(ValueError):
            paths.data_file(bad)


@pytest.mark.req("N6.1")
def test_music_folder_is_created_in_the_data_folder(_isolated_data):
    import music

    assert music.list_tracks() == []
    assert (_isolated_data / "music").is_dir()
