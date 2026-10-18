"""Read a media file's technical properties (design §4.6 step 6, §8.8).

`probe()` combines ffprobe's report with a small walk over the MP4 box tree,
which ffprobe does not report: whether the moov box comes before the media
data (Reels require it) and whether any track carries an edit list (Reels
reject them).
"""
from __future__ import annotations

import json
import struct
import subprocess
from fractions import Fraction
from pathlib import Path

FFPROBE_TIMEOUT = 60
_CONTAINERS = {b"moov", b"trak", b"edts", b"mdia", b"minf", b"stbl", b"udta"}


class ProbeError(RuntimeError):
    pass


def _rate(value: str | None) -> float | None:
    if not value or value in ("0/0", "0"):
        return None
    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return round(rate, 3) if rate > 0 else None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _boxes(f, start: int, end: int):
    """Yield (type, payload_start, box_end) for boxes in [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        payload = pos + 8
        if size == 1:                    # 64-bit size follows
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            payload = pos + 16
        elif size == 0:                  # box runs to the end of its parent
            size = end - pos
        if size < 8 or pos + size > end:
            return
        yield kind, payload, pos + size
        pos += size


def mp4_layout(path: str | Path) -> dict:
    """{'moov_before_mdat': bool|None, 'has_edit_list': bool} from the box tree."""
    path = Path(path)
    total = path.stat().st_size
    moov_at = mdat_at = None
    has_elst = False
    with path.open("rb") as f:
        for kind, payload, end in _boxes(f, 0, total):
            if kind == b"moov" and moov_at is None:
                moov_at = payload
                stack = [(payload, end)]
                while stack:
                    s, e = stack.pop()
                    for sub, sub_payload, sub_end in _boxes(f, s, e):
                        if sub == b"elst":
                            has_elst = True
                        elif sub in _CONTAINERS:
                            stack.append((sub_payload, sub_end))
            elif kind == b"mdat" and mdat_at is None:
                mdat_at = payload
    order = None if moov_at is None or mdat_at is None else moov_at < mdat_at
    return {"moov_before_mdat": order, "has_edit_list": has_elst}


def probe(path: str | Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ProbeError(f"File not found: {path.name}")
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProbeError(f"ffprobe failed: {e}") from e
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe could not read {path.name}: {proc.stderr.strip()[-300:]}")
    data = json.loads(proc.stdout or "{}")
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    info = {
        "duration": _float(fmt.get("duration")),
        "bytes": path.stat().st_size,
        "format": fmt.get("format_name"),
        "bitrate": _int(fmt.get("bit_rate")),
        "width": _int((video or {}).get("width")),
        "height": _int((video or {}).get("height")),
        "fps": _rate((video or {}).get("avg_frame_rate")) or _rate((video or {}).get("r_frame_rate")),
        "video_codec": (video or {}).get("codec_name"),
        "pix_fmt": (video or {}).get("pix_fmt"),
        "field_order": (video or {}).get("field_order"),
        "video_bitrate": _int((video or {}).get("bit_rate")),
        "audio_codec": (audio or {}).get("codec_name"),
        "sample_rate": _int((audio or {}).get("sample_rate")),
        "channels": _int((audio or {}).get("channels")),
    }
    if info["duration"] is None and video:
        info["duration"] = _float(video.get("duration"))
    if fmt.get("format_name", "").startswith(("mov", "mp4")):
        info.update(mp4_layout(path))
    else:
        info.update({"moov_before_mdat": None, "has_edit_list": False})
    return info
