"""Caption and hook overlay stream (task 5.2)."""
from __future__ import annotations

import subprocess

import numpy as np
import pytest
from PIL import Image

from media import overlays as ov
from media.editspec import parse


def W(w, s, e):
    return {"w": w, "s": s, "e": e, "p": 0.9}


# Two sentences; the first word of each starts a caption.
WORDS = [W("Hello", 0.0, 0.4), W("there", 0.5, 0.9), W("my", 1.0, 1.2),
         W("friend.", 1.3, 1.8), W("This", 2.5, 2.8), W("is", 2.9, 3.0),
         W("kimchee", 3.1, 3.6), W("time.", 3.7, 4.2)]
FULL = [(0.0, 5.0)]


def spec(style="words", hook=None, edits=None, captions=True):
    return parse({"in": 0.0, "out": 5.0, "auto_trim": False,
                  "captions": {"enabled": captions, "style": style, "edits": edits or {}},
                  "hook": hook or {"enabled": False}})


def build(tmp_path, s, words=WORDS, spans=FULL, **kw):
    return ov.build_overlays(words, spans, s, tmp_path / "ov", **kw)


def timeline(intervals, field="caption"):
    return [(round(i["start"], 3), round(i["end"], 3), i[field]) for i in intervals]


@pytest.mark.req("3.4")
def test_words_style_chunks_of_up_to_three_words(tmp_path):
    _, intervals = build(tmp_path, spec("words"))
    assert timeline(intervals) == [
        (0.0, 1.3, "Hello there my"),      # 1.2 s from first start to last end
        (1.3, 2.3, "friend."),             # stays 0.5 s after the word ends
        (2.3, 2.5, None),
        (2.5, 3.7, "This is kimchee"),
        (3.7, 4.7, "time."),
        (4.7, 5.0, None)]


@pytest.mark.req("3.4")
def test_phrases_style_shows_one_sentence_at_a_time(tmp_path):
    _, intervals = build(tmp_path, spec("phrases"))
    assert timeline(intervals) == [
        (0.0, 2.3, "Hello there my friend."), (2.3, 2.5, None),
        (2.5, 4.7, "This is kimchee time."), (4.7, 5.0, None)]


@pytest.mark.req("3.4")
def test_karaoke_highlights_each_word_until_the_next_one(tmp_path):
    _, intervals = build(tmp_path, spec("karaoke"))
    assert timeline(intervals, "active") == [
        (0.0, 0.5, 0), (0.5, 1.0, 1), (1.0, 1.3, 2), (1.3, 2.3, 3), (2.3, 2.5, None),
        (2.5, 2.9, 4), (2.9, 3.1, 5), (3.1, 3.7, 6), (3.7, 4.7, 7), (4.7, 5.0, None)]
    assert intervals[0]["caption"] == "Hello there my friend."
    assert intervals[0]["words"] == [0, 1, 2, 3]


@pytest.mark.req("3.4")
@pytest.mark.parametrize("style", ["words", "phrases", "karaoke"])
def test_every_word_is_on_screen_while_it_is_spoken(tmp_path, style):
    _, intervals = build(tmp_path, spec(style))
    for i, word in enumerate(WORDS):
        for t in (word["s"], (word["s"] + word["e"]) / 2):   # as it starts, and midway
            shown = next(iv for iv in intervals if iv["start"] <= t < iv["end"])
            assert i in shown["words"], (style, word["w"], t)
            if style == "karaoke":
                assert shown["active"] == i


@pytest.mark.req("7.4")
def test_caption_edits_are_applied(tmp_path):
    _, intervals = build(tmp_path, spec("words", edits={"6": "Kimchi"}))
    assert "This is Kimchi" in [iv["caption"] for iv in intervals]
    assert not any("kimchee" in (iv["caption"] or "") for iv in intervals)


@pytest.mark.req("3.4", "3.6", "7.3")
def test_cut_words_are_left_out_and_later_words_move_up(tmp_path):
    spans = [(0.0, 1.25), (2.4, 5.0)]          # "friend." (1.3-1.8) is cut
    _, intervals = build(tmp_path, spec("words"), spans=spans)
    assert timeline(intervals) == [
        (0.0, 1.35, "Hello there my"), (1.35, 2.55, "This is kimchee"),
        (2.55, 3.55, "time."), (3.55, 3.85, None)]


@pytest.mark.req("3.4")
def test_long_sentences_split_into_pieces_of_at_most_three_lines(tmp_path):
    words = [W(f"word{k}", 0.3 * k, 0.3 * k + 0.25) for k in range(39)] + \
        [W("end.", 11.7, 11.95)]
    s = parse({"in": 0.0, "out": 12.5, "auto_trim": False,
               "captions": {"style": "phrases"}, "hook": {"enabled": False}})
    _, intervals = build(tmp_path, s, words=words, spans=[(0.0, 12.5)])
    pieces = [iv for iv in intervals if iv["caption"]]
    assert len(pieces) > 1
    assert [w for iv in pieces for w in iv["words"]] == list(range(40))
    ts = ov.Typesetter(64)
    for iv in pieces:
        lines = ts.wrap(ts.words(iv["caption"].upper()), ov.caption_width(1080))
        assert len(lines) <= ov.PHRASE_MAX_LINES


@pytest.mark.req("3.4")
def test_languages_without_spaces_join_without_spaces(tmp_path):
    words = [W("今日", 0.0, 0.3), W("は", 0.3, 0.5), W("晴れ", 0.5, 0.9),
             W("です。", 0.9, 1.4)]
    s = parse({"in": 0.0, "out": 2.0, "auto_trim": False,
               "captions": {"style": "phrases"}, "hook": {"enabled": False}})
    _, intervals = build(tmp_path, s, words=words, spans=[(0.0, 2.0)], language="ja")
    assert intervals[0]["caption"] == "今日は晴れです。"


# ── the ffconcat list and the frames ─────────────────────────────────────

HOOK = {"enabled": True, "text": "Wait for it", "duration": 2.5}


def read_concat(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "ffconcat version 1.0"
    files, durations = [], []
    for line in lines[1:]:
        if line.startswith("file '"):
            files.append(line[len("file '"):-1])
        elif line.startswith("duration "):
            durations.append(float(line.split()[1]))
        else:
            assert line == f"option framerate {ov.CONCAT_FRAMERATE}", line
    return files, durations


def has_pixels(img, top, bottom):
    return img.crop((0, int(1920 * top), 1080, int(1920 * bottom))).getbbox() is not None


@pytest.mark.req("3.4", "3.7")
def test_the_ffconcat_list_is_valid_and_sums_to_the_clip_length(tmp_path):
    concat, intervals = build(tmp_path, spec("karaoke", hook=HOOK))
    files, durations = read_concat(concat)
    assert files[:-1] == [iv["png"].name for iv in intervals]
    assert files[-1] == files[-2]                    # the last image is listed twice
    assert len(durations) == len(intervals) and all(d > 0 for d in durations)
    assert sum(durations) == pytest.approx(5.0, abs=1e-9)
    assert all((concat.parent / f).is_file() for f in files)


@pytest.mark.req("3.4")
def test_ffmpeg_reads_the_stream_with_millisecond_timing(tmp_path):
    concat, intervals = build(tmp_path, spec("karaoke"))
    out = subprocess.run(["ffprobe", "-v", "error", *ov.input_args(concat),
                          "-show_entries", "frame=pts_time", "-of", "csv=p=0"],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    times = [float(t) for t in out.split()]
    assert times == pytest.approx([iv["start"] for iv in intervals] + [5.0], abs=1e-3)


@pytest.mark.req("3.7")
def test_the_hook_shows_for_its_duration_only(tmp_path):
    _, intervals = build(tmp_path, spec("words", hook=HOOK))
    assert timeline(intervals, "hook") == [
        (0.0, 1.3, True), (1.3, 2.3, True), (2.3, 2.5, True),
        (2.5, 3.7, False), (3.7, 4.7, False), (4.7, 5.0, False)]
    for iv in intervals:
        img = Image.open(iv["png"])
        assert has_pixels(img, 0.0, 0.45) == iv["hook"]
        assert has_pixels(img, 0.55, 1.0) == (iv["caption"] is not None)


@pytest.mark.req("3.7")
def test_the_hook_never_outlasts_a_short_clip(tmp_path):
    s = parse({"in": 0.0, "out": 3.0, "auto_trim": False, "captions": {"enabled": False},
               "hook": {"enabled": True, "text": "Wait", "duration": 5.0}})
    _, intervals = build(tmp_path, s, spans=[(0.0, 2.0)])
    assert timeline(intervals, "hook") == [(0.0, 2.0, True)]


@pytest.mark.req("3.4")
def test_frames_are_full_size_rgba_and_karaoke_uses_the_accent_colour(tmp_path):
    _, intervals = build(tmp_path, spec("karaoke"))
    for iv in intervals:
        img = Image.open(iv["png"])
        assert (img.size, img.mode) == ((1080, 1920), "RGBA")
    blank = next(iv for iv in intervals if iv["caption"] is None)
    assert Image.open(blank["png"]).getbbox() is None

    def accent(iv):
        return np.all(np.asarray(Image.open(iv["png"])) == (255, 214, 61, 255), axis=-1)

    first, second = accent(intervals[0]), accent(intervals[1])
    assert first.sum() > 100 and second.sum() > 100
    assert np.nonzero(second)[1].mean() > np.nonzero(first)[1].mean()   # moved right


def test_identical_frames_are_written_once(tmp_path):
    _, intervals = build(tmp_path, spec("words"))
    assert len({iv["png"] for iv in intervals if iv["caption"] is None}) == 1
    written = sorted((tmp_path / "ov").glob("ov_*.png"))
    assert written == sorted({iv["png"] for iv in intervals})


@pytest.mark.req("3.7", "7.5")
def test_captions_off_leaves_only_the_hook(tmp_path):
    _, intervals = build(tmp_path, spec(captions=False, hook=HOOK))
    assert timeline(intervals, "hook") == [(0.0, 2.5, True), (2.5, 5.0, False)]
    assert all(iv["caption"] is None for iv in intervals)


def test_nothing_to_show_is_one_blank_frame(tmp_path):
    concat, intervals = build(tmp_path, spec(captions=False))
    assert timeline(intervals) == [(0.0, 5.0, None)]
    files, durations = read_concat(concat)
    assert files == [intervals[0]["png"].name] * 2 and durations == [5.0]


def test_a_clip_with_no_length_is_refused(tmp_path):
    with pytest.raises(ValueError):
        build(tmp_path, spec(), spans=[])


def test_old_frames_are_replaced_and_spec_dicts_are_accepted(tmp_path):
    stale = tmp_path / "ov" / "ov_999.png"
    stale.parent.mkdir()
    stale.write_bytes(b"old")
    _, intervals = ov.build_overlays(WORDS, FULL, spec("phrases").to_dict(), tmp_path / "ov")
    assert not stale.exists() and len(intervals) == 4
