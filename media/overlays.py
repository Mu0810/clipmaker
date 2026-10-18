"""Burned-in text for a clip: captions and the hook as one overlay stream
(design §4.6 step 3; Requirements 3.4, 3.7, 7.4).

build_overlays() turns a clip's words into full-frame 1080x1920 RGBA images,
each shown for an exact duration, and lists them in an ffconcat file. The
renderer reads that file as one extra input and lays it over the vertical
video with a single overlay filter (the old code needed one per caption):

    ffmpeg ... -safe 0 -f concat -i overlays.ffconcat ...
        [1:v]setpts=PTS-STARTPTS[t];[v][t]overlay=0:0:eof_action=pass

`-safe 0` is required because each image carries `option framerate 1000`,
which makes ffmpeg time the images to the millisecond; without it the concat
demuxer rounds every change to 1/25 s.

Timing, in clip time (after cuts and trimming):

* words: chunks of at most 3 words lasting at most 1.2 s; phrases: one
  sentence, split into pieces that fit 3 lines; karaoke: groups of 4 words,
  each word highlighted from its own start until the next word starts.
  Captions never run across a sentence boundary.
* a caption appears when its first word starts and stays until the next one
  starts, or until 0.5 s after its last word ends, whichever comes first;
* the hook box shows from the start of the clip for hook.duration seconds.

Drawing goes through media/text.py, so every script renders with a font that
covers it. captions.py (the manual flow) draws with the same functions.
"""
from __future__ import annotations

import bisect
import re
from pathlib import Path

from PIL import Image, ImageDraw

from media import editspec, keep
from media.text import Typesetter, strip_emoji
from media.transcript import sentences as make_sentences
from media.transcript import separator

SIZE = (1080, 1920)
ACCENT = (255, 214, 61)              # #FFD63D, the karaoke highlight
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)
CAPTION_CENTER = 0.74                # caption block centred in the lower third
HOOK_TOP = 0.15                      # hook box in the upper third
WORDS_MAX = 3
WORDS_MAX_S = 1.2
KARAOKE_GROUP = 4
PHRASE_MAX_LINES = 3
HOLD_S = 0.5
CONCAT_FRAMERATE = 1000              # image timing resolution: 1 ms
CONCAT_NAME = "overlays.ffconcat"


def caption_font_size(n_words: int) -> int:
    """Punchy short chunks get a bigger font; long lines stay readable."""
    return 88 if n_words <= 3 else 64


def karaoke_font_size(n_words: int) -> int:
    return 92 if n_words <= 3 else (76 if n_words <= 5 else 60)


def caption_width(width: int) -> int:
    return int(width * 0.88)


def _block_top(height: int, line_h: int, n_lines: int) -> int:
    return int(height * CAPTION_CENTER) - line_h * n_lines // 2


def draw_caption(img: Image.Image, text: str, font_size: int | None = None) -> None:
    """Draw a caption, upper-cased and wrapped, centred in the lower third."""
    if font_size is None:
        font_size = caption_font_size(len(text.split()))
    w, h = img.size
    ts = Typesetter(font_size)
    lines = ts.wrap(ts.words(text.upper()), caption_width(w))
    if not lines:
        return
    line_h = int(font_size * 1.25)
    ts.draw_lines(ImageDraw.Draw(img), lines, center_x=w / 2,
                  top=_block_top(h, line_h, len(lines)), line_height=line_h,
                  fill=WHITE, stroke_width=max(2, font_size // 12), stroke_fill=BLACK)


def draw_karaoke(img: Image.Image, words: list[str], active: int,
                 font_size: int | None = None, accent=ACCENT,
                 gap: float | None = None) -> None:
    """Draw a word group in the lower third with the active word in the accent
    colour (the word-by-word highlight that reads as "Shorts karaoke")."""
    if font_size is None:
        font_size = karaoke_font_size(len(words))
    w, h = img.size
    ts = Typesetter(font_size)
    measured = [ts.word(str(x).upper()) for x in words]
    gap = font_size * 0.30 if gap is None else gap
    lines = ts.wrap(measured, int(w * 0.9), gap)
    if not lines:
        return
    line_h = int(font_size * 1.22)
    ts.draw_lines(ImageDraw.Draw(img), lines, center_x=w / 2,
                  top=_block_top(h, line_h, len(lines)), line_height=line_h,
                  fill=WHITE, stroke_width=max(2, font_size // 12), stroke_fill=BLACK,
                  gap=gap, fills={active: (*accent, 255)})


def hook_layer(text: str, size=SIZE, font_size: int = 92) -> Image.Image | None:
    """The on-screen hook: up to 3 upper-cased lines on a translucent rounded
    panel in the upper third (clear of the captions). Emoji are stripped.
    None when nothing drawable is left."""
    text = re.sub(r"\s+", " ", strip_emoji(text or "")).strip()
    if not text:
        return None
    w, h = size
    ts = Typesetter(font_size)
    max_w = int(w * 0.84)
    lines = ts.wrap(ts.words(text.upper()), max_w)[:3]
    if not lines:
        return None
    line_h = int(font_size * 1.2)
    y0 = int(h * HOOK_TOP)
    pad = int(font_size * 0.4)
    box_w = int(min(max_w + 2 * pad, max(ts.line_width(l) for l in lines) + 2 * pad))
    box_x = (w - box_w) // 2
    box = [box_x, y0 - pad, box_x + box_w, y0 + line_h * (len(lines) - 1) + font_size + pad]

    panel = Image.new("RGBA", size, (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(panel)
    try:
        pdraw.rounded_rectangle(box, radius=int(font_size * 0.32), fill=(0, 0, 0, 150))
    except (AttributeError, TypeError):        # very old Pillow
        pdraw.rectangle(box, fill=(0, 0, 0, 150))
    img = Image.alpha_composite(Image.new("RGBA", size, (0, 0, 0, 0)), panel)
    ts.draw_lines(ImageDraw.Draw(img), lines, center_x=w / 2, top=y0, line_height=line_h,
                  fill=WHITE, stroke_width=max(2, font_size // 12), stroke_fill=BLACK)
    return img


# ── timeline ─────────────────────────────────────────────────────────────

def _us(t: float) -> int:
    return int(round(t * 1_000_000))


def kept_words(words: list[dict], spans, edits: dict | None = None) -> list[dict]:
    """The words the clip keeps, in clip time, with caption edits applied:
    [{"i": word number, "text", "s", "e"}]. A word belongs to the kept span
    its middle falls in, as in media/keep.py."""
    edits = edits or {}
    out: list[dict] = []
    if not spans:
        return out
    lo, hi = spans[0][0], spans[-1][1]
    for i, w in enumerate(words):
        mid = (w["s"] + w["e"]) / 2
        if mid < lo or mid > hi or not any(a <= mid <= b for a, b in spans):
            continue
        text = " ".join(str(edits.get(str(i), w["w"])).split())
        if not text:
            continue
        s = keep.to_clip_time(spans, w["s"])
        out.append({"i": i, "text": text, "s": s,
                    "e": max(s, keep.to_clip_time(spans, w["e"]))})
    return out


def _sentence_runs(kept: list[dict], sentences: list[dict]) -> list[list[dict]]:
    """Kept words in runs that never cross a sentence boundary."""
    starts = [s["w0"] for s in sentences]

    def sentence_of(i: int):
        k = bisect.bisect_right(starts, i) - 1
        return k if k >= 0 and i < sentences[k]["w1"] else ("word", i)

    runs: list[list[dict]] = []
    last = None
    for w in kept:
        sid = sentence_of(w["i"])
        if runs and sid == last:
            runs[-1].append(w)
        else:
            runs.append([w])
        last = sid
    return runs


def _word_chunks(run: list[dict]) -> list[list[dict]]:
    chunks, i = [], 0
    while i < len(run):
        j = i + 1
        while (j < len(run) and j - i < WORDS_MAX
               and run[j]["e"] - run[i]["s"] <= WORDS_MAX_S + 1e-9):
            j += 1
        chunks.append(run[i:j])
        i = j
    return chunks


def _phrase_pieces(run: list[dict], sep: str, max_w: int, typesetter) -> list[list[dict]]:
    """A sentence split into pieces that each fit PHRASE_MAX_LINES lines."""
    pieces, start = [], 0
    for k in range(1, len(run)):
        candidate = run[start:k + 1]
        ts = typesetter(caption_font_size(len(candidate)))
        text = sep.join(w["text"] for w in candidate).upper()
        if len(ts.wrap(ts.words(text), max_w)) > PHRASE_MAX_LINES:
            pieces.append(run[start:k])
            start = k
    pieces.append(run[start:])
    return pieces


def _states(kept: list[dict], sentences: list[dict], style: str, sep: str,
            width: int) -> list[dict]:
    """What the captions show, in order: one state per chunk (words, phrases)
    or per active word (karaoke)."""
    typesetters: dict[int, Typesetter] = {}

    def typesetter(size: int) -> Typesetter:
        if size not in typesetters:
            typesetters[size] = Typesetter(size)
        return typesetters[size]

    states: list[dict] = []
    for run in _sentence_runs(kept, sentences):
        if style == "karaoke":
            for g in range(0, len(run), KARAOKE_GROUP):
                group = run[g:g + KARAOKE_GROUP]
                for pos, w in enumerate(group):
                    natural = group[pos + 1]["s"] if pos + 1 < len(group) else w["e"]
                    states.append({"start": w["s"], "natural_end": natural, "words": group,
                                   "active": pos,
                                   "font_size": karaoke_font_size(len(group))})
            continue
        pieces = (_word_chunks(run) if style == "words"
                  else _phrase_pieces(run, sep, caption_width(width), typesetter))
        for piece in pieces:
            states.append({"start": piece[0]["s"], "natural_end": piece[-1]["e"],
                           "words": piece, "active": None,
                           "font_size": caption_font_size(len(piece))})
    return states


def _timeline(states: list[dict], total_us: int, hook_end_us: int) -> list[tuple]:
    """[(start_us, end_us, state index or None, hook shown)]: one entry per
    stretch of identical content, covering the whole clip."""
    for k, st in enumerate(states):
        end = st["natural_end"] + HOLD_S
        if k + 1 < len(states):
            end = min(end, states[k + 1]["start"])
        st["start_us"] = min(_us(st["start"]), total_us)
        st["end_us"] = min(_us(end), total_us)
    live = [k for k, st in enumerate(states) if st["end_us"] > st["start_us"]]
    points = {0, total_us, hook_end_us}
    for k in live:
        points.update((states[k]["start_us"], states[k]["end_us"]))
    points = sorted(p for p in points if 0 <= p <= total_us)
    out: list[tuple] = []
    pos = 0
    for a, b in zip(points, points[1:]):
        while pos < len(live) and states[live[pos]]["end_us"] <= a:
            pos += 1
        state = (live[pos] if pos < len(live) and states[live[pos]]["start_us"] <= a
                 else None)
        key = (state, a < hook_end_us)
        if out and out[-1][2:] == key:
            out[-1] = (out[-1][0], b, *key)
        else:
            out.append((a, b, *key))
    return out


# ── frames and the ffconcat list ─────────────────────────────────────────

def _frame(state: dict | None, hook: Image.Image | None, size, sep: str) -> Image.Image:
    img = hook.copy() if hook is not None else Image.new("RGBA", size, (0, 0, 0, 0))
    if state is not None:
        texts = [w["text"] for w in state["words"]]
        if state["active"] is None:
            draw_caption(img, sep.join(texts), state["font_size"])
        else:
            draw_karaoke(img, texts, state["active"], state["font_size"],
                         gap=0.0 if sep == "" else None)
    return img


def _write_concat(out_dir: Path, entries: list[tuple[Path, int]]) -> Path:
    """entries: (image, duration in microseconds). The last image is listed
    again at the end; the concat demuxer ignores the last duration otherwise."""
    lines = ["ffconcat version 1.0"]

    def file_lines(path: Path) -> list[str]:
        return [f"file '{path.name}'", f"option framerate {CONCAT_FRAMERATE}"]

    for path, dur in entries:
        lines += file_lines(path)
        lines.append(f"duration {dur // 1_000_000}.{dur % 1_000_000:06d}")
    lines += file_lines(entries[-1][0])
    concat = out_dir / CONCAT_NAME
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat


def input_args(concat: Path) -> list[str]:
    """ffmpeg input arguments for the overlay stream."""
    return ["-safe", "0", "-f", "concat", "-i", str(concat)]


def build_overlays(words: list[dict], keep_spans, spec, out_dir, *,
                   sentences: list[dict] | None = None, language: str | None = None,
                   size=SIZE) -> tuple[Path, list[dict]]:
    """Write one clip version's overlay images and ffconcat list.

    `words` are the transcript's words (caption edits refer to their numbers),
    `keep_spans` come from media/keep.py, `spec` is an EditSpec (or its dict).
    Returns (ffconcat path, intervals); each interval is {"start", "end"
    (clip seconds), "png", "caption" (text shown, None when blank), "words"
    (transcript word numbers shown), "active" (karaoke: the highlighted word's
    number), "hook" (whether the hook shows)}."""
    if isinstance(spec, dict):
        spec = editspec.parse(spec)
    spans = [(float(a), float(b)) for a, b in keep_spans]
    total = keep.kept_duration(spans)
    if total <= 0:
        raise ValueError("The clip has no length.")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("ov_*.png"):
        old.unlink()

    sep = separator(language)
    if sentences is None:
        sentences = make_sentences(words, language)
    states: list[dict] = []
    if spec.captions.enabled:
        kept = kept_words(words, spans, spec.captions.edits)
        states = _states(kept, sentences, spec.captions.style, sep, size[0])
    hook = hook_layer(spec.hook.text, size) if spec.hook.enabled else None
    total_us = _us(total)
    hook_end_us = min(_us(spec.hook.duration), total_us) if hook is not None else 0

    files: dict[tuple, Path] = {}
    entries: list[tuple[Path, int]] = []
    intervals: list[dict] = []
    for a, b, state, hook_on in _timeline(states, total_us, hook_end_us):
        st = states[state] if state is not None else None
        key = (state, hook_on)
        if key not in files:
            path = out_dir / f"ov_{len(files):03d}.png"
            _frame(st, hook if hook_on else None, size, sep).save(path, compress_level=1)
            files[key] = path
        entries.append((files[key], b - a))
        intervals.append({
            "start": a / 1_000_000, "end": b / 1_000_000, "png": files[key],
            "caption": sep.join(w["text"] for w in st["words"]) if st else None,
            "words": [w["i"] for w in st["words"]] if st else [],
            "active": (st["words"][st["active"]]["i"]
                       if st and st["active"] is not None else None),
            "hook": hook_on})
    return _write_concat(out_dir, entries), intervals
