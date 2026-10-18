"""Generate small test videos with ffmpeg's built-in sources."""
from __future__ import annotations

import subprocess
from pathlib import Path


def make_video(path: Path, seconds: float = 2.0, size: tuple[int, int] = (320, 240),
               fps: int = 25, audio: bool = True, extra: list[str] | None = None) -> Path:
    """Write an H.264/AAC test clip: a moving test pattern plus a 440 Hz tone."""
    w, h = size
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    cmd += list(extra or []) + [str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return path
