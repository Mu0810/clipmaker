"""Profiles and music clearance (task 3.2)."""
from __future__ import annotations

import pytest

DEFAULTS_14_2 = {
    "clips_per_source": 6, "length_min_s": 20, "length_max_s": 60, "min_score": 60,
    "language": None, "metadata_language": None, "layout": "fill",
    "caption_style": "words", "hook": True, "auto_trim": True,
    "music_track": None, "music_volume": 0.25,
    "platforms": ["youtube", "instagram"], "youtube_visibility": "public",
    "min_gap_hours": 4, "daily_max": 2, "mode": "review", "ai_disclosure": False,
    "brand": {"tone": "", "avoid_words": [], "always_hashtags": []},
}


@pytest.mark.req("14.2", "6.1")
def test_default_profile_matches_the_requirement(client):
    import besttime

    data = client.get("/api/autopilot/profiles").json()
    assert len(data["profiles"]) == 1
    default = data["profiles"][0]
    assert default["is_default"] and default["name"] == "Default"
    settings = default["settings"]
    assert {k: settings[k] for k in DEFAULTS_14_2} == DEFAULTS_14_2
    assert settings["windows"]["youtube"]["0"] == sorted(besttime._WINDOWS["youtube"][0])
    assert settings["windows"]["instagram"]["6"] == sorted(besttime._WINDOWS["instagram"][6])


@pytest.mark.req("14.1")
def test_create_and_update_profile(client):
    created = client.post("/api/autopilot/profiles", json={
        "name": "Podcast", "settings": {"clips_per_source": 4, "mode": "full_auto",
                                        "brand": {"always_hashtags": ["#mypod", "mypod"]}}})
    assert created.status_code == 201
    profile = created.json()
    assert profile["settings"]["clips_per_source"] == 4
    assert profile["settings"]["brand"]["always_hashtags"] == ["mypod"]
    assert profile["settings"]["daily_max"] == 2          # untouched defaults kept

    updated = client.patch(f"/api/autopilot/profiles/{profile['id']}",
                           json={"settings": {"daily_max": 3}})
    assert updated.status_code == 200
    body = updated.json()
    assert body["settings"]["daily_max"] == 3 and body["settings"]["clips_per_source"] == 4
    assert body["revision"] == profile["revision"] + 1


@pytest.mark.req("14.1")
@pytest.mark.parametrize("settings", [
    {"unknown_key": 1},
    {"clips_per_source": 0},
    {"clips_per_source": 21},
    {"length_min_s": 60, "length_max_s": 30},
    {"length_max_s": 181},
    {"min_score": 101},
    {"min_gap_hours": 49},
    {"daily_max": 0},
    {"platforms": []},
    {"platforms": ["youtube", "youtube"]},
    {"platforms": ["tiktok"]},
    {"music_volume": 1.5},
    {"layout": "stretch"},
    {"mode": "yolo"},
    {"language": "Hindi"},
    {"windows": {"youtube": {"0": [25]}}},
    {"brand": {"always_hashtags": ["two words"]}},
])
def test_invalid_settings_are_rejected_with_422(client, settings):
    response = client.post("/api/autopilot/profiles", json={"name": "Bad", "settings": settings})
    assert response.status_code == 422
    assert response.json()["detail"]


@pytest.mark.req("14.1")
@pytest.mark.parametrize("body,field,message", [
    ({"settings": {"clips_per_source": 0}}, "clips_per_source", "Must be at least 1."),
    ({"settings": {"length_max_s": 181}}, "length_max_s", "Must be at most 180."),
    ({"settings": {"length_min_s": 70}}, "length_max_s",
     "The longest clip must be longer than the shortest."),
    ({"settings": {"min_gap_hours": None}}, "min_gap_hours", "Enter a number."),
    ({"settings": {"platforms": []}}, "platforms", "Choose at least one platform."),
    ({"settings": {"language": "Hindi"}}, "language",
     "Clip Maker can't transcribe 'Hindi'; pick a spoken language from the list, or Detect."),
    ({"settings": {"brand": {"always_hashtags": ["two words"]}}}, "brand.always_hashtags",
     "'two words' is not a valid hashtag (letters, numbers and _ only)."),
    ({"settings": {"windows": {"youtube": {str(d): [25] for d in range(7)},
                               "instagram": {str(d): [] for d in range(7)}}}},
     "windows", "Posting hours must be whole hours from 0 to 23."),
    ({"name": "  "}, "name", "A preset needs a name."),
    ({"name": "x" * 61}, "name", "Preset names can be at most 60 characters."),
])
def test_each_refused_setting_names_its_field_in_plain_words(client, body, field, message):
    """Task 12.5: the profile screen shows each problem beside its field."""
    default = client.get("/api/autopilot/profiles").json()["profiles"][0]
    response = client.patch(f"/api/autopilot/profiles/{default['id']}", json=body)
    assert response.status_code == 422
    [problem] = response.json()["detail"]
    assert (problem["field"], problem["message"]) == (field, message)


@pytest.mark.req("2.7", "4.4", "14.1")
def test_the_languages_come_with_the_presets_and_a_spoken_one_must_be_transcribable(client):
    data = client.get("/api/autopilot/profiles").json()
    assert len(data["languages"]) == 100                         # the menus' list, by name
    assert data["languages"][:3] == [{"code": "af", "name": "Afrikaans"},
                                     {"code": "sq", "name": "Albanian"},
                                     {"code": "am", "name": "Amharic"}]
    assert {"code": "hi", "name": "Hindi"} in data["languages"]
    default = data["profiles"][0]
    url = f"/api/autopilot/profiles/{default['id']}"
    # A regional code is stored as its language, which Whisper needs; the
    # metadata language keeps it, since YouTube's defaultLanguage takes it.
    body = client.patch(url, json={"settings": {"language": "en-US",
                                                "metadata_language": "pt-BR"}}).json()
    assert (body["settings"]["language"], body["settings"]["metadata_language"]) == ("en", "pt-BR")
    refused = client.patch(url, json={"settings": {"language": "fil"}})
    assert refused.status_code == 422
    [problem] = refused.json()["detail"]
    assert (problem["field"], problem["message"]) == ("language", (
        "Clip Maker can't transcribe 'fil'; pick a spoken language from the list, or Detect."))
    assert client.patch(url, json={"settings": {"language": ""}}).json()["settings"]["language"] is None


@pytest.mark.req("14.1")
def test_repeated_hours_in_a_day_are_rejected(client):
    import autopilot.profiles as profiles

    windows = profiles.default_windows()
    windows["youtube"]["2"] = [9, 9]
    response = client.post("/api/autopilot/profiles",
                           json={"name": "X", "settings": {"windows": windows}})
    assert response.status_code == 422


@pytest.mark.req("3.9")
def test_uncleared_music_track_is_rejected_and_cleared_one_accepted(client, _isolated_data):
    music = _isolated_data / "music"
    music.mkdir(exist_ok=True)
    (music / "beat.mp3").write_bytes(b"ID3")

    rejected = client.post("/api/autopilot/profiles",
                           json={"name": "M", "settings": {"music_track": "beat.mp3"}})
    assert rejected.status_code == 422
    [problem] = rejected.json()["detail"]
    assert problem["field"] == "music_track" and "not marked as cleared" in problem["message"]

    cleared = client.post("/api/autopilot/music/beat.mp3/clearance", json={"cleared": True})
    assert cleared.status_code == 200
    assert client.get("/api/music").json() == {"tracks": ["beat.mp3"], "cleared": ["beat.mp3"]}

    accepted = client.post("/api/autopilot/profiles",
                           json={"name": "M", "settings": {"music_track": "beat.mp3"}})
    assert accepted.status_code == 201


@pytest.mark.req("3.9")
def test_clearance_of_unknown_track_is_404(client):
    response = client.post("/api/autopilot/music/nope.mp3/clearance", json={"cleared": True})
    assert response.status_code == 404


@pytest.mark.req("14.1")
def test_only_profile_cannot_be_deleted(client):
    only = client.get("/api/autopilot/profiles").json()["profiles"][0]
    response = client.delete(f"/api/autopilot/profiles/{only['id']}")
    assert response.status_code == 409


@pytest.mark.req("14.1")
def test_deleting_the_default_promotes_another(client):
    default = client.get("/api/autopilot/profiles").json()["profiles"][0]
    other = client.post("/api/autopilot/profiles", json={"name": "Other"}).json()
    assert client.delete(f"/api/autopilot/profiles/{default['id']}").status_code == 200
    remaining = client.get("/api/autopilot/profiles").json()["profiles"]
    assert [p["id"] for p in remaining] == [other["id"]] and remaining[0]["is_default"]


@pytest.mark.req("14.1")
def test_make_default_and_duplicate(client):
    other = client.post("/api/autopilot/profiles",
                        json={"name": "Other", "settings": {"daily_max": 5}}).json()
    made = client.patch(f"/api/autopilot/profiles/{other['id']}", json={"is_default": True})
    assert made.json()["is_default"] is True
    defaults = [p for p in client.get("/api/autopilot/profiles").json()["profiles"]
                if p["is_default"]]
    assert [p["id"] for p in defaults] == [other["id"]]

    copy = client.post(f"/api/autopilot/profiles/{other['id']}/duplicate").json()
    assert copy["name"] == "Copy of Other" and copy["settings"]["daily_max"] == 5
    assert copy["is_default"] is False


@pytest.mark.req("14.1", "N4.2")
def test_profiles_are_isolated_between_users(two_users):
    alice, bob = two_users
    mine = alice.post("/api/autopilot/profiles", json={"name": "Alice only"}).json()
    # Alice can act on her own profile...
    assert alice.get(f"/api/autopilot/profiles/{mine['id']}").status_code == 200
    # ...Bob cannot see, change or delete it.
    assert bob.get(f"/api/autopilot/profiles/{mine['id']}").status_code == 404
    assert bob.patch(f"/api/autopilot/profiles/{mine['id']}",
                     json={"name": "hijack"}).status_code == 404
    assert bob.delete(f"/api/autopilot/profiles/{mine['id']}").status_code == 404
    assert "Alice only" not in [p["name"] for p in
                                bob.get("/api/autopilot/profiles").json()["profiles"]]
    assert alice.get(f"/api/autopilot/profiles/{mine['id']}").json()["name"] == "Alice only"
