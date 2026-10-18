"""Whisper transcription fallback for videos without captions (manual flow).

The work happens in media/transcript.py, which also keeps word timestamps and
caches the result as transcript.json in the job folder. The manual flow keeps
its English model (WHISPER_MODEL, default base.en); Autopilot uses a
multilingual model (CLIPMAKER_WHISPER_MODEL, default small).
"""
from __future__ import annotations

import os
from pathlib import Path

from highlights import Segment
from media import transcript as transcript_mod

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "base.en")
LANGUAGE = "en"


def transcribe_detailed(video: Path, progress=None,
                        job_dir: Path | None = None) -> tuple[list[Segment], dict]:
    """Whisper's own segments (as before) plus the word-level transcript.
    With `job_dir`, a matching transcript.json there is reused and a new one saved."""
    if progress:
        progress("No captions found — transcribing audio with Whisper...")
    if job_dir is not None:
        transcript = transcript_mod.load_or_transcribe(
            job_dir, video, language=LANGUAGE, model_name=MODEL_SIZE)
    else:
        transcript = transcript_mod.transcribe_words(
            video, language=LANGUAGE, model_name=MODEL_SIZE)
    return transcript_mod.source_segments(transcript), transcript


def transcribe(video: Path, progress=None) -> list[Segment]:
    """Transcribe a video's audio into timestamped Segments."""
    return transcribe_detailed(video, progress)[0]
