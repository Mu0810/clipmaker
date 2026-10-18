"""Render timed caption overlays as transparent PNGs with Pillow.

The installed ffmpeg lacks libass/libfreetype, so subtitles can't be burned
with the `subtitles`/`drawtext` filters. Instead we render each caption line
to a full-frame RGBA PNG (text baked near the bottom) and let ffmpeg overlay
it for the line's time window.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFont

from highlights import Segment
from media import overlays
from media.text import Typesetter


def _font(size: int) -> ImageFont.FreeTypeFont:
    """The Latin caption font at this size. Captions and hooks draw any script
    through media/overlays.py; this stays for callers that need one font."""
    return Typesetter(size).primary


def _split_segment(seg: Segment, max_words: int, max_dur: float) -> list[Segment]:
    """Split one line into short chunks, sharing the line's time across words
    proportionally to word length (longer words linger a bit longer)."""
    words = seg.text.split()
    if not words:
        return []
    weights = [len(w) + 1 for w in words]
    total = sum(weights)
    dur = max(0.01, seg.end - seg.start)
    bounds, t = [], seg.start
    for wgt in weights:
        wt = dur * wgt / total
        bounds.append((t, t + wt))
        t += wt

    chunks: list[Segment] = []
    i = 0
    while i < len(words):
        gstart = bounds[i][0]
        j = i
        while j < len(words) and (j - i) < max_words \
                and (bounds[j][1] - gstart) <= max_dur:
            j += 1
        if j == i:
            j = i + 1
        chunks.append(Segment(bounds[i][0], bounds[j - 1][1],
                              " ".join(words[i:j])))
        i = j
    return chunks


def _render_png(text: str, out_path: Path, w: int = 1080, h: int = 1920,
                font_size: int | None = None) -> None:
    if font_size is None:
        # Punchy short chunks get a bigger font; long lines stay readable.
        font_size = 88 if len(text.split()) <= 3 else 64

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    overlays.draw_caption(img, text, font_size)
    img.save(out_path)


def _word_bounds(seg: Segment) -> list[tuple[float, float, str]]:
    """Per-word (start, end, word) within a segment, sharing the line's time
    proportionally to word length (longer words linger a touch longer)."""
    words = seg.text.split()
    if not words:
        return []
    weights = [len(w) + 1 for w in words]
    total = sum(weights)
    dur = max(0.01, seg.end - seg.start)
    out, t = [], seg.start
    for w, wgt in zip(words, weights):
        d = dur * wgt / total
        out.append((t, t + d, w))
        t += d
    return out


def _render_karaoke_png(words: list[str], active: int, out_path: Path,
                        w: int = 1080, h: int = 1920, font_size: int | None = None,
                        accent: tuple[int, int, int] = (255, 214, 61)) -> None:
    """Draw a group of words (lower third) with the active word in an accent
    colour — the word-by-word highlight that reads as 'Shorts karaoke'."""
    n = len(words)
    if font_size is None:
        font_size = 92 if n <= 3 else (76 if n <= 5 else 60)

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    overlays.draw_karaoke(img, words, active, font_size, accent)
    img.save(out_path)


def _build_karaoke(segments: list[Segment], clip_start: float, clip_end: float,
                   out_dir: Path, max_dur: float,
                   group_size: int = 4) -> list[dict]:
    """One PNG per spoken word: a group of up to group_size words stays on
    screen while the active word highlights, then the group advances."""
    span = clip_end - clip_start
    captions: list[dict] = []
    for seg in segments:
        if seg.end <= clip_start or seg.start >= clip_end:
            continue
        wb = [(s, e, w) for (s, e, w) in _word_bounds(seg)
              if e > clip_start and s < clip_end]
        if not wb:
            continue
        for i in range(0, len(wb), group_size):
            grp = wb[i:i + group_size]
            words = [w for (_, _, w) in grp]
            for j, (s, e, _w) in enumerate(grp):
                local_start = max(0.0, s - clip_start)
                local_end = min(span, e - clip_start)
                if local_end - local_start < 0.04:
                    continue
                png = out_dir / f"cap_{len(captions):03d}.png"
                _render_karaoke_png(words, j, png)
                captions.append({
                    "png": png,
                    "start": round(local_start, 2),
                    "end": round(local_end, 2),
                })
    return captions


def render_hook(text: str, out_path: Path, w: int = 1080, h: int = 1920,
                font_size: int = 92) -> bool:
    """Render a short on-screen HOOK near the TOP of the frame (clear of the
    lower-third captions), on a translucent rounded panel for contrast.
    Emoji are stripped (the bundled fonts have no colour emoji); any script
    is drawn with a font that covers it.

    Returns True if a PNG was written, False if there was no usable text.
    """
    layer = overlays.hook_layer(text, (w, h), font_size)
    if layer is None:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    layer.save(out_path)
    return True


def build_clip_captions(segments: list[Segment], clip_start: float,
                        clip_end: float, out_dir: Path,
                        style: str = "words", max_words: int = 3,
                        max_dur: float = 1.2) -> list[dict]:
    """Render caption PNGs for segments overlapping [clip_start, clip_end].

    style="words" splits lines into punchy 1-max_words chunks (Shorts style);
    style="phrases" keeps whole caption/transcript lines;
    style="karaoke" shows a word group with the active word highlighted.
    Returns a list of {png, start, end} with clip-local timestamps.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    span = clip_end - clip_start

    if style == "karaoke":
        return _build_karaoke(segments, clip_start, clip_end, out_dir, max_dur)

    if style == "words":
        fine: list[Segment] = []
        for seg in segments:
            fine.extend(_split_segment(seg, max_words, max_dur))
        segments = fine

    captions: list[dict] = []
    for seg in segments:
        if seg.end <= clip_start or seg.start >= clip_end:
            continue
        local_start = max(0.0, seg.start - clip_start)
        local_end = min(span, seg.end - clip_start)
        if local_end - local_start < 0.2:
            continue
        png = out_dir / f"cap_{len(captions):03d}.png"
        _render_png(seg.text, png)
        captions.append({
            "png": png,
            "start": round(local_start, 2),
            "end": round(local_end, 2),
        })

    return captions
