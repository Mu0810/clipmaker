"""Silence & filler trimming.

Given a clip window and the timestamped transcript, decide which spans to KEEP
so that dead air (long pauses, leading/trailing silence) and standalone filler
segments ("um", "uh", "you know") are cut — tightening pacing, which is the
single biggest lever on short-form retention.

This module is pure planning logic (no ffmpeg). pipeline.py renders the kept
spans and remaps caption timings onto the resulting compacted timeline.
"""
from __future__ import annotations

import re

from highlights import Segment

# Whole-segment filler: a transcript line that is *only* one of these is cut.
# Kept conservative — common real words like "so"/"like" are NOT treated as
# filler here, since whole-line matches of those are usually meaningful.
_FILLER = {
    "um", "umm", "uh", "uhh", "uhm", "uh huh", "er", "erm", "hmm", "mm",
    "mhm", "mmhmm", "you know", "i mean",
}


def is_filler(text: str) -> bool:
    """True for empty text or a line consisting solely of filler."""
    t = re.sub(r"[^a-z' ]", " ", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t == "" or t in _FILLER


def plan_keep_intervals(segments: list[Segment], clip_start: float,
                        clip_end: float, *, min_gap: float = 0.5,
                        pad: float = 0.10,
                        drop_filler: bool = True) -> list[tuple[float, float]]:
    """Return absolute-time (start, end) spans to KEEP within the clip.

    Speech spans are merged when separated by <= min_gap (so natural rhythm is
    preserved); larger gaps — plus leading/trailing dead air — are dropped. Each
    kept span is padded by `pad` seconds for breathing room. If there is no
    usable speech, the whole window is kept (i.e. nothing is trimmed).
    """
    spans: list[list[float]] = []
    for s in segments:
        if s.end <= clip_start or s.start >= clip_end:
            continue
        if not (s.text or "").strip():
            continue
        if drop_filler and is_filler(s.text):
            continue
        a = max(clip_start, s.start)
        b = min(clip_end, s.end)
        if b > a:
            spans.append([a, b])

    if not spans:
        return [(clip_start, clip_end)]

    spans.sort()
    merged: list[list[float]] = [spans[0][:]]
    for a, b in spans[1:]:
        if a - merged[-1][1] <= min_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])

    out: list[list[float]] = []
    for a, b in merged:
        a = max(clip_start, a - pad)
        b = min(clip_end, b + pad)
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])

    return [(round(a, 3), round(b, 3)) for a, b in out]


def kept_duration(keeps: list[tuple[float, float]]) -> float:
    return sum(e - s for s, e in keeps)


def remap_segments(segments: list[Segment],
                   keeps: list[tuple[float, float]]) -> tuple[list[Segment], float]:
    """Map absolute-time caption segments onto the compacted (0-based) timeline
    that results from concatenating only the kept spans.

    Returns (remapped_segments, total_duration). Times that fall inside a
    removed gap are snapped to the nearest kept boundary, so captions stay in
    sync with the trimmed video.
    """
    offsets: list[tuple[float, float, float]] = []
    acc = 0.0
    for s, e in keeps:
        offsets.append((s, e, acc))
        acc += (e - s)
    total = acc

    def to_compact(t: float) -> float:
        for s, e, off in offsets:
            if t < s:
                return off
            if t <= e:
                return off + (t - s)
        return total

    out: list[Segment] = []
    for seg in segments:
        if not (seg.text or "").strip():
            continue
        a = to_compact(seg.start)
        b = to_compact(seg.end)
        if b - a > 0.05:
            out.append(Segment(round(a, 3), round(b, 3), seg.text))
    return out, total
