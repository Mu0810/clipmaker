"""Where Clip Maker keeps its data.

Everything the app writes (database, rendered clips, music, account tokens and
settings files) lives in one data folder: the repository folder by default, or
the folder named by CLIPMAKER_DATA_DIR. The variable is read on every call, so
tests can point the whole app at a temporary folder without touching real data.

Code and bundled assets (static/, models/, fonts/) always come from the
repository.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent


def data_dir() -> Path:
    raw = os.environ.get("CLIPMAKER_DATA_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else REPO


def db_path() -> Path:
    return data_dir() / "clipmaker.db"


def output_dir() -> Path:
    return data_dir() / "output"


def music_dir() -> Path:
    return data_dir() / "music"


def data_file(name: str) -> Path:
    """A top-level file in the data folder (tokens, settings, sessions)."""
    if not name or Path(name).name != name:
        raise ValueError(f"Expected a bare file name, got {name!r}")
    return data_dir() / name


def models_dir() -> Path:
    return REPO / "models"


def face_model() -> Path:
    """YuNet face detector used for 9:16 reframing (scripts/fetch_models.sh)."""
    return models_dir() / "face_detection_yunet_2023mar.onnx"


def fonts_dir() -> Path:
    return REPO / "fonts"
