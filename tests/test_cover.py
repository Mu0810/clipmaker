"""Cover frame and YouTube thumbnail (task 5.4)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import thumbnail
from media import cover


def ffmpeg(*args) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *map(str, args)],
                   check=True, capture_output=True, timeout=120)


ENCODE = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]


def colours(path: Path, spans, size=(1280, 720)) -> Path:
    """A video made of solid-colour spans, e.g. [("red", 2), ("blue", 2)]."""
    w, h = size
    inputs = []
    for colour, seconds in spans:
        inputs += ["-f", "lavfi", "-i", f"color={colour}:s={w}x{h}:r=25:d={seconds}"]
    joined = "".join(f"[{k}:v]" for k in range(len(spans)))
    ffmpeg(*inputs, "-filter_complex", f"{joined}concat=n={len(spans)}:v=1:a=0",
           *ENCODE, path)
    return path


def blurred_except(path: Path, start: float, end: float, seconds: float = 8,
                   size=(1080, 1920)) -> Path:
    """A detailed test pattern, box-blurred except between `start` and `end`."""
    w, h = size
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=25:duration={seconds}",
           "-vf", f"boxblur=luma_radius=12:luma_power=2:"
                  f"enable='not(between(t,{start},{end}))'",
           *ENCODE, path)
    return path


def mean_rgb(path: Path, box=None):
    with Image.open(path) as im:
        rgb = np.asarray(im.convert("RGB"), dtype=float)
    if box:
        x0, y0, x1, y1 = box
        rgb = rgb[y0:y1, x0:x1]
    return rgb.mean(axis=(0, 1))


def sample(t, variance, face=False):
    return {"t": t, "variance": variance, "face": face}


# --- scoring (pure) -------------------------------------------------------

@pytest.mark.req("3.8")
def test_a_face_frame_at_least_half_as_sharp_wins():
    samples = [sample(k / 2, 100.0) for k in range(1, 20)]
    samples[4] = sample(2.5, 55.0, face=True)            # 0.55 + 0.5 beats 1.0
    best = cover.choose(samples)
    assert best["t"] == 2.5 and best["face"] is True
    assert best["focus"] == 0.55 and best["score"] == 1.05


@pytest.mark.req("3.8")
def test_a_blurry_face_frame_loses_to_a_sharp_one():
    samples = [sample(k / 2, 100.0) for k in range(1, 20)]
    samples[4] = sample(2.5, 30.0, face=True)            # 0.3 + 0.5 < 1.0
    samples[9] = sample(5.0, 120.0)
    best = cover.choose(samples)
    assert best["t"] == 5.0 and best["face"] is False


@pytest.mark.req("3.8")
def test_one_detailed_frame_does_not_flatten_the_rest():
    # 19 face frames at variance 10 and one face-less flash of detail at 100.
    # Dividing by the maximum would give the faces 0.1 + 0.5 < 1.0; the 90th
    # percentile is 10, so they count as fully in focus.
    samples = [sample(k / 2, 10.0, face=True) for k in range(1, 20)]
    samples.append(sample(10.0, 100.0))
    best = cover.choose(samples)
    assert best["face"] is True and best["focus"] == 1.0 and best["score"] == 1.5


def test_ties_go_to_the_sharper_then_the_earlier_frame():
    assert cover.choose([sample(1.0, 50.0), sample(2.0, 50.0), sample(3.0, 49.0)])["t"] == 1.0
    # Both top frames are above the 90th percentile (1.9), so both have focus 1
    # and score 1.0; the raw variance decides.
    samples = [sample(k, 1.0) for k in range(1, 19)]
    samples += [sample(20.0, 10.0), sample(21.0, 12.0)]
    assert cover.choose(samples)["t"] == 21.0


def test_no_samples_and_flat_frames():
    assert cover.choose([]) is None
    best = cover.choose([sample(1.0, 0.0), sample(1.5, 0.0, face=True)])
    assert best["t"] == 1.5 and best["focus"] == 0.0 and best["score"] == 0.5


def test_focus_variance_is_the_variance_of_the_laplacian():
    img = np.zeros((3, 4))
    img[1, 1] = 10.0                     # Laplacian -40 at (1,1), +10 at (1,2)
    assert cover.focus_variance(img) == pytest.approx(625.0)
    bgr = np.repeat(img[..., None], 3, axis=2).astype(np.uint8)
    assert cover.focus_variance(bgr) == pytest.approx(625.0, rel=1e-5)
    i, j = np.indices((64, 64))
    checks = ((i // 8 + j // 8) % 2 * 255).astype(np.uint8)
    flat = np.full((64, 64), 127, np.uint8)
    assert cover.focus_variance(checks) > 1000 > cover.focus_variance(flat) == 0.0
    assert cover.focus_variance(np.zeros((2, 2))) == 0.0


# --- thumbnail.generate() timing --------------------------------------------

def test_thumbnail_generate_takes_the_frame_at_the_given_time(tmp_path):
    video = colours(tmp_path / "rb.mp4", [("red", 2), ("blue", 2)])
    top = (0, 0, 1280, 300)              # above the darkened band and the words
    at = thumbnail.generate(video, tmp_path / "at.jpg", text="", at=3.0)
    r, g, b = mean_rgb(at, top)
    assert b > 200 and r < 40
    # Without `at`, today's rule is unchanged: 30% in, here 1.2 s.
    default = thumbnail.generate(video, tmp_path / "default.jpg", text="", duration=4.0)
    r, g, b = mean_rgb(default, top)
    assert r > 200 and b < 40
    assert not (tmp_path / "at.frame.jpg").exists()


# --- generated videos ------------------------------------------------------

@pytest.mark.req("3.8", "8.9")
def test_the_cover_comes_from_the_sharp_span(tmp_path):
    original = blurred_except(tmp_path / "o.mp4", 3, 5, size=(1280, 720))
    # The vertical (Fit layout) adds a sharp grid for the first 2.5 s, standing
    # in for the hook text burned over the picture.
    vertical = tmp_path / "v.mp4"
    ffmpeg("-i", original, "-vf", "scale=1080:608,pad=1080:1920:0:656,"
           "drawgrid=w=24:h=24:t=2:c=white:enable='between(t,0,2.5)'", *ENCODE, vertical)
    no_faces = lambda frame: []                                  # noqa: E731
    assert cover.choose(cover.score_frames(vertical, detector=no_faces))["t"] <= 2.5

    out = cover.make(vertical, tmp_path / "c.jpg", tmp_path / "t.jpg",
                     title="How we doubled our reach", source=original, detector=no_faces)
    assert 3.0 <= out["t"] <= 5.0
    assert out["chosen"] == "auto" and out["face"] is False
    band = (slice(656, 1264), slice(None))                       # the picture in Fit
    with Image.open(out["cover"]) as im:
        assert im.format == "JPEG" and im.size == (1080, 1920)
        chosen = np.asarray(im.convert("L"))[band]
    with Image.open(cover.write_cover(vertical, 6.5, tmp_path / "blurred.jpg")) as im:
        blurred = np.asarray(im.convert("L"))[band]
    assert cover.focus_variance(chosen) > 5 * cover.focus_variance(blurred)
    assert out["thumb"].stat().st_size <= 2_000_000
    with Image.open(out["thumb"]) as im:
        assert im.format == "JPEG" and im.size == (1280, 720)


@pytest.mark.req("3.8")
def test_the_first_and_last_half_second_are_skipped(tmp_path):
    # Sharp only in the margins, so a choice there would be the sharpest.
    video = tmp_path / "ends.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=360x640:rate=25:duration=6",
           "-vf", "boxblur=luma_radius=8:enable='between(t,0.3,5.7)'", *ENCODE, video)
    samples = cover.score_frames(video, detector=lambda frame: [])
    times = [s["t"] for s in samples]
    assert times[0] == 0.5 and times[-1] == 5.5 and len(times) == 11
    assert 0.5 <= cover.choose(samples)["t"] <= 5.5


def test_a_clip_with_nothing_between_its_margins_is_scored_whole(tmp_path):
    video = colours(tmp_path / "short.mp4", [("green", 0.8)], size=(360, 640))
    assert [s["t"] for s in cover.score_frames(video, detector=lambda frame: [])][0] == 0.0


@pytest.mark.req("3.8")
def test_a_detected_face_decides_between_equally_sharp_frames(tmp_path):
    # Uniform noise is equally sharp everywhere; a red square stands in for a
    # face between 5 and 6 s.
    video = tmp_path / "face.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=gray:s=360x640:r=25:d=8",
           "-vf", "noise=alls=60:allf=t,"
                  "drawbox=x=40:y=40:w=80:h=80:color=red:t=fill:enable='between(t,5,6)'",
           *ENCODE, video)
    shapes = []

    def detector(frame):                           # frames are BGR
        shapes.append(frame.shape)
        patch = frame[60:100, 60:100].astype(float)
        red = patch[..., 2].mean() > 180 and patch[..., :2].mean() < 80
        return [(40.0, 40.0, 80.0, 80.0, 0.9)] if red else []

    out = cover.make(video, tmp_path / "c.jpg", tmp_path / "t.jpg", title="",
                     detector=detector)
    assert 5.0 <= out["t"] <= 6.0 and out["face"] is True
    assert shapes[0] == (640, 360, 3)


def test_the_detector_sees_half_size_frames_of_a_full_size_vertical(tmp_path):
    video = make_vertical(tmp_path / "v.mp4", seconds=2)
    shapes = []
    cover.score_frames(video, detector=lambda frame: shapes.append(frame.shape) or [])
    assert shapes and set(shapes) == {(960, 540, 3)}


def make_vertical(path: Path, seconds: float) -> Path:
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=1080x1920:rate=25:duration={seconds}",
           *ENCODE, path)
    return path


@pytest.mark.req("3.8")
def test_a_time_set_in_the_editor_is_used_without_scoring(tmp_path):
    vertical = colours(tmp_path / "v.mp4", [("red", 2), ("blue", 2)], size=(360, 640))
    original = colours(tmp_path / "o.mp4", [("red", 2), ("blue", 2)])

    def detector(frame):
        raise AssertionError("a caller's time must not be re-scored")

    out = cover.make(vertical, tmp_path / "c.jpg", tmp_path / "t.jpg", title="Two colours",
                     t=3.0, source=original, detector=detector)
    assert out == {"t": 3.0, "cover": tmp_path / "c.jpg", "thumb": tmp_path / "t.jpg",
                   "face": None, "chosen": "manual"}
    r, g, b = mean_rgb(out["cover"])
    assert b > 200 and r < 40
    with Image.open(out["cover"]) as im:
        assert im.size == (1080, 1920)               # scaled up from 360x640
    with Image.open(out["thumb"]) as im:
        assert im.size == (1280, 720)
    r, g, b = mean_rgb(out["thumb"], (0, 0, 1280, 300))
    assert b > 200 and r < 40                        # from the original, at 3.0 s
    late = cover.make(vertical, tmp_path / "c2.jpg", tmp_path / "t2.jpg", t=99.0,
                      detector=detector)
    assert 3.8 <= late["t"] < 4.0 and late["cover"].is_file()


@pytest.mark.req("8.9")
def test_an_oversized_thumbnail_is_re_saved_smaller(tmp_path, monkeypatch):
    video = tmp_path / "noise.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=gray:s=1280x720:r=25:d=2",
           "-vf", "noise=alls=80:allf=t", *ENCODE, video)
    first = cover.write_thumbnail(video, 1.0, tmp_path / "a.jpg", title="Noise test")
    limit = int(first.stat().st_size * 0.6)
    monkeypatch.setattr(cover, "THUMB_MAX_BYTES", limit)
    second = cover.write_thumbnail(video, 1.0, tmp_path / "b.jpg", title="Noise test")
    assert second.stat().st_size <= limit
    with Image.open(second) as im:
        assert im.format == "JPEG" and im.size == (1280, 720)


@pytest.mark.req("8.9")
def test_a_thumbnail_that_cannot_fit_is_dropped(tmp_path, monkeypatch, caplog):
    video = colours(tmp_path / "v.mp4", [("red", 2)])
    monkeypatch.setattr(cover, "THUMB_MAX_BYTES", 1)
    assert cover.write_thumbnail(video, 1.0, tmp_path / "t.jpg", title="Too big") is None
    assert not (tmp_path / "t.jpg").exists()
    assert "YouTube will choose the frame" in caplog.text


def test_a_failed_grab_raises_and_leaves_no_stale_cover(tmp_path):
    stale = tmp_path / "c.jpg"
    stale.write_bytes(b"old")
    with pytest.raises(cover.CoverError):
        cover.write_cover(tmp_path / "missing.mp4", 1.0, stale)
    assert not stale.exists()
    with pytest.raises(cover.CoverError):
        cover.make(tmp_path / "missing.mp4", stale, tmp_path / "t.jpg")


@pytest.mark.req("3.8")
def test_the_default_detector_runs_on_the_samples(tmp_path):
    pytest.importorskip("cv2")
    video = make_vertical(tmp_path / "v.mp4", seconds=3)
    out = cover.make(video, tmp_path / "c.jpg", tmp_path / "t.jpg", title="Default detector")
    assert 0.5 <= out["t"] <= 2.5 and out["chosen"] == "auto"
    assert isinstance(out["face"], bool) and out["thumb"] is not None


# --- the crop window (Fill layout) -------------------------------------------

def test_the_crop_window_follows_the_track():
    track = {"crop_w": 404, "keyframes": [[0.0, 0], [2.0, 876]]}
    assert cover._window(None, 1.0, 640, 1280) is None           # Fit: whole frame
    assert cover._window(track, 1.0, 640, 1280) == (0, 202)       # samples at half size
    assert cover._window(track, 3.0, 640, 1280) == (438, 640)


@pytest.mark.req("3.8")
def test_only_the_crop_window_is_scored(tmp_path):
    from media import reframe

    video = tmp_path / "half.mp4"                # left half blurred, right half sharp
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=3",
           "-filter_complex", "[0:v]split[a][b];[a]crop=640:720:0:0,boxblur=luma_radius=12[l];"
                              "[b]crop=640:720:640:0[r];[l][r]hstack", *ENCODE, video)
    cw = reframe.crop_width(1280, 720)
    shapes = []

    def detector(frame):
        shapes.append(frame.shape)
        return []

    left = cover.score_frames(video, track={"crop_w": cw, "keyframes": [[0.0, 0]]},
                              detector=detector)
    right = cover.score_frames(video, track={"crop_w": cw, "keyframes": [[0.0, 1280 - cw]]},
                               detector=lambda frame: [])
    assert shapes and set(shapes) == {(720, cw, 3)}
    assert min(s["variance"] for s in right) > 5 * max(s["variance"] for s in left)
