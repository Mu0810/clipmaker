"""Script-aware text drawing (task 5.1)."""
from __future__ import annotations

import logging

import numpy as np
import pytest

from media import text as mt

SAMPLES = {"latin": "Hello", "devanagari": "नमस्ते", "bengali": "নমস্কার",
           "arabic": "مرحبا"}


def faces_or_skip(script: str):
    faces = mt.faces_for(script)
    if not faces:
        pytest.skip(f"No {script} font is installed on this machine.")
    return faces


@pytest.mark.req("3.5")
@pytest.mark.parametrize("script", sorted(SAMPLES))
def test_each_script_gets_a_font_that_covers_every_character(script):
    faces_or_skip(script)
    pieces, dropped = mt.resolve(SAMPLES[script], script)
    assert dropped == []
    assert "".join(piece for piece, _ in pieces) == SAMPLES[script]
    assert all(face.covers(piece) for piece, face in pieces)


@pytest.mark.req("3.5")
@pytest.mark.parametrize("ch,script", [
    ("A", "latin"), ("ж", "latin"), ("न", "devanagari"), ("ক", "bengali"),
    ("ਸ", "gurmukhi"), ("ப", "tamil"), ("ب", "arabic"), ("ש", "hebrew"),
    ("ก", "thai"), ("日", "cjk"), ("한", "cjk"), ("\U00010D30", "other")])
def test_characters_are_assigned_to_their_script(ch, script):
    assert mt.block_script(ch) == script


@pytest.mark.req("3.5")
def test_a_hinglish_line_splits_into_runs():
    assert mt.split_runs("यह trick सच है!") == [
        ("यह ", "devanagari"), ("trick ", "latin"), ("सच है!", "devanagari")]
    assert mt.split_runs("“Hello”") == [("“Hello”", "latin")]
    assert mt.split_runs("2026") == [("2026", "latin")]
    assert mt.split_runs("") == []


@pytest.mark.req("3.5")
def test_a_mixed_word_draws_each_part_with_its_own_font():
    faces_or_skip("devanagari")
    word = mt.Typesetter(64).word("TRICKसच")
    assert [run.text for run in word.runs] == ["TRICK", "सच"]
    assert word.runs[0].face.path != word.runs[1].face.path
    assert word.width == pytest.approx(sum(run.width for run in word.runs))


@pytest.mark.req("3.5")
def test_a_character_no_font_covers_is_dropped_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="media.text"):
        word = mt.Typesetter(64).word("A\U00010D30B")
    assert "".join(run.text for run in word.runs) == "AB"
    assert word.dropped == ["\U00010D30"]
    assert "U+10D30" in caplog.text


@pytest.mark.req("3.5", "11.3")
def test_missing_shaping_is_reported(monkeypatch):
    from autopilot import readiness

    monkeypatch.setattr(mt.features, "check_feature", lambda name: False)
    assert mt.shaping_available() is False
    assert readiness._shaping_available() is False
    assert mt.Typesetter(40).engine == mt.ImageFont.Layout.BASIC


@pytest.mark.req("3.5")
@pytest.mark.parametrize("text,needs", [
    ("Hello", False), ("नमस्ते", True), ("مرحبا", True), ("今日は", False),
    ("Hi 👋", False), ("यह trick", True), ("", False)])
def test_needs_shaping(text, needs):
    assert mt.needs_shaping(text) is needs


@pytest.mark.req("3.5")
def test_emoji_are_stripped_but_indic_joiners_are_kept():
    assert mt.strip_emoji("Fire 🔥 and 👨‍👩‍👧 family") == "Fire  and  family"
    half_form = "क्\u200dष"
    assert mt.strip_emoji(half_form + " 👀") == half_form + " "


@pytest.mark.req("3.5")
def test_arabic_words_come_out_right_to_left():
    faces_or_skip("arabic")
    assert mt.is_rtl("مرحبا بكم") and not mt.is_rtl("Hello مرحبا")
    ts = mt.Typesetter(64)
    line = ts.wrap(ts.words("مرحبا بكم"), 2000)[0]
    left = {word.text: x for _, word, x in ts.place(line, 500, rtl=True)}
    assert left["مرحبا"] > left["بكم"]       # the first word sits on the right


@pytest.mark.req("3.5")
def test_long_cjk_text_breaks_between_characters():
    faces_or_skip("cjk")
    ts = mt.Typesetter(88)
    lines = ts.wrap(ts.words("今日はとても良い天気です"), 900)
    assert len(lines) == 2
    assert "".join(w.text for line in lines for _, w in line) == "今日はとても良い天気です"
    assert all(ts.line_width(line) <= 900 for line in lines)


@pytest.mark.req("3.5")
def test_collections_use_their_bold_face():
    path = mt.system_fonts().get("Kohinoor.ttc")
    if path is None:
        pytest.skip("Kohinoor.ttc is not installed on this machine.")
    face = mt.load_face(path)
    assert (face.family, face.index) == ("Kohinoor Devanagari", 3)


@pytest.mark.req("3.4", "3.5", "3.7")
def test_captions_karaoke_and_hook_draw_their_text(tmp_path):
    import captions
    from PIL import Image

    captions._render_png("नमस्ते दोस्तों", tmp_path / "phrase.png")
    captions._render_karaoke_png(["one", "two", "three"], 1, tmp_path / "karaoke.png")
    assert captions.render_hook("नमस्ते 👀", tmp_path / "hook.png") is True
    assert captions.render_hook("👀🔥", tmp_path / "empty.png") is False
    for name in ("phrase.png", "karaoke.png", "hook.png"):
        img = Image.open(tmp_path / name)
        assert (img.size, img.mode) == ((1080, 1920), "RGBA")
        assert img.getbbox() is not None
    pixels = np.asarray(Image.open(tmp_path / "karaoke.png"))
    highlighted = np.all(pixels == (255, 214, 61, 255), axis=-1).sum()
    assert highlighted > 100                 # the active word is in the accent colour


@pytest.mark.req("3.5", "8.9")
def test_thumbnail_words_keep_their_vowel_signs():
    import thumbnail

    assert thumbnail.thumb_text("नमस्ते दोस्तों आज") == "नमस्ते दोस्तों आज"
    assert thumbnail.thumb_text("How to cook rice") == "COOK RICE"
    assert thumbnail.thumb_text("Don't stop 🔥 now") == "DON'T STOP NOW"
