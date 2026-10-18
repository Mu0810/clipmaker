"""Render one version of a clip from its EditSpec (design §4.6, task 5.5).

render_version() runs the whole chain for version N of clip NN-slug:

1. the original-ratio clip: the keep spans (media/keep.py) cut from the
   source and joined, with square pixels, even dimensions and 48 kHz stereo
   audio (silence when the source has none);
2. the crop track for the Fill layout (media/reframe.py), or Fit when no
   face can be followed;
3. the caption and hook overlay stream (media/overlays.py);
4. the 1080x1920 vertical: libx264 veryfast CRF 20 capped at 20 Mb/s,
   yuv420p, 23-60 fps (30 when the source is outside that range), AAC
   128 kb/s 48 kHz stereo, moov first and no edit list (Instagram's Reels
   requirements, 10.8). Music is mixed under speech and ducked beneath it,
   and only a track `is_cleared` approves is used (3.9);
5. the cover and the 16:9 thumbnail (media/cover.py);
6. the vertical's probe results (media/probe.py).

Outputs are written in a hidden work folder and moved into the job folder
only once every step has succeeded, so a failed render never replaces a
good earlier one. Version 1 keeps today's file names; later versions add
".vN" before the suffix.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SIZE = (1080, 1920)
FPS_MIN, FPS_MAX, FPS_FALLBACK = 23.0, 60.0, 30
VIDEO_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
              # A 20 Mb buffer that starts half full. The design's 40 Mb
              # buffer starts 90% full, and x264 may spend that on top of the
              # 20 Mb/s cap: a detailed 3 s clip averaged 26.6 Mb/s, over
              # Reels' 25 Mb/s limit (measured on ffmpeg 8.1.2). Worst case
              # now: 20 + 10 / 3 = 23.3 Mb/s for the shortest clip.
              "-maxrate", "20M", "-bufsize", "20M", "-x264-params", "vbv-init=0.5",
              "-pix_fmt", "yuv420p",
              # No B-frames: Reels files have no edit list, and without one
              # x264's B-frame delay starts the video 80 ms after the audio
              # (measured on ffmpeg 8.1.2). Costs about 3% in file size.
              "-bf", "0"]
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
MP4_ARGS = ["-movflags", "+faststart", "-use_editlist", "0"]
FFMPEG_TIMEOUT_S = 3600
CMD_NAME = "crop.cmd"
# Today's Fit layout: the whole frame over a blurred, zoomed copy of itself.
FIT_CHAIN = ("split=2[bg][fg];"
             "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
             "crop=1080:1920,boxblur=luma_radius=40:luma_power=1[bg2];"
             "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fg2];"
             "[bg2][fg2]overlay=(W-w)/2:(H-h)/2")
# Music under speech, ducked by sidechain compression (today's settings).
DUCK = "sidechaincompress=threshold=0.03:ratio=12:attack=20:release=300"

Progress = Callable[[str, float], None]
# Share of the whole render each step takes, for progress reporting.
_STEPS = {"cut": (0.0, 0.15), "reframe": (0.15, 0.30), "overlays": (0.30, 0.35),
          "encode": (0.35, 0.90), "cover": (0.90, 0.98), "probe": (0.98, 1.0)}


class RenderError(RuntimeError):
    """A render step failed; the message says which and why."""


def file_names(base_name: str, version: int) -> dict[str, str]:
    """The files of one version. Version 1 keeps today's names
    (NN-slug.mp4, NN-slug-vertical.mp4, NN-slug-thumb.jpg)."""
    if version < 1:
        raise ValueError("Versions start at 1.")
    tag = "" if version == 1 else f".v{version}"
    return {"original": f"{base_name}{tag}.mp4",
            "vertical": f"{base_name}-vertical{tag}.mp4",
            "cover": f"{base_name}-cover{tag}.jpg",
            "thumb": f"{base_name}-thumb{tag}.jpg",
            "crop_track": f"{base_name}-crop{tag}.json"}


def output_fps(source_fps: float | None) -> float:
    """The source's frame rate when it is within 23-60 fps, otherwise 30."""
    if source_fps and FPS_MIN <= source_fps <= FPS_MAX:
        return round(float(source_fps), 3)
    return FPS_FALLBACK


def _report(progress: Progress | None, step: str, part: float = 0.0) -> None:
    if progress:
        lo, hi = _STEPS[step]
        progress(step, round(lo + (hi - lo) * min(1.0, max(0.0, part)), 3))


def run_ffmpeg(args: list[str], *, cwd: Path | None = None, duration: float | None = None,
               on_progress: Callable[[float], None] | None = None, step: str = "ffmpeg") -> None:
    """Run ffmpeg with `args`, reporting the share of `duration` written.
    Raises RenderError with the end of ffmpeg's error output."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin", "-nostats",
           "-progress", "pipe:1", *args]
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise RenderError(f"{step}: couldn't start ffmpeg: {exc}") from exc
    try:
        for line in proc.stdout:
            if on_progress and duration and line.startswith("out_time_us="):
                value = line.split("=", 1)[1].strip()
                if value.isdigit():
                    on_progress(int(value) / 1e6 / duration)
        proc.wait(timeout=FFMPEG_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        raise RenderError(f"{step}: ffmpeg took longer than {FFMPEG_TIMEOUT_S} s.") from exc
    finally:
        proc.stdout.close()
        error = proc.stderr.read().strip()
        proc.stderr.close()
    if proc.returncode != 0:
        raise RenderError(f"{step}: ffmpeg failed: {error[-500:]}")


def cut_original(source: Path, spans: list[tuple[float, float]], out_path: Path, *,
                 has_audio: bool, interlaced: bool = False,
                 on_progress: Callable[[float], None] | None = None) -> Path:
    """Join the keep spans of `source` into the original-ratio clip. Pixels
    are made square and dimensions even (yuv420p needs both), interlaced
    footage is deinterlaced, and a source without sound gets silence."""
    if not spans:
        raise RenderError("cut: the clip keeps nothing.")
    start = spans[0][0]
    total = sum(b - a for a, b in spans)
    prep = "bwdif=deint=interlaced," if interlaced else ""
    prep += "scale='trunc(iw*sar/2)*2':'trunc(ih/2)*2',setsar=1"
    chain, joins = [], []
    for k, (a, b) in enumerate(spans):
        rs, re_ = a - start, b - start
        chain.append(f"[0:v]trim=start={rs:.3f}:end={re_:.3f},setpts=PTS-STARTPTS[v{k}]")
        if has_audio:
            chain.append(f"[0:a]atrim=start={rs:.3f}:end={re_:.3f},asetpts=PTS-STARTPTS,"
                         f"aresample=48000,aformat=channel_layouts=stereo[a{k}]")
        joins.append(f"[v{k}][a{k}]" if has_audio else f"[v{k}]")
    a_flag = 1 if has_audio else 0
    chain.append("".join(joins) + f"concat=n={len(spans)}:v=1:a={a_flag}[vj]"
                 + ("[aj]" if has_audio else ""))
    chain.append(f"[vj]{prep}[v]")
    inputs = ["-ss", f"{start:.3f}", "-i", str(source)]
    audio_map = "[aj]"
    if not has_audio:
        inputs += ["-f", "lavfi", "-t", f"{total:.3f}",
                   "-i", "anullsrc=r=48000:cl=stereo"]
        audio_map = "1:a"
    run_ffmpeg([*inputs, "-filter_complex", ";".join(chain),
                "-map", "[v]", "-map", audio_map, "-map_metadata", "-1", "-map_chapters", "-1",
                *VIDEO_ARGS[:6], "-pix_fmt", "yuv420p", *AUDIO_ARGS,
                "-movflags", "+faststart", str(out_path)],
               duration=total, on_progress=on_progress, step="cut")
    return out_path


def manual_track(x: float, src_w: int, src_h: int) -> dict | None:
    """A fixed crop centred at `x` (a fraction of the width), set in the
    editor; None when the source is already 9:16 or narrower."""
    from media import reframe

    cw = reframe.crop_width(src_w, src_h)
    if cw is None:
        return None
    left = min(max(0, round(x * src_w - cw / 2)), src_w - cw)
    return {"v": 1, "mode": "manual", "src_w": src_w, "src_h": src_h, "crop_w": cw,
            "crop_h": src_h, "keyframes": [[0.0, left]]}


def vertical_graph(*, track: dict | None, fps: float, overlay: bool,
                   music_input: int | None, volume: float = 0.25) -> str:
    """The filtergraph for the vertical. Input 0 is the original-ratio clip;
    the overlay stream, when present, is input 1; music is `music_input`.
    Fill (with a crop track) needs ffmpeg to run in the folder holding
    CMD_NAME. Outputs [v] and, with music, [a]."""
    from media import reframe

    w, h = SIZE
    if track:
        layout = f"{reframe.filter_chain(track, CMD_NAME)},scale={w}:{h}"
    else:
        layout = FIT_CHAIN
    chain = [f"[0:v]{layout},fps={fps:g},setsar=1[base]"]
    if overlay:
        chain.append("[1:v]setpts=PTS-STARTPTS[t]")
        chain.append("[base][t]overlay=0:0:eof_action=pass,format=yuv420p[v]")
    else:
        chain.append("[base]format=yuv420p[v]")
    if music_input is not None:
        chain.append("[0:a]asplit=2[sp][sc]")
        chain.append(f"[{music_input}:a]aresample=48000,aformat=channel_layouts=stereo,"
                     f"volume={max(0.0, min(1.0, volume)):.3f}[mraw]")
        chain.append(f"[mraw][sc]{DUCK}[mduck]")
        chain.append("[sp][mduck]amix=inputs=2:duration=first:normalize=0[a]")
    return ";".join(chain)


def encode_vertical(original: Path, out_path: Path, *, work_dir: Path, track: dict | None,
                    fps: float, concat: Path | None, music: Path | None,
                    volume: float, duration: float,
                    on_progress: Callable[[float], None] | None = None) -> Path:
    """Encode the 1080x1920 vertical. Runs in `work_dir`, which holds the
    crop command file when `track` is given."""
    from media import overlays, reframe

    if track:
        reframe.write_sendcmd(track, work_dir / CMD_NAME)
    inputs = ["-i", str(original)]
    if concat is not None:
        inputs += overlays.input_args(concat)
    music_input = None
    if music is not None:
        music_input = 2 if concat is not None else 1
        inputs += ["-stream_loop", "-1", "-i", str(music)]
    graph = vertical_graph(track=track, fps=fps, overlay=concat is not None,
                           music_input=music_input, volume=volume)
    audio_map = "[a]" if music_input is not None else "0:a"
    shortest = ["-shortest"] if music_input is not None else []
    run_ffmpeg([*inputs, "-filter_complex", graph, "-map", "[v]", "-map", audio_map,
                *shortest, "-map_metadata", "-1", "-map_chapters", "-1",
                *VIDEO_ARGS, *AUDIO_ARGS, *MP4_ARGS, str(out_path)],
               cwd=work_dir, duration=duration, on_progress=on_progress, step="encode")
    return out_path


def render_version(job_dir, source, transcript: dict | None, spec, version: int,
                   base_name: str, progress: Progress | None = None, *, title: str = "",
                   is_cleared: Callable[[str], bool] | None = None,
                   detector=None, thumbnail: bool = True,
                   min_length: float | None = None) -> dict:
    """Render version `version` of clip `base_name` from `source`.

    `transcript` is the job's transcript.json (or None), `spec` an EditSpec
    or its dict; it is validated first, and an EditSpecError lists any
    problems by field. Music needs `is_cleared(track)` to say yes (3.9).
    `title` supplies the thumbnail's words; `thumbnail=False` makes none.
    `min_length` lowers the 3 s minimum for the manual flow, which has always
    made shorter clips. `detector` replaces the face detector (tests).
    `progress(step, fraction)` reports the whole render.

    Returns {"version", "layout" ("fill" or "fit"), "spans", "duration",
    "fps", "files" (names in the job folder: original, vertical, cover,
    thumb and crop_track, None when not made), "cover" ({"t", "face",
    "chosen"}), "crop" (the track's summary, None in Fit), "probe" (the
    vertical's media/probe.py results), "spec" (the validated spec)}."""
    import music as music_mod
    from media import cover as cover_mod
    from media import editspec, keep, overlays, reframe
    from media.probe import ProbeError, probe

    job_dir, source = Path(job_dir), Path(source)
    try:
        src = probe(source)
    except ProbeError as exc:
        raise RenderError(f"probe: {exc}") from exc
    if not src.get("width") or not src.get("height"):
        raise RenderError("probe: the source has no picture.")
    transcript = transcript or {}
    words = transcript.get("words") or []
    spec = editspec.validate(spec, src.get("duration") or 0.0, words=words or None,
                             is_cleared=is_cleared,
                             min_length=editspec.MIN_LENGTH_S if min_length is None
                             else min_length)
    spans = keep.keep_spans(words, spec.in_, spec.out, spec.cuts, spec.auto_trim)
    total = keep.kept_duration(spans)
    music = None
    if spec.music.track:
        music = music_mod.resolve(spec.music.track)
        if music is None:
            raise RenderError(f"music: '{spec.music.track}' isn't in the music folder.")

    names = file_names(base_name, version)
    work = job_dir / f".render-{base_name}-v{version}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        _report(progress, "cut")
        original = cut_original(
            source, spans, work / names["original"], has_audio=bool(src.get("audio_codec")),
            interlaced=src.get("field_order") in ("tt", "bb", "tb", "bt"),
            on_progress=lambda p: _report(progress, "cut", p))
        info = probe(original)
        width, height = info["width"], info["height"]

        _report(progress, "reframe")
        track = None
        if spec.layout == "fill":
            if spec.crop.mode == "manual":
                track = manual_track(spec.crop.x, width, height)
            else:
                track = reframe.analyze(
                    original, detector=detector, src_w=width, src_h=height,
                    progress=lambda t: _report(progress, "reframe", t / total))
        if track:
            reframe.write_track(track, work / names["crop_track"])

        _report(progress, "overlays")
        concat = None
        if spec.captions.enabled or spec.hook.enabled:
            concat, intervals = overlays.build_overlays(
                words, spans, spec, work / "overlays",
                sentences=transcript.get("sentences"), language=transcript.get("language"))
            if not any(iv["caption"] or iv["hook"] for iv in intervals):
                concat = None                   # nothing to draw: skip the overlay
        fps = output_fps(src.get("fps"))

        _report(progress, "encode")
        vertical = encode_vertical(
            original, work / names["vertical"], work_dir=work, track=track, fps=fps,
            concat=concat, music=music, volume=spec.music.volume, duration=total,
            on_progress=lambda p: _report(progress, "encode", p))

        _report(progress, "cover")
        made = cover_mod.make(vertical, work / names["cover"],
                              work / names["thumb"] if thumbnail else None,
                              title=title, t=spec.cover.t, source=original, track=track,
                              detector=detector,
                              progress=lambda t: _report(progress, "cover", t / total))
        _report(progress, "probe")
        result_probe = probe(vertical)

        files: dict[str, str | None] = {}
        for key, name in names.items():
            built = work / name
            if built.is_file():
                os.replace(built, job_dir / name)
                files[key] = name
            else:
                # An earlier render's file must not pass for this one's.
                (job_dir / name).unlink(missing_ok=True)
                files[key] = None
    except (ProbeError, reframe.ReframeError, cover_mod.CoverError, OSError) as exc:
        raise RenderError(f"render: {exc}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _report(progress, "probe", 1.0)
    crop = None
    if track:
        crop = {key: track.get(key) for key in
                ("mode", "crop_w", "shots", "face_samples", "face_in_crop")}
        crop["mode"] = crop["mode"] or "auto"
    log.info("Rendered %s v%d: %.2f s, %s, %g fps.", base_name, version, total,
             "fill" if track else "fit", fps)
    return {"version": version, "layout": "fill" if track else "fit",
            "spans": [[a, b] for a, b in spans], "duration": round(total, 3), "fps": fps,
            "files": files, "cover": {k: made[k] for k in ("t", "face", "chosen")},
            "crop": crop, "probe": result_probe, "spec": spec.to_dict()}
