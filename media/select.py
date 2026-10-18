"""Clip selection from a transcript (design §4.3, Requirement 2).

A clip is a run of whole sentences, so it always starts on a sentence's first
word and ends on a sentence's last word (2.2). The configured AI engine picks
and scores candidates (2.1, 2.3). If it is unavailable or its reply is
unusable, a local Ollama model picks them, and failing that a keyword
heuristic does (2.5); fallback candidates are scored with
highlights.score_viral_batch. Every clip records the method that chose it.

A clip is a dict:
    {"start": s, "end": e,           # seconds, from the sentences' first/last words
     "s0": i, "s1": j,               # sentences[i:j] (j exclusive)
     "title": str, "score": 0-100, "reason": str,
     "method": "ai:<model>" | "ollama:<model>" | "heuristic"}

select_clips returns clips best first.
"""
from __future__ import annotations

import logging
import math
import re

import highlights
import llm
from media.transcript import separator

log = logging.getLogger(__name__)

TOKEN_BUDGET = 60_000          # estimated tokens per AI call (characters ÷ 3)
MAX_CHUNKS = 3
TITLE_MAX = 90
REASON_MAX = 120
AI_TIMEOUT_S = 180


def _mmss(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def sentence_line(i: int, sentence: dict) -> str:
    return f"[{i}] {_mmss(sentence['s'])}–{_mmss(sentence['e'])} {sentence['text']}"


def estimate_tokens(lines: list[str]) -> float:
    return sum(len(line) + 1 for line in lines) / 3


def plan_chunks(sentences: list[dict], overlap_s: float, budget: float | None = None,
                max_chunks: int | None = None) -> list[tuple[int, int]]:
    """Sentence ranges [a, b) to send in separate AI calls. One range when the
    numbered transcript fits the budget; otherwise up to max_chunks ranges of
    similar size, each running on for overlap_s seconds into the next, so a
    clip up to that long is always wholly inside one of them."""
    budget = TOKEN_BUDGET if budget is None else budget
    max_chunks = MAX_CHUNKS if max_chunks is None else max_chunks
    n = len(sentences)
    if n == 0:
        return []
    sizes = [len(sentence_line(i, s)) + 1 for i, s in enumerate(sentences)]
    total = sum(sizes)
    if total / 3 <= budget:
        return [(0, n)]
    k = min(max_chunks, math.ceil(total / 3 / budget))
    cuts, acc = [0], 0
    for i, size in enumerate(sizes):
        acc += size
        if len(cuts) < k and acc >= total / k * len(cuts) and i + 1 < n:
            cuts.append(i + 1)
    cuts.append(n)
    chunks = []
    for a, b in zip(cuts, cuts[1:]):
        end, limit = b, sentences[b - 1]["e"] + overlap_s
        while end < n and sentences[end]["s"] < limit:
            end += 1
        chunks.append((a, end))
    return chunks


def fit(sentences: list[dict], i: int, j: int, lo: float, hi: float) -> tuple[int, int] | None:
    """Fit sentences i..j (inclusive) into lo..hi seconds by dropping trailing
    sentences (too long) or appending following ones (too short). None when
    no run of whole sentences starting at i fits."""
    def duration(a: int, b: int) -> float:
        return sentences[b]["e"] - sentences[a]["s"]

    while j > i and duration(i, j) > hi:
        j -= 1
    if duration(i, j) > hi:
        return None
    while duration(i, j) < lo:
        if j + 1 >= len(sentences) or duration(i, j + 1) > hi:
            return None
        j += 1
    return i, j


def _overlap(a: dict, b: dict) -> float:
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def resolve(candidates: list[dict], n: int) -> list[dict]:
    """Best first. When two clips overlap by more than half of the shorter one,
    only the higher-scoring one is kept (2.4). At most n clips."""
    ranked = sorted(candidates, key=lambda c: (-(c["score"] or 0), c["start"]))
    kept: list[dict] = []
    for c in ranked:
        if len(kept) == n:
            break
        if all(_overlap(c, k) <= 0.5 * min(c["end"] - c["start"], k["end"] - k["start"])
               for k in kept):
            kept.append(c)
    return kept


def _as_index(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        digits = value.strip().strip("[]").strip()
        if digits.isdigit():
            return int(digits)
    return None


def _as_score(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score):
        return None
    return max(0, min(100, int(round(score))))


def _clean(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _candidate(sentences, i, j, title, score, reason, method) -> dict:
    return {"start": sentences[i]["s"], "end": sentences[j]["e"], "s0": i, "s1": j + 1,
            "title": title or highlights._title_from(sentences[i]["text"]),
            "score": score, "reason": reason, "method": method}


def _validated(items, sentences, lo_idx, hi_idx, lo, hi, *, start_key="start",
               end_key="end", method=None) -> list[dict]:
    """Candidates whose sentence range is in [lo_idx, hi_idx), ordered, and fits."""
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        i, j = _as_index(item.get(start_key)), _as_index(item.get(end_key))
        if i is None or j is None or not lo_idx <= i <= j < hi_idx:
            continue
        fitted = fit(sentences, i, j, lo, hi)
        if fitted is None:
            continue
        out.append(_candidate(sentences, *fitted, _clean(item.get("title"), TITLE_MAX),
                              _as_score(item.get("score")),
                              _clean(item.get("reason"), REASON_MAX), method))
    return out


def _text(sentences, clip, language) -> str:
    return separator(language).join(
        sentences[k]["text"] for k in range(clip["s0"], clip["s1"])).strip()


def _score(candidates, sentences, language, ai_attempted) -> None:
    """Score fallback candidates. After a failed AI attempt the heuristic is
    used directly, so a failing engine is not asked again (N8.1 budget)."""
    if not candidates:
        return
    texts = [_text(sentences, c, language) for c in candidates]
    if ai_attempted:
        scores = [highlights._heuristic_viral(t) for t in texts]
    else:
        scores = highlights.score_viral_batch(
            texts, [int(c["end"] - c["start"]) for c in candidates])
    for c, s in zip(candidates, scores):
        c["score"], c["reason"] = s["score"], s["reason"]


# ── AI engine ────────────────────────────────────────────────────────────

def _ai_prompt(sentences, a, b, k, lo, hi, language) -> str:
    lines = "\n".join(sentence_line(i, sentences[i]) for i in range(a, b))
    return (
        "Below is part of a long video's transcript, one sentence per line, as "
        f"[n] start–end text (mm:ss). Language: {language or 'not specified'}.\n\n"
        f"Pick up to {k} moments that work as standalone short-form clips (YouTube "
        "Shorts, Instagram Reels): a strong hook in the first 3 seconds, a complete "
        "thought or payoff, and no missing context. Each clip is a run of whole "
        f"sentences lasting {lo:g} to {hi:g} seconds.\n\n"
        "Score each clip's viral potential from 0 to 100: hook strength, emotional "
        "impact, payoff and shareability. Be discerning and spread the scores. Give "
        "a short, specific title and a reason of at most 12 words.\n\n"
        'Reply ONLY with JSON: {"clips": [{"start": n, "end": n, "title": str, '
        '"score": int, "reason": str}]}, where start and end are the [n] numbers '
        "of the clip's first and last sentence.\n\n"
        f"TRANSCRIPT:\n{lines}\n"
    )


def _ai_pick(sentences, n, lo, hi, language) -> list[dict]:
    k = 2 * n
    candidates: list[dict] = []
    answered_by = None
    for a, b in plan_chunks(sentences, overlap_s=hi):
        raw = llm.chat(_ai_prompt(sentences, a, b, k, lo, hi, language),
                       system=highlights._VIRAL_SYS, temperature=0.4,
                       max_tokens=min(4000, 300 + 60 * k), timeout=AI_TIMEOUT_S)
        answered_by = llm.last_model() or answered_by
        items = highlights._extract_json_object(raw).get("clips")
        if isinstance(items, list):
            candidates.extend(_validated(items, sentences, a, b, lo, hi))
    method = f"ai:{answered_by or llm.model()}"
    for c in candidates:
        c["method"] = method
        if c["score"] is None:             # the engine left the score out
            fallback = highlights._heuristic_viral(_text(sentences, c, language))
            c["score"] = fallback["score"]
            c["reason"] = c["reason"] or fallback["reason"]
    return resolve(candidates, n)


# ── fallbacks ────────────────────────────────────────────────────────────

def _ollama_prompt(sentences, k, lo, hi) -> str:
    lines = "\n".join(sentence_line(i, s) for i, s in enumerate(sentences))
    return (
        "You are a short-form video editor. Below is a timestamped transcript of a "
        f"video, one sentence per line. Pick up to {k} of the MOST engaging, "
        f"self-contained moments that would make great {lo:g}-{hi:g}s clips "
        "(hooks, punchlines, surprising or emotional moments, useful tips).\n\n"
        "Reply ONLY with a JSON array. Each item: "
        '{"start_index": int, "end_index": int, "title": "catchy title"}. '
        "start_index/end_index are the [n] numbers of the clip's first and last "
        "sentence.\n\n"
        f"TRANSCRIPT:\n{lines}\n"
    )


def _ollama_pick(sentences, model, n, lo, hi, language, ai_attempted) -> list[dict]:
    raw = highlights._ollama_generate(model, _ollama_prompt(sentences, 2 * n, lo, hi),
                                      0.4, AI_TIMEOUT_S)
    candidates = _validated(highlights._extract_json_array(raw), sentences, 0,
                            len(sentences), lo, hi, start_key="start_index",
                            end_key="end_index", method=f"ollama:{model}")
    _score(candidates, sentences, language, ai_attempted)
    return resolve(candidates, n)


def _seed_score(text: str) -> float:
    """Cue words, questions and exclamations; very short sentences score low."""
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return 0.0
    cues = sum(1 for w in words if w in highlights._CUE_WORDS)
    return cues * 2.0 + text.count("?") + text.count("!") + min(len(words), 25) / 25.0


def _heuristic_pick(sentences, n, lo, hi, language, ai_attempted) -> list[dict]:
    count = len(sentences)
    target = (lo + hi) / 2
    order = sorted(range(count), key=lambda i: _seed_score(sentences[i]["text"]),
                   reverse=True)
    used: set[int] = set()
    candidates: list[dict] = []
    for seed in order:
        if len(candidates) >= 2 * n:
            break
        if seed in used:
            continue
        a = b = seed                       # grow around the seed towards the target
        while sentences[b]["e"] - sentences[a]["s"] < target:
            grew = False
            if (b + 1 < count and b + 1 not in used
                    and sentences[b + 1]["e"] - sentences[a]["s"] <= hi):
                b += 1
                grew = True
            if (sentences[b]["e"] - sentences[a]["s"] < target and a > 0
                    and a - 1 not in used
                    and sentences[b]["e"] - sentences[a - 1]["s"] <= hi):
                a -= 1
                grew = True
            if not grew:
                break
        if not lo <= sentences[b]["e"] - sentences[a]["s"] <= hi:
            continue
        used.update(range(a, b + 1))
        candidates.append(_candidate(sentences, a, b,
                                     highlights._title_from(sentences[a]["text"]),
                                     None, "", "heuristic"))
    _score(candidates, sentences, language, ai_attempted)
    return resolve(candidates, n)


# ── public ───────────────────────────────────────────────────────────────

def select_clips(transcript: dict, n: int, length_min: float, length_max: float,
                 language: str | None = None) -> tuple[list[dict], str]:
    """Choose up to n clips of length_min..length_max seconds. Returns
    (clips best first, the method that chose them)."""
    sentences = transcript.get("sentences") or []
    if n <= 0 or not sentences:
        return [], "none"
    if not 0 < length_min < length_max:
        raise ValueError("length_min must be positive and less than length_max")
    language = language or transcript.get("language")
    ai_attempted = False
    if llm.is_configured():
        ai_attempted = True
        try:
            clips = _ai_pick(sentences, n, length_min, length_max, language)
            if clips:
                return clips, clips[0]["method"]
            log.warning("The AI engine returned no usable clips; falling back.")
        except Exception:
            log.warning("AI clip selection failed; falling back.", exc_info=True)
    model = highlights._ollama_model()
    if model:
        try:
            clips = _ollama_pick(sentences, model, n, length_min, length_max,
                                 language, ai_attempted)
            if clips:
                return clips, f"ollama:{model}"
        except Exception:
            log.warning("Ollama clip selection failed; falling back.", exc_info=True)
    return (_heuristic_pick(sentences, n, length_min, length_max, language, ai_attempted),
            "heuristic")
