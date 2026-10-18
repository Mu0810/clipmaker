"""The editor's server side (task 11.1, design §10.2, §10.4; Requirements 7.1,
7.2, 7.7-7.9, 8.6, N1.4)."""
from __future__ import annotations

import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

import db
import paths
from autopilot import activity, editor, publisher, sources, store
from autopilot import owner as owner_mod
from media import editspec, render
from media import transcript as transcript_mod
from media.probe import probe
from tests.helpers.media import make_video

API = "/api/editor"
SENTENCE = ("one two three four five six seven eight nine ten eleven twelve thirteen "
            "fourteen fifteen sixteen seventeen eighteen nineteen twenty")


@pytest.fixture
def ed(client, monkeypatch):
    """A render worker the tests drain by hand, and Whisper replaced by one
    word every half second of whatever file it's given."""
    worker = editor.RenderWorker()
    monkeypatch.setattr(editor, "WORKER", worker)
    calls: list[dict] = []

    def transcribe(video, language=None, model_name=None, progress=None):
        length = probe(video)["duration"]
        calls.append({"video": Path(video).name, "language": language, "duration": length})
        words = [{"w": f"w{i}", "s": round(i * 0.5, 3), "e": round(i * 0.5 + 0.4, 3), "p": 0.9}
                 for i in range(int(length / 0.5))]
        return transcript_mod.build(words, language="en", source="whisper", estimated=False,
                                    model="small")

    monkeypatch.setattr(transcript_mod, "transcribe_words", transcribe)
    owner_mod.set_prefs("local", timezone="Asia/Kolkata")
    yield {"worker": worker, "transcribed": calls, "client": client}
    worker.stop()


def old_job(owner=None, *, seconds=12.0, size=(320, 240), fps=25, transcript="missing",
            start=4.0, end=8.0) -> str:
    """A job made the old way: a source, the clip files and result_json, no clip rows."""
    job_id = uuid.uuid4().hex[:12]
    db.create(job_id, "https://youtu.be/old", user_id=owner)
    folder = paths.output_dir() / job_id
    folder.mkdir(parents=True)
    make_video(folder / "source.mp4", seconds=seconds, size=size, fps=fps)
    make_video(folder / "01-old-clip-vertical.mp4", seconds=end - start, size=(180, 320))
    (folder / "01-old-clip.mp4").write_bytes(b"original cut")
    (folder / "01-old-clip-thumb.jpg").write_bytes(b"\xff\xd8 thumb")
    entry = {"index": 1, "title": "Old clip", "hook": "Wait for it",
             "description": "The old description", "tags": ["a"], "hashtags": ["#old"],
             "thumbnail": "01-old-clip-thumb.jpg", "virality": 77, "virality_reason": "Strong",
             "start": start, "end": end, "duration": end - start, "trimmed": 0.0,
             "original": "01-old-clip.mp4", "vertical": "01-old-clip-vertical.mp4"}
    db.finish(job_id, "done", result={"job_id": job_id, "source_title": "Old video",
                                      "method": "llm", "transcript_source": "captions",
                                      "clips": [entry]})
    if transcript != "missing":
        words = transcript_mod.spread_words(SENTENCE, 0.0, seconds)
        transcript_mod.save(folder, transcript_mod.build(
            words, language="en", source="captions" if transcript == "estimated" else "whisper",
            estimated=transcript == "estimated"))
    return job_id


def open_clip(client, ref) -> dict:
    response = client.get(f"{API}/clips/{ref}")
    assert response.status_code == 200, response.text
    return response.json()


def keyframes(path: Path) -> list[float]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame",
                          "nokey", "-show_entries", "frame=pts_time", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, timeout=60, check=True)
    return [float(field) for field in (line.split(",")[0].strip()
                                       for line in out.stdout.splitlines()) if field]


# ── clips made before the editor (§10.4) ─────────────────────────────────

@pytest.mark.req("7.1")
def test_a_clip_from_before_the_editor_is_imported_as_version_1(ed):
    client = ed["client"]
    job = old_job(seconds=70.0, start=62.0, end=66.0)                # the window starts at 2 s
    body = open_clip(client, f"{job}-1")
    clip_id = body["clip"]["id"]
    assert clip_id == editor.imported_id(job, 1)
    assert {k: body["clip"][k] for k in ("job_id", "idx", "title", "score", "src_start",
                                         "src_end", "imported")} == {
        "job_id": job, "idx": 1, "title": "Old clip", "score": 77, "src_start": 62.0,
        "src_end": 66.0, "imported": True}
    v1 = body["version"]
    assert (v1["version"], v1["status"]) == (1, "ready")
    assert v1["files"] == {"original": "01-old-clip.mp4", "vertical": "01-old-clip-vertical.mp4",
                           "thumb": "01-old-clip-thumb.jpg", "cover": None, "crop_track": None}
    assert v1["probe"]["height"] == 320
    spec = v1["spec"]
    assert (spec["in"], spec["out"], spec["layout"], spec["auto_trim"]) == (62.0, 66.0, "fit",
                                                                            True)
    assert spec["captions"] == {"enabled": True, "style": "words", "edits": {}}
    assert (spec["hook"]["enabled"], spec["hook"]["text"]) == (True, "Wait for it")
    assert body["window"] == {"start": 2.0, "end": 70.0}              # the clip ± 60 s
    assert body["min_length"] == editor.MANUAL_MIN_S
    assert (body["transcript"]["state"], body["transcript"]["words"]) == ("preparing", [])

    assert ed["worker"].drain() == 1
    again = open_clip(client, f"{job}-1")
    assert again["clip"]["id"] == clip_id                              # imported once
    words = again["transcript"]
    assert (words["state"], words["source"], words["estimated"]) == ("ready", "window", False)
    assert words["words"][0] == {"i": 0, "w": "w0", "s": 2.0, "e": 2.4}   # shifted to source time
    [call] = ed["transcribed"]
    assert call["video"] == "window.m4a" and abs(call["duration"] - 68.0) < 0.2   # the window only
    assert open_clip(client, clip_id)["clip"]["id"] == clip_id
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM clips WHERE job_id=?", (job,)).fetchone()[0] == 1
    assert len(activity.list_entries("local", {"kind": "clip.imported"})) == 1
    assert client.get(f"{API}/clips/{job}-2").status_code == 404      # no clip 2 in that job


@pytest.mark.req("7.1")
@pytest.mark.parametrize("kind,state,transcribed", [("real", "ready", 0),
                                                    ("estimated", "preparing", 1)])
def test_a_jobs_own_transcript_is_used_unless_it_was_estimated_from_captions(ed, kind, state,
                                                                            transcribed):
    job = old_job(transcript=kind)
    body = open_clip(ed["client"], f"{job}-1")
    assert body["transcript"]["state"] == state
    if kind == "real":
        assert body["transcript"]["source"] == "job"
        assert len(body["transcript"]["words"]) == len(SENTENCE.split())
    assert ed["worker"].drain() == transcribed


def test_a_failed_transcription_falls_back_to_the_estimated_words(ed, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("Whisper ran out of memory")

    monkeypatch.setattr(transcript_mod, "transcribe_words", broken)
    job = old_job(transcript="estimated")
    open_clip(ed["client"], f"{job}-1")
    ed["worker"].drain()
    body = open_clip(ed["client"], f"{job}-1")
    assert (body["transcript"]["state"], body["transcript"]["source"]) == ("estimated",
                                                                          "estimated")
    assert "out of memory" in body["transcript"]["error"] and body["transcript"]["words"]
    assert ed["worker"].drain() == 0                                   # no retry loop


# ── the proxy (§10.2) ────────────────────────────────────────────────────

@pytest.mark.req("7.1", "7.2")
def test_the_proxy_is_540_px_tall_with_a_keyframe_every_15_frames(ed):
    client = ed["client"]
    job = old_job(seconds=12.0, size=(1280, 720), fps=30, transcript="real")
    clip = open_clip(client, f"{job}-1")["clip"]["id"]
    url = f"{API}/clips/{clip}/proxy"
    first = client.post(url, json={"start": 1, "end": 11}).json()
    assert (first["state"], first["name"]) == ("queued", "proxy_1000_11000.mp4")
    assert client.post(url, json={"start": 1, "end": 11}).json()["state"] == "queued"
    assert ed["worker"].drain() == 1                                   # asked twice, built once
    ready = client.post(url, json={"start": 1, "end": 11}).json()
    assert (ready["state"], ready["url"]) == ("ready", f"/api/clip/{job}/proxy_1000_11000.mp4")
    path = paths.output_dir() / job / ready["name"]
    info = probe(path)
    assert info["height"] == 540 and abs(info["duration"] - 10.0) < 0.2
    gaps = [b - a for a, b in zip(keyframes(path), keyframes(path)[1:])]
    assert gaps and max(gaps) <= 0.5 + 1e-3                            # every 15 frames at 30 fps
    ranged = client.get(ready["url"], headers={"Range": "bytes=0-99"})
    assert (ranged.status_code, len(ranged.content), ranged.headers["accept-ranges"]) == (
        206, 100, "bytes")
    assert client.post(url).json()["name"] == "proxy_0_12000.mp4"      # default: the clip ± 60 s
    for bad in ({"start": 5, "end": 5}, {"start": "soon"}):
        assert client.post(url, json=bad).status_code == 422


# ── rendering (7.7-7.9, 8.6) ─────────────────────────────────────────────

def clip_world(ed, owner=None) -> dict:
    job = old_job(owner, transcript="real")
    body = open_clip(ed["client"], f"{job}-1")
    return {"job": job, "clip": body["clip"]["id"], "v1": body["version"]["id"],
            "spec": body["version"]["spec"]}


def seed_post(w, *, status, platform="youtube", step=None, step_data=None, extra=None) -> str:
    pid = uuid.uuid4().hex[:16]
    meta = ({"title": "Old clip", "description": "", "tags": [], "category_id": "22"}
            if platform == "youtube" else {"caption": "Old clip"})
    with store.connect() as conn:
        store.insert_post(conn, {
            "id": pid, "owner_id": "local", "job_id": w["job"], "clip_id": w["clip"],
            "version_id": w["v1"], "platform": platform, "method": "official", "status": status,
            "approval": "approved", "holds": [], "slot_at": time.time() + 3 * 86400,
            "meta": meta, "ai_meta": meta, "step": step, "step_data": step_data,
            "created_at": time.time(), **(extra or {})})
    return pid


def post(pid) -> dict:
    with store.connect() as conn:
        return store.decode_row(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone())


def render_and_wait(ed, clip, spec, **body) -> dict:
    response = ed["client"].post(f"{API}/clips/{clip}/render", json={"spec": spec, **body})
    assert response.status_code == 202, response.text
    ed["worker"].drain()
    return response.json()


def fake_render(calls: list):
    """A fast stand-in for media.render.render_version that writes the files."""
    def fake(job_dir, source, transcript, spec, version, base, progress=None, **kwargs):
        calls.append({"version": version, "base": base})
        checked = editspec.parse(spec)
        names = render.file_names(base, version)
        for key in ("original", "vertical", "cover", "thumb"):
            (Path(job_dir) / names[key]).write_bytes(b"rendered")
        length = checked.out - checked.in_
        return {"version": version, "layout": checked.layout,
                "spans": [[checked.in_, checked.out]], "duration": length, "fps": 30.0,
                "files": {**{k: names[k] for k in ("original", "vertical", "cover", "thumb")},
                          "crop_track": None},
                "cover": {"t": 1.0, "face": False, "chosen": "auto"}, "crop": None,
                "probe": {"duration": length, "width": 1080, "height": 1920},
                "spec": checked.to_dict()}
    return fake


@pytest.mark.req("7.7", "7.9", "8.6", "N1.4")
def test_a_new_version_renders_in_the_background_and_unpublished_posts_switch(ed):
    client = ed["client"]
    w = clip_world(ed)
    switch = {s: seed_post(w, status=s) for s in ("scheduled", "waiting_approval",
                                                  "unscheduled", "needs_reconnect")}
    switch["retrying"] = seed_post(w, status="retrying", step="yt_init")      # nothing sent yet
    blocked = seed_post(w, status="blocked", extra={
        "last_error_class": "format", "last_error": "YouTube Shorts must be at most 180 s"})
    busy = seed_post(w, status="retrying", step="yt_upload", step_data={"session": {
        "uri": "https://www.googleapis.com/upload/x", "size": 1, "sha256": "x", "started_at": 0}})
    uploaded = seed_post(w, status="uploaded", extra={"platform_post_id": "vidU"})
    live = seed_post(w, status="live", extra={"platform_post_id": "vidL",
                                              "published_at": time.time() - 3600})
    assert {n["code"] for n in open_clip(client, w["clip"])["notes"]} == {
        "published", "uploaded", "busy"}                                 # 7.9, 8.6: said first
    assert open_clip(client, w["clip"])["switching"] == len(switch) + 1  # and the blocked one

    spec = {**w["spec"], "out": 9.0, "hook": {**w["spec"]["hook"], "text": "A new hook"}}
    response = client.post(f"{API}/clips/{w['clip']}/render", json={"spec": spec})
    assert response.status_code == 202
    queued = response.json()
    assert (queued["version"], queued["status"]) == (2, "rendering")
    assert 4.5 < queued["length"] <= 5.0                               # 4-9 s, trimmed to words
    notes = {n["code"]: n["message"] for n in queued["notes"]}
    assert set(notes) == {"published", "uploaded", "busy"}
    assert "will keep the current version" in notes["uploaded"]
    status = client.get(f"{API}/renders/{queued['version_id']}").json()
    assert (status["status"], status["state"], status["current"]) == ("rendering", "queued",
                                                                       False)
    assert client.post(f"{API}/clips/{w['clip']}/render", json={"spec": spec}).status_code == 409
    assert all(post(p)["version_id"] == w["v1"] for p in (*switch.values(), blocked))

    started = time.monotonic()
    assert ed["worker"].drain() == 1
    assert time.monotonic() - started < 180                            # N1.4, for a 5 s clip
    done = client.get(f"{API}/renders/{queued['version_id']}").json()
    assert (done["status"], done["progress"], done["current"]) == ("ready", 1.0, True)
    assert done["files"]["vertical"] == "01-old-clip-vertical.v2.mp4"
    info = probe(paths.output_dir() / w["job"] / done["files"]["vertical"])
    assert (info["width"], info["height"]) == (1080, 1920)
    assert abs(info["duration"] - queued["length"]) < 0.3
    for pid in switch.values():
        assert post(pid)["version_id"] == queued["version_id"]
    replanned = post(blocked)                       # the new version may fit now: planned again
    assert (replanned["version_id"], replanned["status"], replanned["last_error_class"]) == (
        queued["version_id"], "scheduled", None) and replanned["slot_at"]
    for pid in (busy, uploaded, live):
        assert post(pid)["version_id"] == w["v1"]
    assert post(uploaded)["step"] is None                               # not replaced unasked
    body = open_clip(client, w["clip"])
    assert body["version"]["version"] == 2 and [v["version"] for v in body["versions"]] == [2, 1]
    assert (body["clip"]["src_start"], body["clip"]["src_end"]) == (4.0, 9.0)
    [rendered] = activity.list_entries("local", {"kind": "clip.rendered"})
    head, tail = rendered["summary"].split("). ", 1)
    assert head.startswith("Rendered version 2 of clip 1 (")
    assert abs(float(head.rsplit("(", 1)[1].removesuffix(" s")) - queued["length"]) < 0.3
    assert tail == (
        "6 unpublished posts use it now. 1 uploaded YouTube video keeps the previous version. "
        "1 post that is publishing now keeps the previous version. 1 published post doesn't "
        "change.")
    assert len(activity.list_entries("local", {"kind": "post.version_changed"})) == 6


@pytest.mark.req("7.8")
def test_a_failed_render_keeps_the_previous_version_and_says_why(ed, monkeypatch):
    def broken(*args, **kwargs):
        raise render.RenderError("encode: ffmpeg failed: no space left on device")

    monkeypatch.setattr(render, "render_version", broken)
    client = ed["client"]
    w = clip_world(ed)
    waiting = seed_post(w, status="scheduled")
    failed = render_and_wait(ed, w["clip"], {**w["spec"], "out": 9.0})
    status = client.get(f"{API}/renders/{failed['version_id']}").json()
    assert (status["status"], status["current"]) == ("failed", False)
    assert "no space left on device" in status["error"]
    body = open_clip(client, w["clip"])
    assert body["version"]["version"] == 1 and body["versions"][0]["status"] == "failed"
    assert post(waiting)["version_id"] == w["v1"]
    [entry] = activity.list_entries("local", {"kind": "clip.render_failed"})
    assert "no space left on device" in entry["summary"]
    assert entry["summary"].endswith("The previous version stays in use.")
    monkeypatch.setattr(render, "render_version", fake_render([]))
    again = render_and_wait(ed, w["clip"], w["spec"])
    assert again["version"] == 3
    assert client.get(f"{API}/renders/{again['version_id']}").json()["status"] == "ready"


@pytest.mark.req("8.6")
def test_confirm_replace_gates_replacing_uploaded_youtube_videos(ed, monkeypatch):
    monkeypatch.setattr(render, "render_version", fake_render([]))
    w = clip_world(ed)
    idle = seed_post(w, status="uploaded", extra={"platform_post_id": "vidA"})
    checking = seed_post(w, status="uploaded", step="yt_verify",
                         extra={"platform_post_id": "vidB", "next_action_at": time.time() + 600})
    pausing = seed_post(w, status="uploaded", step="yt_unschedule",
                        extra={"platform_post_id": "vidC"})
    render_and_wait(ed, w["clip"], w["spec"])
    assert [post(p)["step"] for p in (idle, checking, pausing)] == [None, "yt_verify",
                                                                     "yt_unschedule"]
    second = render_and_wait(ed, w["clip"], w["spec"], confirm_replace=True)
    assert "will be replaced" in {n["code"]: n["message"] for n in second["notes"]}["uploaded"]
    for pid in (idle, checking):
        p = post(pid)
        assert (p["step"], p["step_data"]["replace_with"], p["version_id"]) == (
            "yt_replace", second["version_id"], w["v1"])        # yt_replace switches it (9.2)
    assert post(pausing)["step"] == "yt_unschedule"            # mid-pause: left alone
    bad = ed["client"].post(f"{API}/clips/{w['clip']}/render",
                            json={"spec": w["spec"], "confirm_replace": "yes"})
    assert bad.status_code == 422


def test_a_render_finishes_while_a_job_worker_is_busy(ed, monkeypatch):
    import app as app_module

    monkeypatch.setattr(render, "render_version", fake_render([]))
    w = clip_world(ed)
    busy, release = threading.Event(), threading.Event()

    def stuck(*args):
        busy.set()
        release.wait(30)

    monkeypatch.setattr(app_module, "_dispatch", stuck)

    def job_worker():                                   # one job worker, taking one job
        item = app_module._QUEUE.get()
        try:
            app_module._dispatch(*item)
        finally:
            app_module._QUEUE.task_done()

    thread = threading.Thread(target=job_worker, name="test-job-worker", daemon=True)
    thread.start()
    app_module._QUEUE.put((w["job"], {"autopilot": True}, None))
    try:
        assert busy.wait(10)
        ed["worker"].start()
        response = ed["client"].post(f"{API}/clips/{w['clip']}/render", json={"spec": w["spec"]})
        url = f"{API}/renders/{response.json()['version_id']}"
        deadline = time.monotonic() + 30
        while ed["client"].get(url).json()["status"] == "rendering" and \
                time.monotonic() < deadline:
            time.sleep(0.05)
        assert ed["client"].get(url).json()["status"] == "ready"
        assert thread.is_alive() and not release.is_set()      # the job worker is still busy
    finally:
        release.set()
        thread.join(10)


# ── refusals, restarts and owners ────────────────────────────────────────

def test_bad_specs_and_unready_clips_are_refused(ed):
    client = ed["client"]
    w = clip_world(ed)
    url = f"{API}/clips/{w['clip']}/render"
    bad = client.post(url, json={"spec": {**w["spec"], "out": 99.0}})
    assert bad.status_code == 422 and bad.json()["detail"][0]["field"] == "out"
    assert client.post(url, json={"spec": "everything"}).status_code == 422
    pending = open_clip(client, f"{old_job()}-1")                     # word timings not ready
    refused = client.post(f"{API}/clips/{pending['clip']['id']}/render",
                          json={"spec": pending["version"]["spec"]})
    assert refused.status_code == 409 and "word timings" in refused.json()["detail"]
    sources.delete(w["job"], None)
    assert client.get(f"{API}/clips/{w['clip']}").status_code == 404
    assert client.get(f"{API}/clips/not-a-clip").status_code == 404


@pytest.mark.req("7.8")
def test_a_render_cut_off_by_a_restart_is_marked_failed(ed):
    w = clip_world(ed)
    with store.connect() as conn:
        store.insert_clip_version(conn, {"id": "f" * 16, "clip_id": w["clip"], "version": 2,
                                         "spec": w["spec"], "status": "rendering",
                                         "created_at": time.time()})
    assert editor.recover() == 1
    status = ed["client"].get(f"{API}/renders/{'f' * 16}").json()
    assert (status["status"], status["error"]) == ("failed", editor.INTERRUPTED)
    assert open_clip(ed["client"], w["clip"])["version"]["version"] == 1


@pytest.mark.req("7.7", "N2.2")
def test_an_upload_that_started_meanwhile_keeps_the_version_it_sends(ed):
    w = clip_world(ed)
    pid = seed_post(w, status="scheduled")
    before = post(pid)                                  # what the publisher's step read
    with store.connect() as conn:                       # the editor switches the post meanwhile
        store.update_post(conn, pid, {"version_id": "a" * 16})
    pub = publisher.Publisher(holder="T")
    pub._commit(before, "yt_init", time.time(), outcome="ok", message=None, fields={
        "status": "publishing", "step": "yt_upload",
        "step_data": {"session": {"uri": "https://www.googleapis.com/upload/s"},
                      "version_id": w["v1"]}})
    pub.close()
    assert post(pid)["version_id"] == w["v1"]
    notes = [e["summary"] for e in activity.list_entries("local", {"post_id": pid})]
    assert any("keeps that one" in n for n in notes)


@pytest.mark.req("N4.2", "N3.2", "15.3")
def test_each_user_edits_only_their_own_clips_both_ways(two_users, monkeypatch):
    alice, bob = two_users
    worker = editor.RenderWorker()
    monkeypatch.setattr(editor, "WORKER", worker)
    monkeypatch.setattr(render, "render_version", fake_render([]))
    ids = {n: db.get_user_by_email(f"{n}@example.com")["id"] for n in ("alice", "bob")}
    jobs = {n: old_job(ids[n], transcript="real") for n in ids}
    for me, other, client in (("alice", "bob", alice), ("bob", "alice", bob)):
        mine = client.get(f"{API}/clips/{jobs[me]}-1")
        assert mine.status_code == 200
        clip, spec = mine.json()["clip"]["id"], mine.json()["version"]["spec"]
        queued = client.post(f"{API}/clips/{clip}/render", json={"spec": spec})
        assert queued.status_code == 202
        worker.drain()
        version = queued.json()["version_id"]
        assert client.get(f"{API}/renders/{version}").json()["status"] == "ready"
        theirs = bob if client is alice else alice
        for response in (theirs.get(f"{API}/clips/{jobs[me]}-1"),
                         theirs.get(f"{API}/clips/{clip}"),
                         theirs.post(f"{API}/clips/{clip}/proxy", json={}),
                         theirs.post(f"{API}/clips/{clip}/render", json={"spec": spec}),
                         theirs.get(f"{API}/renders/{version}")):
            assert response.status_code == 404
        assert worker.drain() == 0                      # the other user queued nothing
    worker.stop()
