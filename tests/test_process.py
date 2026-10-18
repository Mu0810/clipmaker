"""The Autopilot source pipeline (task 6.5, design §4)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from autopilot import activity, intake, posts, process, profiles, readiness, store
from autopilot import owner as owner_mod
from media import text as text_mod
from media import transcript as transcript_mod
from media.probe import probe
from tests.helpers.media import make_video

KOL = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=KOL).timestamp()      # Mon 2 Nov 2026, 09:00 IST
SENTENCES = [
    "Welcome to the show, today we talk money.",          # 0  \ clip 1 (score 91)
    "This one trick saves hours every week.",              # 1  /
    "नमस्ते दोस्तों, it works in Hindi too.",                # 2  \ clip 2 (score 72)
    "Try it on your very next video.",                    # 3  /
    "Here is some rather boring filler now.",             # 4  \ clip 3 (score 40)
    "And that really is the whole story.",                # 5  /
    "Thanks for watching, see you soon.",                 # 6
]
SETTINGS = {"clips_per_source": 3, "length_min_s": 6, "length_max_s": 12, "layout": "fit"}


def at(day: int, hour: int) -> float:
    return datetime(2026, 11, 2 + day, hour, 0, tzinfo=KOL).timestamp()


def fixture_transcript() -> dict:
    """Seven 5 s sentences with 0.2 s gaps: sentence k runs 5.2k to 5.2k + 5."""
    words = []
    for k, text in enumerate(SENTENCES):
        words += transcript_mod.spread_words(text, 5.2 * k, 5.2 * k + 5)
    return transcript_mod.build(words, language="en", source="whisper", estimated=False,
                                model="small")


def selection_reply(*clips) -> str:
    return json.dumps({"clips": [dict(zip(("start", "end", "title", "score", "reason"), c))
                                 for c in clips]})


TITLES = {"Welcome": "Save hours every single week", "नमस्ते": "It even works in Hindi",
          "boring": "The rest of the story"}


def metadata_reply(prompt: str) -> str:
    key = next(k for k in TITLES if k in prompt)
    return json.dumps({
        "youtube": {"title": TITLES[key], "description": f"{TITLES[key]}. Watch to the end.",
                    "tags": ["tips", "video"], "category_id": "26"},
        "instagram": {"caption": f"{TITLES[key]} #tips", "hashtags": ["#tips"]},
        "hook": "Wait for this one", "title_suggestions": [TITLES[key]]})


def check_reply(prompt: str) -> str:
    money = "Welcome" in prompt
    return json.dumps({"supported": True, "unsupported_claims": [], "sensitive": money,
                       "sensitive_categories": ["financial claims"] if money else [],
                       "reason": ""})


@pytest.fixture
def world(client, fake_llm, monkeypatch):
    """Everything a run needs, with no network, no Whisper model and a fixed clock:
    returns a dict with the queued ids, transcription calls and the AI replies."""
    state = {"enqueued": [], "transcribed": [], "selection": selection_reply(
        (0, 1, "Opening", 91, "Strong hook"), (2, 3, "Hindi switch", 72, "Fun switch"),
        (4, 5, "Filler", 40, "Weak"))}
    monkeypatch.setattr(intake, "_enqueue_fn", state["enqueued"].append)
    monkeypatch.setattr(posts, "_now", lambda: NOW)
    monkeypatch.setattr(readiness, "platform_ready", lambda owner, platform: (True, []))
    monkeypatch.setattr(text_mod, "shaping_available", lambda: False)

    def transcribe(video, language=None, model_name=None, progress=None):
        state["transcribed"].append({"video": Path(video).name, "language": language,
                                     "model": model_name})
        if progress:
            progress(1.0)
        return fixture_transcript()

    monkeypatch.setattr(transcript_mod, "transcribe_words", transcribe)

    def respond(prompt, system):
        if "Judge the PROPOSED text" in prompt:
            return check_reply(prompt)
        if "Write publish-ready metadata" in prompt:
            return metadata_reply(prompt)
        if "Pick up to" in prompt:
            return state["selection"]
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    fake_llm.responder = respond
    owner_mod.set_prefs("local", timezone="Asia/Kolkata")
    profile = profiles.ensure_default("local")
    profiles.update("local", profile["id"], settings_patch=SETTINGS)
    state["profile_id"] = profile["id"]
    return state


def submit(seconds=40) -> str:
    import paths

    staged = make_video(paths.output_dir() / "upload-test.mp4", seconds=seconds,
                        size=(640, 360))
    return intake.submit_file(staged, "My Talk.mp4", "local", rights_confirmed=True)["job_id"]


def rows(sql: str, *params) -> list[dict]:
    with store.connect() as conn:
        return store.decode_rows(conn.execute(sql, params).fetchall())


@pytest.mark.req("1.2", "1.3", "1.4", "2.3", "3.1", "14.3", "N8.1", "13.3", "3.5")
def test_a_source_runs_end_to_end(client, world, fake_llm):
    job_id = submit()
    assert world["enqueued"] == [job_id]
    # The settings used are the ones stored at submission (14.3): raising the
    # profile's minimum score afterwards changes nothing for this source.
    profiles.update("local", world["profile_id"], settings_patch={"min_score": 95})

    out = process.run(job_id)

    assert world["transcribed"] == [{"video": "source.mp4", "language": None, "model": "small"}]
    clips = rows("SELECT * FROM clips WHERE job_id=? ORDER BY idx", job_id)
    assert [(c["idx"], c["score"], c["score_reason"]) for c in clips] == [
        (1, 91, "Strong hook"), (2, 72, "Fun switch"), (3, 40, "Weak")]
    assert all(c["method"] == "ai:fake-model" for c in clips)
    assert clips[0]["sensitive"] == {"flag": True, "categories": ["financial claims"]}
    assert clips[1]["meta"]["holds"] == ["text_shaping"]
    versions = rows("SELECT v.* FROM clip_versions v JOIN clips c ON c.id = v.clip_id "
                    "WHERE c.job_id=? ORDER BY c.idx", job_id)
    assert [(v["version"], v["status"]) for v in versions] == [(1, "ready")] * 3
    # 9.2: the cover the render chose is kept, for Instagram's thumb_offset.
    assert all(v["cover"]["chosen"] == "auto" and 0 <= v["cover"]["t"] < v["probe"]["duration"]
               for v in versions)
    vertical = probe(_job_dir(job_id) / versions[0]["files"]["vertical"])
    assert (vertical["width"], vertical["height"], vertical["video_codec"],
            vertical["audio_codec"]) == (1080, 1920, "h264", "aac")

    planned = rows("SELECT * FROM posts WHERE job_id=? ORDER BY slot_at", job_id)
    by_clip = {clip["id"]: clip["idx"] for clip in clips}
    got = {(by_clip[p["clip_id"]], p["platform"]): p for p in planned}
    assert set(got) == {(1, "youtube"), (1, "instagram"), (2, "youtube"), (2, "instagram")}
    assert {k: v["slot_at"] for k, v in got.items()} == {
        (1, "instagram"): at(0, 11), (1, "youtube"): at(0, 15),
        (2, "instagram"): at(0, 19), (2, "youtube"): at(1, 12)}
    windows = profiles.ensure_default("local")["settings"]["windows"]
    for (_, platform), post in got.items():
        slot = datetime.fromtimestamp(post["slot_at"], KOL)
        assert slot.hour in windows[platform][str(slot.weekday())]
    assert [h["code"] for h in got[(1, "youtube")]["holds"]] == ["sensitive"]
    assert [h["code"] for h in got[(2, "instagram")]["holds"]] == ["text_shaping"]
    assert all(p["approval"] == "pending" for p in planned)
    assert got[(1, "youtube")]["meta"]["title"] == "Save hours every single week"

    job = rows("SELECT * FROM jobs WHERE job_id=?", job_id)[0]
    assert job["ai_calls"] == 7 == out["ai_calls"]              # 1 selection + 2 per clip
    assert job["ai_calls"] <= 3 * len(clips) + 3
    assert (job["status"], job["pct"], job["stage"], job["best_score"]) == ("done", 100, None, 91)
    assert job["message"] == "Done: 3 clips, 4 posts planned."
    assert [s for s, _ in out["stages"]] == [
        "downloading", "transcribing", "choosing", "metadata", "rendering", "metadata",
        "rendering", "metadata", "rendering", "planning"]
    assert [c for s, c in out["stages"] if s == "rendering"] == [(1, 3), (2, 3), (3, 3)]

    status = client.get(f"/api/status/{job_id}").json()     # the library sees the result
    assert status["status"] == "done"
    assert [c["virality"] for c in status["result"]["clips"]] == [91, 72, 40]
    assert status["result"]["clips"][0]["title"] == "Save hours every single week"
    kinds = [e["kind"] for e in activity.list_entries("local", {"job_id": job_id})]
    assert {"clips.chosen", "clip.rendered", "post.held", "post.planned", "source.done"} <= set(kinds)
    assert not (Path(job["request"]["path"]).exists())        # the upload moved into the job


def _job_dir(job_id: str) -> Path:
    import paths

    return paths.output_dir() / job_id


@pytest.mark.req("1.7")
def test_no_posts_when_every_clip_is_below_the_minimum(client, world):
    profiles.update("local", world["profile_id"], settings_patch={"clips_per_source": 1})
    world["selection"] = selection_reply((0, 1, "Opening", 48, "Meh"))
    job_id = submit(seconds=12)
    out = process.run(job_id)
    assert out["posts"] == [] and rows("SELECT id FROM posts WHERE job_id=?", job_id) == []
    job = rows("SELECT * FROM jobs WHERE job_id=?", job_id)[0]
    assert (job["status"], job["best_score"], job["ai_calls"]) == ("done", 48, 3)
    assert "minimum score of 60 (best: 48)" in job["message"]
    no_clips = activity.list_entries("local", {"kind": "source.no_clips"})
    assert len(no_clips) == 1 and "best: 48" in no_clips[0]["summary"]
    # The clip is still rendered and in the library, for manual use.
    assert len(rows("SELECT id FROM clips WHERE job_id=?", job_id)) == 1
    assert client.get(f"/api/status/{job_id}").json()["result"]["clips"][0]["virality"] == 48


@pytest.mark.req("1.8")
def test_with_autopilot_off_no_posts_are_created(client, world, monkeypatch):
    import app as app_module

    job_id = submit(seconds=4)
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET autopilot=0 WHERE job_id=?", (job_id,))
    with pytest.raises(process.ProcessError, match="Autopilot off"):
        process.run(job_id)
    assert posts.create_posts_for_job("local", job_id) == {"created": [], "skipped": []}
    # The worker sends a manual source to the manual flow, never to process.run.
    calls = []
    monkeypatch.setattr(app_module, "_run_job", lambda job, req, src: calls.append(("manual", job)))
    monkeypatch.setattr(process, "run", lambda job: calls.append(("autopilot", job)))
    app_module._dispatch("m1", app_module.ProcessRequest(url="clip.mp4"), "clip.mp4")
    app_module._dispatch("a1", {"autopilot": True, "kind": "url"}, None)
    assert calls == [("manual", "m1"), ("autopilot", "a1")]
    assert rows("SELECT id FROM posts") == []


@pytest.mark.req("1.5")
def test_a_failure_is_recorded_on_the_source(client, world, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("Whisper ran out of memory")

    monkeypatch.setattr(transcript_mod, "transcribe_words", broken)
    job_id = submit(seconds=4)
    with pytest.raises(RuntimeError, match="out of memory"):
        process.run(job_id)
    job = rows("SELECT * FROM jobs WHERE job_id=?", job_id)[0]
    assert (job["status"], job["error"], job["stage"]) == ("error", "Whisper ran out of memory",
                                                           None)
    assert (job["message"], job["ai_calls"]) == ("Error: Whisper ran out of memory", 0)
    failed = activity.list_entries("local", {"kind": "source.failed"})
    assert len(failed) == 1 and "out of memory" in failed[0]["summary"]


@pytest.mark.req("1.10", "N8.1")
def test_a_failed_video_tried_again_replaces_its_clips_and_counts_every_try(client, world,
                                                                            monkeypatch):
    seen = transcript_mod.transcribe_words           # the world's fake, which counts

    def transcribe(video, language=None, model_name=None, progress=None):
        # Like the real one, the transcript says what it was made from, so a
        # second try can reuse it.
        return {**seen(video, language, model_name, progress),
                "media": transcript_mod._media_id(Path(video)), "requested_language": language}

    monkeypatch.setattr(transcript_mod, "transcribe_words", transcribe)
    original, broken = process._make_clip, {"on": True}

    def make(*args, **kwargs):
        if broken["on"] and args[6] == 2:            # clip 2 of 3
            raise RuntimeError("The AI engine stopped answering")
        return original(*args, **kwargs)

    monkeypatch.setattr(process, "_make_clip", make)
    job_id = submit()
    with pytest.raises(RuntimeError, match="stopped answering"):
        process.run(job_id)
    job = rows("SELECT * FROM jobs WHERE job_id=?", job_id)[0]
    assert (job["status"], job["ai_calls"]) == ("error", 3)     # the selection, and clip 1's two
    [left] = rows("SELECT id FROM clips WHERE job_id=?", job_id)    # clip 1 was rendered

    broken["on"] = False
    reply = client.post(f"/api/autopilot/sources/{job_id}/retry")
    assert reply.status_code == 200, reply.text
    assert world["enqueued"] == [job_id, job_id]                # submitted, then tried again
    out = process.run(job_id)

    clips = rows("SELECT * FROM clips WHERE job_id=? ORDER BY idx", job_id)
    assert [c["idx"] for c in clips] == [1, 2, 3] and left["id"] not in {c["id"] for c in clips}
    assert rows("SELECT id FROM clip_versions WHERE clip_id=?", left["id"]) == []
    planned = rows("SELECT clip_id, platform FROM posts WHERE job_id=?", job_id)
    assert sorted((p["clip_id"], p["platform"]) for p in planned) == sorted(
        (c["id"], p) for c in clips[:2] for p in ("youtube", "instagram"))   # no clip twice
    job = rows("SELECT * FROM jobs WHERE job_id=?", job_id)[0]
    assert (job["status"], job["message"]) == ("done", "Done: 3 clips, 4 posts planned.")
    assert job["ai_calls"] == 3 + 7 == out["ai_calls"]               # every try's calls (N8.1)
    assert len(world["transcribed"]) == 1                           # the first try's transcript
    status = client.get(f"/api/status/{job_id}").json()
    assert [c["virality"] for c in status["result"]["clips"]] == [91, 72, 40]


@pytest.mark.req("1.5", "1.10")
def test_a_re_run_first_discards_the_clips_an_unfinished_try_left(client):
    import paths

    job_id = "abcdef012345"
    job_dir = paths.output_dir() / job_id
    job_dir.mkdir(parents=True)
    with store.connect() as conn:
        for clip_id, idx in (("stale", 1), ("posted", 2)):
            store.insert_clip(conn, {"id": clip_id, "owner_id": "local", "job_id": job_id,
                                     "idx": idx, "src_start": 0.0, "src_end": 5.0,
                                     "current_version_id": f"v-{clip_id}", "created_at": 1.0})
            store.insert_clip_version(conn, {
                "id": f"v-{clip_id}", "clip_id": clip_id, "version": 1, "spec": {},
                "status": "ready", "created_at": 1.0,
                "files": {"vertical": f"0{idx}-clip-v1.mp4", "thumb": "shared.jpg", "cover": None}})
        store.insert_post(conn, {"id": "p1", "owner_id": "local", "job_id": job_id,
                                 "clip_id": "posted", "platform": "youtube", "method": "official",
                                 "status": "live", "approval": "approved", "holds": [],
                                 "meta": {}, "ai_meta": {}, "created_at": 1.0})
    for name in ("01-clip-v1.mp4", "02-clip-v1.mp4", "shared.jpg", "source.mp4"):
        (job_dir / name).write_bytes(b"x")

    assert process._discard_earlier_attempt(job_id, job_dir) == 1
    assert [c["id"] for c in rows("SELECT id FROM clips WHERE job_id=?", job_id)] == ["posted"]
    assert rows("SELECT id FROM clip_versions WHERE clip_id='stale'") == []
    # The stale clip's own file goes; the one a kept version also names, and the video, stay.
    assert sorted(p.name for p in job_dir.iterdir()) == ["02-clip-v1.mp4", "shared.jpg",
                                                         "source.mp4"]
    assert process._discard_earlier_attempt(job_id, job_dir) == 0


@pytest.mark.req("1.9")
def test_deleting_the_source_mid_processing_stops_it_with_no_posts(client, world, monkeypatch):
    from autopilot import sources

    job_id = submit(seconds=40)
    original = process._make_clip

    def make_then_delete(*args, **kwargs):
        made = original(*args, **kwargs)
        if not sources.is_deleted(job_id):
            assert sources.delete(job_id, None)["cancelled"] == []   # nothing planned yet
        return made

    monkeypatch.setattr(process, "_make_clip", make_then_delete)
    out = process.run(job_id)
    assert out == {"job_id": job_id, "deleted": True}
    assert len(rows("SELECT * FROM clips WHERE job_id=?", job_id)) == 1    # stopped after one
    assert rows("SELECT * FROM posts WHERE job_id=?", job_id) == []
    assert not _job_dir(job_id).exists()                    # the render had made it again
    assert activity.list_entries("local", {"kind": "source.failed"}) == []
    assert process.run(job_id) == {"job_id": job_id, "deleted": True}    # queued again: skipped


@pytest.mark.req("1.3")
def test_the_stage_and_overall_percentage_are_shown(client, world):
    assert intake.stage_pct("downloading") == 0
    assert intake.stage_pct("transcribing", None, 0.5) == 28           # 15 + 25 x 0.5
    assert intake.stage_pct("metadata", (1, 2), 0.0) == 45
    assert intake.stage_pct("rendering", (2, 4), 0.5) == 64            # 45 + 50 x 1.5 / 4
    assert intake.stage_pct("planning") == 95
    assert intake.stage_message("metadata", (1, 3)) == "Writing metadata for clip 1 of 3"
    assert intake.stage_message("choosing") == "Choosing clips"
    job_id = submit(seconds=2)
    assert intake.set_stage(job_id, "rendering", (2, 3), 0.5) == 70
    status = client.get(f"/api/status/{job_id}").json()
    assert (status["stage"], status["pct"], status["message"]) == ("rendering", 70,
                                                                   "Rendering clip 2 of 3")
