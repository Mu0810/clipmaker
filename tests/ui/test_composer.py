"""The composer's Autopilot controls and the Sources view in the browser
(task 12.3; Requirements 1.1, 1.2, 1.3, 1.5, 1.7, 1.9, 13.1, 13.2, N8.1,
N5.2, N6.1). Run with `pytest -m ui`.

yt-dlp is faked on the live server (`intake._default_runner`, which the
probe and the submission both use), so the real probe and submit routes run
and no test reaches YouTube. Sources are queued but never processed: the
test server runs no job workers, so a test moves a source through its
stages itself."""
from __future__ import annotations

import json
import re
import subprocess

import pytest
from playwright.sync_api import expect

from autopilot import intake

pytestmark = pytest.mark.ui

DEFAULT_SUMMARY = ("Up to 6 clips per video, 20–60 s each, with word-by-word subtitles, for YouTube and "
                   "Instagram. You approve each post.")
NO_CLIPS = ("No clip reached your minimum score of 60 (best: 48), so no posts were planned. "
            "Lower the minimum score in the preset, or try another video.")
VIDEOS = {"vid1": ("vid1", "First talk", "Other Channel", "UCother1", 600),
          "vid2": ("vid2", "Second talk", "Second Channel", "UCother2", 1200),
          "vid3": ("vid3", "My own talk", "My Channel", "UCmine", 900)}


def link(video: str) -> str:
    return f"https://www.youtube.com/watch?v={video}"


@pytest.fixture
def ytdlp(monkeypatch) -> list[str]:
    """A fake yt-dlp on the live server; returns the URLs it was asked about."""
    asked: list[str] = []

    def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
        url = cmd[-1]
        asked.append(url)
        found = next((v for k, v in VIDEOS.items() if url.endswith(f"v={k}")), None)
        if found is None:
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: Video unavailable")
        return subprocess.CompletedProcess(cmd, 0, "\t".join(map(str, found)) + "\n", "")

    monkeypatch.setattr(intake, "_default_runner", run)
    return asked


def requests_to(page, fragment: str) -> list[str]:
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url) if fragment in r.url else None)
    return seen


def stored() -> dict:
    """The local user's preferences, read from the live server's database."""
    from autopilot import owner as owner_mod

    return owner_mod.get_prefs("local")


def eventually(page, check, timeout_ms: int = 5000) -> None:
    """Wait for server-side state. It waits with page.wait_for_timeout, not
    time.sleep: the page's requests pass through Python route handlers,
    which only run while Playwright has control."""
    for _ in range(timeout_ms // 50):
        if check():
            return
        page.wait_for_timeout(50)
    assert check()


def turn_on(page) -> None:
    page.locator("label.ap-switch").click()
    expect(page.get_by_role("switch", name="Autopilot")).to_be_checked()


def go(page):
    return page.locator("#go")


def rows(page):
    return page.locator("#apView .ap-src")


def row(page, title: str):
    return rows(page).filter(has=page.get_by_role("heading", name=title, exact=True))


@pytest.mark.req("1.1", "N5.2")
def test_the_switch_swaps_the_options_for_the_profile_and_is_remembered(page, seed):
    weekly = seed.profile("Weekly", clips_per_source=3, length_min_s=15, length_max_s=30,
                          caption_style="karaoke", platforms=["youtube"], mode="full_auto")
    page.goto("/")
    switch = page.get_by_role("switch", name="Autopilot")
    expect(switch).not_to_be_checked()
    expect(page.locator("#maxClips")).to_be_visible()                # today's form
    expect(page.locator("#burnCaps")).to_be_attached()
    expect(page.locator("#apSummary")).to_be_hidden()
    expect(go(page)).to_have_text("Make clips")
    expect(page.get_by_label("Preset")).to_be_hidden()

    turn_on(page)
    expect(page.locator("#maxClips")).to_be_hidden()                 # the options give way...
    expect(page.locator(".settings")).to_be_hidden()
    expect(page.locator("#apSummaryText")).to_have_text(DEFAULT_SUMMARY)   # ...to the profile
    expect(page.get_by_role("link", name="Edit presets")).to_have_attribute(
        "href", "#/autopilot/profiles")
    expect(go(page)).to_have_text("Start Autopilot")
    expect(page.get_by_role("button", name="Several links")).to_be_visible()
    picker = page.get_by_label("Preset")
    expect(picker.locator("option")).to_have_text(["Default", "Weekly"])
    picker.select_option(label="Weekly")
    expect(page.locator("#apSummaryText")).to_have_text(
        "Up to 3 clips per video, 15–30 s each, with karaoke subtitles, for YouTube. "
        "Posts go out without asking you.")
    eventually(page, lambda: stored()["last_profile_id"] == weekly["id"]
               and stored()["autopilot_default"] is True and stored()["timezone"] is not None)

    page.reload()                                                    # remembered (1.1)
    expect(switch).to_be_checked()
    expect(page.get_by_label("Preset")).to_have_value(weekly["id"])
    expect(page.locator("#maxClips")).to_be_hidden()
    prefs = stored()
    assert (prefs["autopilot_default"], prefs["last_profile_id"]) == (True, weekly["id"])
    # Taken from the browser, under the name it reports: Chrome gives India's
    # zone its older name, Asia/Calcutta, which zoneinfo accepts as well.
    browser_zone = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
    assert browser_zone in ("Asia/Kolkata", "Asia/Calcutta")
    assert prefs["timezone"] == browser_zone

    page.locator("label.ap-switch").click()
    expect(switch).not_to_be_checked()
    expect(page.locator("#maxClips")).to_be_visible()
    expect(go(page)).to_have_text("Make clips")
    eventually(page, lambda: stored()["autopilot_default"] is False)
    page.reload()
    expect(page.locator("#apBar")).to_be_visible()
    expect(switch).not_to_be_checked()


@pytest.mark.req("1.2")
def test_several_links_takes_at_most_20(page, seed, ytdlp):
    probes = requests_to(page, "/api/autopilot/sources/probe")
    page.goto("/")
    turn_on(page)
    page.locator("#url").fill(link("vid1"))
    page.get_by_role("button", name="Several links").click()
    box = page.locator("#apLinks")
    expect(box).to_be_focused()
    expect(box).to_have_value(link("vid1"))                          # the link carried over
    expect(page.locator("#url")).to_be_hidden()
    expect(page.locator("#apLinksCount")).to_have_text("1 of 20 links")

    box.fill("\n".join(link(f"v{i:02d}") for i in range(21)))
    expect(page.locator("#apLinksCount")).to_have_text("21 links: at most 20 at a time.")
    expect(box).to_have_attribute("aria-invalid", "true")
    go(page).click()
    expect(page.locator("#apMsg")).to_have_text(
        "At most 20 links at a time; there are 21. Remove 1 and try again.")
    expect(box).to_be_focused()
    page.wait_for_timeout(300)
    assert probes == [] and ytdlp == []                              # nothing was read

    box.fill("\n".join(link(f"v{i:02d}") for i in range(20)))
    expect(page.locator("#apLinksCount")).to_have_text("20 of 20 links")
    expect(box).to_have_attribute("aria-invalid", "false")
    page.get_by_role("button", name="Use one link").click()
    expect(page.locator("#url")).to_be_visible()
    expect(page.locator("#url")).to_have_value(link("vid1"))
    expect(box).to_be_hidden()


@pytest.mark.req("1.2", "13.1", "1.3", "N5.2")
def test_starting_names_each_channel_for_the_rights_confirmation_then_queues(page, seed, ytdlp):
    page.goto("/")
    turn_on(page)
    page.get_by_role("button", name="Several links").click()
    page.locator("#apLinks").fill("\n".join([link("vid1"), link("vid2"), link("gone")]))
    go(page).click()

    dialog = page.get_by_role("dialog", name="Confirm your rights")
    expect(dialog).to_be_visible()
    expect(dialog.locator(".ap-rights li strong")).to_have_text(["First talk", "Second talk"])
    expect(dialog.locator(".ap-rights-meta")).to_have_text(["Other Channel · 10:00",
                                                            "Second Channel · 20:00"])
    expect(dialog.locator(".ap-rights-skip li")).to_have_text(
        [f"{link('gone')}: ERROR: Video unavailable"])
    box = dialog.get_by_role("checkbox",
                             name="I own these videos or have the right to republish them.")
    expect(box).to_be_focused()
    dialog.get_by_role("button", name="Start Autopilot").click()     # not ticked yet
    expect(dialog.get_by_role("alert")).to_have_text("Tick the box to confirm, or cancel.")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)
    expect(go(page)).to_be_enabled()
    assert seed.jobs() == []                                         # nothing queued (13.1)

    go(page).click()
    dialog = page.get_by_role("dialog", name="Confirm your rights")
    dialog.get_by_role("checkbox").check()
    dialog.get_by_role("button", name="Start Autopilot").click()
    expect(page).to_have_url(re.compile(r"/#/autopilot/sources$"))
    expect(page.locator("#apView h1")).to_have_text("Videos")
    expect(page.get_by_role("navigation", name="Autopilot").get_by_role("link", name="Videos")).to_have_attribute(
        "aria-current", "page")
    flash = page.locator(".ap-flash")
    expect(flash).to_contain_text("Autopilot started on 2 videos.")
    expect(flash).to_contain_text("1 link wasn't added:")
    expect(flash.locator("li")).to_have_text([f"{link('gone')}: ERROR: Video unavailable"])
    expect(page.locator("#apStatus")).to_have_text(
        "Autopilot started on 2 videos. 1 link wasn't added.")

    jobs = seed.jobs()
    assert sorted(j["source_title"] for j in jobs) == ["First talk", "Second talk"]
    assert {j["rights_basis"] for j in jobs} == {"confirmed"}
    assert [j["request"]["url"] for j in jobs] == [link("vid1"), link("vid2")]   # submission order
    expect(rows(page)).to_have_count(2)
    first = row(page, "First talk")
    expect(first.locator(".ap-pill")).to_have_text("Queued")
    expect(first.locator(".ap-src-meta")).to_contain_text("Other Channel · 10:00 · Preset: Default")
    expect(first.get_by_role("progressbar")).to_be_hidden()

    # The worker picks it up: the next reading shows the stage and percentage (1.3).
    first_id = next(j["job_id"] for j in jobs if j["source_title"] == "First talk")
    seed.stage(first_id, status="running", stage="transcribing", pct=28, message="Transcribing")
    expect(first.locator(".ap-pill")).to_have_text("Processing")
    expect(first.locator(".ap-src-detail")).to_have_text("Transcribing")
    expect(first.get_by_role("progressbar", name="Progress of First talk")).to_have_attribute(
        "aria-valuenow", "28")
    expect(first.locator(".ap-meter-pct")).to_have_text("28%")
    expect(page.locator("#url")).to_have_value("")                  # the form was cleared


@pytest.mark.req("13.2")
def test_a_video_on_the_users_own_channel_skips_the_confirmation(page, seed, ytdlp):
    seed.account("youtube", "UCmine")
    page.goto("/")
    turn_on(page)
    page.locator("#url").fill(link("vid3"))
    go(page).click()
    expect(page).to_have_url(re.compile(r"/#/autopilot/sources$"))
    expect(page.get_by_role("dialog")).to_have_count(0)
    mine = row(page, "My own talk")
    expect(mine.locator(".ap-src-meta")).to_contain_text("My Channel (your channel) · 15:00")
    [job] = seed.jobs()
    assert job["rights_basis"] == "own_channel"
    assert ytdlp == [link("vid3"), link("vid3")]                     # the probe, then the submit


@pytest.mark.req("13.1", "1.2")
def test_an_uploaded_file_always_needs_the_confirmation(page, seed, monkeypatch):
    from media import probe as probe_mod

    monkeypatch.setattr(probe_mod, "probe", lambda path: {"duration": 600.0})
    uploads = requests_to(page, "/api/autopilot/sources/file")
    page.goto("/")
    turn_on(page)
    page.locator("#modeFile").click()
    expect(page.get_by_role("button", name="Several links")).to_be_hidden()
    go(page).click()                                                 # no file yet
    expect(page.locator("#apMsg")).to_have_text("Choose a video file first.")
    page.locator("#fileInput").set_input_files(
        {"name": "talk.mp4", "mimeType": "video/mp4", "buffer": b"\x00" * 2048})
    go(page).click()
    dialog = page.get_by_role("dialog", name="Confirm your rights")
    expect(dialog.locator(".ap-rights li strong")).to_have_text(["talk.mp4"])
    expect(dialog.locator(".ap-rights-meta")).to_have_text(["Uploaded file · 2 KB"])
    dialog.get_by_role("button", name="Cancel").click()
    page.wait_for_timeout(300)
    assert uploads == []                                             # not sent without it

    go(page).click()
    dialog = page.get_by_role("dialog", name="Confirm your rights")
    dialog.get_by_role("checkbox",
                       name="I own this video or have the right to republish it.").check()
    dialog.get_by_role("button", name="Start Autopilot").click()
    expect(page).to_have_url(re.compile(r"/#/autopilot/sources$"))
    expect(page.locator(".ap-flash")).to_have_text("Autopilot started on “talk.mp4”.")
    expect(row(page, "talk.mp4").locator(".ap-src-meta")).to_contain_text("Uploaded file · 10:00")
    [job] = seed.jobs()
    assert (job["source_title"], job["rights_basis"], job["request"]["kind"]) == (
        "talk.mp4", "confirmed", "file")
    assert len(uploads) == 1


@pytest.mark.req("1.3", "1.5", "1.7", "N8.1")
def test_the_sources_list_shows_stage_restart_outcome_and_ai_calls(page, seed):
    seed.source(title="Failed talk", status="error", age=1, ai_calls=2,
                message="Error: Couldn't download the video.", error="Couldn't download the video.")
    planned = seed.source(title="Talk with posts", age=2, ai_calls=7, best_score=82,
                          message="Done: 2 clips, 2 posts planned.")
    seed.post(job_id=planned)
    seed.post(job_id=planned)
    seed.source(title="Quiet talk", age=3, ai_calls=9, best_score=48, message=NO_CLIPS)
    running = seed.source(title="Long interview", status="running", age=4, stage="rendering",
                          pct=70, message="Rendering clip 2 of 6", attempts=1,
                          source_channel_title="Other Channel", source_duration=3725.5)
    seed.source(title="Made by hand", age=5, autopilot=0)
    seed.source(title="Deleted talk", age=6, deleted=True)
    page.goto("/#/autopilot/sources")

    expect(rows(page).locator(".ap-src-title")).to_have_text(
        ["Long interview", "Quiet talk", "Talk with posts", "Failed talk"])
    busy = row(page, "Long interview")
    expect(busy.locator(".ap-pill")).to_have_text("Processing")
    expect(busy.locator(".ap-src-detail")).to_have_text("Rendering clip 2 of 6")
    expect(busy.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "70")
    expect(busy.locator(".ap-meter-pct")).to_have_text("70%")
    expect(busy.locator(".ap-src-note")).to_have_text(
        "Restarted once: Clip Maker restarted while this was being processed.")
    expect(busy.locator(".ap-src-meta")).to_have_text(re.compile(
        r"^Other Channel · 1:02:06 · Preset: Default · Added \d{1,2} [A-Z][a-z]{2,3}, \d\d:\d\d \S+"))
    expect(busy.locator(".ap-src-facts")).to_be_hidden()

    quiet = row(page, "Quiet talk")
    expect(quiet.locator(".ap-pill")).to_have_text("No posts")
    expect(quiet.locator(".ap-src-detail")).to_have_text(NO_CLIPS)          # 1.7
    expect(quiet.locator(".ap-src-facts")).to_have_text("Best score 48 (minimum 60) · 9 AI calls")
    expect(quiet.locator(".ap-src-note")).to_be_hidden()
    expect(row(page, "Talk with posts").locator(".ap-src-facts")).to_have_text(
        "Best score 82 (minimum 60) · 7 AI calls · 2 posts")
    failed = row(page, "Failed talk")
    expect(failed.locator(".ap-pill")).to_have_text("Failed")
    expect(failed.locator(".ap-src-detail")).to_have_text("Couldn't download the video.")
    expect(failed.locator(".ap-src-facts")).to_have_text("2 AI calls")       # N8.1

    seed.stage(running, status="done", stage=None, pct=100, ai_calls=21, best_score=91,
               message="Done: 6 clips, 12 posts planned.")
    expect(busy.locator(".ap-pill")).to_have_text("Done")
    expect(busy.get_by_role("progressbar")).to_be_hidden()
    expect(busy.locator(".ap-src-facts")).to_have_text("Best score 91 (minimum 60) · 21 AI calls")
    expect(page.locator("#apStatus")).to_have_text("Long interview: Done: 6 clips, 12 posts planned.")


@pytest.mark.req("1.9", "N5.1")
def test_deleting_a_source_says_what_it_cancels_and_asks_again_when_that_changes(page, seed):
    job_id = seed.source(title="Talk with posts", message="Done: 1 clip, 1 post planned.")
    first_post = seed.post(job_id=job_id)
    page.goto("/#/autopilot/sources")
    delete = page.get_by_role("button", name="Delete Talk with posts")
    delete.click()
    dialog = page.get_by_role("dialog", name="Delete “Talk with posts”?")
    expect(dialog).to_contain_text("Deleting it cancels 1 post that hasn't been published.")
    expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
    page.keyboard.press("Escape")                                    # closes the dialog only
    expect(dialog).to_have_count(0)
    expect(page).to_have_url(re.compile(r"/#/autopilot/sources$"))
    expect(delete).to_be_focused()

    delete.click()
    dialog = page.get_by_role("dialog", name="Delete “Talk with posts”?")
    expect(dialog).to_contain_text("Deleting it cancels 1 post")
    second_post = seed.post(job_id=job_id)                           # planned meanwhile
    dialog.get_by_role("button", name="Delete video").click()
    dialog = page.get_by_role("dialog", name="Delete “Talk with posts”?")
    expect(dialog.locator(".ap-dialog-warn")).to_have_text("Its posts changed while this was open.")
    expect(dialog).to_contain_text("Deleting it cancels 2 posts that haven't been published.")
    assert seed.job(job_id)["deleted_at"] is None                    # nothing happened yet
    dialog.get_by_role("button", name="Delete video").click()

    expect(rows(page)).to_have_count(0)
    expect(page.locator("#apStatus")).to_have_text("Deleted “Talk with posts”.")
    expect(page.locator("#apLive")).to_have_text("Autopilot: nothing needs you.")   # the chip, apart
    expect(page.get_by_role("button", name="Add a video")).to_be_visible()
    assert seed.job(job_id)["deleted_at"] is not None
    from autopilot import store

    with store.connect() as conn:
        statuses = {r["id"]: r["status"] for r in conn.execute(
            "SELECT id, status FROM posts WHERE job_id=?", (job_id,)).fetchall()}
    assert statuses == {first_post: "cancelled", second_post: "cancelled"}


@pytest.mark.req("1.10", "N5.2")
def test_a_failed_video_can_be_tried_again(page, seed):
    failed = seed.source(title="Failed talk", status="error", age=4, ai_calls=2, attempts=1,
                         message="Error: Couldn't download the video.",
                         error="Couldn't download the video.")
    taken = seed.source(title="Other tab", status="error", age=3, message="Error: Timed out.",
                        error="Timed out.")
    seed.source(title="Lost upload", status="error", age=2,
                message="Error: The uploaded video is missing; submit it again.",
                error="The uploaded video is missing; submit it again.",
                request={"kind": "file", "path": "/nowhere/upload-0000.mp4", "filename": "lost.mp4"})
    seed.source(title="Done talk", age=1, message="Done: 1 clip, 1 post planned.")
    page.goto("/#/autopilot/sources")

    expect(page.get_by_role("button", name=re.compile(r"^Try again "))).to_have_count(2)
    expect(row(page, "Lost upload").get_by_role("button", name=re.compile(r"^Try again"))).to_have_count(0)
    expect(row(page, "Done talk").get_by_role("button", name=re.compile(r"^Try again"))).to_have_count(0)

    # Another tab tried it first: the server says so, and the row catches up.
    seed.stage(taken, status="queued", message="Queued to try again", error=None)
    page.get_by_role("button", name="Try again Other tab").click()
    expect(page.locator("#apView .ap-problem")).to_have_text(
        "Couldn't try “Other tab” again: It's already queued or being processed.")
    expect(row(page, "Other tab").locator(".ap-pill")).to_have_text("Queued")
    expect(page.get_by_role("button", name="Try again Other tab")).to_have_count(0)

    again = page.get_by_role("button", name="Try again Failed talk")
    again.focus()
    page.keyboard.press("Enter")
    first = row(page, "Failed talk")
    expect(first.locator(".ap-pill")).to_have_text("Queued")
    expect(first.locator(".ap-src-detail")).to_have_text("Queued to try again")
    expect(again).to_have_count(0)
    expect(first.get_by_role("heading", name="Failed talk")).to_be_focused()   # not lost with it
    expect(page.locator("#apStatus")).to_have_text("Trying “Failed talk” again.")
    expect(page.locator("#apView .ap-problem")).to_be_hidden()
    expect(first.locator(".ap-src-note")).to_be_hidden()          # its restart count starts afresh
    job = seed.job(failed)
    assert (job["status"], job["error"], job["attempts"], job["ai_calls"]) == ("queued", None, 0, 2)

    seed.stage(failed, status="running", stage="downloading", pct=4, message="Downloading")
    expect(first.locator(".ap-pill")).to_have_text("Processing")  # followed again (1.3)
    expect(first.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "4")


@pytest.mark.req("N6.1")
def test_with_the_switch_off_make_clips_runs_todays_flow(page, seed):
    sent: list[dict] = []

    def process(route) -> None:
        sent.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=400, content_type="application/json",
                      body=json.dumps({"detail": "Stopped by the test."}))

    page.route("**/api/process", process)
    autopilot = requests_to(page, "/api/autopilot/sources")
    page.goto("/")
    expect(page.locator("#apBar")).to_be_visible()
    page.locator("#url").fill(link("vid1"))
    go(page).click()
    expect(page.locator("#statusMsg")).to_have_text("Error: Stopped by the test.")
    assert len(sent) == 1
    assert sent[0]["url"] == link("vid1") and sent[0]["max_clips"] == 6
    assert autopilot == []
