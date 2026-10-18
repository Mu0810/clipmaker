"""Face-tracked 9:16 reframing for the Fill layout (design §4.6 step 2;
Requirements 3.2, 3.3).

analyze() samples a clip at 4 fps (640 px wide), finds faces with OpenCV's
YuNet detector (or its bundled Haar cascade when the model file is missing),
and plans where a full-height 9:16 crop window sits over time:

* a shot cut is where the HSV colour histograms of consecutive samples
  correlate below 0.6; face tracks never cross a cut;
* within a shot, a face is linked to an earlier detection when their boxes
  overlap with IoU >= 0.3; the primary face is the track seen in the most
  samples (ties go to the larger face);
* the crop holds still while the primary face stays inside its central 70%.
  When the face leaves that zone, the crop eases over 0.4 s to re-centre on
  it, starting 0.2 s before the sample that saw it leave (the whole clip is
  known in advance). At a shot cut it jumps to the new shot's face, half-way
  between the samples either side of the cut. A shot with no face is
  centred. The crop never leaves the frame;
* the plan is used only if it keeps the face inside the crop in at least
  95% of the samples where the face was found (Requirement 3.2).

analyze() and plan() return None, meaning "use the Fit layout" (3.3), when no
shot has a face, when the plan can't keep the face in frame, when the source
is already 9:16 or narrower, or when no detector is available.

The result drives ffmpeg's crop filter through sendcmd (write_sendcmd and
filter_chain; run ffmpeg in the folder holding the command file) and is
saved as JSON for the editor's preview (write_track).
"""
from __future__ import annotations

import bisect
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Callable, Iterator

import paths

log = logging.getLogger(__name__)

SAMPLE_FPS = 4.0
SAMPLE_WIDTH = 640
CUT_CORRELATION = 0.6
LINK_IOU = 0.3
SAFE_ZONE = 0.70
EASE_S = 0.4
EASE_STEP_S = 1 / 15
MIN_FACE_IN_CROP = 0.95
SCORE_THRESHOLD = 0.6
CROP_FILTER = "crop@reframe"
_PLAIN_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class ReframeError(RuntimeError):
    pass


def _even(value: float) -> int:
    return int(round(value / 2)) * 2


def crop_width(src_w: int, src_h: int) -> int | None:
    """Width of a full-height 9:16 crop, or None when the source is already
    9:16 or narrower (there is nothing to crop sideways)."""
    cw = _even(src_h * 9 / 16)
    return cw if 0 < cw < src_w else None


def iou(a, b) -> float:
    iw = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def shots(samples: list[dict]) -> list[tuple[int, int]]:
    """Sample index ranges [a, b), split where a sample is marked as a cut."""
    out, start = [], 0
    for i in range(1, len(samples)):
        if samples[i].get("cut"):
            out.append((start, i))
            start = i
    if samples:
        out.append((start, len(samples)))
    return out


def primary_track(samples: list[dict], a: int, b: int) -> dict[int, tuple]:
    """The primary face's box at each sample of one shot where it was found."""
    tracks: list[dict[int, tuple]] = []
    for i in range(a, b):
        faces = [tuple(float(v) for v in f[:4]) for f in samples[i].get("faces") or ()]
        pairs = sorted(((iou(t[max(t)], f), ti, fi) for ti, t in enumerate(tracks)
                        for fi, f in enumerate(faces)), reverse=True)
        used_tracks, used_faces = set(), set()
        for score, ti, fi in pairs:
            if score < LINK_IOU:
                break
            if ti not in used_tracks and fi not in used_faces:
                tracks[ti][i] = faces[fi]
                used_tracks.add(ti)
                used_faces.add(fi)
        tracks.extend({i: f} for fi, f in enumerate(faces) if fi not in used_faces)
    if not tracks:
        return {}
    return max(tracks, key=lambda t: (len(t), sum(b[2] * b[3] for b in t.values()) / len(t)))


def _inside(box, x: float, cw: float, zone: float = 1.0) -> bool:
    """Whether the box is inside the central `zone` share of the crop at x. A
    box wider than that zone counts as inside while its centre is."""
    margin = cw * (1 - zone) / 2
    lo, hi = x + margin, x + cw - margin
    if box[2] <= hi - lo:
        return lo <= box[0] and box[0] + box[2] <= hi
    return lo <= box[0] + box[2] / 2 <= hi


def _x_at(keys: list[tuple[float, int]], t: float) -> int:
    i = bisect.bisect_right([k[0] for k in keys], t) - 1
    return keys[max(i, 0)][1]


def _ease(t0: float, x0: int, x1: int, duration: float) -> list[tuple[float, int]]:
    """Smoothstep from x0 to x1, one command every 1/15 s."""
    if duration < EASE_STEP_S:
        return [(t0, x1)]
    steps = max(1, round(duration / EASE_STEP_S))
    out = []
    for k in range(1, steps + 1):
        u = k / steps
        x = x1 if k == steps else _even(x0 + (x1 - x0) * u * u * (3 - 2 * u))
        out.append((t0 + duration * u, x))
    return out


def _clean(keys) -> list[tuple[float, int]]:
    """Times rounded to the millisecond; same-instant commands collapse to the
    last one, and commands that change nothing are dropped."""
    timed: list[tuple[float, int]] = []
    for t, x in keys:
        t = round(t, 3)
        if timed and t <= timed[-1][0]:
            timed[-1] = (timed[-1][0], x)
        else:
            timed.append((t, x))
    out: list[tuple[float, int]] = []
    for t, x in timed:
        if not out or out[-1][1] != x:
            out.append((t, x))
    return out


def _path(samples, ranges, primaries, src_w: int, cw: int) -> list[tuple[float, int]]:
    max_x = src_w - cw

    def clamp(x: int) -> int:
        return max(0, min(max_x, x))

    def centred(box) -> int:
        return clamp(_even(box[0] + box[2] / 2 - cw / 2))

    starts = [0.0] + [(samples[a - 1]["t"] + samples[a]["t"]) / 2 for a, _ in ranges[1:]]
    keys: list[tuple[float, int]] = []
    for k, ((a, b), prim) in enumerate(zip(ranges, primaries)):
        limit = starts[k + 1] if k + 1 < len(starts) else float("inf")
        target = centred(prim[min(prim)]) if prim else clamp(_even(max_x / 2))
        keys.append((starts[k], target))
        busy = starts[k]
        for i in range(a + 1, b):
            box = prim.get(i)
            if box is None or _inside(box, target, cw, SAFE_ZONE):
                continue
            new = centred(box)
            if new == target:
                continue
            t0 = max(samples[i]["t"] - EASE_S / 2, busy)
            if t0 >= limit:
                break
            duration = min(EASE_S, limit - t0)
            keys.extend(_ease(t0, _x_at(keys, t0), new, duration))
            target, busy = new, t0 + duration
    return _clean(keys)


def plan(samples: list[dict], src_w: int, src_h: int, fps: float = SAMPLE_FPS) -> dict | None:
    """Plan the crop from samples [{"t", "cut", "faces": [(x, y, w, h[, score])]}]
    (faces in source pixels). None means "use the Fit layout"."""
    cw = crop_width(src_w, src_h)
    if cw is None:
        log.info("The video is already 9:16 or narrower; no crop is needed.")
        return None
    ranges = shots(samples)
    primaries = [primary_track(samples, a, b) for a, b in ranges]
    if not any(primaries):
        log.info("No face was found, so this clip uses the Fit layout.")
        return None
    keys = _path(samples, ranges, primaries, src_w, cw)
    found = [(samples[i]["t"], box) for prim in primaries for i, box in prim.items()]
    inside = sum(_inside(box, _x_at(keys, t), cw) for t, box in found)
    share = inside / len(found)
    if share < MIN_FACE_IN_CROP:
        log.info("A 9:16 crop would keep the face in frame only %.0f%% of the time, "
                 "so this clip uses the Fit layout.", share * 100)
        return None
    return {"v": 1, "src_w": src_w, "src_h": src_h, "crop_w": cw, "crop_h": src_h,
            "sample_fps": fps, "samples": len(samples), "shots": len(ranges),
            "face_samples": len(found), "face_in_crop": round(share, 4),
            "keyframes": [[t, x] for t, x in keys]}


def x_at(track: dict, t: float) -> int:
    """The crop's left edge at clip time t."""
    return _x_at([tuple(k) for k in track["keyframes"]], t)


def sendcmd_text(track: dict) -> str:
    return "".join(f"{t:.3f} {CROP_FILTER} x {x};\n" for t, x in track["keyframes"])


def write_sendcmd(track: dict, path) -> Path:
    path = Path(path)
    path.write_text(sendcmd_text(track), encoding="utf-8")
    return path


def write_track(track: dict, path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(track, separators=(",", ":")), encoding="utf-8")
    return path


def filter_chain(track: dict, cmd_name: str) -> str:
    """The sendcmd + crop filters for ffmpeg. `cmd_name` is the command file's
    plain name; run ffmpeg in its folder, which avoids filtergraph escaping."""
    if not _PLAIN_NAME.match(cmd_name):
        raise ValueError("Use a plain file name in ffmpeg's working folder.")
    x0 = track["keyframes"][0][1]
    return (f"sendcmd=f={cmd_name},"
            f"{CROP_FILTER}=w={track['crop_w']}:h={track['crop_h']}:x={x0}:y=0")


# ── detection and sampling ───────────────────────────────────────────────

Detector = Callable[[object], list]


def yunet_detector(model_path, score_threshold: float = SCORE_THRESHOLD) -> Detector:
    """OpenCV's YuNet face detector. Returns (x, y, w, h, score) boxes."""
    import cv2

    net = cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), score_threshold, 0.3, 50)
    size = [None]

    def detect(frame) -> list:
        h, w = frame.shape[:2]
        if size[0] != (w, h):
            net.setInputSize((w, h))
            size[0] = (w, h)
        _, faces = net.detect(frame)
        if faces is None:
            return []
        return [(float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1]))
                for f in faces]

    return detect


def haar_detector() -> Detector | None:
    """OpenCV's bundled frontal-face Haar cascade (less accurate than YuNet)."""
    import cv2

    cascade = cv2.CascadeClassifier(
        str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    if cascade.empty():
        return None

    def detect(frame) -> list:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        found = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(24, 24))
        return [(float(x), float(y), float(w), float(h), 1.0) for x, y, w, h in found]

    return detect


def default_detector() -> Detector | None:
    """YuNet when its model is downloaded, else the Haar cascade; None
    without OpenCV."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return None
    model = paths.face_model()
    if model.is_file():
        try:
            return yunet_detector(model)
        except Exception:
            log.warning("Couldn't load the face model %s; using the Haar cascade.",
                        model, exc_info=True)
    return haar_detector()


def sample_size(src_w: int, src_h: int, width: int = SAMPLE_WIDTH) -> tuple[int, int]:
    w = max(2, min(_even(width), _even(src_w)))
    return w, max(2, _even(w * src_h / src_w))


def sample_frames(video, src_w: int, src_h: int, fps: float = SAMPLE_FPS,
                  width: int = SAMPLE_WIDTH) -> Iterator[tuple[float, object]]:
    """(time, BGR frame) pairs, `fps` per second, read from an ffmpeg pipe."""
    import numpy as np

    w, h = sample_size(src_w, src_h, width)
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-an", "-sn",
           "-vf", f"fps={fps:g},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_bytes = w * h * 3
    count = 0
    try:
        while True:
            data = proc.stdout.read(frame_bytes)
            if len(data) < frame_bytes:
                break
            yield count / fps, np.frombuffer(data, np.uint8).reshape(h, w, 3)
            count += 1
    finally:
        proc.stdout.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        error = proc.stderr.read().decode("utf-8", "replace").strip()
        proc.stderr.close()
    if count == 0 and proc.returncode:
        raise ReframeError(f"ffmpeg couldn't read the video: {error[-300:]}")


def _histogram(frame):
    import cv2

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
    return cv2.normalize(hist, hist)


def _correlation(a, b) -> float:
    import cv2

    return float(cv2.compareHist(a, b, cv2.HISTCMP_CORREL))


def analyze(video, *, detector: Detector | None = None, src_w: int | None = None,
            src_h: int | None = None, fps: float = SAMPLE_FPS, width: int = SAMPLE_WIDTH,
            progress: Callable[[float], None] | None = None) -> dict | None:
    """Plan the 9:16 crop for an original-ratio clip; None means use Fit."""
    video = Path(video)
    if src_w is None or src_h is None:
        from media.probe import probe

        info = probe(video)
        src_w, src_h = info["width"], info["height"]
        if not src_w or not src_h:
            raise ReframeError("The video has no picture to reframe.")
    if crop_width(src_w, src_h) is None:
        log.info("The video is already 9:16 or narrower; no crop is needed.")
        return None
    detector = detector or default_detector()
    if detector is None:
        log.info("No face detector is available, so this clip uses the Fit layout.")
        return None
    try:
        import cv2  # noqa: F401
        find_cuts = True
    except ImportError:
        find_cuts = False
    scale = src_w / sample_size(src_w, src_h, width)[0]
    samples: list[dict] = []
    previous = None
    for t, frame in sample_frames(video, src_w, src_h, fps, width):
        cut = False
        if find_cuts:
            hist = _histogram(frame)
            cut = previous is not None and _correlation(previous, hist) < CUT_CORRELATION
            previous = hist
        faces = [tuple(v * scale for v in f[:4]) + tuple(f[4:5]) for f in detector(frame)]
        samples.append({"t": round(t, 3), "cut": cut, "faces": faces})
        if progress:
            progress(t)
    if not samples:
        return None
    return plan(samples, src_w, src_h, fps)
