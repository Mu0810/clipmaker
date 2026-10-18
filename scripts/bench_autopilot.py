#!/usr/bin/env python3
"""Benchmark Autopilot against its performance budgets (design §18;
Requirements N1.1, N1.2, N1.4).

    .venv/bin/python scripts/bench_autopilot.py --source talk.mp4 [--fake-ai] [--quick]

Everything runs on a scratch data folder, deleted at the end: your own
database, clips and tokens are never opened. The API is served on 127.0.0.1
with the background workers off, and the script:

1. processes the source as an Autopilot upload with the default profile,
   timing each stage of autopilot.process.run. The upload stands in for the
   download, which N1.1 leaves out;
2. renders a new 60 s version of the first clip (10 s with --quick) through
   the editor's render worker, as the editor does;
3. all the while asks for /api/autopilot/posts (the review queue's request,
   over 200 seeded posts) and /api/autopilot/counts (the header chip's), and
   takes the 95th percentile of each one's answer time;
4. prints each result against its budget: N1.1 for a 60-minute source, N1.4
   for a 60 s clip, N1.2 always.

--fake-ai answers every AI call at once with fixed replies, so no AI engine is
needed and the times leave out the AI's own. Without it, the AI engine set up
for this process (its environment) is used, if any. Whisper always runs, with
Autopilot's model (CLIPMAKER_WHISPER_MODEL, or small); a model that isn't
downloaded yet is downloaded first. --quick makes one short clip and a 10 s
re-render, for a fast check.

Exits 0 when every budget that applies is met, 1 otherwise.
"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import logging
import math
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OWNER = "local"
PROCESS_BUDGET_S = 30 * 60           # N1.1: a 60-minute source, download excluded
LATENCY_BUDGET_S = 0.5               # N1.2: 95th percentile of UI requests
RENDER_BUDGET_S = 3 * 60             # N1.4: a new version of a 60 s clip
SOURCE_FOR_N11_S = (57 * 60, 63 * 60)  # what counts as "a 60-minute source"
QUEUE_STATUSES = ("scheduled", "waiting_approval", "unscheduled", "blocked", "needs_reconnect",
                  "publishing", "retrying", "uploaded", "failed", "kept_private")
ENDPOINTS = {"/api/autopilot/posts": "/api/autopilot/posts?" + urlencode({"status": ",".join(QUEUE_STATUSES)}),
             "/api/autopilot/counts": "/api/autopilot/counts"}
QUICK_SETTINGS = {"clips_per_source": 1, "length_min_s": 6, "length_max_s": 15}
LABELS = {"downloading": "Taking the upload", "transcribing": "Transcribing", "choosing": "Choosing clips",
          "metadata": "Titles and checks", "rendering": "Rendering", "planning": "Planning posts"}


# ── the scratch data folder ──────────────────────────────────────────────

@contextlib.contextmanager
def scratch_data():
    """Point the app at a new, empty data folder with no background threads
    and sign-in off; put everything back and delete the folder afterwards."""
    names = ("CLIPMAKER_DATA_DIR", "CLIPMAKER_NO_BACKGROUND", "CLIPMAKER_AUTH")
    saved = {name: os.environ.get(name) for name in names}
    folder = Path(tempfile.mkdtemp(prefix="clipmaker-bench-"))
    os.environ["CLIPMAKER_DATA_DIR"] = str(folder)
    os.environ["CLIPMAKER_NO_BACKGROUND"] = "1"
    os.environ.pop("CLIPMAKER_AUTH", None)
    try:
        yield folder
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(folder, ignore_errors=True)


# ── the fake AI (--fake-ai) ──────────────────────────────────────────────

_LINE = re.compile(r"^\[(\d+)\] (\d+):(\d\d)–(\d+):(\d\d) ", re.M)


def _pick(prompt: str) -> str:
    """Clip selection: runs of whole sentences within the asked length, read
    off the numbered transcript in the prompt, best first."""
    k = int(re.search(r"Pick up to (\d+) moments", prompt).group(1))
    lo, hi = (float(x) for x in re.search(r"lasting ([\d.]+) to ([\d.]+) seconds", prompt).groups())
    aim = min(hi, lo + 2)                                    # mm:ss is coarse: aim inside the range
    lines = [(int(n), int(a) * 60 + int(b), int(c) * 60 + int(d)) for n, a, b, c, d in _LINE.findall(prompt)]
    clips, i = [], 0
    while i < len(lines) and len(clips) < k:
        j = i
        while j < len(lines) and lines[j][2] - lines[i][1] < aim:
            j += 1
        if j == len(lines):
            break
        if lines[j][2] - lines[i][1] <= hi:
            clips.append({"start": lines[i][0], "end": lines[j][0], "title": f"Moment {len(clips) + 1}",
                          "score": max(50, 92 - 6 * len(clips)), "reason": "Picked by the benchmark's fake AI"})
            i = j + 1
        else:
            i += 1
    return json.dumps({"clips": clips})


def fake_reply(prompt: str) -> str:
    if "Pick up to" in prompt:
        return _pick(prompt)
    if "Judge the PROPOSED text" in prompt:
        return json.dumps({"supported": True, "unsupported_claims": [], "sensitive": False,
                           "sensitive_categories": [], "reason": ""})
    if "Write publish-ready metadata" in prompt:
        return json.dumps({
            "youtube": {"title": "A moment worth watching", "description": "A moment worth watching.",
                        "tags": ["benchmark"], "category_id": "22"},
            "instagram": {"caption": "A moment worth watching #benchmark", "hashtags": ["#benchmark"]},
            "hook": "Wait for it", "title_suggestions": ["A moment worth watching"]})
    return "{}"                                              # anything else falls back as it would


@contextlib.contextmanager
def fake_ai():
    import llm

    saved = llm.chat, llm.is_configured

    def chat(prompt: str, system: str = "", **kwargs) -> str:
        llm.count_call()                                     # counted like a real call (N8.1)
        llm._req.last_model = "fake-ai"
        return fake_reply(prompt)

    llm.chat, llm.is_configured = chat, (lambda: True)
    try:
        yield
    finally:
        llm.chat, llm.is_configured = saved


# ── the API, and the requests timed against it ───────────────────────────

@contextlib.contextmanager
def api_server():
    """Clip Maker's API on a free 127.0.0.1 port, in a thread; yields the port."""
    import uvicorn

    import app as app_module

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app_module.app, log_level="warning", lifespan="on", ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True,
                              name="bench-api")
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("The API didn't start.")
        time.sleep(0.02)
    try:
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(15)
        sock.close()


class Sampler(threading.Thread):
    """Asks for each endpoint in turn while `phase` is "job" or "render",
    recording (endpoint, phase, seconds, HTTP status)."""

    def __init__(self, port: int, pause_s: float):
        super().__init__(daemon=True, name="bench-sampler")
        self.port, self.pause_s, self.phase = port, pause_s, None
        self.samples: list[tuple[str, str, float, int]] = []
        self.done = threading.Event()

    def run(self) -> None:
        conn, turn = None, 0
        while not self.done.is_set():
            phase = self.phase
            if phase:
                name = list(ENDPOINTS)[turn % len(ENDPOINTS)]
                turn += 1
                start = time.perf_counter()
                try:
                    conn = conn or http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
                    conn.request("GET", ENDPOINTS[name])
                    response = conn.getresponse()
                    response.read()
                    status = response.status
                except (OSError, http.client.HTTPException):
                    conn, status = None, 0
                self.samples.append((name, phase, time.perf_counter() - start, status))
            self.done.wait(self.pause_s)
        if conn is not None:
            conn.close()


def p95(values: list[float]) -> float | None:
    """The 95th percentile, nearest rank."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def latency_summary(samples) -> dict:
    out = {}
    for name in ENDPOINTS:
        mine = [s for s in samples if s[0] == name]
        ok = [s[2] for s in mine if s[3] == 200]
        out[name] = {"n": len(mine), "errors": len(mine) - len(ok), "p95_s": p95(ok),
                     "max_s": max(ok) if ok else None,
                     "phases": {phase: p95([s[2] for s in mine if s[1] == phase and s[3] == 200])
                                for phase in ("job", "render")}}
    return out


# ── the stages of a run ──────────────────────────────────────────────────

@contextlib.contextmanager
def stage_marks(progress):
    """Record when each stage of process.run begins: (stage, clip, time)."""
    from autopilot import process

    marks: list[tuple[str, tuple | None, float]] = []
    original = process._Stages.__call__

    def timed(self, stage, clip=None, part=None):
        if not self.seen or self.seen[-1] != (stage, clip):
            marks.append((stage, clip, time.perf_counter()))
            progress(label(stage, clip) + "…")
        return original(self, stage, clip, part)

    process._Stages.__call__ = timed
    try:
        yield marks
    finally:
        process._Stages.__call__ = original


def label(stage: str, clip) -> str:
    name = LABELS.get(stage, stage)
    return f"{name}, clip {clip[0]} of {clip[1]}" if clip else name


def seed_queue(n: int) -> None:
    """A full review queue: n posts waiting for approval, a few hours apart."""
    from autopilot import store

    now = time.time()
    with store.connect() as conn:
        for i in range(n):
            platform = "youtube" if i % 2 == 0 else "instagram"
            meta = ({"title": f"Queued post {i + 1}", "description": "", "tags": [], "category_id": "22"}
                    if platform == "youtube" else {"caption": f"Queued post {i + 1} #queue"})
            store.insert_post(conn, {"id": uuid.uuid4().hex[:16], "owner_id": OWNER, "platform": platform,
                                     "method": "official", "status": "waiting_approval", "approval": "pending",
                                     "holds": [], "meta": meta, "ai_meta": meta,
                                     "slot_at": now + 3600 * (6 + 3 * i), "created_at": now})


def stage_times(marks, end: float) -> list[dict]:
    rows = []
    for k, (stage, clip, start) in enumerate(marks):
        stop = marks[k + 1][2] if k + 1 < len(marks) else end
        rows.append({"stage": stage, "clip": list(clip) if clip else None, "seconds": stop - start})
    return rows


def rerender(job_id: str, seconds: float, duration: float, poll_s: float) -> dict | None:
    """A new version of the job's first rendered clip, `seconds` long, through
    the editor's render worker; timed from the request until it's ready."""
    from autopilot import editor, store

    with store.connect() as conn:
        clip = store.decode_row(conn.execute(
            "SELECT c.* FROM clips c JOIN clip_versions v ON v.id = c.current_version_id "
            "WHERE c.job_id=? AND v.status='ready' ORDER BY c.idx LIMIT 1", (job_id,)).fetchone())
        if clip is None:
            return None
        version = store.decode_row(conn.execute("SELECT * FROM clip_versions WHERE id=?",
                                                (clip["current_version_id"],)).fetchone())
    spec = version["spec"] if isinstance(version["spec"], dict) else json.loads(version["spec"])
    length = min(seconds, duration)
    start = min(float(clip["src_start"]), max(0.0, duration - length))
    # Every second kept, so the new version is the full length (no pauses trimmed).
    spec = {**spec, "in": round(start, 3), "out": round(start + length, 3), "cuts": [], "auto_trim": False}
    began = time.perf_counter()
    try:
        reply = editor.request_render(OWNER, clip["id"], spec)
    except editor.EditorError as exc:
        reasons = " ".join(p.get("message", "") for p in exc.problems)
        return {"clip": clip["idx"], "start_s": start, "length_s": length, "seconds": None,
                "status": "refused", "error": f"{exc.message} {reasons}".strip()}
    while True:
        status = editor.render_status(OWNER, reply["version_id"])
        if status["status"] != "rendering":
            break
        time.sleep(poll_s)
    return {"clip": clip["idx"], "start_s": start, "length_s": reply["length"],
            "seconds": time.perf_counter() - began, "status": status["status"], "error": status.get("error")}


# ── the run ──────────────────────────────────────────────────────────────

def run_benchmark(source: Path, *, fake: bool, quick: bool, progress=None) -> dict:
    """Process `source`, re-render one clip and time the API meanwhile; the
    results, with each budget and whether it applies and is met."""
    progress = progress or (lambda text: print(text, file=sys.stderr, flush=True))
    source = Path(source).resolve()
    results: dict = {"fake_ai": fake, "quick": quick, "error": None, "stages": [], "rerender": None}
    with contextlib.ExitStack() as stack:
        results["scratch"] = str(stack.enter_context(scratch_data()))   # before any app code runs
        from media.probe import probe

        info = probe(source)
        duration = float(info.get("duration") or 0.0)
        results["source"] = {"name": source.name, "duration_s": duration, "width": info.get("width"),
                             "height": info.get("height")}
        if fake:
            stack.enter_context(fake_ai())
        marks = stack.enter_context(stage_marks(progress))
        port = stack.enter_context(api_server())

        import llm
        import paths
        from autopilot import editor, intake, profiles
        from autopilot import owner as owner_mod
        from media import transcript as transcript_mod

        results["whisper_model"] = transcript_mod.autopilot_model_name()
        results["ai"] = "fake" if fake else ("configured" if llm.is_configured() else "none")
        saved_enqueuer = intake._enqueue_fn
        intake.set_enqueuer(lambda job_id: None)               # the run below is the only worker
        stack.callback(intake.set_enqueuer, saved_enqueuer)
        editor.WORKER.start()
        stack.callback(editor.WORKER.stop)
        sampler = Sampler(port, 0.05 if quick else 0.25)
        sampler.start()
        stack.callback(sampler.join, 5)
        stack.callback(sampler.done.set)

        owner_mod.set_prefs(OWNER, timezone="UTC")
        profile = profiles.ensure_default(OWNER)
        if quick:
            profiles.update(OWNER, profile["id"], settings_patch=QUICK_SETTINGS)
        seed_queue(50 if quick else 200)
        staged = paths.output_dir() / f"bench-upload{source.suffix.lower() or '.mp4'}"
        staged.parent.mkdir(parents=True, exist_ok=True)
        progress(f"Copying {source.name} into the scratch folder…")
        shutil.copyfile(source, staged)                        # the run moves its upload; never the original
        job_id = intake.submit_file(staged, source.name, OWNER, rights_confirmed=True)["job_id"]

        from autopilot import process

        sampler.phase = "job"
        try:
            process.run(job_id)
        except Exception as exc:                               # reported, not raised: the times so far count
            results["error"] = str(exc) or exc.__class__.__name__
        results["stages"] = stage_times(marks, time.perf_counter())
        from autopilot import store

        with store.connect() as conn:
            lengths = conn.execute(
                "SELECT c.idx, v.probe_json FROM clips c JOIN clip_versions v ON v.id = c.current_version_id "
                "WHERE c.job_id=? AND v.status='ready'", (job_id,)).fetchall()
        results["clip_lengths"] = {int(idx): float((json.loads(probe_json or "{}") or {}).get("duration") or 0)
                                   for idx, probe_json in lengths}
        results["clips"] = len(results["clip_lengths"])
        if results["error"] is None:
            sampler.phase = "render"
            progress("Rendering a new version of clip 1…")
            results["rerender"] = rerender(job_id, 10.0 if quick else 60.0, duration, 0.1 if quick else 0.5)
        sampler.phase = None
        results["latency"] = latency_summary(sampler.samples)
    results["budgets"] = budgets(results)
    return results


def budgets(r: dict) -> list[dict]:
    processing = sum(s["seconds"] for s in r["stages"] if s["stage"] != "downloading")
    whole = r["error"] is None and SOURCE_FOR_N11_S[0] <= r["source"]["duration_s"] <= SOURCE_FOR_N11_S[1]
    out = [{"id": "N1.1", "what": "processing a 60-minute source, download excluded", "limit_s": PROCESS_BUDGET_S,
            "measured_s": processing, "applies": whole, "met": processing <= PROCESS_BUDGET_S}]
    worst = [v["p95_s"] for v in (r.get("latency") or {}).values() if v["p95_s"] is not None]
    errors = sum(v["errors"] for v in (r.get("latency") or {}).values())
    out.append({"id": "N1.2", "what": "UI requests at the 95th percentile while processing and rendering",
                "limit_s": LATENCY_BUDGET_S, "measured_s": max(worst) if worst else None,
                "applies": bool(worst), "met": bool(worst) and max(worst) <= LATENCY_BUDGET_S and not errors})
    again = r.get("rerender") or {}
    sixty = again.get("status") == "ready" and abs((again.get("length_s") or 0) - 60.0) <= 0.5
    out.append({"id": "N1.4", "what": "rendering a new version of a 60 s clip", "limit_s": RENDER_BUDGET_S,
                "measured_s": again.get("seconds"), "applies": sixty,
                "met": sixty and again["seconds"] <= RENDER_BUDGET_S})
    return out


# ── the report ───────────────────────────────────────────────────────────

def clock(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}"


def ms(seconds: float | None) -> str:
    return "–" if seconds is None else f"{seconds * 1000:.0f} ms"


def verdict(budget: dict, why_not: str) -> str:
    limit = ms(budget["limit_s"]) if budget["id"] == "N1.2" else f"{budget['limit_s'] / 60:g} min"
    if not budget["applies"]:
        return f"not comparable: {why_not}"
    return f"{'within' if budget['met'] else 'OVER'} the budget ({limit})"


def report(r: dict) -> str:
    src = r["source"]
    by_id = {b["id"]: b for b in r["budgets"]}
    ai = {"fake": "fake, answering at once", "configured": "the configured engine",
          "none": "none set up, so the fallbacks"}[r["ai"]]
    lines = [f"Autopilot benchmark: {src['name']}, {clock(src['duration_s'])}, {src['width']}x{src['height']}",
             f"Whisper {r['whisper_model']} · AI: {ai} · {'quick' if r['quick'] else 'full'} run · "
             "scratch data folder, deleted afterwards", ""]
    lines.append("Processing (N1.1)")
    lengths = r.get("clip_lengths") or {}
    for s in r["stages"]:
        name = label(s["stage"], s["clip"])
        if s["stage"] == "rendering" and s["clip"] and lengths.get(s["clip"][0]):
            name += f" ({lengths[s['clip'][0]]:.1f} s)"
        lines.append(f"  {name:<40}{clock(s['seconds']):>10}")
    n11 = by_id["N1.1"]
    length = clock(src["duration_s"])
    lines.append(f"  {'Total, download excluded':<40}{clock(n11['measured_s']):>10}   " + verdict(
        n11, "the run failed" if r["error"] else f"the budget is for a 60-minute source; this one is {length}"))
    if r["error"]:
        lines.append(f"  The run stopped: {r['error']}")
    lines += ["", "New version of a clip (N1.4)"]
    again, n14 = r.get("rerender"), by_id["N1.4"]
    if again:
        what = f"Clip {again['clip']}, {again['length_s']:.1f} s from {clock(again['start_s'])}"
        lines.append(f"  {what:<40}{clock(again['seconds']):>10}   " + verdict(
            n14, f"the clip is {again['length_s']:.1f} s; the budget is for 60 s" if again["status"] == "ready"
            else f"the render {again['status']}: {again.get('error') or 'no reason given'}"))
    else:
        lines.append("  Not run: no clip was rendered.")
    lines += ["", "API answer times while processing and rendering (N1.2)"]
    for name, v in (r.get("latency") or {}).items():
        phases = ", ".join(f"{'processing' if p == 'job' else 'rendering'} {ms(t)}"
                           for p, t in v["phases"].items() if t is not None)
        lines.append(f"  {name:<24} p95 {ms(v['p95_s']):>7}, max {ms(v['max_s']):>7}, {v['n']} requests"
                     + (f" ({v['errors']} failed)" if v["errors"] else "") + (f"; p95 {phases}" if phases else ""))
    lines.append(f"  {'Slowest p95':<24} {ms(by_id['N1.2']['measured_s']):>11}   "
                 + verdict(by_id["N1.2"], "no request was answered"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Time Autopilot against its performance budgets "
                                                 "(N1.1, N1.2, N1.4) on a scratch data folder.")
    parser.add_argument("--source", required=True, type=Path, help="a video file to process")
    parser.add_argument("--fake-ai", action="store_true", help="answer AI calls at once with fixed replies")
    parser.add_argument("--quick", action="store_true", help="one short clip and a 10 s re-render")
    args = parser.parse_args(argv)
    if not args.source.is_file():
        parser.error(f"{args.source} isn't a file")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    results = run_benchmark(args.source, fake=args.fake_ai, quick=args.quick)
    print(report(results))
    return 0 if results["error"] is None and all(b["met"] for b in results["budgets"] if b["applies"]) else 1


if __name__ == "__main__":
    sys.exit(main())
