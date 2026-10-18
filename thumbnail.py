"""Generate a click-worthy YouTube thumbnail for a clip.

Grabs a frame with ffmpeg, cover-fits it to 1280x720, darkens the lower band
for legibility, and overlays a few bold words. Reuses the Pillow font loader
from captions.py so it works with the same ffmpeg build (no libass needed).
"""
from __future__ import annotations

import subprocess
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from media.text import Typesetter, strip_emoji

_STOP = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "as", "is", "are", "was", "were", "be", "it", "this", "that", "you", "your",
    "i", "we", "they", "how", "why", "what", "when", "my", "me", "so", "just",
    "from", "at", "by", "about", "into", "if", "not", "no", "do", "does",
}


def _word_tokens(text: str) -> list[str]:
    """Words in any script: runs of letters, combining marks (Devanagari vowel
    signs, for example), digits, apostrophes and the zero-width joiners Indic
    conjuncts use. Python's regex word class misses combining marks, so a
    regex would cut "नमस्ते" into pieces."""
    words: list[str] = []
    current: list[str] = []
    for ch in text:
        if unicodedata.category(ch)[0] in "LMN" or ch in "'’\u200c\u200d":
            current.append(ch)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


def thumb_text(title: str, max_words: int = 4, max_chars: int = 22) -> str:
    """Pick a few punchy words from the title for the overlay. Words in any
    script are kept whole."""
    words = _word_tokens(strip_emoji(title or ""))
    keep = [w for w in words if w.lower() not in _STOP] or words
    out, used = [], 0
    for w in keep:
        if len(out) >= max_words or used + len(w) > max_chars:
            break
        out.append(w)
        used += len(w) + 1
    return " ".join(out).upper() or (title or "CLIP").upper()[:max_chars]


def _grab_frame(video: Path, out_jpg: Path, ss: float) -> bool:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{ss:.2f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "2", str(out_jpg)],
            capture_output=True, text=True, timeout=60)
        return proc.returncode == 0 and out_jpg.exists() and out_jpg.stat().st_size > 0
    except Exception:
        return False


def generate(video: Path, out_path: Path, text: str = "",
             duration: float = 30.0, accent=(236, 72, 153),
             at: float | None = None) -> Path | None:
    """Render a 1280x720 thumbnail from `video` with `text` overlaid.
    The frame is taken `at` seconds in when given (the clip's cover time),
    otherwise 30% into the clip, kept between 0.5 and 3 s.
    Returns the path, or None on failure."""
    size = (1280, 720)
    frame = out_path.with_suffix(".frame.jpg")
    if at is not None:
        ss = max(0.0, float(at))
    else:
        ss = min(max(0.5, duration * 0.3), 3.0)
    if not _grab_frame(video, frame, ss):
        return None

    try:
        img = ImageOps.fit(Image.open(frame).convert("RGB"), size,
                           Image.LANCZOS)
    except Exception:
        frame.unlink(missing_ok=True)
        return None

    w, h = size
    draw = ImageDraw.Draw(img, "RGBA")

    # Darken the bottom band so text reads on any footage.
    band_top = int(h * 0.52)
    for i, y in enumerate(range(band_top, h)):
        alpha = int(220 * (y - band_top) / (h - band_top))
        draw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))

    # Accent bar.
    draw.rectangle([0, h - 12, w, h], fill=accent + (255,))

    text = (text or "").strip().upper()
    if text:
        ts = Typesetter(116)
        max_w = int(w * 0.9)
        lines = ts.wrap(ts.words(text), max_w)[:3]

        line_h = int(116 * 1.16)
        block_h = line_h * len(lines)
        y = h - 70 - block_h
        stroke = 8
        ts.draw_lines(draw, lines, center_x=w / 2, top=y, line_height=line_h,
                      fill=(255, 255, 255, 255), stroke_width=stroke,
                      stroke_fill=(0, 0, 0, 255))

    try:
        img.save(out_path, "JPEG", quality=90)
    except Exception:
        frame.unlink(missing_ok=True)
        return None
    frame.unlink(missing_ok=True)
    return out_path
