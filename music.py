"""Background music tracks.

Tracks live in the local ``music/`` folder. Drop your own royalty-free files
there (or upload them from the UI). The pipeline mixes a chosen track under a
clip's audio, auto-ducked beneath speech.

Only use music you have the rights to — this tool does not provide or license
any audio.
"""
from __future__ import annotations

import re
from pathlib import Path

import paths

_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}


def ensure_dir() -> None:
    paths.music_dir().mkdir(parents=True, exist_ok=True)


def list_tracks() -> list[str]:
    """Filenames of usable audio tracks in the music folder, sorted."""
    ensure_dir()
    return sorted(
        p.name for p in paths.music_dir().iterdir()
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
    )


def resolve(name: str) -> Path | None:
    """Return the path for a track name, or None. Guards against traversal and
    non-audio files."""
    if not name:
        return None
    safe = Path(name).name                     # strip any directory components
    path = (paths.music_dir() / safe)
    if path.exists() and path.suffix.lower() in _AUDIO_EXTS:
        return path
    return None


def is_audio(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in _AUDIO_EXTS


def safe_name(filename: str) -> str:
    """Sanitize an uploaded track's filename, keeping its extension."""
    p = Path(filename or "")
    stem = re.sub(r"[^A-Za-z0-9._ -]", "", p.stem).strip() or "track"
    ext = p.suffix.lower()
    if ext not in _AUDIO_EXTS:
        ext = ".mp3"
    return f"{stem}{ext}"
