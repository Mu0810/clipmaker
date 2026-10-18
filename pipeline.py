"""Core pipeline: download a video, build a transcript, pick highlights,
and cut each highlight into an original-ratio clip plus a 9:16 vertical clip.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

import monetize
import music as music_mod
import paths
import transcribe
from highlights import (Segment, generate_metadata, make_hook,
                        score_viral_batch, select_highlights)
from media import keep
from media import render as render_mod
from media import transcript as transcript_mod
from media.editspec import EditSpecError, HOOK_MAX
from media.probe import ProbeError, probe

# The manual flow has always produced clips shorter than Reels' 3 s minimum, so
# render_version's 3 s floor is lowered here.
MANUAL_MIN_S = 1.0

log = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise PipelineError(
            f"Command failed: {' '.join(cmd[:3])} ...\n{proc.stderr[-2000:]}"
        )
    return proc


# ------------------------------ download -------------------------------------

_YTDLP_BIN: str | None = None
_IMPERSONATE_ARGS: list[str] | None = None


def _ytdlp_bin() -> str:
    """Prefer the venv's yt-dlp (it has curl_cffi for impersonation) over a
    system/Homebrew one on PATH."""
    global _YTDLP_BIN
    if _YTDLP_BIN is None:
        cand = Path(sys.executable).with_name("yt-dlp")
        _YTDLP_BIN = str(cand) if cand.exists() else (
            shutil.which("yt-dlp") or "yt-dlp")
    return _YTDLP_BIN


def _impersonation_args() -> list[str]:
    """yt-dlp can impersonate a real browser (via curl_cffi) to dodge YouTube
    throttling/429s. Detect a real target from the actual binary so we never
    pass an unsupported target (which is a hard error)."""
    global _IMPERSONATE_ARGS
    if _IMPERSONATE_ARGS is None:
        _IMPERSONATE_ARGS = []
        try:
            out = subprocess.run(
                [_ytdlp_bin(), "--list-impersonate-targets"],
                capture_output=True, text=True, timeout=30).stdout.lower()
            for target in ("chrome", "edge", "safari", "firefox"):
                if target in out:
                    _IMPERSONATE_ARGS = ["--impersonate", target]
                    break
        except Exception:
            _IMPERSONATE_ARGS = []
    return _IMPERSONATE_ARGS


def _ytdlp_base() -> list[str]:
    # Shared flags: no playlists, retry, and gentle pacing to avoid 429s.
    return [
        _ytdlp_bin(), "--no-playlist",
        "--retries", "5", "--fragment-retries", "5",
        "--sleep-requests", "1.5",
        *_impersonation_args(),
    ]


def _local_source(source_path: str, progress,
                  display_name: str = "") -> tuple[Path, dict]:
    """Use an already-saved local upload as the source, skipping yt-dlp.

    Local files have no caption track, so the transcript always comes from
    Whisper downstream. The title is derived from the original file name.
    """
    progress("Preparing uploaded video...")
    src = Path(source_path)
    if not src.exists():
        raise PipelineError("Uploaded video file not found.")
    stem = Path(display_name or src.name).stem
    title = re.sub(r"[._\-]+", " ", stem).strip() or "video"
    return src, {"title": title}


def download(url: str, job_dir: Path, progress) -> tuple[Path, dict]:
    """Download the video (mp4) into job_dir, then best-effort fetch subtitles.

    Subtitle failures are non-fatal — the pipeline falls back to Whisper.
    Returns (video_path, info_dict).
    """
    job_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(job_dir / "source.%(ext)s")
    base = _ytdlp_base()

    progress("Downloading video...")
    try:
        _run(base + [
            "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
            "--merge-output-format", "mp4",
            "-o", out_tmpl,
            url,
        ])
    except PipelineError as e:
        if "429" in str(e) or "Too Many Requests" in str(e):
            raise PipelineError(
                "YouTube is rate-limiting downloads (HTTP 429). Wait a few "
                "minutes and try again. To reduce this, install browser "
                "impersonation: pip install \"yt-dlp[default,curl-cffi]\"."
            ) from e
        raise

    videos = sorted(job_dir.glob("source.*"))
    video = next((p for p in videos if p.suffix.lower() in
                  {".mp4", ".mkv", ".webm", ".mov"}), None)
    if not video:
        raise PipelineError("Download produced no video file.")

    # Best-effort subtitles in a separate step: a 429 here must not abort the
    # whole job, since Whisper can transcribe the audio instead.
    progress("Fetching captions...")
    try:
        _run(base + [
            "--skip-download",
            "--write-auto-subs", "--write-subs",
            "--sub-langs", "en-orig,en,en-US,en-GB",
            "--convert-subs", "vtt",
            "-o", out_tmpl,
            url,
        ], timeout=180)
    except Exception:
        pass  # no captions — transcribe.py (Whisper) takes over downstream

    info = {}
    info_json = job_dir / "source.info.json"
    if info_json.exists():
        try:
            info = json.loads(info_json.read_text())
        except Exception:
            info = {}
    # Fetch the title cheaply if we don't already have it.
    if not info:
        try:
            meta = _run(base + ["--skip-download",
                                "--print", "%(title)s\n%(duration)s", url],
                        timeout=60)
            lines = meta.stdout.strip().splitlines()
            info = {"title": lines[0] if lines else "video"}
        except Exception:
            info = {"title": "video"}

    return video, info


# ---------------------------- transcript -------------------------------------

_TS = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)


def _vtt_time(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_subtitles(job_dir: Path) -> list[Segment]:
    """Parse the first English .vtt subtitle file into Segments."""
    vtts = sorted(job_dir.glob("source*.vtt"))
    if not vtts:
        return []
    text = vtts[0].read_text(encoding="utf-8", errors="ignore")

    segments: list[Segment] = []
    cur_start = cur_end = None
    cur_lines: list[str] = []

    def flush():
        nonlocal cur_start, cur_end, cur_lines
        if cur_start is not None and cur_lines:
            line = " ".join(cur_lines)
            line = re.sub(r"<[^>]+>", "", line)          # strip inline tags
            line = re.sub(r"&[a-z]+;", " ", line)         # entities
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                segments.append(Segment(cur_start, cur_end, line))
        cur_start = cur_end = None
        cur_lines = []

    for raw in text.splitlines():
        line = raw.strip()
        m = _TS.search(line)
        if m:
            flush()
            cur_start = _vtt_time(*m.groups()[:4])
            cur_end = _vtt_time(*m.groups()[4:])
        elif line and not line.isdigit() and line != "WEBVTT" \
                and "-->" not in line and not line.startswith(("NOTE", "Kind:", "Language:")):
            cur_lines.append(line)
    flush()

    return _dedupe(segments)


def _save_caption_transcript(job_dir: Path, segments: list[Segment]) -> dict:
    """The word-level transcript (estimated word timing) the renderer trims and
    captions from, also saved beside the clips. A failure here never fails the
    job: the clips are then made without captions or pause trimming."""
    try:
        transcript = transcript_mod.from_caption_segments(segments)
    except (ValueError, TypeError):
        log.warning("Could not build a transcript from the captions in %s", job_dir,
                    exc_info=True)
        return {}
    try:
        transcript_mod.save(job_dir, transcript)
    except (OSError, ValueError, TypeError):
        log.warning("Could not save the caption transcript in %s", job_dir,
                    exc_info=True)
    return transcript


def _dedupe(segments: list[Segment]) -> list[Segment]:
    """Auto-subs repeat rolling lines; merge adjacent duplicates."""
    out: list[Segment] = []
    for s in segments:
        if out and s.text == out[-1].text:
            out[-1].end = s.end
            continue
        if out and out[-1].text.endswith(s.text):
            continue
        out.append(s)
    return out


# ------------------------------ cutting --------------------------------------

def _probe_dims(video: Path) -> tuple[int, int]:
    proc = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0", str(video),
    ], timeout=60)
    w, h = proc.stdout.strip().split("x")
    return int(w), int(h)


# ------------------------------ orchestrate ----------------------------------

def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (s or "clip")[:n]


def _clip_text(segments: list[Segment], start: float, end: float) -> str:
    """Join transcript text of segments overlapping the [start, end] window."""
    parts = [s.text for s in segments if s.end > start and s.start < end]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _fit_hook(hook: str) -> str:
    """One line, within EditSpec's hook limit, so the spec always validates.
    generate_metadata already caps at 48 and make_hook at 40; this guards a
    hook from any other source."""
    return re.sub(r"\s+", " ", hook or "").strip()[:HOOK_MAX]


def process(url: str, job_id: str, progress, max_clips: int = 6,
            target_len: float = 45.0, burn_captions: bool = True,
            caption_style: str = "words", burn_hook: bool = True,
            write_metadata: bool = True,
            rank_by_virality: bool = True, make_thumbnails: bool = True,
            trim_silence: bool = True, source_path: str | None = None,
            music: str | None = None, music_volume: float = 0.25,
            monetize_config: dict | None = None) -> dict:
    """Full run. `progress(msg, pct=None)` reports status. Returns result dict."""
    job_dir = paths.output_dir() / job_id
    if source_path:
        video, info = _local_source(source_path, progress, url)
    else:
        video, info = download(url, job_dir, progress)
    title = info.get("title", "video")

    progress("Reading captions...")
    segments = parse_subtitles(job_dir)
    transcript_source = "captions"
    if segments:
        transcript = _save_caption_transcript(job_dir, segments)
    else:
        segments, transcript = transcribe.transcribe_detailed(video, progress,
                                                              job_dir=job_dir)
        transcript_source = "whisper"
    if not segments:
        raise PipelineError(
            "No captions were available and audio transcription produced no "
            "text, so highlights can't be auto-detected."
        )

    progress("Finding highlights...")
    # When ranking, pull a larger candidate pool, score it, and keep the best.
    pool = min(max_clips * 2, 20) if rank_by_virality else max_clips
    clips, method = select_highlights(segments, pool, target_len)
    if not clips:
        raise PipelineError("Could not identify any highlights.")

    if rank_by_virality:
        progress("Scoring clips for viral potential...", pct=35)
        # Clip selection already scored each clip; score only any it didn't.
        unscored = [c for c in clips if c.get("score") is None]
        if unscored:
            texts = [_clip_text(segments, c["start"], c["end"]) for c in unscored]
            durs = [int(c["end"] - c["start"]) for c in unscored]
            for c, s in zip(unscored, score_viral_batch(texts, durs)):
                c["score"], c["reason"] = s["score"], s["reason"]
        for c in clips:
            c["virality"] = c["score"]
            c["virality_reason"] = c.get("reason", "")
        clips.sort(key=lambda c: c.get("virality", 0), reverse=True)
    clips = clips[:max_clips]

    mpath = music_mod.resolve(music) if music else None
    track = mpath.name if mpath else None
    volume = max(0.0, min(1.0, music_volume))
    # An unknown style has always fallen through to whole caption lines.
    style = caption_style if caption_style in ("words", "karaoke") else "phrases"
    words = (transcript or {}).get("words") or []
    try:
        source_dur = probe(video)["duration"] or 0.0
    except ProbeError as e:
        raise PipelineError(str(e)) from e
    results = []
    total = len(clips)
    for idx, c in enumerate(clips, 1):
        lo, hi = 40 + 55 * (idx - 1) / total, 40 + 55 * idx / total
        progress(f"Cutting clip {idx}/{total}: {c['title']}", pct=int(lo))
        base = f"{idx:02d}-{_slug(c['title'])}"
        # A caption line can run past the end of the video; render what exists.
        t_in = max(0.0, c["start"])
        t_out = min(c["end"], source_dur) if source_dur else c["end"]
        clip_text = _clip_text(segments, c["start"], c["end"])

        # Pause and filler trimming (media/keep.py). As before, a clip is only
        # trimmed when that removes more than 0.4 s.
        clip_dur = t_out - t_in
        auto_trim = False
        if trim_silence and words:
            kept = keep.kept_duration(keep.keep_spans(words, t_in, t_out, (), True))
            if kept < clip_dur - 0.4:
                auto_trim, clip_dur = True, kept

        # Metadata first: we need the title + on-screen hook BEFORE rendering
        # the vertical, since the hook is burned into its first seconds.
        clip_title = c["title"]
        description = ""
        tags: list[str] = []
        title_suggestions: list[str] = []
        hashtags: list[str] = []
        hook = ""
        if write_metadata:
            progress(f"Writing title & description {idx}/{total}...",
                     pct=int(lo + (hi - lo) * 0.1))
            meta = generate_metadata(
                clip_text, source_title=title, fallback_title=c["title"],
                dur=int(clip_dur))
            clip_title = meta["title"] or c["title"]
            description = meta["description"]
            tags = meta["tags"]
            title_suggestions = meta.get("title_suggestions", [])
            hashtags = meta.get("hashtags", [])
            hook = meta.get("hook", "")
        if burn_hook and not hook:
            hook = make_hook(clip_text, clip_title)

        # One version-1 render through the shared renderer (media/render.py), in
        # today's Fit layout; captions, hook, trim and music come from the request.
        hook_text = _fit_hook(hook) if burn_hook else ""
        spec = {
            "in": t_in, "out": t_out, "auto_trim": auto_trim, "layout": "fit",
            "captions": {"enabled": burn_captions, "style": style},
            # Shown for 2.5 s as before, or 1 s to the clip's length when shorter.
            "hook": {"enabled": bool(hook_text), "text": hook_text,
                     "duration": round(min(2.5, max(1.0, clip_dur)), 2)},
            "music": {"track": track, "volume": volume},
        }
        shown = [-1]

        def on_render(step: str, fraction: float, idx=idx, lo=lo, hi=hi) -> None:
            pct = int(lo + (hi - lo) * (0.2 + 0.8 * fraction))
            if pct != shown[0]:
                shown[0] = pct
                progress(f"Rendering vertical {idx}/{total}...", pct=pct)

        def render_clip(spec: dict) -> dict:
            chosen = spec["music"]["track"]
            return render_mod.render_version(
                job_dir, video, transcript, spec, 1, base, on_render, title=clip_title,
                is_cleared=lambda name: name == chosen, thumbnail=make_thumbnails,
                min_length=MANUAL_MIN_S)

        try:
            try:
                rendered = render_clip(spec)
            except render_mod.RenderError:
                if track is None:
                    raise
                log.warning("Rendering clip %d with music failed; retrying without it.",
                            idx, exc_info=True)
                spec["music"]["track"] = None
                rendered = render_clip(spec)
        except (render_mod.RenderError, EditSpecError) as e:
            raise PipelineError(f"Couldn't render clip {idx}: {e}") from e
        files = rendered["files"]
        hook_used = hook_text

        # Monetization: fold in default hashtags + CTA/affiliate footer.
        for h in monetize.default_hashtags(monetize_config):
            if h not in hashtags:
                hashtags.append(h)
        description = monetize.apply_to_description(description, monetize_config)

        results.append({
            "index": idx,
            "title": clip_title,
            "hook": hook_used,
            "description": description,
            "tags": tags,
            "title_suggestions": title_suggestions,
            "hashtags": hashtags,
            "thumbnail": files["thumb"],
            "virality": c.get("virality"),
            "virality_reason": c.get("virality_reason", ""),
            "start": round(t_in, 2),
            "end": round(t_out, 2),
            "duration": round(rendered["duration"], 2),
            "trimmed": round(max(0.0, (t_out - t_in) - rendered["duration"]), 2)
            if auto_trim else 0.0,
            "original": files["original"],
            "vertical": files["vertical"],
        })

    progress("Done", pct=100)
    return {
        "job_id": job_id,
        "source_title": title,
        "method": method,
        "transcript_source": transcript_source,
        "clips": results,
    }
