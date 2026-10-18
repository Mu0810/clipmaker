"""Clip metadata and highlight helpers.

Highlight selection lives in media/select.py (the AI engine, then a local
ollama model, then a keyword heuristic); select_highlights() below adapts it
for the manual flow. This module writes titles, descriptions, tags, hooks and
viral scores, and keeps the shared Segment type.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

import llm

OLLAMA_URL = "http://localhost:11434"


@dataclass
class Segment:
    start: float
    end: float
    text: str


# ----------------------------- ollama path ----------------------------------

def _ollama_model() -> str | None:
    """Return the name of an available ollama model, or None."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        r.raise_for_status()
        models = r.json().get("models", [])
        if models:
            return models[0]["name"]
    except Exception:
        return None
    return None


def _ollama_generate(model: str, prompt: str, temperature: float,
                     timeout: float) -> str:
    """One local Ollama completion. Counted as an AI call (llm.count_calls)."""
    llm.count_call()
    r = httpx.post(
        f"{OLLAMA_URL}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False,
              "options": {"temperature": temperature}},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json().get("response", "")


def _extract_json_array(raw: str) -> list:
    # Models sometimes wrap JSON in prose or code fences.
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return []


def _extract_json_object(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


# --------------------------- clip metadata -----------------------------------
# Generate a YouTube title / description / tags for a single clip, reusing the
# same ollama client as highlight selection. Falls back to a keyword heuristic
# when no model is available.

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for",
    "with", "as", "is", "are", "was", "were", "be", "been", "it", "its", "this",
    "that", "these", "those", "i", "you", "he", "she", "we", "they", "them",
    "me", "my", "your", "our", "their", "so", "just", "like", "really", "very",
    "do", "does", "did", "have", "has", "had", "not", "no", "yes", "can", "will",
    "would", "could", "should", "what", "when", "why", "how", "who", "at", "by",
    "from", "up", "out", "about", "into", "than", "then", "there", "here", "all",
    "get", "got", "going", "gonna", "want", "know", "think", "thing", "things",
    # common filler that produced irrelevant tags
    "been", "being", "because", "also", "even", "much", "many", "more", "most",
    "some", "any", "every", "each", "other", "such", "only", "well", "back",
    "right", "okay", "yeah", "yep", "gonna", "wanna", "kinda", "sorta", "stuff",
    "actually", "basically", "literally", "honestly", "maybe", "probably",
    "people", "person", "today", "around", "still", "never", "always", "ever",
    "make", "makes", "made", "said", "says", "say", "tell", "told", "talk",
    "look", "looks", "looking", "come", "came", "take", "took", "give", "gave",
    "good", "great", "nice", "thanks", "thank", "please", "hey", "hello",
}

# Vague pronouns / quantifiers / empty intensifiers that make weak tags and
# phrases. Folded into the stopword set used by _keywords and _keyphrases.
_STOPWORDS |= {
    "something", "someone", "somebody", "anything", "anyone", "anybody",
    "everything", "everyone", "everybody", "nothing", "nobody", "almost",
    "beautiful", "incredible", "amazing", "awesome", "stuff", "guys",
    "lot", "kind", "sort",
}


# A small set of BROAD format tags. In 2026 these mega-tags don't drive reach,
# so at most 1-2 are blended in behind the clip's own niche/content tags.
_BROAD_TAGS = ["reels", "shorts", "fyp", "explore", "viral"]

# Hook templates for the offline title fallback (filled with the clip's top
# keyword). Kept specific-ish; the AI engines above produce far better hooks.
_TITLE_HOOKS = [
    "The {kw} moment nobody saw coming",
    "Why {kw} matters more than you think",
    "How {kw} actually works in {dur}s",
    "{kw}: the part everyone replays",
    "What everyone gets wrong about {kw}",
    "The {kw} detail you missed",
]


def _keywords(text: str, n: int = 10) -> list[str]:
    # Allow >=3 letters so real subjects like "box"/"car"/"ceo" survive; the
    # stopword set strips short filler ("the"/"say"/"hey").
    counts: dict[str, int] = {}
    for w in re.findall(r"[a-z][a-z']{2,}", (text or "").lower()):
        w = w.strip("'")
        if len(w) < 3 or w in _STOPWORDS:
            continue
        counts[w] = counts.get(w, 0) + 1
    # Sort by frequency, then by length (longer ≈ more specific/relevant).
    return [w for w, _ in sorted(counts.items(),
                                 key=lambda kv: (kv[1], len(kv[0])),
                                 reverse=True)[:n]]


def _keyphrases(text: str, n: int = 6) -> list[str]:
    """Two-word phrases of content words, ranked so the clip's REAL subject
    surfaces. A phrase scores by how often it occurs and by how frequent its
    constituent words are overall — so 'baja box' (box is said a lot) beats a
    one-off filler pair like 'twenty dollars'."""
    toks = [w.strip("'") for w in
            re.findall(r"[a-z][a-z']{2,}", (text or "").lower())]
    freq: dict[str, int] = {}
    for w in toks:
        if len(w) >= 3 and w not in _STOPWORDS:
            freq[w] = freq.get(w, 0) + 1

    counts: dict[str, int] = {}
    for a, b in zip(toks, toks[1:]):
        if a == b or len(a) < 3 or len(b) < 3 \
                or a in _STOPWORDS or b in _STOPWORDS:
            continue
        counts[f"{a} {b}"] = counts.get(f"{a} {b}", 0) + 1

    def _score(phrase: str) -> tuple[int, int]:
        a, b = phrase.split(" ")
        return counts[phrase], freq.get(a, 0) + freq.get(b, 0)

    return sorted(counts, key=_score, reverse=True)[:n]


def _content_tags(text: str, n: int = 12) -> list[str]:
    """Niche tag candidates ordered most-specific first: concatenated key
    phrases ('bajabox') ahead of single keywords ('box')."""
    out: list[str] = []
    for p in _keyphrases(text, 6):
        c = re.sub(r"[^a-z0-9]", "", p)
        if c and c not in out:
            out.append(c)
    for w in _keywords(text, n):
        if w not in out:
            out.append(w)
    return out[:n]


def suggest_hashtags(tags: list[str], n: int = 7, broad: int = 2) -> list[str]:
    """Build a tight, niche-leaning hashtag set, deduped and '#'-prefixed.

    2026 strategy: lead with content/niche tags (where a clip can actually
    rank) and add at most `broad` broad format tags. Keeping the set small
    (~5-8) avoids the low-intent signal that long hashtag dumps now trigger.
    """
    def norm(t: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(t).lower())

    niche: list[str] = []
    for t in (tags or []):
        c = norm(t)
        if c and len(c) > 2 and c not in niche:
            niche.append(c)
    out = niche[: max(1, n - broad)]
    for t in _BROAD_TAGS:                 # fill remaining slots with format tags
        if len(out) >= n:
            break
        c = norm(t)
        if c not in out:
            out.append(c)
    return ["#" + t for t in out[:n]]


def suggest_titles(text: str, base_title: str = "", dur: int = 30,
                   n: int = 5) -> list[str]:
    """A few hook-style title options seeded from the clip's REAL subject.
    Prefers two-word key phrases so each hook names something concrete."""
    seeds = (_keyphrases(text, 4) + _keywords(text, 4)) or ["this moment"]
    titles: list[str] = []
    if base_title:
        titles.append(base_title.strip()[:80])
    for i, hook in enumerate(_TITLE_HOOKS):
        kw = seeds[i % len(seeds)]
        titles.append(hook.format(kw=kw, dur=dur)[:80])
    # Dedupe, preserve order.
    out: list[str] = []
    for t in titles:
        t = t.strip()
        if t and t not in out:
            out.append(t)
    return out[:n]


# Short on-screen HOOK lines burned at the TOP of the vertical clip. These are
# deliberately distinct from the spoken words (a wasted curiosity slot if they
# match). A few are proven-generic; the rest seed the clip's real subject.
_HOOK_GENERIC = ["Wait for it", "Watch till the end", "You need to see this"]
_HOOK_SEEDED = [
    "The {kw} part",
    "It's all about the {kw}",
    "Watch what the {kw} does",
    "Nobody expected this {kw}",
    "This {kw} moment",
]


def make_hook(text: str, title: str = "") -> str:
    """A short, punchy on-screen hook (<=~40 chars) for the top of a vertical
    clip — intentionally different from the spoken opening. Seeds the clip's
    top subject when there is one, else uses a proven generic line."""
    seed = _keyphrases(text, 1) or _keywords(text, 1)
    if seed:
        kw = seed[0]
        return _HOOK_SEEDED[len(kw) % len(_HOOK_SEEDED)].format(kw=kw)[:40]
    return _HOOK_GENERIC[len(text or "") % len(_HOOK_GENERIC)]


def suggest(text: str, base_title: str = "", dur: int = 30) -> dict:
    """On-demand suggestions: {title_suggestions, hashtags}. Tries Kimchi first,
    then a local ollama model, then keyword/template heuristics."""
    if llm.is_configured():
        try:
            return _kimchi_suggest(text, base_title, dur)
        except Exception:
            pass
    model = _ollama_model()
    if model:
        try:
            return _ollama_suggest(text, model, base_title, dur)
        except Exception:
            pass
    return {
        "title_suggestions": suggest_titles(text, base_title, dur),
        "hashtags": suggest_hashtags(_content_tags(text)),
    }


_VIRAL_SYS = (
    "You are a world-class short-form content strategist who has grown YouTube "
    "Shorts, Reels and TikToks to tens of millions of views. You understand the "
    "2026 algorithm: distribution is won by watch time, likes-per-reach and "
    "sends-per-reach (DM shares) — NOT by hashtags. Hashtags only classify a "
    "post; caption keywords drive search/discovery. You write metadata that is "
    "ALWAYS specific to the actual clip content — never generic filler. "
    "You reply with strict minified JSON only: no prose, no markdown, no code fences."
)

# What "engineered for reach" means — injected into every prompt. Reflects 2026
# best practice: keyword-rich caption SEO + a SMALL, niche-leaning hashtag set,
# with hooks tuned for watch time and shareability (sends-per-reach).
_REACH_RULES = (
    "Rules for maximum reach (2026 algorithm):\n"
    "- First, silently extract from the transcript: the clip's REAL topic, the "
    "specific people/places/brands/things named, and the single most surprising, "
    "emotional or useful moment. Everything below must be built from THESE, not "
    "generic phrasing.\n"
    "- TITLES: specific enough that someone could only have written it after "
    "watching THIS clip (name the actual subject). 40-70 characters, front-load "
    "a curiosity gap, stake or bold-but-true claim in the first 3 words. Must be "
    "instantly readable — no clutter, no ALL CAPS walls, no fake numbers, no "
    "'you won't believe this' filler. Vary the 5 options across proven angles: "
    "curiosity gap, bold claim, how-to/value, number/list, and relatable/"
    "controversial take.\n"
    "- DESCRIPTION = caption SEO + a share trigger. Line 1: a natural-language "
    "hook sentence that restates the payoff and front-loads the 2-3 most "
    "searchable keywords/topic words (this is what gets you found). Line 2: one "
    "short line of context or a mini-payoff. Line 3: ONE light call-to-action "
    "that nudges a save, share or comment (sends and engagement are top ranking "
    "signals) — natural, never spammy or engagement-bait. Then a blank line and "
    "ONLY 3-5 hashtags. Do NOT repeat the title verbatim as line 1.\n"
    "- HASHTAGS: 5-8 total, lowercase, no spaces, NO punctuation. Lead with "
    "niche / mid-size tags pulled from the clip's actual subject and keywords "
    "(these are where the content can actually rank); include AT MOST 2 broad "
    "format tags (e.g. reels, shorts). Every tag must be genuinely relevant. "
    "More than ~8 hashtags can flag a post as low-intent — keep it tight.\n"
    "- 'tags' (YouTube keywords, no # symbol): 10-15 specific search phrases "
    "from the clip's subject, most relevant first."
)


def _parse_kimchi(obj: dict, clip_text: str,
                  max_tags: int = 8) -> tuple[list[str], list[str]]:
    """Pull clean title_suggestions + a TIGHT hashtag set from model JSON.

    Keeps the model's niche-first order, dedupes, and trims to max_tags so we
    never ship a low-intent hashtag dump. Guarantees one broad format tag for
    classification rather than padding with four mega-tags."""
    titles = [re.sub(r"\s+", " ", str(t)).strip()[:80]
              for t in (obj.get("title_suggestions") or []) if str(t).strip()]
    raw_tags = obj.get("hashtags") or []
    if isinstance(raw_tags, str):
        raw_tags = re.split(r"[,\s]+", raw_tags)
    hashtags: list[str] = []
    for t in raw_tags:
        t = re.sub(r"[^a-z0-9]", "", str(t).lower())
        if t and len(t) > 1 and ("#" + t) not in hashtags:
            hashtags.append("#" + t)
    hashtags = hashtags[:max_tags]
    if hashtags and not any(("#" + b) in hashtags for b in _BROAD_TAGS):
        if len(hashtags) >= max_tags:
            hashtags = hashtags[:max_tags - 1]
        hashtags.append("#reels")
    return titles, hashtags


def _kimchi_suggest(text: str, base_title: str, dur: int) -> dict:
    prompt = (
        f"{_REACH_RULES}\n\n"
        f"This clip is ~{int(dur)}s long.\nCLIP TRANSCRIPT:\n{text[:3500]}\n\n"
        'Reply ONLY with JSON: {"title_suggestions": [5 strings], '
        '"hashtags": [5-8 strings, niche first]}'
    )
    raw = llm.chat(prompt, system=_VIRAL_SYS, temperature=0.85, max_tokens=700)
    titles, hashtags = _parse_kimchi(_extract_json_object(raw), text)
    if not titles:
        raise ValueError("no titles from kimchi")
    if base_title:
        bt = base_title.strip()[:80]
        titles = [bt] + [t for t in titles if t != bt]
    return {"title_suggestions": titles[:5],
            "hashtags": hashtags or suggest_hashtags(_content_tags(text))}


def _kimchi_metadata(clip_text: str, source_title: str, fallback_title: str,
                     dur: int = 30) -> dict:
    prompt = (
        "Write publish-ready, reach-optimized metadata for ONE short-form clip "
        "cut from a longer video.\n\n"
        f"{_REACH_RULES}\n\n"
        f"Longer video title (context only): {source_title or 'unknown'}\n"
        f"This clip is ~{int(dur)}s long.\nCLIP TRANSCRIPT:\n{clip_text[:3500]}\n\n"
        'Reply ONLY with JSON: {"title": str, "title_suggestions": [5 strings], '
        '"hook": str (a 2-6 word ON-SCREEN text hook for the first 2 seconds — '
        'a curiosity gap, DIFFERENT from the title and from the first spoken '
        'line), "description": str (multi-line: hook line / context line / one '
        'light CTA / blank line / 3-5 hashtags), "tags": [10-15 strings], '
        '"hashtags": [5-8 strings, niche first]}'
    )
    raw = llm.chat(prompt, system=_VIRAL_SYS, temperature=0.8, max_tokens=1000)
    obj = _extract_json_object(raw)

    title = re.sub(r"\s+", " ", str(obj.get("title") or "")).strip()[:90]
    desc = str(obj.get("description") or "").strip()[:4900]
    hook = re.sub(r"\s+", " ", str(obj.get("hook") or "")).strip()[:48]
    tags = obj.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,\n]", tags)]
    tags = [re.sub(r"[^a-z0-9 ]", "", str(t).strip().lstrip("#").lower()).strip()
            for t in tags if str(t).strip()]
    tags = [t for t in tags if t]
    if not title:
        raise ValueError("kimchi returned no title")
    if not hook or hook.lower() == title.lower():
        hook = make_hook(clip_text, title)

    titles, hashtags = _parse_kimchi(obj, clip_text)
    titles = [title] + [t for t in titles if t != title]
    if not desc:
        # Never ship an empty caption: assemble a keyword-rich SEO one.
        desc = _seo_description(clip_text, title, hashtags, int(dur))
    elif "#" not in desc and hashtags:
        desc = f"{desc}\n\n{' '.join(hashtags[:5])}"

    return {
        "title": title,
        "hook": hook,
        "description": desc[:4900],
        "tags": (tags or _content_tags(clip_text))[:15],
        "title_suggestions": titles[:5],
        "hashtags": hashtags or suggest_hashtags(_content_tags(clip_text)),
    }


def score_titles(titles: list[str], clip_text: str = "") -> list[dict]:
    """Rate each title's hook strength 1-10 with a short reason, sorted best
    first. Uses Kimchi when configured, else a deterministic heuristic."""
    titles = [str(t).strip() for t in (titles or []) if str(t).strip()][:8]
    if not titles:
        return []
    scored = None
    if llm.is_configured():
        try:
            scored = _kimchi_score(titles, clip_text)
        except Exception:
            scored = None
    if scored is None:
        scored = [_heuristic_score(t) for t in titles]
    scored.sort(key=lambda s: s["score"], reverse=True)
    return scored


def _kimchi_score(titles: list[str], clip_text: str) -> list[dict]:
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    prompt = (
        "Score each candidate short-form video title for scroll-stopping HOOK "
        "strength and viral reach potential from 1 to 10 (10 = irresistible, "
        "would make someone stop scrolling instantly). Judge relevance to the "
        "clip too — a title that misrepresents the clip scores low. Give a "
        "concise reason (max 12 words) per title.\n\n"
        f"CLIP TRANSCRIPT:\n{clip_text[:2000]}\n\nCANDIDATE TITLES:\n{numbered}\n\n"
        'Reply ONLY with JSON: {"scores":[{"n":int,"score":int,"reason":str}]}'
    )
    raw = llm.chat(prompt, system=_VIRAL_SYS, temperature=0.3, max_tokens=700)
    obj = _extract_json_object(raw)
    by_n: dict[int, dict] = {}
    for item in (obj.get("scores") or []):
        try:
            n = int(item.get("n"))
            by_n[n] = {
                "score": max(1, min(10, int(round(float(item.get("score", 5)))))),
                "reason": str(item.get("reason") or "").strip()[:120],
            }
        except Exception:
            continue
    if not by_n:
        raise ValueError("no scores from kimchi")
    out = []
    for i, t in enumerate(titles, start=1):
        s = by_n.get(i)
        out.append({"title": t, **s} if s else _heuristic_score(t))
    return out


_POWER_WORDS = {
    "secret", "truth", "never", "why", "how", "stop", "mistake", "nobody",
    "actually", "insane", "shocking", "proven", "instantly", "finally", "real",
}


def _heuristic_score(title: str) -> dict:
    t = title.strip()
    n = len(t)
    score, reasons = 5, []
    if 40 <= n <= 70:
        score += 2; reasons.append("strong length")
    elif n < 25:
        score -= 1; reasons.append("a bit short")
    elif n > 85:
        score -= 1; reasons.append("too long")
    if re.search(r"\d", t):
        score += 1; reasons.append("has a number")
    if "?" in t:
        score += 1; reasons.append("question hook")
    if any(w in t.lower() for w in _POWER_WORDS):
        score += 1; reasons.append("power word")
    if re.search(r"[\U0001F000-\U0001FAFF☀-➿]", t):
        reasons.append("emoji")
    return {"title": t, "score": max(1, min(10, score)),
            "reason": ", ".join(reasons) or "decent hook"}


def score_viral_batch(texts: list[str], durs: list[int] | None = None) -> list[dict]:
    """Score each clip's viral potential 0-100 with a short reason, aligned to
    the input order. One Kimchi call for the whole batch; heuristic fallback."""
    if not texts:
        return []
    durs = durs or [30] * len(texts)
    if llm.is_configured():
        try:
            return _kimchi_viral_batch(texts, durs)
        except Exception:
            pass
    return [_heuristic_viral(t) for t in texts]


def _kimchi_viral_batch(texts: list[str], durs: list[int]) -> list[dict]:
    blocks = []
    for i, (t, d) in enumerate(zip(texts, durs), start=1):
        snippet = re.sub(r"\s+", " ", t or "").strip()[:600]
        blocks.append(f"[{i}] (~{int(d)}s) {snippet}")
    prompt = (
        "Rate each candidate clip's VIRAL POTENTIAL for short-form video "
        "(Shorts/Reels/TikTok) from 0 to 100. Weigh: hook strength in the first "
        "3 seconds, emotional impact, payoff/completeness as a standalone clip, "
        "relatability and shareability. Be discerning — spread the scores, don't "
        "cluster them. Give a concise reason (max 12 words) per clip.\n\n"
        f"CLIPS:\n" + "\n\n".join(blocks) + "\n\n"
        'Reply ONLY with JSON: {"scores":[{"n":int,"score":int,"reason":str}]}'
    )
    raw = llm.chat(prompt, system=_VIRAL_SYS, temperature=0.3, max_tokens=1200)
    obj = _extract_json_object(raw)
    by_n: dict[int, dict] = {}
    for item in (obj.get("scores") or []):
        try:
            n = int(item.get("n"))
            by_n[n] = {
                "score": max(0, min(100, int(round(float(item.get("score", 50)))))),
                "reason": str(item.get("reason") or "").strip()[:120],
            }
        except Exception:
            continue
    if not by_n:
        raise ValueError("no viral scores from kimchi")
    return [by_n.get(i, _heuristic_viral(texts[i - 1]))
            for i in range(1, len(texts) + 1)]


def _heuristic_viral(text: str) -> dict:
    words = re.findall(r"[a-z']+", (text or "").lower())
    cues = sum(1 for w in words if w in _CUE_WORDS)
    punch = (text or "").count("?") + (text or "").count("!")
    base = 45 + cues * 6 + punch * 4
    if 30 <= len(words) <= 140:
        base += 8
    reasons = []
    if cues:
        reasons.append("strong cue words")
    if punch:
        reasons.append("question/emotion hooks")
    return {"score": max(5, min(95, base)),
            "reason": ", ".join(reasons) or "moderate hook"}


def _ollama_suggest(text: str, model: str, base_title: str, dur: int) -> dict:
    prompt = (
        "You are a viral short-form strategist. From this clip transcript, "
        "write 5 scroll-stopping, SPECIFIC title options (each <=80 chars, "
        "front-loaded hook, names the clip's real subject, no false claims) "
        "and 5-8 lowercase hashtags — mostly niche tags pulled from the clip's "
        "subject and keywords, with at most 2 broad ones (reels, shorts).\n\n"
        f"Clip is ~{dur}s.\nTranscript:\n{text[:3000]}\n\n"
        'Reply ONLY with JSON: {"title_suggestions": [str], "hashtags": [str]}'
    )
    obj = _extract_json_object(_ollama_generate(model, prompt, 0.9, 90))
    titles = [str(t).strip()[:80] for t in (obj.get("title_suggestions") or [])
              if str(t).strip()]
    tags = obj.get("hashtags") or []
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    hashtags = []
    for t in tags:
        t = re.sub(r"[^a-z0-9]", "", str(t).lower())
        if t and len(t) > 1 and ("#" + t) not in hashtags:
            hashtags.append("#" + t)
    hashtags = hashtags[:8]
    if not titles:
        raise ValueError("no titles")
    if base_title:
        titles = [base_title.strip()[:80]] + [t for t in titles
                                              if t != base_title.strip()[:80]]
    return {"title_suggestions": titles[:5],
            "hashtags": hashtags or suggest_hashtags(_content_tags(text))}


def _ollama_metadata(clip_text: str, model: str, source_title: str) -> dict:
    prompt = (
        "You are a short-form video producer writing publish-ready Reels/Shorts "
        "metadata. Given the transcript of ONE clip, write: a punchy, SPECIFIC "
        "title (<=80 chars, names the real subject, no clickbait lies); a "
        "description that opens with a keyword-rich hook line (caption SEO), "
        "then a short context line, then one light call-to-action (save/share), "
        "then a blank line and 3-5 relevant hashtags; and 10-15 lowercase "
        "keyword tags from the clip's subject.\n\n"
        f"Source video: {source_title or 'unknown'}\n"
        f"Clip transcript:\n{clip_text[:3000]}\n\n"
        'Reply ONLY with JSON: {"title": str, "description": str, '
        '"tags": [str, ...]}'
    )
    obj = _extract_json_object(_ollama_generate(model, prompt, 0.7, 90))
    title = str(obj.get("title") or "").strip()[:90]
    desc = str(obj.get("description") or "").strip()[:4900]
    tags = obj.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,\n]", tags)]
    tags = [str(t).strip().lstrip("#").lower() for t in tags if str(t).strip()]
    if not (title and desc):
        raise ValueError("model returned incomplete metadata")
    return {
        "title": title,
        "hook": make_hook(clip_text, title),
        "description": desc,
        "tags": tags[:15],
        "title_suggestions": suggest_titles(clip_text, title),
        "hashtags": suggest_hashtags(tags),
    }


# Light, non-spammy CTAs that nudge the signals that matter in 2026 (saves,
# shares/sends, comments) without tipping into engagement-bait.
_CTAS = [
    "Save this so you don't lose it.",
    "Send this to someone who needs to see it.",
    "Follow for more clips like this.",
    "Which part stood out? Tell me below.",
]


def _seo_description(text: str, title: str, hashtags: list[str],
                     dur: int = 30) -> str:
    """Assemble a caption built for discovery: a keyword-rich hook line, a
    short context line, one light CTA, then a blank line and 3-5 hashtags.
    Pulls real words from the clip so the first line is searchable."""
    text = re.sub(r"\s+", " ", text or "").strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    hook = (sentences[0] if sentences else (title or "")).strip()
    # Surface the strongest key phrase up front for caption SEO.
    kp = _keyphrases(text, 1)
    if kp and kp[0] not in hook.lower():
        hook = f"{hook} ({kp[0]})"
    context = next((s for s in sentences[1:] if s.lower() != hook.lower()), "")
    cta = _CTAS[len(hook) % len(_CTAS)]
    parts = [p for p in (hook[:160], context[:160], cta) if p]
    body = "\n".join(parts) or (title or "Watch this")
    tail = " ".join(hashtags[:5])
    return f"{body}\n\n{tail}".strip()


def _heuristic_metadata(clip_text: str, fallback_title: str) -> dict:
    text = re.sub(r"\s+", " ", clip_text or "").strip()
    tags = _content_tags(text, 12) or _keywords(text, 8)
    hashtags = suggest_hashtags(tags)
    return {
        "title": fallback_title[:90],
        "hook": make_hook(text, fallback_title),
        "description": _seo_description(text, fallback_title, hashtags),
        "tags": tags[:15],
        "title_suggestions": suggest_titles(text, fallback_title),
        "hashtags": hashtags,
    }


def generate_metadata(clip_text: str, source_title: str = "",
                      fallback_title: str = "Clip", dur: int = 30) -> dict:
    """Return {title, hook, description, tags, title_suggestions, hashtags} for
    a clip. Tries Kimchi first, then a local ollama model, then a keyword
    heuristic. Always returns a usable dict."""
    if llm.is_configured():
        try:
            return _kimchi_metadata(clip_text, source_title, fallback_title, dur)
        except Exception:
            pass
    model = _ollama_model()
    if model:
        try:
            return _ollama_metadata(clip_text, model, source_title)
        except Exception:
            pass
    return _heuristic_metadata(clip_text, fallback_title)


# --------------------------- heuristic path ----------------------------------

# Words that tend to mark interesting/quotable moments.
_CUE_WORDS = {
    "best", "worst", "never", "always", "secret", "amazing", "incredible",
    "important", "crazy", "shocking", "mistake", "tip", "trick", "remember",
    "warning", "free", "money", "love", "hate", "wow", "insane", "huge",
    "first", "finally", "honestly", "actually", "literally", "wait",
}


def _title_from(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split(" ")
    return (" ".join(words[:8]) or "Highlight").strip()[:90]


# ------------------------------- public --------------------------------------

def select_highlights(segments: list[Segment], max_clips: int = 6,
                      target_len: float = 45.0) -> tuple[list[dict], str]:
    """Return (clips, method) in time order; method is 'ai:<model>',
    'ollama:<model>' or 'heuristic' (see media/select.py).

    Each segment is one selection unit, so clips start and end on the source's
    own line boundaries, as before. The target length t (clamped to 10-180 s)
    becomes the allowed range max(10, 0.6t) to min(180, 1.4t). Two edge cases
    keep today's behaviour: a source shorter than the range still gives one
    clip, and a single line longer than the range can still be a clip. Each
    clip also carries its score, reason and method."""
    if not segments:
        return [], "none"
    from media import select as select_mod
    from media import transcript as transcript_mod

    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    transcript = transcript_mod.from_lines(ordered, language="en")
    units = transcript["sentences"]
    if not units:
        return [], "none"
    span = units[-1]["e"] - units[0]["s"]
    if span <= 0:
        return [], "none"
    t = min(180.0, max(10.0, float(target_len)))
    lo = min(max(10.0, 0.6 * t), span)
    hi = max(min(180.0, 1.4 * t), max(u["e"] - u["s"] for u in units))
    clips, method = select_mod.select_clips(transcript, max_clips, lo, hi)
    clips.sort(key=lambda c: c["start"])
    return clips, method
