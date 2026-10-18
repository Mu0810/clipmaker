"""Deterministic text validators for YouTube and Instagram metadata, plus the
brand and hook rules (design §5.2, Requirements 4.3, 4.5).

These run AFTER the AI writes and BEFORE a post is created, so generated text
never breaks a platform's documented limits. They are pure functions: given
the fields and the brand settings, they return cleaned fields; the Autopilot
metadata step decides holds from what they report.
"""
from __future__ import annotations

import re

# Assignable YouTube categories (design §5.1). People & Blogs (22) is the default.
YOUTUBE_CATEGORIES = {
    "1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music",
    "15": "Pets & Animals", "17": "Sports", "19": "Travel & Events",
    "20": "Gaming", "22": "People & Blogs", "23": "Comedy", "24": "Entertainment",
    "25": "News & Politics", "26": "Howto & Style", "27": "Education",
    "28": "Science & Technology", "29": "Nonprofits & Activism",
}
DEFAULT_CATEGORY = "22"

TITLE_MAX = 100               # characters
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500          # total, YouTube's own counting
CAPTION_MAX = 2200            # characters
MAX_HASHTAGS = 30
MAX_MENTIONS = 20

_ANGLE = re.compile(r"[<>]")
_HASHTAG = re.compile(r"#[^\s#]+")
_MENTION = re.compile(r"@[A-Za-z0-9._]+")


def _strip_angles(text: str) -> str:
    return _ANGLE.sub("", text or "")


def _trim_to_word(text: str, limit: int) -> str:
    """At most `limit` characters, cut at the last space when that doesn't lose
    more than a quarter of the budget, else a hard cut."""
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= limit * 0.75:
        cut = cut[:space]
    return cut.rstrip()


def _trim_bytes(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes, never splitting a character."""
    data = (text or "").encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", "ignore")


def tag_cost(tag: str) -> int:
    """How YouTube counts one tag toward the 500-char total: its length, plus
    the two quotes it wraps in when it contains a space, plus the joining comma."""
    base = len(tag)
    if " " in tag:
        base += 2
    return base + 1          # the comma between tags


def fit_tags(tags: list[str], limit: int = TAGS_MAX_CHARS) -> list[str]:
    """Keep tags in order until the running total (as YouTube counts it) would
    exceed `limit`; lowest-ranked tags are dropped first (they come last)."""
    out, total = [], 0
    for tag in tags:
        tag = _strip_angles(str(tag)).strip()
        if not tag:
            continue
        cost = tag_cost(tag)
        if total + cost > limit + 1:        # the first tag has no leading comma
            break
        out.append(tag)
        total += cost
    return out


def fit_description(body: str, footer: str, limit: int = DESCRIPTION_MAX_BYTES) -> str:
    """Join `body` and `footer` within `limit` UTF-8 bytes, always keeping the
    whole footer: the body is trimmed (never the footer) when the two together
    overflow (§5.2). A footer that alone exceeds the limit is returned trimmed."""
    body = _strip_angles(body or "")
    footer = _strip_angles(footer or "").strip()
    if not footer:
        return _trim_bytes(body, limit)
    joined = f"{body.rstrip()}\n\n{footer}" if body.strip() else footer
    if len(joined.encode("utf-8")) <= limit:
        return joined
    sep_bytes = len("\n\n".encode("utf-8"))
    room = limit - len(footer.encode("utf-8")) - sep_bytes
    if room <= 0:
        return _trim_bytes(footer, limit)
    return f"{_trim_bytes(body, room).rstrip()}\n\n{footer}"


def youtube_fields(meta: dict) -> dict:
    """Clean a YouTube metadata dict in place-safe fashion: strip angle
    brackets, trim the title and description, fit the tags, and force a known
    category. `description` is assumed to already carry its footer."""
    title = _trim_to_word(_strip_angles(meta.get("title") or "").strip(), TITLE_MAX)
    description = _trim_bytes(_strip_angles(meta.get("description") or ""),
                             DESCRIPTION_MAX_BYTES)
    tags = fit_tags(meta.get("tags") or [])
    category = str(meta.get("category_id") or DEFAULT_CATEGORY)
    if category not in YOUTUBE_CATEGORIES:
        category = DEFAULT_CATEGORY
    return {**meta, "title": title or "Clip", "description": description,
            "tags": tags, "category_id": category}


def instagram_fields(meta: dict) -> dict:
    """Clean an Instagram caption: at most 2,200 characters, 30 hashtags and
    20 @-mentions. Hashtags past 30 and mentions past 20 are removed from the
    end; then the whole caption is trimmed to the character limit."""
    caption = _strip_angles(meta.get("caption") or "")
    caption = _drop_extra(caption, _HASHTAG, MAX_HASHTAGS)
    caption = _drop_extra(caption, _MENTION, MAX_MENTIONS)
    caption = _trim_to_word(caption.strip(), CAPTION_MAX)
    return {**meta, "caption": caption}


def _drop_extra(text: str, pattern: re.Pattern, keep: int) -> str:
    """Remove every match of `pattern` after the first `keep`, from the end."""
    matches = list(pattern.finditer(text))
    if len(matches) <= keep:
        return text
    out, removed = text, 0
    for m in reversed(matches[keep:]):
        out = out[:m.start()] + out[m.end():]
        removed += 1
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _word_boundary(word: str) -> re.Pattern:
    """A case-insensitive, Unicode-aware whole-word match for `word`."""
    return re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE | re.UNICODE)


def surviving_avoid_words(text: str, avoid: list[str]) -> list[str]:
    """Which words-to-avoid still appear in `text`, matched case-insensitively
    at word boundaries (§5.2)."""
    hit = []
    for word in avoid:
        word = (word or "").strip()
        if word and _word_boundary(word).search(text or ""):
            hit.append(word)
    return hit


def avoid_words_in_meta(meta: dict, platform: str, avoid: list[str]) -> list[str]:
    """Words to avoid still present across a post's visible text."""
    if platform == "youtube":
        text = " ".join(str(meta.get(k) or "") for k in ("title", "description"))
        text += " " + " ".join(meta.get("tags") or [])
    else:
        text = str(meta.get("caption") or "")
    return surviving_avoid_words(text, avoid)


def _words(text: str) -> list[str]:
    return re.findall(r"\S+", (text or "").strip())


def hook_ok(hook: str, title: str, first_sentence: str) -> bool:
    """A usable hook is 2-6 words and not the title or the first spoken
    sentence, compared case-folded (§5.2)."""
    words = _words(hook)
    if not 2 <= len(words) <= 6:
        return False
    folded = " ".join(words).casefold()
    return folded not in {(title or "").strip().casefold(),
                          (first_sentence or "").strip().casefold()}


# ── problem reports for user edits (task 6.4) ─────────────────────────────
# The AI's text is trimmed to fit; text the user typed is never silently cut.
# These report each violation by field, for a 422 the editor shows in place.

def youtube_problems(meta: dict) -> list[dict]:
    problems = []
    for key in ("title", "description"):
        if _ANGLE.search(str(meta.get(key) or "")):
            problems.append({"field": key, "message": "Remove the < and > characters."})
    title = str(meta.get("title") or "")
    if not title.strip():
        problems.append({"field": "title", "message": "The title can't be empty."})
    elif len(title) > TITLE_MAX:
        problems.append({"field": "title", "message":
                         f"The title is {len(title)} characters; YouTube allows {TITLE_MAX}."})
    size = len(str(meta.get("description") or "").encode("utf-8"))
    if size > DESCRIPTION_MAX_BYTES:
        problems.append({"field": "description", "message":
                         f"The description is {size} bytes; YouTube allows "
                         f"{DESCRIPTION_MAX_BYTES}."})
    tags = meta.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        problems.append({"field": "tags", "message": "Tags must be a list of words."})
    else:
        if any(_ANGLE.search(t) for t in tags):
            problems.append({"field": "tags", "message": "Remove the < and > characters."})
        total = sum(tag_cost(t.strip()) for t in tags if t.strip()) - (1 if tags else 0)
        if total > TAGS_MAX_CHARS:
            problems.append({"field": "tags", "message":
                             f"The tags come to {total} characters as YouTube counts them "
                             f"(with commas and quotes); the limit is {TAGS_MAX_CHARS}."})
    if str(meta.get("category_id") or DEFAULT_CATEGORY) not in YOUTUBE_CATEGORIES:
        problems.append({"field": "category_id", "message": "Choose a category from the list."})
    return problems


def instagram_problems(meta: dict) -> list[dict]:
    problems = []
    caption = str(meta.get("caption") or "")
    if _ANGLE.search(caption):
        problems.append({"field": "caption", "message": "Remove the < and > characters."})
    if len(caption) > CAPTION_MAX:
        problems.append({"field": "caption", "message":
                         f"The caption is {len(caption)} characters; Instagram allows "
                         f"{CAPTION_MAX}."})
    hashtags = len(_HASHTAG.findall(caption))
    if hashtags > MAX_HASHTAGS:
        problems.append({"field": "caption", "message":
                         f"The caption has {hashtags} hashtags; Instagram allows {MAX_HASHTAGS}."})
    mentions = len(_MENTION.findall(caption))
    if mentions > MAX_MENTIONS:
        problems.append({"field": "caption", "message":
                         f"The caption has {mentions} @-mentions; Instagram allows "
                         f"{MAX_MENTIONS}."})
    return problems


def summary() -> dict:
    """The limits, for the review queue's live counters."""
    return {"youtube": {"title_chars": TITLE_MAX, "description_bytes": DESCRIPTION_MAX_BYTES,
                        "tags_chars": TAGS_MAX_CHARS, "categories": YOUTUBE_CATEGORIES},
            "instagram": {"caption_chars": CAPTION_MAX, "hashtags": MAX_HASHTAGS,
                          "mentions": MAX_MENTIONS}}


# ── file format check before publishing (§8.8, Requirement 10.8) ─────────
# Instagram Reels limits from Meta's media reference (updated 28 Sep 2026);
# YouTube Shorts limits from YouTube's upload help. The renderer meets both;
# this guards edited and legacy files and names the limit a file breaks.

YT_MAX_DURATION_S = 180
YT_MAX_BYTES = 256 * 1000 ** 3
IG_MIN_DURATION_S, IG_MAX_DURATION_S = 3.0, 15 * 60.0
IG_MAX_BYTES = 300 * 1000 ** 2              # "300 MB", read in decimal to be safe
IG_MAX_WIDTH = 1920
IG_FPS = (23.0, 60.0)
IG_MAX_VIDEO_BPS = 25_000_000
IG_MAX_SAMPLE_RATE = 48_000
_TOLERANCE_S = 0.05                          # container durations aren't frame-exact


def _mb(n: float) -> str:
    return f"{n / 1000 ** 2:.0f} MB"


def check_file(probe: dict | None, platform: str) -> list[str]:
    """The limits a rendered file breaks for `platform` ("youtube" or
    "instagram"), each named with the file's value; empty when it can be
    published. `probe` is media/probe.py's result for the file."""
    if not probe:
        return ["The video file couldn't be read, so its format can't be checked."]
    p = probe
    problems: list[str] = []
    duration, size = p.get("duration"), p.get("bytes")
    width, height = p.get("width"), p.get("height")
    vcodec, acodec = (p.get("video_codec") or "").lower(), (p.get("audio_codec") or "").lower()
    container = (p.get("format") or "").lower()
    if not width or not height or not vcodec:
        return ["The file has no video stream."]

    if platform == "youtube":
        if duration is not None and duration > YT_MAX_DURATION_S + _TOLERANCE_S:
            problems.append(f"YouTube Shorts can be at most {YT_MAX_DURATION_S} s long; "
                            f"this clip is {duration:.1f} s.")
        if size is not None and size > YT_MAX_BYTES:
            problems.append("YouTube allows files up to 256 GB.")
        if height < width:
            problems.append(f"YouTube Shorts must be vertical or square; this video is "
                            f"{width}x{height}.")
        if vcodec != "h264":
            problems.append(f"The video must be H.264 for YouTube; it is {vcodec}.")
        if acodec and acodec != "aac":
            problems.append(f"The audio must be AAC for YouTube; it is {acodec}.")
        if "mp4" not in container:
            problems.append("The file must be an MP4 for YouTube.")
        return problems

    if platform != "instagram":
        raise ValueError(f"Unknown platform: {platform}")
    if duration is not None and not (IG_MIN_DURATION_S - _TOLERANCE_S <= duration
                                     <= IG_MAX_DURATION_S + _TOLERANCE_S):
        problems.append(f"Instagram Reels must be 3 s to 15 min long; this clip is "
                        f"{duration:.1f} s.")
    if size is not None and size > IG_MAX_BYTES:
        problems.append(f"Instagram Reels can be at most {_mb(IG_MAX_BYTES)}; this file is "
                        f"{_mb(size)}.")
    ratio = width / height
    if not 0.01 <= ratio <= 10:
        problems.append(f"Instagram Reels need an aspect ratio between 0.01:1 and 10:1; this "
                        f"video is {width}x{height}.")
    if width > IG_MAX_WIDTH:
        problems.append(f"Instagram Reels can be at most {IG_MAX_WIDTH} px wide; this video is "
                        f"{width} px.")
    if vcodec not in ("h264", "hevc"):
        problems.append(f"The video must be H.264 or HEVC for Instagram; it is {vcodec}.")
    if (p.get("field_order") or "progressive") not in ("progressive", "unknown"):
        problems.append("Instagram Reels must be progressive, not interlaced.")
    pix = (p.get("pix_fmt") or "").lower()
    if pix and not pix.startswith(("yuv420", "yuvj420", "nv12")):
        problems.append(f"Instagram Reels need 4:2:0 chroma; this video is {pix}.")
    fps = p.get("fps")
    if fps and not IG_FPS[0] - 0.01 <= fps <= IG_FPS[1] + 0.01:
        problems.append(f"Instagram Reels need 23 to 60 frames per second; this video is "
                        f"{fps:g} fps.")
    bps = p.get("video_bitrate") or p.get("bitrate")
    if bps and bps > IG_MAX_VIDEO_BPS:
        problems.append(f"Instagram Reels can be at most 25 Mb/s; this video is "
                        f"{bps / 1e6:.1f} Mb/s.")
    if acodec:
        if acodec != "aac":
            problems.append(f"The audio must be AAC for Instagram; it is {acodec}.")
        if (p.get("sample_rate") or 0) > IG_MAX_SAMPLE_RATE:
            problems.append(f"Instagram audio can be at most 48 kHz; this is "
                            f"{p['sample_rate'] / 1000:g} kHz.")
        if p.get("channels") not in (None, 1, 2):
            problems.append(f"Instagram audio must be mono or stereo; this has "
                            f"{p['channels']} channels.")
    if not ("mp4" in container or "mov" in container):
        problems.append("The file must be an MP4 or MOV for Instagram.")
    if p.get("moov_before_mdat") is False:
        problems.append("Instagram needs the MP4's moov atom at the start of the file.")
    if p.get("has_edit_list"):
        problems.append("Instagram doesn't accept MP4 files with edit lists.")
    return problems
