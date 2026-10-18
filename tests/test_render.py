"""EditSpec renderer (task 5.5)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from media import render

ENCODE = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]


def ffmpeg(*args) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *map(str, args)],
                   check=True, capture_output=True, timeout=180)


def source(path: Path, *, seconds=12, fps=25, video=None, audio="sine=frequency=440"
           ":sample_rate=44100") -> Path:
    """A 1280x720 source; `video` is a lavfi graph (default: test pattern)."""
    video = video or f"testsrc2=size=1280x720:rate={fps}"
    args = ["-f", "lavfi", "-t", str(seconds), "-i", video]
    if audio:
        args += ["-f", "lavfi", "-t", str(seconds), "-i", audio]
    ffmpeg(*args, *ENCODE, *(["-c:a", "aac"] if audio else []), path)
    return path


def streams(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-print_format", "json", "-show_entries",
         "stream=codec_type,codec_name,pix_fmt,field_order,r_frame_rate,sample_rate,"
         "channels,nb_read_frames,duration", str(path)],
        check=True, capture_output=True, text=True, timeout=120).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


def frames(path: Path, size=(108, 192)) -> list[np.ndarray]:
    """Every decoded frame, scaled down, as RGB arrays."""
    w, h = size
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale={w}:{h}",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24",
                          "-"], check=True, capture_output=True, timeout=180).stdout
    n = w * h * 3
    return [np.frombuffer(raw[k * n:(k + 1) * n], np.uint8).reshape(h, w, 3)
            for k in range(len(raw) // n)]


def frame_at(path: Path, n: int, size=(270, 480)) -> np.ndarray:
    """Decoded frame number `n`, at a quarter of 1080x1920, as RGB."""
    w, h = size
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                          f"select=eq(n\\,{n}),scale={w}:{h}", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         check=True, capture_output=True, timeout=120).stdout
    return np.frombuffer(raw[:w * h * 3], np.uint8).reshape(h, w, 3)


def spec(**over) -> dict:
    base = {"in": 0, "out": 12, "auto_trim": False, "layout": "fit",
            "captions": {"enabled": False}, "hook": {"enabled": False}}
    base.update(over)
    return base


# --- pure parts --------------------------------------------------------------

def test_file_names_keep_todays_names_for_version_1():
    assert render.file_names("01-a-fair", 1) == {
        "original": "01-a-fair.mp4", "vertical": "01-a-fair-vertical.mp4",
        "cover": "01-a-fair-cover.jpg", "thumb": "01-a-fair-thumb.jpg",
        "crop_track": "01-a-fair-crop.json"}
    v3 = render.file_names("01-a-fair", 3)
    assert v3["vertical"] == "01-a-fair-vertical.v3.mp4" and v3["thumb"] == "01-a-fair-thumb.v3.jpg"
    with pytest.raises(ValueError):
        render.file_names("01-a-fair", 0)


@pytest.mark.req("3.1", "10.8")
def test_the_frame_rate_is_kept_inside_23_to_60_fps_and_30_otherwise():
    assert [render.output_fps(f) for f in (25, 23.976, 60, 59.94)] == [25, 23.976, 60, 59.94]
    assert [render.output_fps(f) for f in (15, 22.9, 60.1, 120, None, 0)] == [30] * 6


def test_the_filter_graphs():
    fit = render.vertical_graph(track=None, fps=25, overlay=False, music_input=None)
    assert "boxblur" in fit and "crop@reframe" not in fit and "fps=25" in fit
    track = {"crop_w": 404, "crop_h": 720, "keyframes": [[0.0, 438]]}
    fill = render.vertical_graph(track=track, fps=30, overlay=True, music_input=2,
                                 volume=0.4)
    assert fill.startswith("[0:v]sendcmd=f=crop.cmd,crop@reframe=w=404:h=720:x=438:y=0,"
                           "scale=1080:1920,fps=30")
    assert "overlay=0:0:eof_action=pass" in fill and "[2:a]" in fill
    # Speech feeds both the mix and the sidechain, so it must be split first.
    assert "[0:a]asplit=2[sp][sc]" in fill and "[mraw][sc]sidechaincompress" in fill
    assert "volume=0.400" in fill


def test_a_manual_crop_is_centred_on_x_and_kept_in_frame():
    assert render.manual_track(0.5, 1280, 720)["keyframes"] == [[0.0, 438]]
    assert render.manual_track(0.0, 1280, 720)["keyframes"] == [[0.0, 0]]
    assert render.manual_track(1.0, 1280, 720)["keyframes"] == [[0.0, 876]]
    assert render.manual_track(0.5, 720, 1280) is None            # already 9:16


# --- full renders ------------------------------------------------------------

@pytest.mark.req("3.1", "3.4", "3.6", "10.8", "N1.4")
def test_a_render_meets_the_reels_format(tmp_path):
    # Noise is incompressible, so this also exercises the bit-rate cap.
    src = source(tmp_path / "src.mp4", video="color=c=0x204060:s=1280x720:r=25,"
                                             "noise=alls=20:allf=t")
    words = [{"w": w, "s": s, "e": s + 0.4, "p": 0.9}
             for w, s in (("ONE", 1.0), ("TWO", 1.5), ("THREE", 2.0))]
    steps = []
    out = render.render_version(
        tmp_path, src, {"language": "en", "words": words}, spec(
            cuts=[[4, 6]], captions={"enabled": True, "style": "words"},
            hook={"enabled": True, "text": "Watch this", "duration": 0.8}),
        1, "01-test", progress=lambda step, f: steps.append((step, f)), title="Format test")

    names = render.file_names("01-test", 1)
    assert out["files"] == {**names, "crop_track": None} and out["layout"] == "fit"
    assert out["duration"] == 10.0 and out["fps"] == 25
    assert not list(tmp_path.glob(".render-*"))                   # work folder removed
    p = out["probe"]
    assert (p["width"], p["height"], p["video_codec"], p["pix_fmt"]) == (1080, 1920, "h264",
                                                                         "yuv420p")
    assert (p["audio_codec"], p["sample_rate"], p["channels"]) == ("aac", 48000, 2)
    assert p["moov_before_mdat"] is True and p["has_edit_list"] is False
    assert p["bitrate"] <= 25_000_000
    s = streams(tmp_path / names["vertical"])
    assert s["video"]["field_order"] == "progressive" and s["video"]["r_frame_rate"] == "25/1"
    assert abs(int(s["video"]["nb_read_frames"]) - 250) <= 1     # 10 s kept, within a frame
    assert abs(float(s["audio"]["duration"]) - 10.0) <= 0.05

    vertical = tmp_path / names["vertical"]

    def white(n, rows):
        return (frame_at(vertical, n)[rows] > 220).all(axis=2).mean()

    top, bottom = slice(0, 160), slice(320, 480)                  # upper and lower thirds
    assert white(10, top) > 0.002 and white(40, top) == 0          # hook: 0.4 s, gone at 1.6 s
    assert white(40, bottom) > 0.002 and white(200, bottom) == 0   # caption: 1.6 s, not at 8 s

    with Image.open(tmp_path / names["cover"]) as im:
        assert im.size == (1080, 1920)
    assert (tmp_path / names["thumb"]).stat().st_size <= 2_000_000
    fractions = [f for _, f in steps]
    assert fractions[0] == 0.0 and fractions[-1] == 1.0 and fractions == sorted(fractions)
    assert {st for st, _ in steps} == set(render._STEPS)


@pytest.mark.req("3.1", "10.8")
def test_a_15_fps_source_renders_at_30_fps(tmp_path):
    src = source(tmp_path / "src.mp4", fps=15)
    out = render.render_version(tmp_path, src, None, spec(out=4), 1, "01-slow")
    s = streams(tmp_path / out["files"]["vertical"])
    assert s["video"]["r_frame_rate"] == "30/1" and out["fps"] == 30
    assert abs(int(s["video"]["nb_read_frames"]) - 120) <= 1


@pytest.mark.req("10.8", "N1.4")
def test_the_shortest_detailed_clip_stays_under_25_mbps(tmp_path):
    # A 3.2 s clip of noise: the encoder's buffer allowance is spread over
    # the fewest seconds here, so this is the worst case for the average.
    src = source(tmp_path / "src.mp4", seconds=4,
                 video="color=c=0x204060:s=1280x720:r=30,noise=alls=40:allf=t")
    out = render.render_version(tmp_path, src, None, spec(out=3.2), 1, "01-noise")
    rate = out["probe"]["bitrate"]
    assert 15_000_000 < rate <= 25_000_000            # pressed against the cap, and under it


@pytest.mark.req("3.4", "3.7")
def test_an_overlay_interval_lands_within_one_frame(tmp_path, monkeypatch):
    from media import overlays as ov

    def solid_red(words, spans, spec, out_dir, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        clear, red = out_dir / "ov_000.png", out_dir / "ov_001.png"
        Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)).save(clear)
        Image.new("RGBA", (1080, 1920), (255, 0, 0, 255)).save(red)
        concat = ov._write_concat(out_dir, [(clear, 2_000_000), (red, 1_500_000),
                                            (clear, 2_500_000)])
        return concat, [{"start": 2.0, "end": 3.5, "caption": "red", "hook": False}]

    monkeypatch.setattr(ov, "build_overlays", solid_red)
    src = source(tmp_path / "src.mp4", seconds=6, video="color=c=0x202020:s=1280x720:r=25")
    out = render.render_version(tmp_path, src, None, spec(out=6, captions={"enabled": True}),
                                1, "01-red")
    reds = [k for k, f in enumerate(frames(tmp_path / out["files"]["vertical"]))
            if f[..., 0].mean() > 200 and f[..., 1:].mean() < 60]
    assert abs(reds[0] - 50) <= 1 and abs(reds[-1] - 87) <= 1      # 2.0 s to 3.5 s at 25 fps
    assert reds == list(range(reds[0], reds[-1] + 1))


def white_box(frame):
    """Detector for the test: the white box, in the sample's pixels."""
    ys, xs = np.nonzero(frame.min(axis=2) > 200)
    if not len(xs):
        return []
    return [(float(xs.min()), float(ys.min()), float(np.ptp(xs) + 1), float(np.ptp(ys) + 1),
             0.9)]


@pytest.mark.req("3.2")
def test_the_fill_crop_follows_the_face_across_a_cut(tmp_path):
    src = tmp_path / "two_shots.mp4"
    box = "drawbox=y=260:w=120:h=200:color=white:t=fill"
    ffmpeg("-f", "lavfi", "-t", "3", "-i", f"color=c=0x500000:s=1280x720:r=25,{box}:x=150",
           "-f", "lavfi", "-t", "3", "-i", f"color=c=0x000050:s=1280x720:r=25,{box}:x=1000",
           "-f", "lavfi", "-t", "6", "-i", "sine=frequency=440:sample_rate=48000",
           "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
           *ENCODE, "-c:a", "aac", src)
    out = render.render_version(tmp_path, src, None, spec(out=6, layout="fill"), 1, "01-cut",
                                detector=white_box)
    assert out["layout"] == "fill" and out["crop"]["shots"] == 2
    track = json.loads((tmp_path / out["files"]["crop_track"]).read_text())
    assert [x for _, x in track["keyframes"]] == [8, 858]          # box centres 210 and 1060
    vertical = tmp_path / out["files"]["vertical"]
    for n in (25, 112):                                            # 1.0 s and 4.5 s
        assert frame_at(vertical, n)[240, 135].min() > 200         # the box is centred
    assert frame_at(vertical, 112)[20, 135][2] > frame_at(vertical, 112)[20, 135][0]


@pytest.mark.req("3.3")
def test_without_faces_or_sound_the_clip_renders_in_fit_with_silence(tmp_path):
    src = source(tmp_path / "src.mp4", seconds=4, audio=None)
    (tmp_path / "01-quiet-crop.json").write_text("{}")            # from an earlier render
    out = render.render_version(tmp_path, src, None, spec(out=4, layout="fill"), 1, "01-quiet",
                                detector=lambda frame: [])
    assert out["layout"] == "fit" and out["crop"] is None
    assert out["files"]["crop_track"] is None and not (tmp_path / "01-quiet-crop.json").exists()
    s = streams(tmp_path / out["files"]["vertical"])
    assert (s["audio"]["codec_name"], s["audio"]["sample_rate"], s["audio"]["channels"]) == \
        ("aac", "48000", 2)


def test_a_failed_render_keeps_the_earlier_files(tmp_path, monkeypatch):
    src = source(tmp_path / "src.mp4", seconds=4)
    (tmp_path / "01-keep-vertical.mp4").write_bytes(b"earlier render")

    def broken(*args, **kwargs):
        raise render.RenderError("encode: ffmpeg failed: test")

    monkeypatch.setattr(render, "encode_vertical", broken)
    with pytest.raises(render.RenderError, match="encode"):
        render.render_version(tmp_path, src, None, spec(out=4), 1, "01-keep")
    assert (tmp_path / "01-keep-vertical.mp4").read_bytes() == b"earlier render"
    assert not (tmp_path / "01-keep.mp4").exists() and not list(tmp_path.glob(".render-*"))


def audio(path: Path, rate: int = 48000) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(rate),
                          "-f", "f32le", "-"], check=True, capture_output=True, timeout=120).stdout
    return np.frombuffer(raw, np.float32)


def tone(x: np.ndarray, freq: float, a: float, b: float, rate: int = 48000) -> float:
    """Amplitude of the `freq` component between `a` and `b` seconds (half
    the sine's peak)."""
    seg = x[int(a * rate):int(b * rate)].astype(np.float64)
    t = np.arange(len(seg)) / rate
    return float(abs(seg @ np.exp(-2j * np.pi * freq * t)) / len(seg))


@pytest.mark.req("3.9")
def test_music_is_mixed_under_speech_and_ducked(tmp_path):
    import music as music_mod
    import paths
    from media import editspec

    music_mod.ensure_dir()
    ffmpeg("-f", "lavfi", "-t", "2", "-i", "sine=frequency=1000:sample_rate=48000",
           paths.music_dir() / "bed.wav")
    # "Speech" is a 440 Hz tone for the first 3 s, then silence.
    src = source(tmp_path / "src.mp4", seconds=6,
                 audio="aevalsrc='if(lt(t,3),0.5*sin(2*PI*440*t),0)':s=48000")
    asked = []
    out = render.render_version(
        tmp_path, src, None, spec(out=6, music={"track": "bed.wav", "volume": 0.5}), 1,
        "01-music", is_cleared=lambda track: asked.append(track) or True)
    assert asked == ["bed.wav"]
    x = audio(tmp_path / out["files"]["vertical"])
    assert abs(len(x) / 48000 - 6.0) < 0.1          # the looped 2 s track doesn't lengthen it
    assert tone(x, 440, 1.0, 2.5) > 0.2              # speech kept at full level (0.25)
    alone, under = tone(x, 1000, 4.0, 5.5), tone(x, 1000, 1.0, 2.5)
    assert alone > 0.02                              # music plays: 0.125 x 0.5 / 2
    assert alone > 3 * under                         # and ducks beneath speech

    with pytest.raises(editspec.EditSpecError, match="cleared"):
        render.render_version(tmp_path, src, None, spec(out=6, music={"track": "bed.wav"}), 2,
                              "01-music")
    assert not list(tmp_path.glob("01-music*.v2.*"))


def test_no_thumbnail_and_a_lower_minimum_for_the_manual_flow(tmp_path):
    src = source(tmp_path / "src.mp4", seconds=3)
    (tmp_path / "01-short-thumb.jpg").write_bytes(b"from an earlier render")
    with pytest.raises(Exception, match="at least 3 s"):
        render.render_version(tmp_path, src, None, spec(out=2), 1, "01-short")
    out = render.render_version(tmp_path, src, None, spec(out=2), 1, "01-short",
                                thumbnail=False, min_length=0.5)
    assert out["files"]["thumb"] is None and not (tmp_path / "01-short-thumb.jpg").exists()
    assert out["files"]["cover"] == "01-short-cover.jpg" and out["duration"] == 2.0
