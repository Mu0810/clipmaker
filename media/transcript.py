"""Word-level transcripts (design §4.2).

A transcript is stored as `transcript.json` in a job's folder:

    {"v": 1, "language": "hi", "language_probability": 0.97,
     "source": "whisper" | "captions", "estimated": false,
     "model": "small", "requested_language": null,
     "media": {"name": "source.mp4", "bytes": 331555168},
     "words": [{"w": "नमस्ते", "s": 12.34, "e": 12.71, "p": 0.93}, ...],
     "sentences": [{"i": 0, "s": 12.34, "e": 15.2, "w0": 0, "w1": 7, "text": "..."}, ...],
     "segments": [{"s": 12.34, "e": 15.2, "text": "..."}, ...]}

* `words` are in time order; `w` has no surrounding spaces; `p` is Whisper's
  confidence (None for words whose timing was estimated).
* `sentences` are the units clips are cut on: `words[w0:w1]` (w1 exclusive).
* `segments` are the source's own lines (Whisper segments or caption cues),
  kept so the manual flow sees exactly what it saw before this module.
* `estimated` is true when word times were spread across a line by word
  length rather than measured (YouTube captions, or Whisper output that came
  back without word timings).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, Iterable

from highlights import Segment

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FILE_NAME = "transcript.json"
AUTOPILOT_MODEL_ENV = "CLIPMAKER_WHISPER_MODEL"
AUTOPILOT_MODEL_DEFAULT = "small"

SENTENCE_END = frozenset(".!?।॥؟。！？")
_CLOSERS = "\"'”’»)]}」』）"
_OPENERS = "\"'“‘«([{「『（"
# Words that end in "." without ending a sentence (titles before a name, Latin
# abbreviations). Kept short on purpose: a missed boundary only makes a
# sentence longer, a false one can start a clip mid-sentence.
_ABBREVIATIONS = frozenset({"mr.", "mrs.", "ms.", "dr.", "prof.", "e.g.", "i.e.", "vs."})
PAUSE_BREAK_S = 0.8
MAX_SENTENCE_S = 25.0
# Languages Whisper splits into words without spaces (faster-whisper's own set).
NO_SPACE_LANGUAGES = frozenset({"zh", "ja", "th", "lo", "my", "yue"})
AUDIO_TIMEOUT_S = 3600

_models: dict[str, object] = {}
_models_lock = threading.Lock()


class TranscriptError(RuntimeError):
    pass


def autopilot_model_name() -> str:
    """Whisper model for Autopilot: multilingual, CLIPMAKER_WHISPER_MODEL or 'small'."""
    return os.environ.get(AUTOPILOT_MODEL_ENV, "").strip() or AUTOPILOT_MODEL_DEFAULT


def separator(language: str | None) -> str:
    return "" if (language or "").split("-")[0] in NO_SPACE_LANGUAGES else " "


def join_words(words: Iterable[dict], language: str | None = None) -> str:
    return separator(language).join(w["w"] for w in words).strip()


# ── sentences ────────────────────────────────────────────────────────────

def _ends_sentence(text: str) -> bool:
    core = text.rstrip(_CLOSERS + " ")
    if not core or core[-1] not in SENTENCE_END:
        return False
    return core.lstrip(_OPENERS).casefold() not in _ABBREVIATIONS


def _split_long(words: list[dict], a: int, b: int, max_len: float) -> list[tuple[int, int]]:
    """Split words[a:b] at its longest internal pause until every part is at
    most max_len seconds (ties go to the pause nearest the middle)."""
    out: list[tuple[int, int]] = []
    stack = [(a, b)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2 or words[hi - 1]["e"] - words[lo]["s"] <= max_len:
            out.append((lo, hi))
            continue
        mid = (words[lo]["s"] + words[hi - 1]["e"]) / 2
        cut = max(range(lo + 1, hi),
                  key=lambda k: (words[k]["s"] - words[k - 1]["e"],
                                 -abs(words[k]["s"] - mid)))
        stack.append((cut, hi))            # right half is popped after the left
        stack.append((lo, cut))
    return out


def sentences(words: list[dict], language: str | None = None, *,
              pause: float = PAUSE_BREAK_S, max_len: float = MAX_SENTENCE_S) -> list[dict]:
    """Group words into sentences: a sentence ends after a word ending in
    . ! ? । ॥ ؟ 。 ！ ？ (closing quotes allowed), or before a pause longer
    than `pause` seconds; sentences longer than `max_len` are split."""
    n = len(words)
    groups: list[tuple[int, int]] = []
    start = 0
    for i in range(n):
        if (i == n - 1 or _ends_sentence(words[i]["w"])
                or words[i + 1]["s"] - words[i]["e"] > pause):
            groups.append((start, i + 1))
            start = i + 1
    spans: list[tuple[int, int]] = []
    for a, b in groups:
        spans.extend(_split_long(words, a, b, max_len))
    return [{"i": k, "s": words[a]["s"], "e": words[b - 1]["e"], "w0": a, "w1": b,
             "text": join_words(words[a:b], language)}
            for k, (a, b) in enumerate(spans)]


# ── building transcripts ─────────────────────────────────────────────────

def _word(text, start, end, prob=None) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    s = float(start)
    e = max(float(end), s)
    return {"w": text, "s": round(s, 3), "e": round(e, 3),
            "p": None if prob is None else round(float(prob), 3)}


def spread_words(text: str, start: float, end: float) -> list[dict]:
    """Estimated word times for one line: the line's duration shared across its
    words in proportion to word length (the rule captions.py has always used)."""
    tokens = (text or "").split()
    if not tokens:
        return []
    weights = [len(t) + 1 for t in tokens]
    total = sum(weights)
    dur = max(0.01, float(end) - float(start))
    out, t = [], float(start)
    for token, weight in zip(tokens, weights):
        d = dur * weight / total
        out.append({"w": token, "s": round(t, 3), "e": round(t + d, 3), "p": None})
        t += d
    return out


def _segment(start, end, text) -> dict:
    return {"s": round(float(start), 3), "e": round(float(end), 3), "text": text.strip()}


def build(words: list[dict], *, language: str | None, source: str, estimated: bool,
          language_probability: float | None = None, segments: list[dict] | None = None,
          model: str | None = None, requested_language: str | None = None,
          media: dict | None = None) -> dict:
    return {
        "v": SCHEMA_VERSION,
        "language": language,
        "language_probability": (None if language_probability is None
                                 else round(float(language_probability), 3)),
        "source": source,
        "estimated": bool(estimated),
        "model": model,
        "requested_language": requested_language,
        "media": media,
        "words": words,
        "sentences": sentences(words, language),
        "segments": segments or [],
    }


def from_caption_segments(segments: Iterable[Segment], language: str = "en") -> dict:
    """Transcript from caption lines (pipeline.parse_subtitles): estimated words."""
    lines = [s for s in segments if (s.text or "").strip()]
    words: list[dict] = []
    for line in lines:
        words.extend(spread_words(line.text, line.start, line.end))
    return build(words, language=language, source="captions", estimated=True,
                 segments=[_segment(s.start, s.end, s.text) for s in lines])


def from_lines(segments: Iterable[Segment], language: str | None = "en") -> dict:
    """Transcript whose sentences are exactly the given lines, one per line, with
    estimated word timing. Used where clips must stay on the source's own line
    boundaries (the manual flow's highlight selection)."""
    lines = [s for s in segments if (s.text or "").strip()]
    words: list[dict] = []
    units: list[dict] = []
    for k, line in enumerate(lines):
        w0 = len(words)
        words.extend(spread_words(line.text, line.start, line.end))
        units.append({"i": k, "s": round(float(line.start), 3),
                      "e": round(float(line.end), 3), "w0": w0, "w1": len(words),
                      "text": line.text.strip()})
    transcript = build(words, language=language, source="lines", estimated=True,
                       segments=[_segment(s.start, s.end, s.text) for s in lines])
    transcript["sentences"] = units
    return transcript


# ── Whisper ──────────────────────────────────────────────────────────────

def get_model(name: str):
    """Load a faster-whisper model once per process (CPU, int8)."""
    with _models_lock:
        model = _models.get(name)
        if model is None:
            from faster_whisper import WhisperModel
            model = WhisperModel(name, device="cpu", compute_type="int8")
            _models[name] = model
        return model


def _extract_audio(video: Path, wav: Path) -> None:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn",
             "-ac", "1", "-ar", "16000", "-f", "wav", str(wav)],
            capture_output=True, text=True, check=True, timeout=AUDIO_TIMEOUT_S)
    except subprocess.CalledProcessError as e:
        raise TranscriptError(
            "Couldn't read the video's audio track: "
            + ((e.stderr or "").strip()[-300:] or "ffmpeg failed")) from e
    except subprocess.TimeoutExpired as e:
        raise TranscriptError("Reading the video's audio track took too long.") from e


def _media_id(video: Path) -> dict:
    return {"name": video.name, "bytes": video.stat().st_size}


def transcribe_words(video, language: str | None = None, model_name: str | None = None,
                     progress: Callable[[float], None] | None = None) -> dict:
    """Run Whisper with word timestamps. `language=None` detects the language.
    `progress` receives the fraction of the audio transcribed (0.0 to 1.0)."""
    video = Path(video)
    model_name = model_name or autopilot_model_name()
    wav = video.with_suffix(".audio.wav")
    _extract_audio(video, wav)
    words: list[dict] = []
    segments: list[dict] = []
    estimated = False
    reported = -1
    try:
        model = get_model(model_name)
        seg_iter, info = model.transcribe(
            str(wav), language=language, word_timestamps=True, vad_filter=True,
            beam_size=1, condition_on_previous_text=False)
        total = float(getattr(info, "duration", 0) or 0)
        for seg in seg_iter:
            text = (seg.text or "").strip()
            if not text:
                continue
            segments.append(_segment(seg.start, seg.end, text))
            seg_words = [w for w in (_word(x.word, x.start, x.end,
                                           getattr(x, "probability", None))
                                     for x in (getattr(seg, "words", None) or []))
                         if w is not None]
            if not seg_words:                  # no word timings came back
                seg_words = spread_words(text, seg.start, seg.end)
                estimated = True
            words.extend(seg_words)
            if progress and total > 0:
                pct = int(min(1.0, float(seg.end) / total) * 100)
                if pct > reported:
                    reported = pct
                    progress(pct / 100)
    finally:
        wav.unlink(missing_ok=True)
    if progress and reported < 100:
        progress(1.0)
    detected = getattr(info, "language", None) or language
    return build(words, language=detected, source="whisper", estimated=estimated,
                 language_probability=getattr(info, "language_probability", None),
                 segments=segments, model=model_name, requested_language=language,
                 media=_media_id(video))


# ── storage ──────────────────────────────────────────────────────────────

def path_for(job_dir) -> Path:
    return Path(job_dir) / FILE_NAME


def save(job_dir, transcript: dict) -> Path:
    """Write transcript.json atomically (readable by this account only)."""
    job_dir = Path(job_dir)
    fd, tmp = tempfile.mkstemp(prefix=".transcript.", suffix=".tmp", dir=job_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path_for(job_dir))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path_for(job_dir)


def load(job_dir) -> dict | None:
    """The saved transcript, or None if it is missing, unreadable or another version."""
    try:
        data = json.loads(path_for(job_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get("v") != SCHEMA_VERSION
            or not isinstance(data.get("words"), list)
            or not isinstance(data.get("sentences"), list)):
        return None
    return data


def load_or_transcribe(job_dir, video, language: str | None = None,
                       model_name: str | None = None,
                       progress: Callable[[float], None] | None = None) -> dict:
    """Reuse the job's Whisper transcript when it was made from the same file
    with the same model and language request; otherwise transcribe and save."""
    video = Path(video)
    model_name = model_name or autopilot_model_name()
    cached = load(job_dir)
    if (cached and cached.get("source") == "whisper"
            and cached.get("model") == model_name
            and cached.get("requested_language") == language
            and cached.get("media") == _media_id(video)):
        return cached
    transcript = transcribe_words(video, language=language, model_name=model_name,
                                  progress=progress)
    try:
        save(job_dir, transcript)
    except OSError:
        log.warning("Could not save %s", path_for(job_dir), exc_info=True)
    return transcript


# ── adapters ─────────────────────────────────────────────────────────────

def to_segments(transcript: dict) -> list[Segment]:
    """One Segment per sentence."""
    return [Segment(s["s"], s["e"], s["text"]) for s in transcript.get("sentences") or []]


def source_segments(transcript: dict) -> list[Segment]:
    """The source's own lines (Whisper segments or caption cues); sentences
    when a transcript has none stored."""
    lines = transcript.get("segments")
    if lines:
        return [Segment(s["s"], s["e"], s["text"]) for s in lines]
    return to_segments(transcript)
