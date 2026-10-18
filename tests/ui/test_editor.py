"""The clip editor in the browser (task 12.6; Requirements 7.1-7.10, 8.6,
N5.1, N5.2), in the Script layout chosen in task 12.1. Run with `pytest -m ui`.

A real 40 s test video and its preview proxy are made once per session with
ffmpeg and copied into each test's job folder, so the editor plays a real
proxy. Renders go through the editor's real render worker with
media.render.render_version replaced by a quick fake that reports progress."""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid

import pytest
from playwright.sync_api import expect

import db
import editing
import paths
from autopilot import editor, store
from media import editspec, keep, overlays, render
from media import transcript as transcript_mod
from media.probe import probe
from tests.helpers.media import make_video

pytestmark = pytest.mark.ui

TITLE = "Why your first 1,000 followers are the hardest"
SCRIPT = [(0.5, "People ask me how long it took."), (3.4, "Honestly? Longer than I'd like to admit."),
          (10.2, "Your first thousand followers are the hardest."),
          (13.5, "Not because you're bad, but because nobody's watching yet."),
          (18.5, "Um,"), (19.6, "so you post into silence for months."),
          (22.9, "And that silence is the test."), (26.5, "And then everything changed in one week.")]
CLIP_IN, CLIP_OUT = 10.0, 25.4


def script_words(script=SCRIPT) -> list[dict]:
    """A word every 0.4 s within a sentence, each 0.3 s long."""
    words = []
    for start, sentence in script:
        for n, token in enumerate(sentence.split()):
            s = round(start + 0.4 * n, 3)
            words.append({"w": token, "s": s, "e": round(s + 0.3, 3), "p": 0.95})
    return words


@pytest.fixture(scope="session")
def media_files(tmp_path_factory) -> dict:
    """The source video and the proxy of its default window, made once."""
    folder = tmp_path_factory.mktemp("editor-media")
    source = make_video(folder / "source.mp4", seconds=40.0, size=(640, 360), fps=25)
    duration = float(probe(source)["duration"])
    a, b = editor.default_window({"src_start": CLIP_IN, "src_end": CLIP_OUT}, duration)
    proxy = folder / editor.proxy_name(a, b)
    editor._build_proxy(source, proxy, a, b, lambda fraction: None)
    return {"source": source, "proxy": proxy, "duration": duration, "window": (a, b)}


@pytest.fixture
def clip(seed, media_files) -> dict:
    """An Autopilot clip with a real transcript, version 1 and its proxy."""
    seed.timezone("Asia/Kolkata")
    job = seed.source(title="Creator Economy Podcast · Ep 42")
    folder = paths.output_dir() / job
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(media_files["source"], folder / "source.mp4")
    shutil.copy(media_files["proxy"], folder / media_files["proxy"].name)
    words = script_words()
    transcript_mod.save(folder, transcript_mod.build(words, language="en", source="whisper", estimated=False,
                                                     model="small"))
    clip_id, version_id = uuid.uuid4().hex[:16], uuid.uuid4().hex[:16]
    spec = editspec.initial(CLIP_IN, CLIP_OUT, {"layout": "fill", "caption_style": "words", "hook": True,
                                                "auto_trim": True}, hook_text="Nobody tells you this").to_dict()
    with store.connect() as conn:
        store.insert_clip(conn, {"id": clip_id, "owner_id": "local", "job_id": job, "idx": 1,
                                 "src_start": CLIP_IN, "src_end": CLIP_OUT, "score": 84,
                                 "score_reason": "Clear payoff at 0:38", "method": "ai:test",
                                 "meta": {"youtube": {"title": TITLE}}, "current_version_id": version_id,
                                 "created_at": time.time()})
        store.insert_clip_version(conn, {"id": version_id, "clip_id": clip_id, "version": 1, "spec": spec,
                                         "status": "ready", "files": {"vertical": "01-clip-vertical.mp4",
                                                                      "cover": None, "thumb": None,
                                                                      "crop_track": None},
                                         "created_at": time.time(), "finished_at": time.time()})
    db.finish(job, "done", result={"job_id": job, "source_title": "Creator Economy Podcast · Ep 42",
                                   "method": "llm", "transcript_source": "whisper", "clips": [{
                                       "index": 1, "title": TITLE, "hook": "Nobody tells you this",
                                       "start": CLIP_IN, "end": CLIP_OUT, "duration": 13.1,
                                       "original": "01-clip.mp4", "vertical": "01-clip-vertical.mp4"}]})
    return {"job": job, "id": clip_id, "v1": version_id, "spec": spec, "words": words, "seed": seed}


def open_editor(page, clip) -> None:
    page.goto(f"/#/editor/{clip['id']}")
    expect(page.locator("#apView h1")).to_have_text(TITLE)
    page.wait_for_function("document.querySelector('.ap-pv-video').readyState >= 2")


def word(page, text: str, nth: int = 0):
    return page.locator("#apView .ap-w").filter(has_text=re.compile(rf"^{re.escape(text)}")).nth(nth)


# ── the time mapping matches media/keep.py (7.6) ─────────────────────────

FIXTURES = [
    {"in": CLIP_IN, "out": CLIP_OUT, "cuts": [], "auto_trim": True},
    {"in": CLIP_IN, "out": CLIP_OUT, "cuts": [], "auto_trim": False},
    {"in": CLIP_IN, "out": CLIP_OUT, "cuts": [[12.0, 13.5], [13.4, 14.0], [20.0, 20.0005]], "auto_trim": True},
    {"in": 0.0, "out": 40.0, "cuts": [[18.4, 18.9]], "auto_trim": True},
    {"in": 13.55, "out": 22.0, "cuts": [[15.0, 15.0], [16.9, 16.0]], "auto_trim": True},
]
# Exact binary halves at the third decimal (12.0625 is one): Python rounds them
# half to even; Math.round(x * 1000) / 1000 would round them up. A cut ends just
# before "then", so its span starts half-way between "uh" and "then": 12.0625.
TIES = [{"w": "uh", "s": 11.9, "e": 12.0}, {"w": "then", "s": 12.125, "e": 12.5},
        {"w": "we", "s": 13.1875, "e": 13.3125}, {"w": "went", "s": 13.4375, "e": 14.0}]
MIXED = [{"w": "UH...", "s": 1.0, "e": 1.2}, {"w": "you", "s": 2.0, "e": 2.2}, {"w": "know", "s": 2.3, "e": 2.6},
         {"w": "नमस्ते", "s": 3.4, "e": 3.9}, {"w": "दोस्तों,", "s": 4.0, "e": 4.6}, {"w": "I", "s": 5.4, "e": 5.5},
         {"w": "mean", "s": 5.6, "e": 5.9}, {"w": "it.", "s": 6.0, "e": 6.2}, {"w": "Hmm", "s": 7.0, "e": 7.3}]


@pytest.mark.req("7.6", "7.2", "3.6")
def test_the_preview_time_mapping_matches_keep_py(page, clip):
    words = clip["words"]
    cases = [(words, f) for f in FIXTURES]
    cases += [(TIES, {"in": 11.0, "out": 14.2, "cuts": [[11.5, 12.06]], "auto_trim": True}),
              (MIXED, {"in": 0.5, "out": 7.5, "cuts": [], "auto_trim": True}),
              (MIXED, {"in": 0.5, "out": 7.5, "cuts": [[2.25, 2.3]], "auto_trim": True}),
              ([], {"in": 1.0, "out": 9.0, "cuts": [[2.0, 3.0]], "auto_trim": True})]
    expected = []
    for ws, f in cases:
        spans = keep.keep_spans(ws, f["in"], f["out"], f["cuts"], f["auto_trim"])
        total = keep.kept_duration(spans)
        probes = [f["in"] - 1, f["in"], 12.05, 13.3, 18.6, 20.0, f["out"], f["out"] + 3]
        clip_times = [0.0, 0.5, total / 2, total - 0.001, total, total + 1]
        numbered = [{**w, "i": 100 + k} for k, w in enumerate(ws)]
        edits = {str(100 + k): "EDIT" for k in range(0, len(ws), 3)}
        expected.append({
            "spans": [list(s) for s in spans], "total": total,
            "clip": [keep.to_clip_time(spans, t) for t in probes],
            "source": [keep.to_source_time(spans, t) for t in clip_times],
            "kept": [[w["text"], w["s"], w["e"]] for w in overlays.kept_words(numbered, spans, {
                str(k): edits[str(100 + k)] for k in range(0, len(ws), 3)})],
            "probes": probes, "clip_times": clip_times, "numbered": numbered, "edits": edits})
    page.goto("/")
    got = page.evaluate("""async ({cases, expected}) => {
        const P = await import('/js/preview.js');
        return cases.map(([words, f], n) => {
          const spans = P.keepSpans(words, f.in, f.out, f.cuts, f.auto_trim);
          const e = expected[n];
          return {spans, total: P.keptDuration(spans),
                  clip: e.probes.map(t => P.toClipTime(spans, t)),
                  source: e.clip_times.map(t => P.toSourceTime(spans, t)),
                  kept: P.keptWords(e.numbered, spans, e.edits).map(w => [w.text, w.s, w.e])};
        });
      }""", {"cases": cases, "expected": expected})
    for n, (mine, theirs) in enumerate(zip(got, expected)):
        assert mine["spans"] == theirs["spans"], f"case {n}"
        # The length: exact under Python 3.12+ (compensated sum), as near as a bit otherwise.
        assert mine["total"] == pytest.approx(theirs["total"], abs=1e-12), f"case {n}"
        assert mine["clip"] == theirs["clip"], f"case {n}"
        assert mine["source"] == theirs["source"], f"case {n}"
        assert mine["kept"] == theirs["kept"], f"case {n}"
    assert expected[len(FIXTURES)]["spans"][0][0] == 12.062            # the tie, half to even
    js = page.evaluate("async () => { const P = await import('/js/preview.js');"
                       " return {filler: [...P.FILLER].sort(), gap: P.GAP_S, pad: P.PAD_S,"
                       " ties: [0.0625, 2.5005, 1.0005, 12.0625, 7.8125].map(P.round3)}; }")
    assert js["filler"] == sorted(editing._FILLER)
    assert (js["gap"], js["pad"]) == (keep.GAP_S, keep.PAD_S)
    assert js["ties"] == [round(x, 3) for x in (0.0625, 2.5005, 1.0005, 12.0625, 7.8125)]
    sentences = page.evaluate("async (words) => (await import('/js/preview.js')).sentences(words)"
                              ".map(s => [s.w0, s.w1])", words)
    assert sentences == [[s["w0"], s["w1"]] for s in transcript_mod.sentences(words)]


# ── handles, words, undo ────────────────────────────────────────────────

HANDLE_MOVE = """async (key) => {
  const video = document.querySelector('.ap-pv-video');
  const handle = document.querySelector('[aria-label="Clip start, fine trim"]');
  const seeked = new Promise(resolve => video.addEventListener('seeked', () => resolve(performance.now()),
                                                               { once: true }));
  const t0 = performance.now();
  handle.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
  const t1 = performance.now();
  const t2 = await seeked;
  return { sync: t1 - t0, seeked: t2 - t0, now: video.currentTime, text: handle.getAttribute('aria-valuetext') };
}"""


@pytest.mark.req("7.1", "7.2", "N5.1")
def test_a_handle_move_updates_the_preview_within_200_ms(page, clip):
    open_editor(page, clip)
    expect(page.locator("#apView .ap-ed-strip-lab")).to_contain_text("start 0:10.0 · end 0:25.4")
    expect(page.locator("#apView .ap-ed-eyebrow")).to_have_text(re.compile(r"^Clip 1 · \d+\.\d s · fits Shorts and Reels$"))
    handle = page.get_by_role("slider", name="Clip start, fine trim")
    expect(handle).to_have_attribute("aria-valuetext", "0:10.0")
    timing = page.evaluate(HANDLE_MOVE, "ArrowRight")
    assert timing["seeked"] < 200, timing                          # 7.2: within 200 ms
    assert timing["sync"] < 50, timing
    assert timing["text"] == "0:10.1"
    spans = keep.keep_spans(clip["words"], 10.1, CLIP_OUT, [], True)
    assert timing["now"] == pytest.approx(spans[0][0], abs=0.01)    # the preview shows the new start
    expect(page.locator("#apView .ap-ed-strip-lab")).to_contain_text("start 0:10.1")

    handle.focus()
    page.keyboard.press("Shift+ArrowRight")                        # 1 s with Shift
    expect(handle).to_have_attribute("aria-valuetext", "0:11.1")
    page.keyboard.press("ArrowLeft")
    expect(handle).to_have_attribute("aria-valuetext", "0:11.0")
    end = page.get_by_role("slider", name="Clip end, fine trim")
    end.focus()
    page.keyboard.press("Shift+ArrowLeft")
    expect(end).to_have_attribute("aria-valuetext", "0:24.4")
    # The START pin moves a word at a time, and [ sets it on the focused word.
    pin = page.get_by_role("slider", name="Clip start pin")
    expect(pin).to_have_attribute("aria-valuetext", re.compile(r"“thousand”$"))     # in = 11.0 s
    pin.focus()
    page.keyboard.press("ArrowLeft")
    expect(pin).to_have_attribute("aria-valuetext", re.compile(r"“first”$"))
    word(page, "followers").click()
    page.keyboard.press("[")
    expect(pin).to_have_attribute("aria-valuetext", re.compile(r"“followers”$"))
    expect(word(page, "Your")).to_have_class(re.compile(r"\bout\b"))


@pytest.mark.req("7.3", "7.4", "7.6", "N5.1")
def test_deleting_words_cuts_them_and_editing_a_word_changes_it(page, clip):
    open_editor(page, clip)
    length = page.locator("#apView .ap-ed-eyebrow")
    before = float(re.search(r"(\d+\.\d) s", length.text_content()).group(1))
    pending = page.locator("#apView .ap-ed-pending")
    expect(pending).to_be_hidden()                                   # nothing edited yet
    word(page, "Not").click()
    word(page, "bad,").click(modifiers=["Shift"])
    expect(word(page, "because")).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("Delete")
    for text in ("Not", "because", "you're", "bad,"):
        expect(word(page, text)).to_have_class(re.compile(r"\bcut\b"))
    expect(word(page, "Not")).to_have_text("Not (cut)")
    expect(page.locator("#apStatus")).to_have_text("Cut 4 words.")
    expect(pending).to_have_text("Edits not saved yet")          # so Back isn't taken for saving
    expect(page.get_by_role("button", name="Save as version 2")).to_have_accessible_description(
        "Not saved yet: the clip's posts keep version 1 until you save.")
    expect(page.locator("#apView .ap-ed-removed .cut")).to_have_count(1)
    after = float(re.search(r"(\d+\.\d) s", length.text_content()).group(1))
    assert after < before - 1.0
    # The filler and the long pauses are trimmed and marked (3.6).
    expect(word(page, "Um,")).to_have_class(re.compile(r"\btrim\b"))
    expect(page.locator("#apView .ap-ed-hint")).to_contain_text("1 filler word and")

    word(page, "thousand").dblclick()
    edit = page.get_by_role("textbox", name=re.compile(r"^Subtitle for “thousand”"))
    expect(edit).to_be_focused()
    edit.fill("1,000")
    page.keyboard.press("Enter")
    fixed = word(page, "1,000")
    expect(fixed).to_have_class(re.compile(r"\bfixed\b"))
    expect(fixed).to_be_focused()
    expect(fixed).to_have_attribute("title", "The transcript says “thousand”")
    expect(page.locator("#apView .ap-pv-caption")).to_contain_text("1,000")   # the preview's caption
    expect(page.locator("#apView .ap-pv-badge")).to_have_text("Preview · the rendered video may look slightly different")
    page.keyboard.press("Enter")                                   # Enter edits too; Escape cancels
    page.keyboard.type("x")
    page.keyboard.press("Escape")
    expect(word(page, "1,000")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/#/editor/"))            # Escape didn't leave the editor


@pytest.mark.req("7.10", "N5.1")
def test_undo_and_redo_walk_every_edit(page, clip):
    open_editor(page, clip)
    undo = page.get_by_role("button", name="Undo")
    redo = page.get_by_role("button", name="Redo")
    expect(undo).to_be_disabled()
    word(page, "silence", 0).click()
    page.keyboard.press("Delete")
    expect(word(page, "silence", 0)).to_have_class(re.compile(r"\bcut\b"))
    hook = page.get_by_role("textbox", name="Hook text")
    hook.fill("Nobody tells you")                                  # typing is one undo step
    hook.press("End")
    hook.type(" this, ever")
    expect(page.locator("#apView .ap-pv-hook")).to_have_text("NOBODY TELLS YOU THIS, EVER")

    word(page, "silence", 0).focus()
    page.keyboard.press("ControlOrMeta+z")                         # the hook text
    expect(hook).to_have_value("Nobody tells you this")
    expect(page.locator("#apStatus")).to_have_text("Undid: Changed the hook text.")
    page.keyboard.press("ControlOrMeta+z")                         # the cut
    expect(word(page, "silence", 0)).not_to_have_class(re.compile(r"\bcut\b"))
    expect(undo).to_be_disabled()
    expect(redo).to_be_enabled()
    page.keyboard.press("ControlOrMeta+Shift+z")
    expect(word(page, "silence", 0)).to_have_class(re.compile(r"\bcut\b"))
    redo.click()
    expect(hook).to_have_value("Nobody tells you this, ever")
    expect(redo).to_be_disabled()
    expect(page.locator("#apStatus")).to_have_text("Redid: Changed the hook text.")
    # Unrendered edits wait while the editor is closed.
    page.get_by_role("button", name="Back").click()
    page.goto(f"/#/editor/{clip['id']}")
    expect(page.locator("#apView .ap-ed-status")).to_have_text("Your edits that weren't saved yet are back.")
    expect(word(page, "silence", 0)).to_have_class(re.compile(r"\bcut\b"))
    expect(undo).to_be_enabled()


# ── rendering, warnings, entry points, panels ───────────────────────────

@pytest.fixture
def worker(monkeypatch) -> list:
    """The editor's render worker, running, with a fake render that reports
    progress over a second and a half. Returns the specs it rendered."""
    from pathlib import Path

    calls: list = []

    def fake(job_dir, source, transcript, spec, version, base, progress=None, **kwargs):
        checked = editspec.parse(spec)
        for step, fraction in (("cut", 0.2), ("encode", 0.5), ("encode", 0.8)):
            if progress:
                progress(step, fraction)
            time.sleep(0.5)
        names = render.file_names(base, version)
        for key in ("original", "vertical", "cover", "thumb"):
            (Path(job_dir) / names[key]).write_bytes(b"rendered")
        spans = keep.keep_spans(transcript["words"], checked.in_, checked.out, checked.cuts, checked.auto_trim)
        length = keep.kept_duration(spans)
        calls.append(checked.to_dict())
        return {"version": version, "layout": checked.layout, "spans": spans, "duration": length, "fps": 30.0,
                "files": {**{k: names[k] for k in ("original", "vertical", "cover", "thumb")}, "crop_track": None},
                "cover": {"t": 1.0, "face": False, "chosen": "auto"}, "crop": None,
                "probe": {"duration": length, "width": 1080, "height": 1920}, "spec": checked.to_dict()}

    monkeypatch.setattr(render, "render_version", fake)
    editor.WORKER.start()
    yield calls
    editor.WORKER.stop()


def add_post(clip, **fields) -> str:
    return clip["seed"].post(job_id=clip["job"], clip_id=clip["id"], version_id=clip["v1"], approval="approved",
                             **fields)


@pytest.mark.req("7.7", "7.8", "N5.2")
def test_render_shows_progress_and_the_new_version_takes_over(page, clip, worker, monkeypatch):
    pid = add_post(clip, platform="youtube", status="scheduled", slot_at=time.time() + 3 * 86400)
    open_editor(page, clip)
    render_button = page.get_by_role("button", name=re.compile(r"^Save as version 2"))
    expect(render_button).to_be_disabled()                         # nothing changed yet
    word(page, "months.").click()
    page.keyboard.press("Delete")
    expect(render_button).to_be_enabled()
    render_button.click()
    meter = page.get_by_role("progressbar", name="Rendering version 2")
    expect(meter).to_be_visible()
    expect(page.locator("#apStatus")).to_have_text("Rendering version 2.")
    page.wait_for_function("""() => {
        const m = document.querySelector('[role=progressbar][aria-label="Rendering version 2"]');
        return m && Number(m.getAttribute('aria-valuenow')) >= 20; }""")
    expect(page.locator("#apView .ap-ed-meter")).to_contain_text(re.compile(r"Rendering version 2 · [1-9]\d%"))
    status = page.locator("#apView .ap-ed-status")
    expect(status).to_have_text("Version 2 is ready and in use. The clip's posts that aren't out yet use it now.",
                                timeout=10_000)
    expect(page.locator("#apView .ap-ed-versions summary")).to_have_text("Version 2 · in use")
    expect(meter).to_be_hidden()
    expect(page.get_by_role("button", name=re.compile(r"^Save as version 3"))).to_be_disabled()
    [rendered] = worker
    months = next(w for w in clip["words"] if w["w"] == "months.")
    assert [list(c) for c in rendered["cuts"]] == [[months["s"], months["e"]]]
    assert clip["seed"].read_post(pid)["version_id"] not in (None, clip["v1"])      # 7.7

    def broken(*args, **kwargs):
        raise render.RenderError("encode: ffmpeg failed: no space left on device")

    monkeypatch.setattr(render, "render_version", broken)          # 7.8: the failure keeps version 2
    page.get_by_role("switch", name="Hook").click()
    page.get_by_role("button", name=re.compile(r"^Save as version 3")).click()
    expect(page.locator("#apView .ap-problem")).to_have_text(
        "Couldn't render version 3: encode: ffmpeg failed: no space left on device. Version 2 stays in use.",
        timeout=10_000)
    expect(page.locator("#apView .ap-ed-versions summary")).to_have_text("Version 2 · in use")


@pytest.mark.req("7.2", "7.9", "8.6")
def test_each_warning_appears_before_rendering(page, clip):
    add_post(clip, platform="instagram", status="live", url="https://www.instagram.com/reel/abc/",
             published_at=time.time() - 3600)
    add_post(clip, platform="youtube", status="uploaded", platform_post_id="vid1",
             scheduled_publish_at=time.time() + 86400)

    def short_shorts(route) -> None:                                # Shorts at most 10 s, for the warning
        response = route.fetch()
        body = response.json()
        body["limits"]["youtube_max_s"] = 10
        route.fulfill(response=response, json=body)

    sent: list = []

    def render_request(route) -> None:
        sent.append(route.request.post_data_json)
        route.fulfill(status=422, json={"detail": [{"field": "length", "message": "Stopped by the test."}]})

    page.route(re.compile(r"/api/editor/clips/[0-9a-f]{16}$"), short_shorts)
    page.route(re.compile(r"/api/editor/clips/[0-9a-f]{16}/render$"), render_request)
    open_editor(page, clip)
    pills = page.locator("#apView .ap-ed-hint .ap-pill")
    expect(pills.filter(has_text="YouTube Shorts can be at most 10 s.")).to_have_count(1)      # 7.2
    expect(pills.filter(has_text="1 post is already published and won't change.")).to_have_count(1)   # 7.9
    expect(pills.filter(has_text="1 YouTube video is already uploaded")).to_have_count(1)      # 8.6
    posts = page.locator("#apView .ap-ed-posts")
    expect(posts).to_contain_text("Instagram post is published and won't change.")
    expect(posts).to_contain_text("YouTube video is uploaded and not public yet.")

    end = page.get_by_role("slider", name="Clip end, fine trim")    # a clip too short to render
    end.focus()
    page.keyboard.press("Home")
    expect(pills.filter(has_text="Instagram Reels must be 3 s to 15 min long.")).to_have_count(1)
    expect(page.locator("#apView .ap-ed-hint .ap-pill.err")).to_have_text(re.compile(r"clips must be at least 3 s\.$"))
    render_button = page.get_by_role("button", name=re.compile(r"^Save as version 2"))
    expect(render_button).to_be_disabled()
    expect(render_button).to_have_accessible_description(re.compile(r"clips must be at least 3 s"))
    page.get_by_role("button", name="Undo").click()

    word(page, "months.").click()
    page.keyboard.press("Delete")
    page.get_by_label("Replace the uploaded YouTube video with version 2").check()
    render_button.click()
    dialog = page.get_by_role("dialog", name="Save as version 2?")
    lead = dialog.locator("p").first
    expect(lead).to_have_text("1 of the clip's posts will use version 2.")
    expect(dialog.locator("li")).to_have_text(["1 post is already published and won't change."])
    replace = dialog.get_by_label("Replace the uploaded YouTube video with version 2")
    expect(replace).to_be_checked()                                  # the choice made under Posts
    replace.uncheck()
    expect(lead).to_have_text("None of the clip's posts will use version 2.")
    replace.check()
    dialog.get_by_role("button", name="Save as version 2").click()
    expect(page.locator("#apView .ap-problem")).to_have_text("Length: Stopped by the test.")
    assert sent and sent[0]["confirm_replace"] is True
    assert sent[0]["spec"]["out"] == CLIP_OUT                     # the undone move didn't stick


@pytest.mark.req("7.1", "6.5", "12.4")
def test_edit_on_a_clip_card_opens_the_editor(page, clip):
    add_post(clip, platform="youtube", status="scheduled", slot_at=time.time() + 3 * 86400)
    add_post(clip, platform="instagram", status="live", url="https://www.instagram.com/reel/abc/",
             published_at=time.time() - 3600)
    page.goto("/")
    page.locator("#libBtn").click()
    row = page.locator(f'#libBody .lib-row[data-job="{clip["job"]}"]')
    expect(row.locator(".ap-card-post")).to_have_text([re.compile(r"^YouTube · 1 planned"),
                                                        re.compile(r"^Instagram · 1 posted")])
    row.get_by_role("button", name="Open").click()
    card = page.locator(f'#clips .clip[data-job="{clip["job"]}"]')
    chips = card.locator(".ap-card-post")
    expect(chips).to_have_text([re.compile(r"^Instagram · Posted"),
                                re.compile(r"^YouTube · Scheduled \w{3} \d{1,2} \w{3,4} \d\d:\d\d IST")])
    expect(chips.first).to_have_attribute("href", "https://www.instagram.com/reel/abc/")
    expect(chips.nth(1)).to_have_attribute("href", "#/autopilot/queue")
    card.get_by_role("link", name=re.compile(r"^Edit clip")).click()
    expect(page).to_have_url(re.compile(rf"/#/editor/{clip['job']}-1$"))
    expect(page.locator("#apView h1")).to_have_text(TITLE)
    expect(page.locator("header.nav")).to_be_hidden()
    page.get_by_role("button", name="Back").click()
    expect(page.locator("main.wrap")).to_be_visible()
    expect(card).to_be_visible()


@pytest.mark.req("7.5", "3.9", "N5.1")
def test_the_panels_change_captions_hook_layout_crop_music_and_cover(page, clip):
    from autopilot import clearance

    clip["seed"].track("cleared.mp3")
    clip["seed"].track("other.mp3")
    clearance.set_cleared("local", "cleared.mp3", True)
    open_editor(page, clip)
    word(page, "thousand").click()                                 # the playhead on a captioned word
    caption = page.locator("#apView .ap-pv-caption")
    expect(caption).to_contain_text("THOUSAND")
    captions = page.get_by_role("switch", name="Subtitles")
    captions.click()
    expect(captions).to_have_attribute("aria-checked", "false")
    expect(caption).to_be_hidden()
    captions.click()
    page.locator("#apView").get_by_label("Subtitle style").select_option("karaoke")
    expect(caption.locator(".on")).to_have_text("THOUSAND")         # the word being said, highlighted
    hook = page.locator("#apView .ap-pv-hook")
    expect(hook).to_have_text("NOBODY TELLS YOU THIS")
    page.get_by_role("switch", name="Hook").click()
    expect(hook).to_be_hidden()

    video = page.locator("#apView .ap-pv-video")
    page.get_by_role("group", name="Layout").get_by_role("button", name="Fit").click()
    expect(video).to_have_css("object-fit", "contain")
    page.get_by_role("group", name="Layout").get_by_role("button", name="Fill").click()
    expect(video).to_have_css("object-fit", "cover")
    expect(video).to_have_css("object-position", "50% 50%")
    crop = page.get_by_role("slider", name=re.compile(r"^Crop position"))
    crop.focus()
    page.keyboard.press("Shift+ArrowRight")                        # 10 % to the right
    expect(crop).to_have_attribute("aria-valuenow", "60")
    expect(video).not_to_have_css("object-position", "50% 50%")
    expect(page.get_by_role("button", name="Automatic crop")).to_be_visible()

    music = page.locator("#apView").get_by_label("Background music")
    expect(music.locator("option")).to_have_text(["None", "cleared.mp3"])    # cleared tracks only (3.9)
    volume = page.locator("#apView").get_by_label("Music volume")
    expect(volume).to_be_disabled()
    music.select_option("cleared.mp3")
    expect(volume).to_be_enabled()

    page.get_by_role("button", name="Use this frame").click()
    expect(page.locator("#apView [data-row=cover] .ap-ed-val")).to_have_text(re.compile(r"^0:0\d\.\d · you chose it$"))
    expect(page.get_by_role("button", name="Choose when rendering")).to_be_visible()


@pytest.mark.req("7.2")
def test_a_handle_near_the_edge_brings_in_more_of_the_video(page, clip, media_files):
    a, b = media_files["window"]
    asked: list = []

    def longer_source(route) -> None:                               # a 200 s source, so there's more to load
        response = route.fetch()
        body = response.json()
        body["source"]["duration"] = 200.0
        route.fulfill(response=response, json=body)

    def proxy(route) -> None:
        data = route.request.post_data_json or {}
        asked.append(data)
        route.fulfill(json={"state": "ready", "name": media_files["proxy"].name, "progress": 1.0,
                            "start": data.get("start", a), "end": data.get("end", b),
                            "url": f"/api/clip/{clip['job']}/{media_files['proxy'].name}"})

    page.route(re.compile(r"/api/editor/clips/[0-9a-f]{16}$"), longer_source)
    page.route(re.compile(r"/api/editor/clips/[0-9a-f]{16}/proxy$"), proxy)
    open_editor(page, clip)
    labels = page.locator("#apView .ap-ed-strip-lab > span")
    expect(labels.last).to_have_text("0:40")
    end = page.get_by_role("slider", name="Clip end, fine trim")
    end.focus()
    page.keyboard.press("End")                                     # within 5 s of the window's edge
    expect(page.locator("#apStatus")).to_have_text("More of the video is in the timeline now.")
    assert asked == [{"start": a, "end": round(b + 60, 3)}]
    expect(labels.last).to_have_text("1:40")
    expect(end).to_have_attribute("aria-valuemax", f"{b + 60:.1f}")
