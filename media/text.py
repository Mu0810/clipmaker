"""Script-aware text drawing for burned-in captions, hooks and thumbnails
(design §4.7, Requirement 3.5).

Text is drawn word by word. Each word is split into same-script runs, and
each run is drawn with a font whose character map covers it, so a line can
mix scripts (Hinglish, for example) and no character renders as a
missing-glyph box:

* fonts are tried in order: the preferred system fonts for the run's script
  (macOS names first, then Debian/Docker Noto names), then any font the user
  drops in fonts/, then Arial Unicode as a broad fallback;
* coverage is read from each font's character map with fontTools, because
  Pillow can't report a missing glyph;
* in a font collection the Bold face is used, then a Semibold one, then the
  first face; hidden system faces (names starting with ".") are skipped;
* a character no font covers is left out and logged.

Complex scripts (Indic, Arabic, Hebrew, Thai) need Pillow's Raqm layout with
FriBiDi; shaping_available() says whether both are present (readiness shows
it, and posts whose text needs shaping are held without it). Lines whose
first strong character is Arabic or Hebrew are laid out right to left. Emoji
are stripped from burned text (the bundled fonts have no colour emoji).
"""
from __future__ import annotations

import bisect
import functools
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from PIL import ImageFont, features

import paths

log = logging.getLogger(__name__)
# Font files often carry harmless table quirks that fontTools logs as warnings
# ("extra bytes in post.stringData array"); they aren't worth the log noise.
logging.getLogger("fontTools").setLevel(logging.ERROR)

LATIN = "latin"
OTHER = "other"
COMPLEX = frozenset({"devanagari", "bengali", "gurmukhi", "gujarati", "oriya", "tamil",
                     "telugu", "kannada", "malayalam", "arabic", "hebrew", "thai"})
RTL = frozenset({"arabic", "hebrew"})

# Unicode blocks per script. Characters outside every block are OTHER.
_RANGES = sorted([
    (0x0000, 0x036F, LATIN),          # Basic Latin .. combining diacritics
    (0x0370, 0x052F, LATIN),          # Greek, Cyrillic (drawn with the Latin fonts)
    (0x0590, 0x05FF, "hebrew"), (0xFB1D, 0xFB4F, "hebrew"),
    (0x0600, 0x06FF, "arabic"), (0x0750, 0x077F, "arabic"), (0x08A0, 0x08FF, "arabic"),
    (0xFB50, 0xFDFF, "arabic"), (0xFE70, 0xFEFF, "arabic"),
    (0x0900, 0x097F, "devanagari"), (0xA8E0, 0xA8FF, "devanagari"),
    (0x0980, 0x09FF, "bengali"),
    (0x0A00, 0x0A7F, "gurmukhi"),
    (0x0A80, 0x0AFF, "gujarati"),
    (0x0B00, 0x0B7F, "oriya"),
    (0x0B80, 0x0BFF, "tamil"),
    (0x0C00, 0x0C7F, "telugu"),
    (0x0C80, 0x0CFF, "kannada"),
    (0x0D00, 0x0D7F, "malayalam"),
    (0x0E00, 0x0E7F, "thai"),
    (0x1100, 0x11FF, "cjk"),          # Hangul Jamo
    (0x1E00, 0x1FFF, LATIN),          # Latin Extended Additional, Greek Extended
    (0x2E80, 0x9FFF, "cjk"),          # CJK radicals, kana, CJK symbols, ideographs
    (0xAC00, 0xD7AF, "cjk"),          # Hangul syllables
    (0xF900, 0xFAFF, "cjk"),
    (0xFF00, 0xFFEF, "cjk"),          # full-width forms
    (0x20000, 0x3FFFF, "cjk"),
])
_STARTS = [r[0] for r in _RANGES]

# Default-ignorable characters (joiners, direction marks, variation
# selectors): shaping uses them, but fonts needn't contain them.
_IGNORABLE = frozenset([0x00AD, 0x034F, 0x061C, 0x180E, 0x2060, 0xFEFF,
                        *range(0x200B, 0x2010), *range(0x202A, 0x202F),
                        *range(0x2066, 0x206A), *range(0xFE00, 0xFE10)])
_ZW_JOINERS = frozenset({0x200C, 0x200D})

# Emoji and pictographs (the ranges today's hook renderer already stripped).
_EMOJI = ("\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\u20E3"
          "\U000E0020-\U000E007F")
_EMOJI_JOINER = re.compile(f"(?<=[{_EMOJI}\uFE0F])\u200D|\u200D(?=[{_EMOJI}])")
_EMOJI_CHAR = re.compile(f"[{_EMOJI}\uFE0F]")

FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")
SYSTEM_FONT_DIRS = (
    "/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
    "~/Library/Fonts",
    "/usr/share/fonts/truetype/noto", "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/truetype/dejavu",
)
PREFERRED = {
    LATIN: ("Arial Bold.ttf", "HelveticaNeue.ttc", "Helvetica.ttc", "Arial.ttf",
            "NotoSans-Bold.ttf", "DejaVuSans-Bold.ttf"),
    "devanagari": ("Kohinoor.ttc", "ITFDevanagari.ttc", "DevanagariMT.ttc",
                   "Devanagari Sangam MN.ttc", "NotoSansDevanagari-Bold.ttf"),
    "bengali": ("KohinoorBangla.ttc", "Bangla Sangam MN.ttc", "Bangla MN.ttc",
                "NotoSansBengali-Bold.ttf"),
    "gurmukhi": ("MuktaMahee.ttc", "Gurmukhi Sangam MN.ttc", "Gurmukhi MN.ttc",
                 "NotoSansGurmukhi-Bold.ttf"),
    "gujarati": ("KohinoorGujarati.ttc", "Gujarati Sangam MN.ttc",
                 "NotoSansGujarati-Bold.ttf"),
    "oriya": ("NotoSansOriya.ttc", "Oriya Sangam MN.ttc", "NotoSansOriya-Bold.ttf"),
    "tamil": ("Tamil Sangam MN.ttc", "Tamil MN.ttc", "NotoSansTamil-Bold.ttf"),
    "telugu": ("KohinoorTelugu.ttc", "Telugu Sangam MN.ttc", "NotoSansTelugu-Bold.ttf"),
    "kannada": ("NotoSansKannada.ttc", "Kannada Sangam MN.ttc", "NotoSansKannada-Bold.ttf"),
    "malayalam": ("Malayalam Sangam MN.ttc", "Malayalam MN.ttc",
                  "NotoSansMalayalam-Bold.ttf"),
    "arabic": ("GeezaPro.ttc", "NotoNastaliq.ttc", "Arial Bold.ttf",
               "NotoSansArabic-Bold.ttf", "NotoNaskhArabic-Bold.ttf"),
    "hebrew": ("ArialHB.ttc", "Arial Bold.ttf", "NotoSansHebrew-Bold.ttf"),
    "thai": ("SukhumvitSet.ttc", "Ayuthaya.ttf", "Krungthep.ttf", "NotoSansThai-Bold.ttf"),
    "cjk": ("Hiragino Sans GB.ttc", "STHeiti Medium.ttc", "AppleSDGothicNeo.ttc",
            "Songti.ttc", "NotoSansCJK-Bold.ttc", "NotoSansCJKsc-Bold.otf"),
    OTHER: (),
}
BROAD = ("Arial Unicode.ttf",)


# ── characters and scripts ───────────────────────────────────────────────

def block_script(ch: str) -> str:
    cp = ord(ch)
    i = bisect.bisect_right(_STARTS, cp) - 1
    if i >= 0 and cp <= _RANGES[i][1]:
        return _RANGES[i][2]
    return OTHER


def _is_mark(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "M"


def _is_common(ch: str) -> bool:
    """Spaces, punctuation, symbols, ASCII digits and format characters: they
    belong to the run they appear in instead of starting one."""
    if ch.isspace() or "0" <= ch <= "9" or ord(ch) in _IGNORABLE:
        return True
    return unicodedata.category(ch)[0] in "PSZC"


def strip_emoji(text: str) -> str:
    """Remove emoji, including the joiners inside emoji sequences; joiners
    between letters (as in Indic conjuncts) are kept."""
    return _EMOJI_CHAR.sub("", _EMOJI_JOINER.sub("", text or ""))


def split_runs(text: str) -> list[tuple[str, str]]:
    """Same-script runs of `text`, as (run, script). Common characters join
    the run they're in (or the first run when they lead); combining marks
    stay with the character before them."""
    runs: list[list[str]] = []          # [script, text]
    lead = ""
    for ch in text:
        if _is_mark(ch) or _is_common(ch):
            if runs:
                runs[-1][1] += ch
            else:
                lead += ch
            continue
        script = block_script(ch)
        if runs and runs[-1][0] == script:
            runs[-1][1] += ch
        else:
            runs.append([script, ch])
    if not runs:
        if not lead:
            return []
        first = next((c for c in lead if not c.isspace() and ord(c) not in _IGNORABLE), "")
        script = block_script(first) if first and not ("0" <= first <= "9") else LATIN
        return [(lead, LATIN if script == OTHER else script)]
    runs[0][1] = lead + runs[0][1]
    return [(t, s) for s, t in runs]


def needs_shaping(text: str) -> bool:
    """True when the text contains a script that needs Raqm and FriBiDi."""
    return any(block_script(c) in COMPLEX for c in strip_emoji(text) if not _is_common(c))


def is_rtl(text: str) -> bool:
    """True when the first strong character is right-to-left (Arabic, Hebrew)."""
    for ch in text or "":
        kind = unicodedata.bidirectional(ch)
        if kind in ("R", "AL"):
            return True
        if kind == "L":
            return False
    return False


def shaping_available() -> bool:
    try:
        return bool(features.check_feature("raqm") and features.check_feature("fribidi"))
    except Exception:
        return False


def _clusters(text: str) -> list[str]:
    """Characters grouped so a base keeps its marks, joiners and (after a
    virama or a joiner) the next consonant: a group is never split across fonts."""
    out: list[str] = []
    for ch in text:
        if out and (_is_mark(ch) or ord(ch) in _IGNORABLE
                    or ord(out[-1][-1]) in _ZW_JOINERS
                    or unicodedata.combining(out[-1][-1]) == 9):
            out[-1] += ch
        else:
            out.append(ch)
    return out


# ── fonts ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Face:
    path: str
    index: int
    family: str
    cmap: frozenset = field(repr=False)

    def covers(self, text: str) -> bool:
        return all(ord(c) in self.cmap or ord(c) in _IGNORABLE for c in text)


# Pillow's own font, used only if no font file is found at all (ASCII only).
_PILLOW_DEFAULT = Face("", 0, "Pillow default", frozenset(range(0x20, 0x7F)))


def _face_rank(weight: int, subfamily: str) -> int:
    sub = subfamily.casefold().replace("-", "").replace(" ", "")
    if weight == 700 or sub == "bold":
        return 0
    if 600 <= weight < 700 or sub in ("semibold", "demibold", "demi", "w6"):
        return 1
    return 2


@functools.lru_cache(maxsize=None)
def load_face(path: str) -> Face | None:
    """The face used from a font file, with its character map; None if unusable."""
    from fontTools.ttLib import TTCollection, TTFont

    container = None
    try:
        if path.lower().endswith((".ttc", ".otc")):
            container = TTCollection(path, lazy=True)
            fonts = list(container.fonts)
        else:
            container = TTFont(path, lazy=True)
            fonts = [container]
        best = None
        for i, font in enumerate(fonts):
            names = font["name"]
            family = names.getDebugName(1) or ""
            if family.startswith("."):
                continue
            weight = font["OS/2"].usWeightClass if "OS/2" in font else 400
            key = (_face_rank(weight, names.getDebugName(2) or ""), i)
            if best is None or key < best[0]:
                best = (key, i, family, font)
        if best is None:
            return None
        _, index, family, font = best
        cmap = font.getBestCmap() or {}
        return Face(path, index, family, frozenset(cmap))
    except Exception:
        log.warning("Couldn't read the font %s", path, exc_info=True)
        return None
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:
                pass


@functools.lru_cache(maxsize=1)
def system_fonts() -> dict[str, str]:
    """Font file name -> path, from the system font folders (first one wins)."""
    index: dict[str, str] = {}
    for folder in SYSTEM_FONT_DIRS:
        try:
            entries = sorted(Path(folder).expanduser().iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() in FONT_EXTS and entry.name not in index:
                index[entry.name] = str(entry)
    return index


def user_fonts() -> list[str]:
    """Font files the user dropped in the repository's fonts/ folder."""
    try:
        entries = sorted(paths.fonts_dir().iterdir())
    except OSError:
        return []
    return [str(e) for e in entries if e.is_file() and e.suffix.lower() in FONT_EXTS]


def faces_for(script: str) -> list[Face]:
    """Fonts to try for a script, best first."""
    system = system_fonts()
    candidates = [system[n] for n in PREFERRED.get(script, ()) if n in system]
    candidates += user_fonts()
    candidates += [system[n] for n in BROAD if n in system]
    out: list[Face] = []
    seen: set[str] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        face = load_face(path)
        if face is not None:
            out.append(face)
    return out


def describe(ch: str) -> str:
    return f"U+{ord(ch):04X} {unicodedata.name(ch, 'unnamed character')}"


def resolve(text: str, script: str) -> tuple[list[tuple[str, Face]], list[str]]:
    """Split a run into (piece, face) pieces whose face covers the piece.
    Returns the pieces and the characters no font could draw."""
    faces = faces_for(script)
    for face in faces:
        if face.covers(text):
            return [(text, face)], []
    if not faces and script == LATIN and _PILLOW_DEFAULT.covers(text):
        return [(text, _PILLOW_DEFAULT)], []
    pieces: list[list] = []
    dropped: list[str] = []
    for cluster in _clusters(text):
        own = block_script(next((c for c in cluster if not _is_common(c)), cluster[0]))
        options = faces + [f for f in faces_for(own) if f not in faces]
        face = next((f for f in options if f.covers(cluster)), None)
        if face is None:
            dropped.extend(c for c in cluster if not _is_mark(c) and ord(c) not in _IGNORABLE)
            continue
        if pieces and pieces[-1][1] is face:
            pieces[-1][0] += cluster
        else:
            pieces.append([cluster, face])
    return [(p, f) for p, f in pieces], dropped


_local = threading.local()     # FreeType faces must not be shared across threads


def pil_font(face: Face, size: int, engine) -> ImageFont.FreeTypeFont:
    cache = getattr(_local, "fonts", None)
    if cache is None:
        cache = _local.fonts = {}
    key = (face.path, face.index, size, engine)
    font = cache.get(key)
    if font is None:
        if face is _PILLOW_DEFAULT or not face.path:
            font = ImageFont.load_default(size=size)
        else:
            font = ImageFont.truetype(face.path, size, index=face.index,
                                      layout_engine=engine)
        cache[key] = font
    return font


# ── typesetting ──────────────────────────────────────────────────────────

@dataclass
class Run:
    text: str
    face: Face
    font: ImageFont.FreeTypeFont
    width: float


@dataclass
class Word:
    text: str
    runs: list[Run]
    width: float
    breakable: bool = False             # may break between characters (CJK)
    dropped: list[str] = field(default_factory=list)


def _breakable(text: str) -> bool:
    strong = [c for c in text if not _is_common(c) and not _is_mark(c)]
    return bool(strong) and all(block_script(c) == "cjk" for c in strong)


class Typesetter:
    """Measures and draws words at one font size."""

    def __init__(self, size: int):
        self.size = int(size)
        self.engine = (ImageFont.Layout.RAQM if shaping_available()
                       else ImageFont.Layout.BASIC)
        latin = faces_for(LATIN)
        self.primary_face = latin[0] if latin else _PILLOW_DEFAULT
        self.primary = pil_font(self.primary_face, self.size, self.engine)
        self.ascent, self.descent = self.primary.getmetrics()
        self.space = self.primary.getlength(" ")
        self._memo: dict[str, Word] = {}

    def font(self, face: Face) -> ImageFont.FreeTypeFont:
        return pil_font(face, self.size, self.engine)

    def word(self, text: str) -> Word:
        """One word, emoji removed. A word with nothing drawable has no runs."""
        text = strip_emoji(text or "").strip()
        cached = self._memo.get(text)
        if cached is not None:
            return cached
        runs: list[Run] = []
        dropped: list[str] = []
        for run_text, script in split_runs(text):
            pieces, missing = resolve(run_text, script)
            dropped.extend(missing)
            for piece, face in pieces:
                if self.engine == ImageFont.Layout.BASIC:
                    piece = "".join(c for c in piece if ord(c) not in _IGNORABLE)
                if piece:
                    font = self.font(face)
                    runs.append(Run(piece, face, font, font.getlength(piece)))
        for ch in dict.fromkeys(dropped):
            log.warning("None of the caption fonts can draw %s; it was left out of %r.",
                        describe(ch), text)
        word = Word(text, runs, sum(r.width for r in runs), _breakable(text), dropped)
        self._memo[text] = word
        return word

    def words(self, text: str) -> list[Word]:
        return [w for w in (self.word(t) for t in strip_emoji(text or "").split()) if w.runs]

    def _split(self, word: Word, max_width: float) -> list[Word]:
        """A breakable (CJK) word cut into pieces that each fit max_width."""
        pieces: list[Word] = []
        current = ""
        for cluster in _clusters(word.text):
            trial = self.word(current + cluster)
            if current and trial.width > max_width:
                pieces.append(self.word(current))
                current = cluster
            else:
                current += cluster
        if current:
            pieces.append(self.word(current))
        return [p for p in pieces if p.runs]

    def wrap(self, words: list[Word], max_width: float,
             gap: float | None = None) -> list[list[tuple[int, Word]]]:
        """Greedy lines of (index into words, word). Words with nothing to
        draw are skipped; a CJK word wider than a line is broken."""
        gap = self.space if gap is None else gap
        lines: list[list[tuple[int, Word]]] = []
        current: list[tuple[int, Word]] = []
        width = 0.0
        for index, word in enumerate(words):
            if not word.runs:
                continue
            parts = (self._split(word, max_width)
                     if word.breakable and word.width > max_width else [word])
            for part in parts:
                add = part.width + (gap if current else 0.0)
                if current and width + add > max_width:
                    lines.append(current)
                    current, width, add = [], 0.0, part.width
                current.append((index, part))
                width += add
        if current:
            lines.append(current)
        return lines

    def line_width(self, line: list[tuple[int, Word]], gap: float | None = None) -> float:
        gap = self.space if gap is None else gap
        return sum(w.width for _, w in line) + gap * max(0, len(line) - 1)

    def place(self, line, center_x: float, gap: float | None = None,
              rtl: bool = False) -> list[tuple[int, Word, float]]:
        """Left x of each word, centred on center_x; right to left for RTL lines."""
        gap = self.space if gap is None else gap
        x = center_x - self.line_width(line, gap) / 2
        out = []
        for index, word in (list(reversed(line)) if rtl else line):
            out.append((index, word, x))
            x += word.width + gap
        return out

    def draw_word(self, draw, word: Word, x: float, baseline: float, fill,
                  stroke_width: int = 0, stroke_fill=None, rtl: bool = False) -> None:
        for run in (reversed(word.runs) if rtl else word.runs):
            draw.text((x, baseline), run.text, font=run.font, fill=fill, anchor="ls",
                      stroke_width=stroke_width, stroke_fill=stroke_fill)
            x += run.width

    def draw_lines(self, draw, lines, *, center_x: float, top: float, line_height: float,
                   fill, stroke_width: int = 0, stroke_fill=None, gap: float | None = None,
                   rtl: bool | None = None, fills: dict | None = None) -> None:
        """Draw wrapped lines; `top` is the top of the first line, as with
        Pillow's default anchor. `fills` maps word indices to their own colour."""
        if rtl is None:
            rtl = is_rtl(" ".join(w.text for line in lines for _, w in line))
        for k, line in enumerate(lines):
            baseline = top + k * line_height + self.ascent
            for index, word, x in self.place(line, center_x, gap, rtl):
                colour = fills.get(index, fill) if fills else fill
                self.draw_word(draw, word, x, baseline, colour, stroke_width,
                               stroke_fill, rtl)


def primary_font(size: int) -> ImageFont.FreeTypeFont:
    """The Latin font at this size (for code that draws Latin text only)."""
    return Typesetter(size).primary
