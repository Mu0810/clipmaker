"""Cover frame and YouTube thumbnail for a rendered clip (design §4.6 step 5,
Requirements 3.8 and 8.9).

choose() picks the cover from samples taken at 2 fps, leaving out the
clip's first and last 0.5 s. The samples come from the original-ratio clip
when the caller passes it, inside the crop window for the Fill layout: the
captions and hook burned into the vertical are sharp text, and scoring the
vertical would let them count as focus, so a blurred shot under the hook
could win. Each sample scores

    focus + 0.5 when a face is detected in it

where focus is the frame's Laplacian variance (computed with numpy) divided
by the clip's 90th-percentile variance and capped at 1. Every frame among
the clip's sharpest tenth therefore counts as fully in focus, and one
unusually detailed frame (a flash of on-screen text, a cut to a crowd) can't
push every other frame's focus toward 0, as dividing by the maximum would.
A face frame beats any frame without a face when it is at least half as
sharp as that sharpest tenth; a blurrier face frame loses to a sharp frame
without one. Ties go to the higher raw variance, then the earlier frame.

make() writes the 1080x1920 cover JPEG from the vertical, and the 1280x720
YouTube thumbnail through thumbnail.generate() at the same time with the
title's key words. The thumbnail comes from the original-ratio clip too:
both files share the clip's timeline, and a 9:16 frame fitted to 16:9 would
keep only its middle third. A thumbnail over 2 MB is
re-saved at lower JPEG quality; one that still doesn't fit is dropped, and
YouTube chooses the frame (8.9).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Callable

from PIL import Image

import thumbnail
from media import reframe
from media.probe import ProbeError, probe

log = logging.getLogger(__name__)

SAMPLE_FPS = 2.0
SAMPLE_WIDTH = 540              # half the vertical's width: 540x960 samples
MARGIN_S = 0.5
FACE_BONUS = 0.5
FOCUS_PERCENTILE = 90
COVER_SIZE = (1080, 1920)
END_GUARD_S = 0.1               # a grab at the very end can land past the last frame
# YouTube's custom-thumbnail limit is 2 MB. Read as 2,000,000 bytes, so the
# file fits whichever meaning of "MB" YouTube applies.
THUMB_MAX_BYTES = 2_000_000
RESAVE_QUALITIES = (80, 70, 60, 50, 40)

Detector = Callable[[object], list]


class CoverError(RuntimeError):
    """The cover couldn't be chosen or written."""


def focus_variance(frame) -> float:
    """Variance of the 4-neighbour Laplacian of a BGR frame's luma: high for
    a sharp frame, low for a blurred one."""
    import numpy as np

    img = np.asarray(frame, dtype=np.float32)
    if img.ndim == 3:
        img = 0.114 * img[..., 0] + 0.587 * img[..., 1] + 0.299 * img[..., 2]
    if img.shape[0] < 3 or img.shape[1] < 3:
        return 0.0
    lap = (img[:-2, 1:-1] + img[2:, 1:-1] + img[1:-1, :-2] + img[1:-1, 2:]
           - 4.0 * img[1:-1, 1:-1])
    return float(lap.var())


def choose(samples: list[dict]) -> dict | None:
    """The best sample, with its `focus` and `score` added; None for no
    samples. A sample is {"t": seconds, "variance": focus_variance(),
    "face": bool}."""
    if not samples:
        return None
    import numpy as np

    ref = float(np.percentile([s["variance"] for s in samples], FOCUS_PERCENTILE))
    best = best_key = None
    for s in samples:
        focus = min(1.0, s["variance"] / ref) if ref > 0 else 0.0
        score = focus + (FACE_BONUS if s["face"] else 0.0)
        key = (score, s["variance"], -s["t"])
        if best_key is None or key > best_key:
            best, best_key = {**s, "focus": round(focus, 4), "score": round(score, 4)}, key
    return best


def _window(track: dict | None, t: float, sample_w: int, src_w: int) -> tuple[int, int] | None:
    """The crop window at clip time `t` in sample pixels; None for the whole frame."""
    if not track:
        return None
    scale = sample_w / src_w
    width = max(1, min(sample_w, round(track["crop_w"] * scale)))
    left = min(max(0, round(reframe.x_at(track, t) * scale)), sample_w - width)
    return left, left + width


def score_frames(video, *, track: dict | None = None, detector: Detector | None = None,
                 duration: float | None = None, size: tuple[int, int] | None = None,
                 fps: float = SAMPLE_FPS, margin: float = MARGIN_S,
                 width: int = SAMPLE_WIDTH,
                 progress: Callable[[float], None] | None = None) -> list[dict]:
    """Sample `video` at `fps` and measure each frame's focus and whether a
    face is in it, leaving out the first and last `margin` seconds. With a
    crop `track` (reframe.plan) only the window the vertical shows is
    measured. The measured area is about `width` pixels wide. A clip too
    short to have a sample between its margins is scored whole. `detector`
    defaults to reframe.default_detector(); without one the choice rests on
    focus alone."""
    video = Path(video)
    if duration is None or size is None:
        try:
            info = probe(video)
        except ProbeError as exc:
            raise CoverError(str(exc)) from exc
        duration = info["duration"] if duration is None else duration
        size = size or (info["width"], info["height"])
    src_w, src_h = size
    if not src_w or not src_h:
        raise CoverError("The video has no picture to take a cover from.")
    visible = track["crop_w"] if track else src_w
    sample_w = min(src_w, round(width * src_w / visible))
    if detector is None:
        detector = reframe.default_detector()
        if detector is None:
            log.info("No face detector is available, so the cover is chosen on focus alone.")
    samples: list[dict] = []
    try:
        for t, frame in reframe.sample_frames(video, src_w, src_h, fps, sample_w):
            window = _window(track, t, frame.shape[1], src_w)
            if window:
                import numpy as np

                frame = np.ascontiguousarray(frame[:, window[0]:window[1]])
            samples.append({"t": round(t, 3), "variance": focus_variance(frame),
                            "face": bool(detector(frame)) if detector else False})
            if progress:
                progress(t)
    except reframe.ReframeError as exc:
        raise CoverError(str(exc)) from exc
    if duration:
        inside = [s for s in samples
                  if margin - 1e-6 <= s["t"] <= duration - margin + 1e-6]
        samples = inside or samples
    return samples


def write_cover(video, t: float, out_path, size: tuple[int, int] = COVER_SIZE) -> Path:
    """Write the frame at `t` seconds as a JPEG of exactly `size`. The frame
    is scaled to fill and centre-cropped, so a source of a slightly different
    shape is never stretched."""
    w, h = size
    out_path = Path(out_path)
    out_path.unlink(missing_ok=True)          # a stale file must not pass the check
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}",
           "-i", str(video), "-frames:v", "1", "-vf", vf, "-q:v", "3",
           "-update", "1", str(out_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoverError(f"ffmpeg couldn't write the cover: {exc}") from exc
    if proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        raise CoverError(f"ffmpeg couldn't write the cover at {t:.2f} s: "
                         f"{proc.stderr.strip()[-300:]}")
    return out_path


def _fit_size(path: Path, limit: int) -> bool:
    """Re-save the JPEG at falling quality until it is at most `limit` bytes."""
    if path.stat().st_size <= limit:
        return True
    with Image.open(path) as im:
        img = im.convert("RGB")
    for quality in RESAVE_QUALITIES:
        img.save(path, "JPEG", quality=quality, optimize=True)
        if path.stat().st_size <= limit:
            log.info("Re-saved the thumbnail at JPEG quality %d to fit in %d bytes.",
                     quality, limit)
            return True
    return False


def write_thumbnail(video, t: float, out_path, title: str = "") -> Path | None:
    """The 1280x720 YouTube thumbnail: the frame at `t` with the title's key
    words, at most THUMB_MAX_BYTES. None means there is no thumbnail and
    YouTube chooses the frame (Requirement 8.9)."""
    out_path = Path(out_path)
    out_path.unlink(missing_ok=True)
    text = thumbnail.thumb_text(title) if (title or "").strip() else ""
    if thumbnail.generate(Path(video), out_path, text=text, at=t) is None:
        log.warning("Couldn't make a thumbnail from %s; YouTube will choose the frame.",
                    Path(video).name)
        return None
    if not _fit_size(out_path, THUMB_MAX_BYTES):
        out_path.unlink(missing_ok=True)
        log.warning("The thumbnail stayed over %d bytes at JPEG quality %d, so it was "
                    "dropped; YouTube will choose the frame.",
                    THUMB_MAX_BYTES, RESAVE_QUALITIES[-1])
        return None
    return out_path


def make(vertical, cover_path, thumb_path, *, title: str = "", t: float | None = None,
         source=None, track: dict | None = None, detector: Detector | None = None,
         progress: Callable[[float], None] | None = None) -> dict:
    """Choose the cover time (or take the caller's `t`, set in the editor),
    then write the cover from the vertical and the thumbnail.

    `source` is the original-ratio clip, which shares the vertical's
    timeline. When given, frames are scored on it, inside `track`'s crop
    window for the Fill layout (the whole frame for Fit), so the captions
    and hook burned into the vertical don't count as detail; the thumbnail
    comes from it too. Without it, the vertical is scored and used.

    Returns {"t", "cover", "thumb", "face", "chosen"}: `t` is clip time in
    seconds to the millisecond (Instagram's thumb_offset), `thumb` is None
    when no thumbnail could be made within 2 MB, `face` says whether a face
    was detected in the chosen frame (None for a caller's time), and `chosen`
    is "auto" or "manual"."""
    vertical = Path(vertical)
    try:
        info = probe(vertical)
    except ProbeError as exc:
        raise CoverError(str(exc)) from exc
    duration = info.get("duration") or 0.0
    best = None
    if t is None:
        scored = Path(source) if source else vertical
        best = choose(score_frames(scored, track=track if source else None,
                                   detector=detector, progress=progress))
        if best is None:
            raise CoverError("No frame could be read to choose the cover from.")
        cover_t, face, chosen = best["t"], best["face"], "auto"
    else:
        cover_t, face, chosen = float(t), None, "manual"
    if duration:
        cover_t = min(max(0.0, cover_t), max(0.0, duration - END_GUARD_S))
    cover_t = round(cover_t, 3)
    cover = write_cover(vertical, cover_t, cover_path)
    thumb = None
    if thumb_path is not None:
        thumb = write_thumbnail(source or vertical, cover_t, thumb_path, title)
    if best is not None:
        log.info("Cover at %.2f s (focus %.2f, face %s).", cover_t, best["focus"],
                 "found" if face else "not found")
    return {"t": cover_t, "cover": cover, "thumb": thumb, "face": face, "chosen": chosen}
