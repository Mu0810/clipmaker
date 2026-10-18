"""EditSpec (task 4.5)."""
from __future__ import annotations

import json

import pytest

from media.editspec import EditSpecError, initial, parse, validate


def spec(**changes):
    base = {"in": 10.0, "out": 20.0, "hook": {"enabled": False}}
    base.update(changes)
    return base


def problems(exc_info) -> dict:
    return {p["field"]: p["message"] for p in exc_info.value.problems}


def W(w, s, e):
    return {"w": w, "s": s, "e": e, "p": 0.9}


@pytest.mark.req("7.7")
def test_a_minimal_spec_gets_the_documented_defaults():
    assert validate(spec(), 60).to_dict() == {
        "v": 1, "in": 10.0, "out": 20.0, "cuts": [], "auto_trim": True, "layout": "fill",
        "crop": {"mode": "auto", "x": None},
        "captions": {"enabled": True, "style": "words", "edits": {}},
        "hook": {"enabled": False, "text": "", "duration": 2.5},
        "music": {"track": None, "volume": 0.25},
        "cover": {"t": None}}


@pytest.mark.req("7.3")
def test_cuts_are_merged_and_must_lie_inside_the_clip():
    s = validate(spec(cuts=[[16, 17], [12, 13], [12.5, 14]]), 60)
    assert s.cuts == [(12, 14), (16, 17)]
    with pytest.raises(EditSpecError) as e:
        validate(spec(cuts=[[9, 11], [15, 14]]), 60)
    assert set(problems(e)) == {"cuts.0", "cuts.1"}


@pytest.mark.req("7.7")
def test_the_window_must_be_inside_the_video():
    with pytest.raises(EditSpecError) as e:
        validate(spec(out=10.0), 60)
    assert "end after it starts" in problems(e)["out"]
    with pytest.raises(EditSpecError) as e:
        validate(spec(out=75.0), 60)
    assert "after the video ends" in problems(e)["out"]
    with pytest.raises(EditSpecError) as e:
        validate(spec(**{"in": -1.0}), 60)
    assert "in" in problems(e)


@pytest.mark.req("7.3", "7.7")
def test_clips_must_be_at_least_3_seconds_after_cuts():
    with pytest.raises(EditSpecError) as e:
        validate(spec(out=12.5), 60)
    assert "at least 3 s" in problems(e)["length"]
    with pytest.raises(EditSpecError) as e:
        validate(spec(out=14.0, cuts=[[10.5, 12.0]]), 60)      # 4 s minus 1.5 s
    assert "after cuts" in problems(e)["length"]
    assert validate(spec(out=13.0), 60).out == 13.0


@pytest.mark.req("3.6", "7.7")
def test_the_3_second_minimum_applies_after_trimming_pauses():
    words = [W("hi", 10.0, 11.0)]                               # 1 s of speech in 10 s
    with pytest.raises(EditSpecError) as e:
        validate(spec(), 60, words=words)
    assert "trimming pauses" in problems(e)["length"]
    assert validate(spec(auto_trim=False), 60, words=words).auto_trim is False


@pytest.mark.req("3.7", "7.5")
def test_hook_text_is_limited_and_required_when_the_hook_is_on():
    with pytest.raises(EditSpecError) as e:
        validate(spec(hook={"enabled": True, "text": "x" * 49}), 60)
    assert "at most 48" in problems(e)["hook.text"]
    with pytest.raises(EditSpecError) as e:
        validate(spec(hook={"enabled": True, "text": "   "}), 60)
    assert "turn the hook off" in problems(e)["hook.text"]
    s = validate(spec(hook={"enabled": True, "text": "  The   part\nnobody saw "}), 60)
    assert s.hook.text == "The part nobody saw"


@pytest.mark.req("3.9", "7.5")
@pytest.mark.parametrize("volume", [1.5, -0.1])
def test_volume_must_be_between_0_and_1(volume):
    with pytest.raises(EditSpecError) as e:
        parse(spec(music={"volume": volume}))
    assert "music.volume" in problems(e)


@pytest.mark.req("3.9")
def test_music_must_be_cleared_for_publishing():
    with pytest.raises(EditSpecError) as e:
        validate(spec(music={"track": "beat.mp3"}), 60, is_cleared=lambda t: False)
    assert "isn't marked as cleared" in problems(e)["music.track"]
    with pytest.raises(EditSpecError):
        validate(spec(music={"track": "beat.mp3"}), 60)               # nobody vouched
    with pytest.raises(EditSpecError) as e:
        validate(spec(music={"track": "../beat.mp3"}), 60, is_cleared=lambda t: True)
    assert "Choose a track" in problems(e)["music.track"]
    s = validate(spec(music={"track": "beat.mp3", "volume": 0.4}), 60,
                 is_cleared=lambda t: t == "beat.mp3")
    assert (s.music.track, s.music.volume) == ("beat.mp3", 0.4)


@pytest.mark.req("7.4")
def test_caption_edits_are_keyed_by_word_number():
    s = validate(spec(captions={"edits": {"05": " Kimchi  "}}), 60)
    assert s.captions.edits == {"5": "Kimchi"}
    with pytest.raises(EditSpecError) as e:
        validate(spec(captions={"edits": {"abc": "x", "-1": "y", "3": "  "}}), 60)
    assert set(problems(e)) == {"captions.edits.abc", "captions.edits.-1", "captions.edits.3"}
    with pytest.raises(EditSpecError) as e:
        validate(spec(captions={"edits": {"5": "x"}}), 60,
                 words=[W("a", 10, 11), W("b", 11, 12), W("c", 12, 19)])
    assert "no word with that number" in problems(e)["captions.edits.5"]


@pytest.mark.req("7.5")
def test_a_manual_crop_needs_a_position():
    with pytest.raises(EditSpecError) as e:
        validate(spec(crop={"mode": "manual"}), 60)
    assert "crop.x" in problems(e)
    with pytest.raises(EditSpecError):
        parse(spec(crop={"mode": "manual", "x": 1.2}))
    assert validate(spec(crop={"mode": "manual", "x": 0.3}), 60).crop.x == 0.3
    assert validate(spec(crop={"mode": "auto", "x": 0.3}), 60).crop.x is None


@pytest.mark.req("3.8", "7.5")
def test_the_cover_frame_must_be_inside_the_clip():
    with pytest.raises(EditSpecError) as e:
        validate(spec(cover={"t": 10.0}), 60)
    assert "cover.t" in problems(e)
    assert validate(spec(cover={"t": 9.5}), 60).cover.t == 9.5


@pytest.mark.req("7.7")
@pytest.mark.parametrize("bad", [{"colour": "red"}, {"layout": "stretch"},
                                 {"out": float("nan")}, {"v": 2},
                                 {"captions": {"style": "comic"}}])
def test_unknown_keys_and_bad_values_are_rejected(bad):
    with pytest.raises(EditSpecError):
        parse(spec(**bad))


@pytest.mark.req("7.7")
def test_specs_round_trip_through_json():
    s = validate(spec(cuts=[[12, 13]], captions={"edits": {"3": "Hi"}}), 60)
    stored = json.loads(json.dumps(s.to_dict()))
    assert json.loads(json.dumps(validate(stored, 60).to_dict())) == stored


@pytest.mark.req("3.7", "3.9", "7.7")
def test_the_initial_spec_follows_the_profile():
    from autopilot.profiles import ProfileSettings

    settings = ProfileSettings().model_dump()
    first = initial(12.0, 40.0, settings, "  The part nobody saw ")
    assert first.to_dict() == {
        "v": 1, "in": 12.0, "out": 40.0, "cuts": [], "auto_trim": True, "layout": "fill",
        "crop": {"mode": "auto", "x": None},
        "captions": {"enabled": True, "style": "words", "edits": {}},
        "hook": {"enabled": True, "text": "The part nobody saw", "duration": 2.5},
        "music": {"track": None, "volume": 0.25}, "cover": {"t": None}}
    assert validate(first.to_dict(), 60).hook.text == "The part nobody saw"
    assert initial(12.0, 40.0, settings, "").hook.enabled is False
    no_hook = dict(settings, hook=False, layout="fit", caption_style="karaoke")
    later = initial(12.0, 40.0, no_hook, "A hook")
    assert (later.hook.enabled, later.layout, later.captions.style) == (False, "fit", "karaoke")


def test_the_caller_can_lower_the_minimum_length():
    # The manual flow has always made clips shorter than Reels' 3 s minimum.
    data = {"in": 0, "out": 2, "hook": {"enabled": False}}
    with pytest.raises(EditSpecError) as exc:
        validate(data, 10.0)
    assert "at least 3 s" in problems(exc)["length"]
    assert validate(data, 10.0, min_length=0.5).out == 2
    with pytest.raises(EditSpecError) as exc:
        validate({**data, "out": 0.3}, 10.0, min_length=0.5)
    assert "at least 0.5 s" in problems(exc)["length"]
