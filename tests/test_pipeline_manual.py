"""The manual flow on the shared renderer (task 5.6).

These tests were first run against the manual flow as it was before task 5.6
(its own ffmpeg cut, caption overlays and thumbnail), and passed there, so the
key sets below are the result shape it already had, not a guess."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from media.probe import probe
from tests.helpers.media import make_video

FIXTURES = Path(__file__).parent / "fixtures"
JOB = "dddddddddddd"

RESULT_KEYS = {"job_id", "source_title", "method", "transcript_source", "clips"}
CLIP_KEYS = {"index", "title", "hook", "description", "tags", "title_suggestions",
             "hashtags", "thumbnail", "virality", "virality_reason", "start", "end",
             "duration", "trimmed", "original", "vertical"}

METADATA = {
    "title": "Why this changed everything",
    "title_suggestions": ["Why this changed everything", "The one change that worked"],
    "hook": "Wait for the end",
    "description": "One change made all the difference.\n\n#tips #shorts",
    "tags": ["tips", "change", "shorts"],
    "hashtags": ["#tips", "#shorts"],
}


@pytest.fixture
def manual_job(_isolated_data, fake_llm, monkeypatch):
    """A 15 s generated source with the recorded YouTube captions beside it.
    The AI writes the metadata; clip selection falls back to keyword rules."""
    import highlights
    import paths

    monkeypatch.setattr(highlights, "_ollama_model", lambda: None)

    def respond(prompt, system):
        if "CLIP TRANSCRIPT" in prompt:
            return json.dumps(METADATA)
        raise RuntimeError("no AI clip selection in this test")

    fake_llm.responder = respond
    job_dir = paths.output_dir() / JOB
    job_dir.mkdir(parents=True)
    source = make_video(job_dir / "source.mp4", seconds=15, size=(640, 360))
    shutil.copy(FIXTURES / "youtube_auto.en.vtt", job_dir / "source.en.vtt")
    return job_dir, source


def run(source, **options):
    import pipeline

    messages = []
    args = {"max_clips": 1, "target_len": 6, "source_path": str(source), **options}
    result = pipeline.process("talk.mp4", JOB, lambda m, pct=None: messages.append((m, pct)),
                              **args)
    return result, messages


def gray(path: Path, t: float, size=(540, 960)) -> np.ndarray:
    w, h = size
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(path),
                          "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"],
                         check=True, capture_output=True, timeout=60).stdout
    return np.frombuffer(raw[:w * h], np.uint8).reshape(h, w)


@pytest.mark.req("1.8", "N6.1")
def test_the_result_keeps_its_recorded_shape(manual_job):
    job_dir, source = manual_job
    result, messages = run(source)
    assert set(result) == RESULT_KEYS
    assert (result["job_id"], result["transcript_source"]) == (JOB, "captions")
    assert len(result["clips"]) == 1
    clip = result["clips"][0]
    assert set(clip) == CLIP_KEYS
    assert (clip["index"], clip["title"], clip["hook"]) == (1, METADATA["title"],
                                                            METADATA["hook"])
    assert clip["tags"] == METADATA["tags"] and clip["description"].startswith("One change")
    for key in ("original", "vertical", "thumbnail"):
        assert (job_dir / clip[key]).is_file(), key
    base = clip["original"][:-len(".mp4")]
    assert (clip["vertical"], clip["thumbnail"]) == (f"{base}-vertical.mp4", f"{base}-thumb.jpg")
    info = probe(job_dir / clip["vertical"])
    assert (info["width"], info["height"]) == (1080, 1920)
    assert abs(probe(job_dir / clip["original"])["duration"] - clip["duration"]) < 0.1
    assert clip["trimmed"] == pytest.approx(clip["end"] - clip["start"] - clip["duration"],
                                            abs=0.02)
    assert messages[-1] == ("Done", 100)
    pcts = [p for _, p in messages if p is not None]
    assert pcts == sorted(pcts)


@pytest.mark.req("1.8", "N6.1")
def test_switched_off_options_leave_their_keys_empty(manual_job):
    job_dir, source = manual_job
    result, _ = run(source, burn_captions=False, burn_hook=False, make_thumbnails=False,
                    trim_silence=False, write_metadata=False)
    clip = result["clips"][0]
    assert set(clip) == CLIP_KEYS
    assert (clip["hook"], clip["thumbnail"], clip["trimmed"]) == ("", None, 0.0)
    assert clip["duration"] == pytest.approx(clip["end"] - clip["start"], abs=0.02)
    assert not list(job_dir.glob("*-thumb.jpg"))


@pytest.mark.req("1.8")
def test_the_vertical_keeps_todays_fit_layout(manual_job):
    job_dir, source = manual_job
    result, _ = run(source, burn_captions=False, burn_hook=False, make_thumbnails=False)
    frame = gray(job_dir / result["clips"][0]["vertical"], 2.0)
    from media.cover import focus_variance

    # A 640x360 source fills the width: 1080x608 at y 656-1264 of 1920, which is
    # rows 328-632 here. Above and below it is the blurred copy.
    picture, above = frame[340:620], frame[20:300]
    assert focus_variance(picture) > 10 * focus_variance(above)
