"""Per-platform AI metadata for one clip: Write, Check and Rewrite (design §5,
Requirements 4.1-4.9, 13.3, N8.1).

`generate(clip_text, ...)` returns the YouTube and Instagram fields, the hook,
and any holds (§5.3). It makes at most 3 AI calls per clip — Write, Check, and
one Rewrite only when Check or a validator fails — so a source with N clips
stays within the 3N + 3 budget of N8.1. Brand hashtags and the monetization
footer are appended by code and the platform limits re-applied, so generated
text never breaks a documented limit (platforms/limits.py).

The AI never decides "made for kids" (4.7) or the AI-content disclosure (4.8):
those come from the account and the profile, set by the caller.
"""
from __future__ import annotations

import json
import logging

import llm
import monetize
from autopilot import languages
from platforms import limits

log = logging.getLogger(__name__)

def language_label(code: str | None) -> str:
    """The language for the prompt, by name with its code ("Hindi (hi)")."""
    if not code:
        return "the clip's spoken language"
    name = languages.name(code)
    return f"{name} ({code})" if name else code


def _extract(raw: str) -> dict:
    from highlights import _extract_json_object

    return _extract_json_object(raw)


def _category_lines() -> str:
    return ", ".join(f"{cid} {name}" for cid, name in limits.YOUTUBE_CATEGORIES.items())


def _write_prompt(clip_text: str, language: str | None, brand: dict) -> str:
    from highlights import _REACH_RULES

    avoid = ", ".join(brand.get("avoid_words") or []) or "none"
    tone = (brand.get("tone") or "").strip() or "neutral and natural"
    return (
        "Write publish-ready metadata for ONE short-form clip, SEPARATELY for "
        "YouTube and Instagram.\n\n"
        f"{_REACH_RULES}\n\n"
        f"Write everything in {language_label(language)}.\n"
        f"Brand tone: {tone}. Never use these words: {avoid}.\n"
        f"YouTube limits: title <=100 chars, description <=5000 bytes, tags "
        f"<=500 chars total; no < or > anywhere. Instagram caption <=2200 chars, "
        f"<=30 hashtags, <=20 @-mentions.\n"
        f"Pick ONE YouTube category id from: {_category_lines()}. "
        f"Default to 22 if unsure.\n\n"
        f"CLIP TRANSCRIPT:\n{clip_text[:3500]}\n\n"
        'Reply ONLY with minified JSON: {"youtube": {"title": str, "description": '
        'str, "tags": [10-15 strings], "category_id": str}, "instagram": '
        '{"caption": str, "hashtags": [5-8 strings]}, "hook": str (a 2-6 word '
        'on-screen hook, a curiosity gap, DIFFERENT from the title and the first '
        'spoken line), "title_suggestions": [5 strings]}'
    )


def _write(clip_text: str, language: str | None, brand: dict) -> dict:
    from highlights import _VIRAL_SYS

    raw = llm.chat(_write_prompt(clip_text, language, brand),
                   system=_VIRAL_SYS, temperature=0.8, max_tokens=1100)
    return _extract(raw)


_CHECK_SYS = ("You are a careful fact-checker. You compare proposed social-media "
              "text against a video transcript and reply with strict minified JSON only.")


def _check_prompt(clip_text: str, youtube: dict, instagram: dict) -> str:
    proposed = {"youtube_title": youtube.get("title"),
                "youtube_description": youtube.get("description"),
                "instagram_caption": instagram.get("caption")}
    return (
        "Judge the PROPOSED text against the TRANSCRIPT.\n"
        "- unsupported_claims: specific factual claims in the text that the "
        "transcript does not support (names, numbers, outcomes). Opinion, hype "
        "and curiosity gaps are allowed.\n"
        "- sensitive: true if the clip contains strong profanity, sexual content, "
        "violence, hateful language, or health or financial claims; list which in "
        "sensitive_categories.\n\n"
        f"TRANSCRIPT:\n{clip_text[:3500]}\n\n"
        f"PROPOSED:\n{json.dumps(proposed, ensure_ascii=False)}\n\n"
        'Reply ONLY with minified JSON: {"supported": bool, "unsupported_claims": '
        '[strings], "sensitive": bool, "sensitive_categories": [strings], "reason": str}'
    )


def _check(clip_text: str, youtube: dict, instagram: dict) -> dict:
    raw = llm.chat(_check_prompt(clip_text, youtube, instagram),
                   system=_CHECK_SYS, temperature=0.0, max_tokens=400)
    obj = _extract(raw)
    return {
        "supported": bool(obj.get("supported", True)),
        "unsupported_claims": [str(c) for c in (obj.get("unsupported_claims") or [])],
        "sensitive": bool(obj.get("sensitive", False)),
        "sensitive_categories": [str(c) for c in (obj.get("sensitive_categories") or [])],
        "reason": str(obj.get("reason") or ""),
    }


def _rewrite(clip_text: str, language: str | None, brand: dict, problems: list[str]) -> dict:
    from highlights import _VIRAL_SYS

    prompt = (
        _write_prompt(clip_text, language, brand)
        + "\n\nThe previous attempt had these problems; fix ALL of them:\n- "
        + "\n- ".join(problems)
        + '\nAdd "self_check": {"supported": bool, "notes": str} to your JSON.'
    )
    raw = llm.chat(prompt, system=_VIRAL_SYS, temperature=0.4, max_tokens=1100)
    return _extract(raw)


def _heuristic(clip_text: str, fallback_title: str) -> dict:
    """No AI engine answered: build usable text from keywords (4.9 hold)."""
    from highlights import (_content_tags, _keyphrases, _seo_description, make_hook)

    title = (fallback_title or " ".join(_keyphrases(clip_text, 1)) or "Clip").strip()[:90]
    tags = _content_tags(clip_text)
    desc = _seo_description(clip_text, title, ["#" + t for t in tags[:5]], 30)
    return {
        "youtube": {"title": title, "description": desc, "tags": tags, "category_id": "22"},
        "instagram": {"caption": desc, "hashtags": ["#" + t for t in tags[:6]]},
        "hook": make_hook(clip_text, title), "title_suggestions": [title],
    }


def _finish(raw: dict, *, brand: dict, monetize_cfg: dict | None) -> tuple[dict, dict]:
    """Append brand hashtags and the monetization footer, then re-apply the
    platform limits. Returns the cleaned (youtube, instagram) field dicts."""
    yt = dict(raw.get("youtube") or {})
    ig = dict(raw.get("instagram") or {})
    always = ["#" + t.lstrip("#") for t in (brand.get("always_hashtags") or [])]
    default_tags = monetize.default_hashtags(monetize_cfg)
    footer_tags = [t for t in always + default_tags]

    # YouTube: description gets the footer; tags get the brand/default hashtags
    # as plain words (YouTube tags are not hashtags).
    body = yt.get("description") or ""
    footer_text = monetize.apply_to_description("", monetize_cfg)
    extra_lines = " ".join(footer_tags)
    footer = "\n".join(p for p in (footer_text, extra_lines) if p.strip())
    yt["description"] = limits.fit_description(body, footer)
    yt_tags = list(yt.get("tags") or []) + [t.lstrip("#") for t in footer_tags]
    yt["tags"] = yt_tags
    yt = limits.youtube_fields(yt)

    # Instagram: append any always/default hashtags not already present.
    caption = ig.get("caption") or ""
    present = caption.lower()
    for tag in footer_tags:
        if tag.lower() not in present:
            caption += (" " if caption and not caption.endswith("\n") else "") + tag
    ig["caption"] = caption
    ig = limits.instagram_fields(ig)
    return yt, ig


def _pick_hook(raw: dict, clip_text: str, title: str, first_sentence: str) -> str:
    from highlights import make_hook

    hook = (raw.get("hook") or "").strip()
    if limits.hook_ok(hook, title, first_sentence):
        return hook
    return make_hook(clip_text, title)


def generate(clip_text: str, *, language: str | None = None, brand: dict | None = None,
             monetize_cfg: dict | None = None, fallback_title: str = "",
             first_sentence: str = "", disclosure: bool = False) -> dict:
    """Write per-platform metadata for one clip, check it, rewrite once if
    needed, and finish it in code. Returns:

        {"youtube": {...}, "instagram": {...}, "hook": str,
         "title_suggestions": [...], "holds": [codes], "sensitive_categories": [...],
         "disclosure_ai": bool}

    Holds come from §5.3: `heuristic_metadata`, `unsupported_claims`,
    `avoid_words`, `sensitive`. The caller adds `text_shaping` (it depends on
    the clip's script, not the text). At most 3 AI calls are made."""
    brand = brand or {}
    avoid = brand.get("avoid_words") or []
    holds: list[str] = []

    if not llm.is_configured():
        raw = _heuristic(clip_text, fallback_title)
        holds.append("heuristic_metadata")
        sensitive_categories: list[str] = []
    else:
        raw = _write(clip_text, language, brand)
        if not raw.get("youtube") or not raw.get("instagram"):
            raw = _heuristic(clip_text, fallback_title)
            holds.append("heuristic_metadata")
            sensitive_categories = []
        else:
            check = _check(clip_text, raw["youtube"], raw["instagram"])
            yt0, ig0 = _finish(raw, brand=brand, monetize_cfg=monetize_cfg)
            avoid_hit = (limits.avoid_words_in_meta(yt0, "youtube", avoid)
                         or limits.avoid_words_in_meta(ig0, "instagram", avoid))
            problems = list(check["unsupported_claims"])
            if avoid_hit:
                problems.append("Remove these words entirely: " + ", ".join(avoid_hit))
            if (not check["supported"] and check["unsupported_claims"]) or avoid_hit:
                rewritten = _rewrite(clip_text, language, brand, problems)
                if rewritten.get("youtube") and rewritten.get("instagram"):
                    raw = rewritten
                    # The rewrite carries its own self_check; trusting it keeps
                    # the clip within 3 AI calls (Write + Check + Rewrite, N8.1).
                    self_check = rewritten.get("self_check") or {}
                    if check["unsupported_claims"]:
                        supported = bool(self_check.get("supported", False))
                        check = {**check, "supported": supported}
            sensitive_categories = check["sensitive_categories"]
            if check["sensitive"]:
                holds.append("sensitive")
            if not check["supported"] and check["unsupported_claims"]:
                holds.append("unsupported_claims")

    yt, ig = _finish(raw, brand=brand, monetize_cfg=monetize_cfg)
    still_avoid = (limits.avoid_words_in_meta(yt, "youtube", avoid)
                   or limits.avoid_words_in_meta(ig, "instagram", avoid))
    if still_avoid and "avoid_words" not in holds:
        holds.append("avoid_words")

    hook = _pick_hook(raw, clip_text, yt.get("title", ""), first_sentence)
    return {"youtube": yt, "instagram": ig, "hook": hook,
            "title_suggestions": raw.get("title_suggestions") or [yt.get("title", "")],
            "holds": holds, "sensitive_categories": sensitive_categories,
            "disclosure_ai": bool(disclosure)}
