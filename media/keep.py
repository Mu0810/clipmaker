"""Which parts of the source a clip keeps (design §4.5, Requirement 3.6).

A clip's keep spans are its [in, out] window minus the user's cuts and, when
auto-trim is on, minus dead air:

* a pause longer than 0.5 s between kept words is cut, and so is the silence
  before the first and after the last kept word;
* a filler-only phrase (a run of words bounded by such pauses whose text is
  only filler, such as "um", "uh" or "you know") is cut, the way editing.py
  cuts filler-only caption lines today. Filler inside a phrase is kept,
  because cutting it would chop up speech;
* each kept span is padded by 0.1 s, but never past the window or a cut, and
  never more than half-way to a neighbouring word, so no part of a removed
  word comes back.

If the window holds no usable speech, nothing is trimmed.

Words must be in time order (transcript.json keeps them that way).
"""
from __future__ import annotations

import bisect
import unicodedata

from editing import _FILLER

GAP_S = 0.5
PAD_S = 0.1
_SCAN_BACK_S = 60.0       # a word this long before a span can't belong to it


def merge(intervals) -> list[tuple[float, float]]:
    """Intervals sorted, with overlapping or touching ones merged; empty or
    reversed intervals are dropped."""
    out: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in intervals):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def base_spans(t_in: float, t_out: float, cuts=()) -> list[tuple[float, float]]:
    """[t_in, t_out] minus the cuts."""
    spans: list[tuple[float, float]] = []
    cur = float(t_in)
    for a, b in merge(cuts):
        a, b = max(a, t_in), min(b, t_out)
        if b <= a:
            continue
        if a > cur:
            spans.append((cur, a))
        cur = max(cur, b)
    if cur < t_out:
        spans.append((cur, float(t_out)))
    return spans


def _normalise(text: str) -> str:
    """Casefolded letters, marks, digits and apostrophes, in any script."""
    chars = [c if (unicodedata.category(c)[0] in "LMN" or c == "'") else " "
             for c in (text or "").casefold()]
    return " ".join("".join(chars).split())


def is_filler(text: str) -> bool:
    """True when the text is only filler. The filler list is English, so speech
    in any other language is never treated as filler."""
    norm = _normalise(text)
    return bool(norm) and norm in _FILLER


def _phrases(words: list[dict], indices: list[int], gap: float) -> list[list[int]]:
    phrases: list[list[int]] = []
    for k in indices:
        if phrases and words[k]["s"] - words[phrases[-1][-1]]["e"] <= gap:
            phrases[-1].append(k)
        else:
            phrases.append([k])
    return phrases


def _rounded(spans) -> list[tuple[float, float]]:
    out = [(round(a, 3), round(b, 3)) for a, b in spans]
    return [(a, b) for a, b in out if b > a]


def keep_spans(words: list[dict], t_in: float, t_out: float, cuts=(),
               auto_trim: bool = True, *, gap: float = GAP_S,
               pad: float = PAD_S) -> list[tuple[float, float]]:
    """Source-time (start, end) spans the clip keeps, in order."""
    base = base_spans(t_in, t_out, cuts)
    if not auto_trim or not base:
        return _rounded(base)
    starts = [w["s"] for w in words]
    kept: list[tuple[float, float]] = []
    for a, b in base:
        lo = bisect.bisect_left(starts, a - _SCAN_BACK_S)
        hi = bisect.bisect_right(starts, b)
        # A word belongs to the span its middle falls in.
        inside = [k for k in range(lo, hi)
                  if a <= (words[k]["s"] + words[k]["e"]) / 2 <= b]
        for phrase in _phrases(words, inside, gap):
            if is_filler(" ".join(words[k]["w"] for k in phrase)):
                continue
            first, last = phrase[0], phrase[-1]
            s, e = max(a, words[first]["s"]), min(b, words[last]["e"])
            lower, upper = a, b
            if first > 0:
                lower = max(lower, min(s, (words[first - 1]["e"] + s) / 2))
            if last + 1 < len(words):
                upper = min(upper, max(e, (e + words[last + 1]["s"]) / 2))
            kept.append((max(lower, s - pad), min(upper, e + pad)))
    if not kept:
        return _rounded(base)
    return _rounded(merge(kept))


def kept_duration(spans) -> float:
    return sum(b - a for a, b in spans)


def to_clip_time(spans, t: float) -> float:
    """Source time to clip time on the joined-up timeline. A time inside a
    removed stretch maps to where the next kept span begins; a time after the
    clip maps to its end."""
    acc = 0.0
    for a, b in spans:
        if t < a:
            return acc
        if t <= b:
            return acc + (t - a)
        acc += b - a
    return acc


def to_source_time(spans, ct: float) -> float:
    """Clip time to source time. A time exactly on a join maps to the start of
    the later span; a time at or after the clip's end maps to its last frame."""
    acc = 0.0
    for a, b in spans:
        if ct < acc + (b - a):
            return a + max(0.0, ct - acc)
        acc += b - a
    return spans[-1][1] if spans else 0.0
