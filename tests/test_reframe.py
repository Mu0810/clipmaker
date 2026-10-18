"""Face-tracked reframing (task 5.3)."""
from __future__ import annotations

import math
import re
import subprocess

import numpy as np
import pytest

from media import reframe as rf
from tests.helpers.media import make_video

SRC = (1920, 1080)             # the crop is 608 px wide; its left edge runs 0-1312
FACE_W, FACE_H = 120, 160


def face(cx, cy=400.0):
    return (cx - FACE_W / 2, cy, FACE_W, FACE_H, 0.9)


def scripted(centre, seconds=10.0, fps=4, cuts=()):
    """4 fps samples with one face centred at centre(t) (None: not found)."""
    out = []
    for k in range(int(seconds * fps)):
        t = k / fps
        cx = centre(t)
        out.append({"t": t, "cut": any(abs(t - c) < 1e-9 for c in cuts),
                    "faces": [] if cx is None else [face(cx)]})
    return out


def plan(samples, src=SRC):
    return rf.plan(samples, *src)


def keys(track):
    return [tuple(k) for k in track["keyframes"]]


@pytest.mark.req("3.2")
def test_a_still_face_gets_one_fixed_crop():
    track = plan(scripted(lambda t: 900))
    assert keys(track) == [(0.0, 596)]
    assert (track["crop_w"], track["crop_h"], track["face_in_crop"]) == (608, 1080, 1.0)


@pytest.mark.req("3.2")
def test_small_movements_inside_the_safe_zone_hold_the_crop():
    track = plan(scripted(lambda t: 900 + 60 * math.sin(3 * t)))
    assert keys(track) == [(0.0, 596)]


@pytest.mark.req("3.2")
def test_leaving_the_safe_zone_eases_the_crop_over_0_4_seconds():
    # The face drifts right 50 px per sample from t = 5 s and stops at x = 1400.
    track = plan(scripted(lambda t: min(1400, 900 + 200 * max(0.0, t - 4.75))))
    k = keys(track)
    assert k[0] == (0.0, 596) and len(k) == 13
    first, second = k[1:7], k[7:13]      # the face leaves the zone at 5.75 s and 6.75 s
    assert [t for t, _ in first] == pytest.approx([5.55 + i / 15 for i in range(1, 7)],
                                                  abs=1e-3)
    assert [t for t, _ in second] == pytest.approx([6.55 + i / 15 for i in range(1, 7)],
                                                   abs=1e-3)
    assert (first[-1][1], second[-1][1]) == (796, 996)
    xs = [x for _, x in k]
    assert xs == sorted(xs) and all(x % 2 == 0 for x in xs)
    assert rf.x_at(track, 5.54) == 596 and rf.x_at(track, 9.9) == 996


@pytest.mark.req("3.2")
def test_a_shot_cut_jumps_straight_to_the_new_face_and_clamps_to_the_frame():
    track = plan(scripted(lambda t: 900 if t < 6 else 200, cuts=(6.0,)))
    assert keys(track) == [(0.0, 596), (5.875, 0)]
    assert track["shots"] == 2


@pytest.mark.req("3.2")
def test_the_crop_never_leaves_the_frame():
    assert keys(plan(scripted(lambda t: 1830))) == [(0.0, 1312)]


@pytest.mark.req("3.2")
def test_a_moving_face_stays_in_the_crop_in_at_least_95_percent_of_samples():
    samples = scripted(lambda t: 300 + 130 * t)          # crosses the frame in 10 s
    track = plan(samples)
    cw = track["crop_w"]
    inside = [rf.x_at(track, s["t"]) <= s["faces"][0][0] and
              s["faces"][0][0] + FACE_W <= rf.x_at(track, s["t"]) + cw for s in samples]
    assert sum(inside) / len(inside) >= 0.95 and track["face_in_crop"] >= 0.95
    assert len(track["keyframes"]) > 1


@pytest.mark.req("3.2", "3.3")
@pytest.mark.parametrize("cut_off,kept", [(1, True), (4, False)])
def test_the_face_must_stay_in_the_crop_95_percent_of_the_time(cut_off, kept):
    # Centred at 1900 the face runs past the frame's right edge, so no crop can
    # hold all of it: 1 such sample in 40 is 97.5% in frame, 4 is only 90%.
    track = plan(scripted(lambda t: 1900 if int(t * 4) < cut_off else 1850))
    assert (track is not None) is kept
    if kept:
        assert track["face_in_crop"] == 0.975


@pytest.mark.req("3.3")
def test_no_face_means_fit():
    assert plan(scripted(lambda t: None)) is None


@pytest.mark.req("3.3")
def test_a_source_already_9_16_needs_no_crop():
    assert rf.crop_width(1080, 1920) is None and rf.crop_width(720, 1280) is None
    assert rf.crop_width(1920, 1080) == 608
    assert plan(scripted(lambda t: 500), src=(1080, 1920)) is None


@pytest.mark.req("3.2")
def test_a_shot_without_a_face_is_centred():
    track = plan(scripted(lambda t: 900 if t < 5 else None, cuts=(5.0,)))
    assert keys(track) == [(0.0, 596), (4.875, 656)]


@pytest.mark.req("3.2")
def test_missed_detections_hold_the_crop():
    assert keys(plan(scripted(lambda t: None if 2 <= t < 4 else 900))) == [(0.0, 596)]


@pytest.mark.req("3.2")
def test_the_primary_face_is_the_one_seen_most_often():
    samples = scripted(lambda t: 500)
    for k, s in enumerate(samples):
        if k % 2 == 0:
            s["faces"].append((1400, 300, 300, 400, 0.9))   # bigger, but seen half as often
    assert keys(plan(samples)) == [(0.0, 196)]


@pytest.mark.req("3.2")
def test_ties_go_to_the_larger_face():
    samples = scripted(lambda t: 500)
    for s in samples:
        s["faces"].append((1400, 300, 300, 400, 0.9))
    assert keys(plan(samples)) == [(0.0, 1246)]


@pytest.mark.req("3.2")
def test_sendcmd_lines_are_well_formed(tmp_path):
    track = plan(scripted(lambda t: 300 + 130 * t, cuts=(6.0,)))
    lines = rf.write_sendcmd(track, tmp_path / "crop.cmd").read_text().splitlines()
    parsed = [re.fullmatch(r"(\d+\.\d{3}) crop@reframe x (\d+);", line) for line in lines]
    assert all(parsed) and len(lines) == len(track["keyframes"])
    times = [float(m.group(1)) for m in parsed]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert all(0 <= int(m.group(2)) <= 1312 and int(m.group(2)) % 2 == 0 for m in parsed)


# ── ffmpeg, sampling and detectors ───────────────────────────────────────

def ffmpeg(*args, cwd=None):
    return subprocess.run(["ffmpeg", "-v", "error", "-y", *args], cwd=cwd, check=True,
                          capture_output=True, timeout=120).stdout


def colour_split(path):
    """3 s at 10 fps, 640x360: red on the left half, blue on the right."""
    ffmpeg("-f", "lavfi", "-i", "color=red:s=320x360:r=10:d=3",
           "-f", "lavfi", "-i", "color=blue:s=320x360:r=10:d=3",
           "-filter_complex", "hstack", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", str(path))
    return path


@pytest.mark.req("3.2")
def test_ffmpeg_moves_the_crop_when_the_command_file_says(tmp_path):
    video = colour_split(tmp_path / "split.mp4")
    track = {"crop_w": 202, "crop_h": 360, "keyframes": [[0.0, 0], [1.5, 438]]}
    rf.write_sendcmd(track, tmp_path / "crop.cmd")
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video),
                          "-vf", rf.filter_chain(track, "crop.cmd"),
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         cwd=tmp_path, capture_output=True, check=True, timeout=120).stdout
    size = 202 * 360 * 3
    frames = [np.frombuffer(out[k * size:(k + 1) * size], np.uint8).reshape(360, 202, 3)
              for k in range(len(out) // size)]
    assert len(frames) == 30
    assert frames[5].mean(axis=(0, 1)).argmax() == 0          # t = 0.5 s: left, red
    assert frames[25].mean(axis=(0, 1)).argmax() == 2         # t = 2.5 s: right, blue


def test_filter_chain_needs_a_plain_file_name():
    with pytest.raises(ValueError):
        rf.filter_chain({"crop_w": 2, "crop_h": 2, "keyframes": [[0.0, 0]]}, "a b/c'.cmd")


@pytest.mark.req("3.2")
def test_analyze_samples_the_video_and_finds_the_shot_cut(tmp_path):
    video = tmp_path / "two_shots.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=red:s=1280x720:r=25:d=2",
           "-f", "lavfi", "-i", "color=blue:s=1280x720:r=25:d=2",
           "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", "-c:v", "libx264",
           "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video))
    shapes = []

    def detector(frame):                                      # frames are BGR
        shapes.append(frame.shape)
        red = frame[..., 2].mean() > frame[..., 0].mean()
        return [(100, 100, 40, 50, 0.9)] if red else [(500, 100, 40, 50, 0.9)]

    track = rf.analyze(video, detector=detector)
    assert shapes[0] == (360, 640, 3) and 15 <= len(shapes) <= 17
    assert track["shots"] == 2 and track["crop_w"] == 404
    k = keys(track)
    assert k[0] == (0.0, 38)                 # red-shot face centred at 240 px
    assert len(k) == 2 and 1.7 < k[1][0] < 2.1 and k[1][1] == 838


@pytest.mark.req("3.3")
def test_analyze_without_faces_returns_none(tmp_path):
    video = make_video(tmp_path / "v.mp4", seconds=2, size=(640, 360))
    assert rf.analyze(video, detector=lambda frame: []) is None


@pytest.mark.req("3.2")
def test_the_face_detectors_run_on_a_frame():
    pytest.importorskip("cv2")
    import paths

    frame = np.full((360, 640, 3), 127, np.uint8)
    haar = rf.haar_detector()
    assert haar is not None and haar(frame) == []
    if not paths.face_model().is_file():
        pytest.skip("The face model isn't downloaded (run scripts/fetch_models.sh).")
    assert rf.yunet_detector(paths.face_model())(frame) == []
    assert rf.default_detector() is not None


def test_the_haar_cascade_is_used_when_the_model_is_missing(monkeypatch, tmp_path):
    pytest.importorskip("cv2")
    import paths

    monkeypatch.setattr(paths, "face_model", lambda: tmp_path / "missing.onnx")
    used = []
    monkeypatch.setattr(rf, "haar_detector", lambda: used.append(True) or (lambda f: []))
    assert rf.default_detector() is not None and used == [True]
