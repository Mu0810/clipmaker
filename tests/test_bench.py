"""scripts/bench_autopilot.py (task 13.1; Requirements N1.1, N1.2, N1.4): a
--quick --fake-ai run on a generated 30 s source. Whisper is replaced by a
fixture transcript, as everywhere in the suite, so nothing is downloaded and
the script's real path runs: its own scratch data folder, the API on
127.0.0.1, the pipeline, the editor's render worker and the timed requests."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from media import transcript as transcript_mod
from tests.helpers.media import make_video

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bench_autopilot.py"
SENTENCES = ["Here is the first thing you should know.", "It saves an hour every single week.",
             "Most people never try it at all.", "So here is how you start today.",
             "Open the settings and turn it on.", "That is really all there is to it."]


def load_bench():
    spec = importlib.util.spec_from_file_location("bench_autopilot", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture_whisper(monkeypatch):
    """Six 4.6 s sentences over the 30 s, one every 5 s."""
    calls = []

    def transcribe(video, language=None, model_name=None, progress=None):
        calls.append(Path(video).name)
        words = []
        for k, text in enumerate(SENTENCES):
            words += transcript_mod.spread_words(text, 5.0 * k + 0.2, 5.0 * k + 4.8)
        if progress:
            progress(1.0)
        return transcript_mod.build(words, language="en", source="whisper", estimated=False,
                                    model=model_name or "small")

    monkeypatch.setattr(transcript_mod, "transcribe_words", transcribe)
    return calls


@pytest.mark.req("N1.1", "N1.2", "N1.4")
def test_a_quick_run_times_each_stage_a_new_version_and_the_api(tmp_path, fixture_whisper):
    bench = load_bench()
    source = make_video(tmp_path / "talk.mp4", seconds=30, size=(640, 360))
    data_before = os.environ["CLIPMAKER_DATA_DIR"]
    said: list[str] = []

    results = bench.run_benchmark(source, fake=True, quick=True, progress=said.append)

    assert results["error"] is None, results["error"]
    assert fixture_whisper == ["source.mp4"]                       # transcribed once, in the job folder
    stages = [(s["stage"], s["clip"]) for s in results["stages"]]
    assert stages == [("downloading", None), ("transcribing", None), ("choosing", None),
                      ("metadata", [1, 1]), ("rendering", [1, 1]), ("planning", None)]
    assert all(s["seconds"] >= 0 for s in results["stages"])
    assert "Rendering, clip 1 of 1…" in said
    assert results["clips"] == 1
    assert 6 <= results["clip_lengths"][1] <= 15                     # within the quick profile's range

    again = results["rerender"]
    assert again["status"] == "ready", again
    assert again["length_s"] == pytest.approx(10.0, abs=0.01)       # every second kept
    assert again["seconds"] > 0

    for name in ("/api/autopilot/posts", "/api/autopilot/counts"):
        timing = results["latency"][name]
        assert timing["n"] > 0 and timing["errors"] == 0, (name, timing)
        assert timing["phases"]["job"] is not None and timing["phases"]["render"] is not None

    budgets = {b["id"]: b for b in results["budgets"]}
    assert not budgets["N1.1"]["applies"]                            # 30 s, not 60 minutes
    assert not budgets["N1.4"]["applies"]                            # a 10 s clip, not 60 s
    assert budgets["N1.2"]["applies"] and budgets["N1.2"]["met"], budgets["N1.2"]   # p95 within 500 ms

    text = bench.report(results)
    assert "not comparable: the budget is for a 60-minute source; this one is 0:30.0" in text
    assert "not comparable: the clip is 10.0 s; the budget is for 60 s" in text
    assert "/api/autopilot/posts" in text and "Slowest p95" in text
    assert f"Rendering, clip 1 of 1 ({results['clip_lengths'][1]:.1f} s)" in text

    # The scratch folder is gone, and the app points at the test's own folder again.
    assert os.environ["CLIPMAKER_DATA_DIR"] == data_before
    assert results["scratch"] != data_before and not Path(results["scratch"]).exists()


@pytest.mark.req("N1.1")
def test_a_missing_source_is_refused_before_anything_runs(tmp_path, capsys):
    bench = load_bench()
    with pytest.raises(SystemExit) as stopped:
        bench.main(["--source", str(tmp_path / "missing.mp4")])
    assert stopped.value.code == 2
    assert "isn't a file" in capsys.readouterr().err


@pytest.mark.req("N1.1")
def test_the_fake_ai_picks_whole_sentences_within_the_asked_length():
    bench = load_bench()
    lines = "\n".join(f"[{i}] 00:{5 * i:02d}–00:{5 * i + 4:02d} Sentence {i}." for i in range(8))
    prompt = (f"Pick up to 2 moments that work as standalone short-form clips. Each clip is a run of "
              f"whole sentences lasting 6 to 15 seconds.\n\nTRANSCRIPT:\n{lines}\n")
    import json

    clips = json.loads(bench.fake_reply(prompt))["clips"]
    assert [(c["start"], c["end"]) for c in clips] == [(0, 1), (2, 3)]
    assert [c["score"] for c in clips] == [92, 86]
    assert json.loads(bench.fake_reply("something else")) == {}
