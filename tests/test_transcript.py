"""Word-level transcripts (task 4.3)."""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from media.transcript import sentences
from tests.helpers.media import make_video

FIXTURES = Path(__file__).parent / "fixtures"


def W(w, s, e, p=0.9):
    return {"w": w, "s": s, "e": e, "p": p}


def evenly(tokens, start=0.0, step=0.5, gap=0.125):
    """Words one after another; times are exact binary fractions so equal
    gaps compare equal."""
    return [W(tok, start + i * step, start + i * step + step - gap)
            for i, tok in enumerate(tokens)]


def texts(words, language=None, **kw):
    return [s["text"] for s in sentences(words, language, **kw)]


def fake_segment(start, end, text, words=()):
    return NS(start=start, end=end, text=text,
              words=[NS(word=w, start=s, end=e, probability=p) for (w, s, e, p) in words])


@pytest.fixture
def whisper(monkeypatch):
    """Stands in for faster-whisper: records each call, returns scripted segments."""
    from media import transcript as tr

    state = {"models": {}, "detect": "hi", "duration": 4.0, "segments": [
        fake_segment(0.0, 1.9, " नमस्ते दोस्तों।",
                     [(" नमस्ते", 0.0, 0.62, 0.91), (" दोस्तों।", 0.7, 1.9, 0.884)]),
        fake_segment(2.0, 2.2, "   "),
        fake_segment(2.3, 4.0, " आज हम बात करेंगे",
                     [(" आज", 2.3, 2.6, 0.8), (" हम", 2.65, 2.9, 0.7),
                      (" बात", 3.0, 3.4, 0.95), (" करेंगे", 3.45, 4.0, 0.99)]),
    ]}

    class Model:
        def __init__(self, name):
            self.name, self.calls = name, []

        def transcribe(self, audio, **kwargs):
            assert Path(audio).is_file(), "the audio file must exist while transcribing"
            self.calls.append({"audio": audio, **kwargs})
            info = NS(language=kwargs.get("language") or state["detect"],
                      language_probability=0.97, duration=state["duration"])
            return iter(state["segments"]), info

    monkeypatch.setattr(tr, "get_model",
                        lambda name: state["models"].setdefault(name, Model(name)))
    return state


@pytest.fixture
def video(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    return make_video(job / "source.mp4", seconds=1)


# ── sentence rules ───────────────────────────────────────────────────────

@pytest.mark.req("2.2")
def test_sentences_end_at_punctuation():
    words = evenly(["Hello", "world.", "How", "are", "you?", "Great!"])
    assert texts(words) == ["Hello world.", "How are you?", "Great!"]


@pytest.mark.req("2.2", "2.6")
@pytest.mark.parametrize("mark", ". ! ? । ॥ ؟ 。 ！ ？".split())
def test_every_sentence_end_mark(mark):
    assert texts(evenly(["one", "two" + mark, "three"])) == [f"one two{mark}", "three"]


@pytest.mark.req("2.2")
def test_closing_quotes_and_brackets_still_end_a_sentence():
    words = evenly(["He", "said", '"stop."', "Then", "left.)", "Done"])
    assert texts(words) == ['He said "stop."', "Then left.)", "Done"]


@pytest.mark.req("2.2")
def test_titles_and_abbreviations_do_not_end_a_sentence():
    words = evenly(["I", "met", "Dr.", "Rao", "and", "Mr.", "Shah,", "e.g.", "today."])
    assert texts(words) == ["I met Dr. Rao and Mr. Shah, e.g. today."]


@pytest.mark.req("2.2")
def test_a_pause_longer_than_0_8_seconds_ends_a_sentence():
    words = [W("so", 0.0, 0.3), W("then", 0.4, 0.7), W("we", 1.51, 1.7), W("won", 1.8, 2.0)]
    assert texts(words) == ["so then", "we won"]
    words[2] = W("we", 1.49, 1.7)
    assert texts(words) == ["so then we won"]


@pytest.mark.req("2.2")
def test_long_sentences_split_at_the_longest_pause():
    words, t = [], 0.0
    for i in range(30):                       # ~30 s, no punctuation
        words.append(W(f"w{i}", round(t, 3), round(t + 0.95, 3)))
        t += 1.0 + (0.6 if i == 19 else 0.0)  # the longest pause follows word 19
    parts = sentences(words)
    assert [(s["w0"], s["w1"]) for s in parts] == [(0, 20), (20, 30)]
    assert all(s["e"] - s["s"] <= 25 for s in parts)


@pytest.mark.req("2.2")
def test_very_long_runs_are_split_evenly_until_short():
    parts = sentences(evenly([f"w{i}" for i in range(240)]))   # 120 s, equal pauses
    assert [(s["w0"], s["w1"]) for s in parts] == [(k, k + 30) for k in range(0, 240, 30)]
    assert all(s["e"] - s["s"] <= 25 for s in parts)


@pytest.mark.req("2.2")
def test_sentence_records_point_at_their_words():
    words = evenly(["Hello", "world.", "Bye."])
    first, second = sentences(words)
    assert (first["i"], first["w0"], first["w1"]) == (0, 0, 2)
    assert (first["s"], first["e"]) == (words[0]["s"], words[1]["e"])
    assert " ".join(w["w"] for w in words[first["w0"]:first["w1"]]) == first["text"]
    assert (second["i"], second["w0"], second["w1"]) == (1, 2, 3)


@pytest.mark.req("2.6")
def test_languages_without_spaces_join_without_spaces():
    assert texts(evenly(["今日", "は", "晴れ", "です。", "明日", "も。"]), "ja") == \
        ["今日は晴れです。", "明日も。"]
    assert texts(evenly(["नमस्ते", "दोस्तों।"]), "hi") == ["नमस्ते दोस्तों।"]


def test_no_words_give_no_sentences():
    assert sentences([]) == []


# ── Whisper ──────────────────────────────────────────────────────────────

@pytest.mark.req("2.6", "3.4")
def test_whisper_runs_with_word_timestamps_and_detects_the_language(whisper, video):
    from media import transcript as tr

    result = tr.transcribe_words(video, language=None, model_name="small")
    call = whisper["models"]["small"].calls[0]
    assert {k: call[k] for k in ("language", "word_timestamps", "vad_filter", "beam_size",
                                 "condition_on_previous_text")} == {
        "language": None, "word_timestamps": True, "vad_filter": True, "beam_size": 1,
        "condition_on_previous_text": False}
    assert (result["language"], result["requested_language"],
            result["language_probability"]) == ("hi", None, 0.97)
    assert (result["source"], result["estimated"], result["model"]) == ("whisper", False, "small")
    assert [w["w"] for w in result["words"]] == ["नमस्ते", "दोस्तों।", "आज", "हम", "बात", "करेंगे"]
    assert result["words"][1] == {"w": "दोस्तों।", "s": 0.7, "e": 1.9, "p": 0.884}
    assert [s["text"] for s in result["sentences"]] == ["नमस्ते दोस्तों।", "आज हम बात करेंगे"]
    assert [s["text"] for s in result["segments"]] == ["नमस्ते दोस्तों।", "आज हम बात करेंगे"]
    assert result["media"] == {"name": "source.mp4", "bytes": video.stat().st_size}
    assert not video.with_suffix(".audio.wav").exists()


@pytest.mark.req("2.7")
def test_a_requested_language_is_passed_through(whisper, video):
    from media import transcript as tr

    result = tr.transcribe_words(video, language="ur", model_name="small")
    assert whisper["models"]["small"].calls[0]["language"] == "ur"
    assert (result["language"], result["requested_language"]) == ("ur", "ur")


@pytest.mark.req("2.6")
def test_autopilot_model_is_small_unless_configured(whisper, video, monkeypatch):
    from media import transcript as tr

    monkeypatch.delenv("CLIPMAKER_WHISPER_MODEL", raising=False)
    assert tr.autopilot_model_name() == "small"
    monkeypatch.setenv("CLIPMAKER_WHISPER_MODEL", "medium")
    tr.transcribe_words(video)
    assert list(whisper["models"]) == ["medium"]


@pytest.mark.req("3.4")
def test_lines_without_word_timings_are_marked_estimated(whisper, video):
    from media import transcript as tr

    whisper["segments"] = [fake_segment(0.0, 2.0, " hello there friend")]
    result = tr.transcribe_words(video, model_name="small")
    assert result["estimated"] is True
    assert [w["w"] for w in result["words"]] == ["hello", "there", "friend"]
    assert result["words"][0]["p"] is None and result["words"][-1]["e"] == 2.0


def test_progress_reports_rising_fractions_ending_at_one(whisper, video):
    from media import transcript as tr

    seen = []
    tr.transcribe_words(video, model_name="small", progress=seen.append)
    assert seen == sorted(set(seen)) and seen[-1] == 1.0 and 0 < seen[0] < 1


def test_a_video_without_audio_gives_a_clear_error(whisper, tmp_path):
    from media import transcript as tr

    silent = make_video(tmp_path / "silent.mp4", seconds=1, audio=False)
    with pytest.raises(tr.TranscriptError, match="audio track"):
        tr.transcribe_words(silent, model_name="small")
    assert not silent.with_suffix(".audio.wav").exists()


# ── cache ────────────────────────────────────────────────────────────────

@pytest.mark.req("1.5")
def test_the_saved_transcript_is_reused(whisper, video):
    from media import transcript as tr

    job = video.parent
    first = tr.load_or_transcribe(job, video, language=None, model_name="small")
    second = tr.load_or_transcribe(job, video, language=None, model_name="small")
    assert len(whisper["models"]["small"].calls) == 1
    assert second == first == tr.load(job)


@pytest.mark.req("1.5", "2.7")
def test_the_cache_is_not_reused_for_another_model_language_or_file(whisper, video):
    from media import transcript as tr

    job = video.parent
    tr.load_or_transcribe(job, video, language=None, model_name="small")
    tr.load_or_transcribe(job, video, language="hi", model_name="small")
    assert len(whisper["models"]["small"].calls) == 2
    tr.load_or_transcribe(job, video, language="hi", model_name="medium")
    assert len(whisper["models"]["medium"].calls) == 1
    make_video(video, seconds=2)                       # another file, same name
    tr.load_or_transcribe(job, video, language="hi", model_name="medium")
    assert len(whisper["models"]["medium"].calls) == 2


def test_a_corrupt_or_foreign_file_is_ignored(whisper, video):
    from media import transcript as tr

    job = video.parent
    (job / "transcript.json").write_text("{not json")
    assert tr.load(job) is None
    tr.load_or_transcribe(job, video, model_name="small")
    assert len(whisper["models"]["small"].calls) == 1
    (job / "transcript.json").write_text(json.dumps({"v": 99, "words": [], "sentences": []}))
    assert tr.load(job) is None


def test_save_is_atomic_private_and_utf8(tmp_path):
    from media import transcript as tr

    job = tmp_path / "job"
    job.mkdir()
    transcript = tr.build([W("नमस्ते", 0.0, 0.5)], language="hi", source="whisper",
                          estimated=False)
    path = tr.save(job, transcript)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "नमस्ते" in path.read_text(encoding="utf-8")
    assert [p.name for p in job.iterdir()] == ["transcript.json"]
    assert tr.load(job) == transcript


# ── captions and the manual flow ─────────────────────────────────────────

@pytest.mark.req("1.8", "3.4")
def test_caption_lines_become_estimated_words_with_todays_timing():
    import captions
    from highlights import Segment
    from media import transcript as tr

    lines = [Segment(0.16, 2.879, "hey everyone welcome back"), Segment(2.879, 5.43, "  "),
             Segment(5.44, 8.15, "the secret trick")]
    result = tr.from_caption_segments(lines)
    assert (result["source"], result["estimated"], result["language"]) == ("captions", True, "en")
    expected = [b for line in lines for b in captions._word_bounds(line)]
    assert [w["w"] for w in result["words"]] == [w for (_, _, w) in expected]
    for word, (s, e, _) in zip(result["words"], expected):
        assert word["s"] == pytest.approx(s, abs=1e-3)
        assert word["e"] == pytest.approx(e, abs=1e-3)
        assert word["p"] is None
    assert [s["text"] for s in result["segments"]] == \
        ["hey everyone welcome back", "the secret trick"]


@pytest.mark.req("1.8", "N6.1")
def test_parse_subtitles_output_is_unchanged(tmp_path):
    import pipeline

    shutil.copy(FIXTURES / "youtube_auto.en.vtt", tmp_path / "source.en.vtt")
    got = [[s.start, s.end, s.text] for s in pipeline.parse_subtitles(tmp_path)]
    expected = json.loads((FIXTURES / "youtube_auto.parse_subtitles.json")
                          .read_text(encoding="utf-8"))
    assert got == expected


@pytest.mark.req("1.8", "N6.1")
def test_manual_whisper_path_keeps_its_model_language_and_return_type(whisper, video):
    import transcribe
    from highlights import Segment

    segments = transcribe.transcribe(video)
    call = whisper["models"][transcribe.MODEL_SIZE].calls[0]
    assert transcribe.MODEL_SIZE == os.environ.get("WHISPER_MODEL", "base.en")
    assert (call["language"], call["word_timestamps"]) == ("en", True)
    assert all(type(s) is Segment for s in segments)
    assert [(s.start, s.end, s.text) for s in segments] == [
        (0.0, 1.9, "नमस्ते दोस्तों।"), (2.3, 4.0, "आज हम बात करेंगे")]
    assert not (video.parent / "transcript.json").exists()


@pytest.mark.req("1.5", "1.8")
def test_manual_whisper_path_caches_in_the_job_folder(whisper, video):
    import transcribe

    first, saved = transcribe.transcribe_detailed(video, job_dir=video.parent)
    again, _ = transcribe.transcribe_detailed(video, job_dir=video.parent)
    assert len(whisper["models"][transcribe.MODEL_SIZE].calls) == 1
    assert first == again
    assert (saved["model"], saved["requested_language"]) == (transcribe.MODEL_SIZE, "en")


@pytest.mark.req("2.2")
def test_sentences_convert_to_segments():
    from highlights import Segment
    from media import transcript as tr

    transcript = tr.build(evenly(["Hi", "there.", "Bye."]), language="en",
                          source="whisper", estimated=False)
    segments = tr.to_segments(transcript)
    assert [type(s) for s in segments] == [Segment, Segment]
    assert [s.text for s in segments] == ["Hi there.", "Bye."]
    assert tr.source_segments(transcript) == segments    # no stored lines: sentences


def _talk(seconds):
    segs, t = [], 0.0
    while t + 3.0 <= seconds:
        words = [(" " + tok, t + k * 0.5, t + k * 0.5 + 0.4, 0.9)
                 for k, tok in enumerate(["this", "is", "a", "really", "amazing", "tip."])]
        segs.append(fake_segment(t, t + 3.0, " this is a really amazing tip.", words))
        t += 3.0
    return segs


def _run_manual_pipeline(job_id, source):
    import pipeline

    return pipeline.process("talk.mp4", job_id, lambda message, pct=None: None,
                            max_clips=1, target_len=6, burn_captions=False,
                            burn_hook=False, make_thumbnails=False, trim_silence=False,
                            source_path=str(source))


@pytest.mark.req("1.8", "N6.1")
def test_manual_pipeline_saves_a_whisper_transcript(whisper, _isolated_data):
    import paths
    import transcribe

    job_dir = paths.output_dir() / "aaaaaaaaaaaa"
    job_dir.mkdir(parents=True)
    source = make_video(job_dir / "source.mp4", seconds=12, size=(640, 360))
    whisper["segments"], whisper["duration"] = _talk(12), 12.0

    result = _run_manual_pipeline("aaaaaaaaaaaa", source)
    assert result["transcript_source"] == "whisper" and len(result["clips"]) == 1
    assert (job_dir / result["clips"][0]["vertical"]).is_file()
    saved = json.loads((job_dir / "transcript.json").read_text(encoding="utf-8"))
    assert (saved["source"], saved["model"], saved["requested_language"],
            saved["estimated"]) == ("whisper", transcribe.MODEL_SIZE, "en", False)
    assert len(saved["words"]) == 24 and len(saved["sentences"]) == 4
    assert not (job_dir / "source.audio.wav").exists()


@pytest.mark.req("1.8", "N6.1")
def test_manual_pipeline_saves_an_estimated_caption_transcript(whisper, _isolated_data):
    import paths

    job_dir = paths.output_dir() / "bbbbbbbbbbbb"
    job_dir.mkdir(parents=True)
    source = make_video(job_dir / "source.mp4", seconds=15, size=(640, 360))
    shutil.copy(FIXTURES / "youtube_auto.en.vtt", job_dir / "source.en.vtt")

    result = _run_manual_pipeline("bbbbbbbbbbbb", source)
    assert result["transcript_source"] == "captions" and len(result["clips"]) == 1
    saved = json.loads((job_dir / "transcript.json").read_text(encoding="utf-8"))
    assert (saved["source"], saved["estimated"]) == ("captions", True)
    expected = json.loads((FIXTURES / "youtube_auto.parse_subtitles.json")
                          .read_text(encoding="utf-8"))
    assert [s["text"] for s in saved["segments"]] == [row[2] for row in expected]
    assert whisper["models"] == {}                      # Whisper never ran
