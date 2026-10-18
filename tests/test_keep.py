"""Keep spans and time mapping (task 4.5)."""
from __future__ import annotations

import pytest

from media import keep


def W(w, s, e):
    return {"w": w, "s": s, "e": e, "p": 0.9}


SPANS = [(1.0, 3.0), (5.0, 6.5), (10.0, 12.0)]      # 2 + 1.5 + 2 = 5.5 s of clip


# ── cuts ─────────────────────────────────────────────────────────────────

@pytest.mark.req("7.3")
def test_cuts_are_merged_clamped_and_removed_from_the_window():
    assert keep.merge([(3, 4), (2, 3), (8, 12), (5, 5), (7, 6)]) == [(2, 4), (8, 12)]
    assert keep.base_spans(0, 10, [(2, 3), (3, 4), (8, 12)]) == [(0, 2), (4, 8)]
    assert keep.keep_spans([], 10, 20, [(12, 13), (12.5, 14), (19, 25)],
                           auto_trim=False) == [(10, 12), (14, 19)]


@pytest.mark.req("7.3")
def test_a_word_belongs_to_the_span_its_middle_falls_in():
    words = [W("long", 1.0, 2.0)]
    assert keep.keep_spans(words, 0, 5, [(1.8, 3.0)]) == [(0.9, 1.8)]


# ── auto-trim ────────────────────────────────────────────────────────────

@pytest.mark.req("3.6")
def test_pauses_longer_than_half_a_second_are_cut():
    words = [W("hello", 1.0, 1.4), W("there", 1.5, 1.9),     # 0.1 s apart
             W("friend", 2.5, 2.9), W("again", 3.3, 3.7)]    # 0.6 s pause, then 0.4 s
    # Leading and trailing silence go; the 0.6 s pause is cut (less the padding).
    assert keep.keep_spans(words, 0, 5) == [(0.9, 2.0), (2.4, 3.8)]


@pytest.mark.req("3.6", "7.3")
def test_padding_never_reaches_into_a_neighbouring_word():
    words = [W("keep", 1.0, 1.5), W("cut", 1.55, 1.9), W("also", 1.95, 2.4)]
    spans = keep.keep_spans(words, 0, 5, [(1.55, 1.9)])        # the user cut "cut"
    assert spans == [(0.9, 1.525), (1.925, 2.5)]               # half-way, not 0.1 s
    assert all(b <= 1.55 or a >= 1.9 for a, b in spans)        # none of "cut" comes back


@pytest.mark.req("3.6")
def test_padding_stops_at_the_window_edges():
    assert keep.keep_spans([W("hi", 0.05, 0.5)], 0.0, 0.55) == [(0.0, 0.55)]


@pytest.mark.req("3.6")
def test_filler_only_phrases_are_cut_but_filler_inside_speech_is_kept():
    words = [W("so", 1.0, 1.3), W("um", 1.35, 1.6), W("we", 1.65, 1.9),   # speech
             W("Um,", 2.7, 3.0),                                          # filler only
             W("you", 3.8, 4.0), W("know?", 4.05, 4.3),                   # filler only
             W("great", 5.1, 5.5)]
    assert keep.keep_spans(words, 0, 6) == [(0.9, 2.0), (5.0, 5.6)]


@pytest.mark.req("3.6", "2.6")
def test_speech_in_other_languages_is_never_treated_as_filler():
    assert not keep.is_filler("नमस्ते") and not keep.is_filler("हाँ")
    assert keep.is_filler("Um...") and keep.is_filler("Uh-huh") and keep.is_filler("I mean,")
    assert not keep.is_filler("...") and not keep.is_filler("umbrella")
    assert keep.keep_spans([W("हाँ", 1.0, 1.4)], 0, 3) == [(0.9, 1.5)]


@pytest.mark.req("3.6")
def test_no_usable_speech_means_nothing_is_trimmed():
    assert keep.keep_spans([], 0, 10, [(2, 3)]) == [(0, 2), (3, 10)]
    assert keep.keep_spans([W("um", 1.0, 1.3)], 0, 5) == [(0, 5)]
    assert keep.keep_spans([W("far", 50, 51)], 0, 5) == [(0, 5)]


@pytest.mark.req("3.6")
def test_trimming_off_keeps_the_whole_window():
    words = [W("hello", 1.0, 1.4), W("again", 3.3, 3.7)]
    assert keep.keep_spans(words, 0, 5, auto_trim=False) == [(0, 5)]


# ── time mapping ─────────────────────────────────────────────────────────

@pytest.mark.req("3.6", "7.3")
@pytest.mark.parametrize("t,ct", [(0.0, 0.0), (1.0, 0.0), (2.0, 1.0), (3.0, 2.0),
                                  (4.0, 2.0), (5.5, 2.5), (6.5, 3.5), (8.0, 3.5),
                                  (11.0, 4.5), (12.0, 5.5), (20.0, 5.5)])
def test_source_time_to_clip_time(t, ct):
    assert keep.to_clip_time(SPANS, t) == pytest.approx(ct)


@pytest.mark.req("3.6", "7.3")
@pytest.mark.parametrize("ct,t", [(0.0, 1.0), (1.0, 2.0), (2.0, 5.0), (2.5, 5.5),
                                  (3.5, 10.0), (4.5, 11.0), (5.5, 12.0), (9.0, 12.0),
                                  (-1.0, 1.0)])
def test_clip_time_to_source_time(ct, t):
    assert keep.to_source_time(SPANS, ct) == pytest.approx(t)


@pytest.mark.req("3.6", "7.3")
def test_mapping_round_trips():
    for a, b in SPANS:
        for k in range(10):
            t = a + (b - a) * k / 10
            assert keep.to_source_time(SPANS, keep.to_clip_time(SPANS, t)) == pytest.approx(t)
    total = keep.kept_duration(SPANS)
    assert total == pytest.approx(5.5)
    for k in range(56):
        ct = total * k / 55
        assert keep.to_clip_time(SPANS, keep.to_source_time(SPANS, ct)) == pytest.approx(ct)
    assert keep.to_source_time([], 1.0) == 0.0 and keep.to_clip_time([], 1.0) == 0.0
