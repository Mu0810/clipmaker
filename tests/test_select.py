"""Clip selection (task 4.4)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

from highlights import Segment, select_highlights
from media import select as sel
from media.transcript import build, spread_words

FIXTURES = Path(__file__).parent / "fixtures"


def talk(durations, gap=0.2, words_per=5):
    """A transcript of back-to-back sentences lasting the given seconds.
    With durations [5] * n, sentence k runs from 5.2k to 5.2k + 5, and a run
    of m sentences lasts 5.2m - 0.2 seconds."""
    words, t = [], 0.0
    for k, d in enumerate(durations):
        tokens = [f"s{k}w{m}" for m in range(words_per - 1)] + [f"end{k}."]
        words.extend(spread_words(" ".join(tokens), t, t + d))
        t += d + gap
    return build(words, language="en", source="whisper", estimated=False)


def bare(count, dur=4.0, gap=0.2, text="x" * 150):
    """Sentence records only (no words), for chunking and long transcripts."""
    sents, t = [], 0.0
    for k in range(count):
        sents.append({"i": k, "s": round(t, 3), "e": round(t + dur, 3),
                      "w0": k, "w1": k + 1, "text": f"{text} {k}."})
        t += dur + gap
    return sents


def reply(*clips, wrap=False):
    """An AI reply naming clips as (start, end, title, score, reason)."""
    body = json.dumps({"clips": [dict(zip(("start", "end", "title", "score", "reason"), c))
                                 for c in clips]})
    return f"Here you go:\n```json\n{body}\n```" if wrap else body


def C(start, end, score):
    return {"start": start, "end": end, "score": score}


# ── building blocks ──────────────────────────────────────────────────────

@pytest.mark.req("2.2")
def test_fit_keeps_trims_extends_or_rejects():
    s = talk([5] * 6)["sentences"]
    assert sel.fit(s, 0, 2, 10, 20) == (0, 2)        # 15.4 s: fits as it is
    assert sel.fit(s, 0, 5, 10, 20) == (0, 2)        # 31 s: trailing sentences dropped
    assert sel.fit(s, 1, 1, 12, 20) == (1, 3)        # 5 s: following sentences added
    assert sel.fit(s, 4, 5, 12, 20) is None          # too short at the end of the video
    assert sel.fit(s, 0, 0, 7, 9) is None            # 5 s, and the next run is 10.2 s
    assert sel.fit(s, 0, 0, 1, 4) is None            # one sentence is longer than allowed


@pytest.mark.req("2.1")
@pytest.mark.parametrize("value,expected", [
    (3, 3), (3.0, 3), ("3", 3), (" [3] ", 3),
    (True, None), (1.5, None), ("x", None), (None, None), (-1, -1)])
def test_sentence_numbers_from_the_ai_are_parsed_strictly(value, expected):
    assert sel._as_index(value) == expected


@pytest.mark.req("2.3")
@pytest.mark.parametrize("value,expected", [
    (150, 100), (-5, 0), ("85", 85), (72.6, 73),
    (float("nan"), None), (True, None), (None, None), ("high", None)])
def test_scores_are_clamped_to_0_100(value, expected):
    assert sel._as_score(value) == expected


@pytest.mark.req("2.4")
def test_overlapping_clips_keep_only_the_higher_score():
    a, b, c, d = C(0, 10, 90), C(5, 15, 60), C(2, 9, 80), C(20, 30, 70)
    # c overlaps a by 7 s, more than half of c (3.5 s): dropped.
    # b overlaps a by exactly half of the shorter clip (5 s): kept.
    assert sel.resolve([b, c, d, a], 3) == [a, d, b]
    assert sel.resolve([b, c, d, a], 2) == [a, d]


@pytest.mark.req("2.1", "N8.1")
def test_a_short_transcript_is_one_chunk():
    assert sel.plan_chunks(bare(50), overlap_s=60) == [(0, 50)]


@pytest.mark.req("2.1", "N8.1")
def test_a_long_transcript_is_split_into_three_overlapping_chunks():
    sents = bare(3000)                     # ~178 characters a line: ~178,000 tokens
    chunks = sel.plan_chunks(sents, overlap_s=60)
    assert len(chunks) == 3
    assert chunks[0][0] == 0 and chunks[-1][1] == len(sents)
    for (a1, e1), (a2, _) in zip(chunks, chunks[1:]):
        assert a1 < a2 < e1                # each chunk runs on into the next one
        # Every sentence starting within 60 s of this chunk's own last sentence
        # is included, so any clip of up to 60 s that crosses the boundary is
        # wholly inside this chunk.
        assert e1 == len(sents) or sents[e1]["s"] >= sents[a2 - 1]["e"] + 60


# ── the manual flow ──────────────────────────────────────────────────────

def lines(*spans):
    return [Segment(s, e, f"line {k} has a few words") for k, (s, e) in enumerate(spans)]


@pytest.mark.req("2.2", "N6.1")
@pytest.mark.parametrize("target,expected", [(45, (27, 63)), (5, (10, 14)),
                                             (300, (108, 180))])
def test_the_manual_target_length_becomes_a_range(monkeypatch, target, expected):
    from media import select as select_mod

    seen = {}

    def record(transcript, n, lo, hi, language=None):
        seen.update(n=n, lo=lo, hi=hi)
        return [], "heuristic"

    monkeypatch.setattr(select_mod, "select_clips", record)
    select_highlights(lines(*[(10 * k, 10 * k + 9) for k in range(100)]), 6, target)
    assert seen["n"] == 6
    assert (seen["lo"], seen["hi"]) == pytest.approx(expected)


@pytest.mark.req("2.2", "N6.1")
def test_manual_clips_stay_on_the_sources_own_line_boundaries():
    src = lines(*[(6 * k, 6 * k + 5.5) for k in range(40)])
    clips, method = select_highlights(src, 3, 20)          # range 12-28 s
    assert method == "heuristic" and clips
    starts, ends = {s.start for s in src}, {s.end for s in src}
    assert all(c["start"] in starts and c["end"] in ends for c in clips)
    assert [c["start"] for c in clips] == sorted(c["start"] for c in clips)
    assert all(12 <= c["end"] - c["start"] <= 28 for c in clips)


@pytest.mark.req("N6.1")
def test_a_source_shorter_than_the_range_still_gives_one_clip():
    clips, _ = select_highlights(lines((0.0, 4.0), (4.2, 8.0), (8.1, 12.0)), 6, 45)
    assert [(c["start"], c["end"]) for c in clips] == [(0.0, 12.0)]


@pytest.mark.req("N6.1")
def test_a_line_longer_than_the_range_can_still_be_a_clip():
    clips, _ = select_highlights(lines((0.0, 70.0)), 6, 45)
    assert [(c["start"], c["end"]) for c in clips] == [(0.0, 70.0)]


def test_no_lines_give_no_clips():
    assert select_highlights([], 6, 45) == ([], "none")
    assert select_highlights([Segment(0, 1, "   ")], 6, 45) == ([], "none")


@pytest.mark.req("2.3", "N6.1")
def test_the_manual_pipeline_ranks_by_the_selection_scores(_isolated_data, monkeypatch):
    import paths
    import pipeline
    from tests.helpers.media import make_video

    def no_second_scoring(*args, **kwargs):
        raise AssertionError("clips were already scored when they were selected")

    monkeypatch.setattr(pipeline, "score_viral_batch", no_second_scoring)
    job_dir = paths.output_dir() / "cccccccccccc"
    job_dir.mkdir(parents=True)
    source = make_video(job_dir / "source.mp4", seconds=15, size=(640, 360))
    shutil.copy(FIXTURES / "youtube_auto.en.vtt", job_dir / "source.en.vtt")
    result = pipeline.process("talk.mp4", "cccccccccccc", lambda m, pct=None: None,
                              max_clips=1, target_len=6, burn_captions=False,
                              burn_hook=False, make_thumbnails=False,
                              trim_silence=False, source_path=str(source))
    clip = result["clips"][0]
    assert result["method"] == "heuristic"
    assert isinstance(clip["virality"], int) and clip["virality_reason"]


# ── the AI engine ────────────────────────────────────────────────────────

@pytest.mark.req("2.1", "2.3")
def test_the_ai_engine_chooses_and_scores_the_clips(fake_llm):
    t = talk([5] * 12)
    fake_llm.queue(reply((0, 2, "Opening", 91, "Strong hook"),
                         (6, 8, "The twist", 77, "Surprising payoff")))
    clips, method = sel.select_clips(t, 2, 10, 20)
    assert method == "ai:fake-model"
    assert [(c["s0"], c["s1"], c["title"], c["score"], c["reason"], c["method"])
            for c in clips] == [
        (0, 3, "Opening", 91, "Strong hook", "ai:fake-model"),
        (6, 9, "The twist", 77, "Surprising payoff", "ai:fake-model")]
    s = t["sentences"]
    assert (clips[0]["start"], clips[0]["end"]) == (s[0]["s"], s[2]["e"])
    prompt = fake_llm.calls[0]["prompt"]
    assert "[0] 00:00–00:05 s0w0" in prompt
    assert "10 to 20 seconds" in prompt and "Pick up to 4 moments" in prompt
    assert "Language: en" in prompt


@pytest.mark.req("2.2", "2.4")
def test_ai_suggestions_are_validated_fitted_and_deduplicated(fake_llm):
    fake_llm.queue(reply(
        (0, 1, "short", 60, "r"),          # 10.2 s: fits (minimum 10)
        (3, 3, "too short", 70, "r"),      # 5 s: extended to sentences 3-4
        (5, 11, "too long", 80, "r"),      # 36.2 s: trimmed to sentences 5-7
        (5, 6, "duplicate", 65, "r"),      # mostly inside "too long": dropped
        (9, 4, "reversed", 99, "r"),       # ends before it starts: dropped
        (2, 40, "out of range", 99, "r"),  # no sentence 40: dropped
        ("abc", 3, "bad number", 99, "r"),
    ))
    clips, _ = sel.select_clips(talk([5] * 12), 5, 10, 20)
    assert [(c["title"], c["s0"], c["s1"]) for c in clips] == [
        ("too long", 5, 8), ("too short", 3, 5), ("short", 0, 2)]


@pytest.mark.req("2.1")
def test_replies_wrapped_in_prose_or_code_fences_are_read(fake_llm):
    fake_llm.queue(reply((0, 2, "Wrapped", 80, "r"), wrap=True))
    clips, method = sel.select_clips(talk([5] * 6), 1, 10, 20)
    assert method == "ai:fake-model" and clips[0]["title"] == "Wrapped"


@pytest.mark.req("2.3")
def test_a_missing_ai_score_gets_the_heuristic_score(fake_llm):
    fake_llm.queue(json.dumps({"clips": [{"start": 0, "end": 2, "title": "No score"}]}))
    clips, _ = sel.select_clips(talk([5] * 6), 1, 10, 20)
    assert 0 <= clips[0]["score"] <= 100 and clips[0]["reason"]


@pytest.mark.req("2.1", "2.3")
def test_at_most_n_clips_best_first(fake_llm):
    fake_llm.queue(reply(*[(2 * k, 2 * k + 1, f"c{k}", 50 + k, "r") for k in range(6)]))
    clips, _ = sel.select_clips(talk([5] * 12), 3, 10, 20)
    assert [c["title"] for c in clips] == ["c5", "c4", "c3"]


@pytest.mark.req("2.2")
def test_clip_edges_are_the_first_and_last_words_of_whole_sentences(fake_llm):
    t = talk([5] * 12)
    fake_llm.queue(reply((1, 3, "a", 90, "r"), (6, 6, "b", 80, "r")))
    clips, _ = sel.select_clips(t, 2, 10, 20)
    words, s = t["words"], t["sentences"]
    assert [(c["s0"], c["s1"]) for c in clips] == [(1, 4), (6, 8)]
    for c in clips:
        assert c["start"] == words[s[c["s0"]]["w0"]]["s"]
        assert c["end"] == words[s[c["s1"] - 1]["w1"] - 1]["e"]


# ── fallbacks and call counting ──────────────────────────────────────────

def ollama(net, response, model="llama3:8b"):
    """A fake local Ollama server answering the model list and generate calls."""
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": model}]})
        assert request.url.path == "/api/generate"
        return httpx.Response(200, json={"response": response})
    net.register("localhost:11434", handler)


@pytest.mark.req("2.5", "N8.1")
@pytest.mark.parametrize("bad", ["Sorry, I can't help with that.", '{"clips": "none"}',
                                 RuntimeError("Kimchi error 500")])
def test_an_unusable_ai_reply_falls_back_without_asking_again(fake_llm, bad):
    import llm

    fake_llm.queue(bad)
    with llm.count_calls() as counter:
        clips, method = sel.select_clips(talk([5] * 12), 2, 10, 20)
    assert method == "heuristic" and len(clips) == 2
    assert all(c["method"] == "heuristic" for c in clips)
    assert counter.count == 1 and len(fake_llm.calls) == 1


@pytest.mark.req("2.3", "2.5", "N8.1")
def test_the_local_ollama_model_is_the_first_fallback(net):
    import llm

    ollama(net, json.dumps([{"start_index": 3, "end_index": 5, "title": "From Ollama"}]))
    with llm.count_calls() as counter:
        clips, method = sel.select_clips(talk([5] * 12), 2, 10, 20)
    assert method == "ollama:llama3:8b"
    assert [(c["title"], c["s0"], c["s1"], c["method"]) for c in clips] == [
        ("From Ollama", 3, 6, "ollama:llama3:8b")]
    assert 0 <= clips[0]["score"] <= 100 and clips[0]["reason"]
    assert counter.count == 1            # the generate call; listing models is free


@pytest.mark.req("2.5")
def test_unusable_ollama_output_falls_back_to_the_heuristic(net):
    ollama(net, "no json here")
    clips, method = sel.select_clips(talk([5] * 12), 2, 10, 20)
    assert method == "heuristic" and clips


@pytest.mark.req("2.2", "2.3", "2.4", "2.5")
def test_the_heuristic_alone_gives_fitting_non_overlapping_clips():
    clips, method = sel.select_clips(talk([5] * 30), 4, 10, 20)
    assert method == "heuristic" and len(clips) == 4
    for c in clips:
        assert 10 <= c["end"] - c["start"] <= 20
        assert 0 <= c["score"] <= 100 and c["reason"] and c["method"] == "heuristic"
    for a in clips:
        for b in clips:
            if a is not b:
                assert sel._overlap(a, b) <= 0.5 * min(a["end"] - a["start"],
                                                       b["end"] - b["start"])
    scores = [c["score"] for c in clips]
    assert scores == sorted(scores, reverse=True)


def test_nothing_fitting_and_empty_input():
    assert sel.select_clips(talk([5] * 3), 2, 30, 40) == ([], "heuristic")
    assert sel.select_clips({"sentences": []}, 3, 10, 20) == ([], "none")
    assert sel.select_clips(talk([5] * 6), 0, 10, 20) == ([], "none")
    with pytest.raises(ValueError):
        sel.select_clips(talk([5] * 6), 2, 20, 10)


@pytest.mark.req("2.1", "N8.1")
def test_long_transcripts_use_one_call_per_chunk_at_most_three(fake_llm, monkeypatch):
    import llm

    monkeypatch.setattr(sel, "TOKEN_BUDGET", 300)
    transcript = {"language": "en", "sentences": bare(60, text="word " * 6)}
    seen = []

    def responder(prompt, system):
        numbers = [int(line[1:line.index("]")]) for line in prompt.splitlines()
                   if line.startswith("[") and "]" in line]
        seen.append((numbers[0], numbers[-1]))
        a = numbers[0]
        return reply((a, a + 2, f"chunk {len(seen)}", 90 - len(seen), "r"),
                     (a + 500, a + 501, "outside its chunk", 99, "r"))

    fake_llm.responder = responder
    with llm.count_calls() as counter:
        clips, method = sel.select_clips(transcript, 3, 10, 20)
    assert counter.count == 3 and len(seen) == 3
    assert seen[0][0] == 0 and seen[-1][1] == 59
    assert [c["title"] for c in clips] == ["chunk 1", "chunk 2", "chunk 3"]
    assert method == "ai:fake-model"


@pytest.mark.req("2.1", "2.5", "N8.1")
def test_the_label_names_the_model_that_actually_answered(net, monkeypatch):
    """The real chat(): the first model is out of credits, the next one answers."""
    import llm

    monkeypatch.setenv("KIMCHI_API_KEY", "test-key")
    monkeypatch.delenv("KIMCHI_MODEL", raising=False)
    used = []

    def kimchi(request):
        model = json.loads(request.content)["model"]
        used.append(model)
        if model == llm.DEFAULT_MODEL:
            return httpx.Response(402, json={"error": "out of credits"})
        content = reply((0, 2, "Real engine", 88, "r"))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    net.register("llm.kimchi.dev", kimchi)
    with llm.count_calls() as counter:
        clips, method = sel.select_clips(talk([5] * 6), 1, 10, 20)
    assert used == [llm.DEFAULT_MODEL, llm.MODELS[1]]
    assert method == f"ai:{llm.MODELS[1]}" and clips[0]["method"] == method
    assert counter.count == 1
    assert net.requests[0].headers["authorization"] == "Bearer test-key"
