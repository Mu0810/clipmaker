"""The review queue in the browser (task 12.4; Requirements 5.5, 5.6, 6.2,
6.5-6.8, 8.7, 9.5, N1.3, N5.1, N5.2), in the Agenda layout chosen in task
12.1. Run with `pytest -m ui`.

Posts are seeded straight into the live server's database, with slots a few
days ahead in Asia/Kolkata (the user's preferred zone here), so the day
headings never say "Today" or "Tomorrow" and nothing depends on the hour the
suite runs at. The real posts API answers every action."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from autopilot import planner, posts, readiness

pytestmark = pytest.mark.ui

ZONE = "Asia/Kolkata"
KOL = ZoneInfo(ZONE)
YT_META = {"title": "Stop chasing viral. Do this instead.",
           "description": "Most creators spend years chasing one viral video.",
           "tags": ["creator tips", "growth"], "category_id": "22"}
IG_META = {"caption": "Stop chasing viral 🚫 Do this instead. #creatortips #growth"}
LIMIT_NOTE = ("Instagram's publishing limit for this account is reached (100 of 100 posts in 24 h). "
              "It resets around 09:00, so the post moved to 11:00.")

MEASURE = """() => new Promise(resolve => {
  const view = document.getElementById("apView");
  const start = performance.now();
  const observer = new MutationObserver(() => {
    if (view.querySelectorAll(".ap-q-row").length >= 200) {
      observer.disconnect();
      requestAnimationFrame(() => resolve(performance.now() - start));    // once painted
    }
  });
  observer.observe(view, { childList: true, subtree: true });
  location.hash = "#/autopilot/queue";
})"""


def day(days: int):
    return datetime.now(KOL).date() + timedelta(days=days)


def at(days: int, hour: int, minute: int = 0) -> float:
    d = day(days)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=KOL).timestamp()


def day_label(days: int) -> str:
    """The short date as the page writes it in en-GB, "Mon 19 Oct" ("Sept" in September)."""
    d = day(days)
    month = "Sept" if d.month == 9 else f"{d:%b}"
    return f"{d:%a} {d.day} {month}"


def when(days: int, hhmm: str) -> str:
    return f"{day_label(days)} {hhmm} IST"


def slot_value(days: int, hour: int) -> str:
    return f"{day(days).isoformat()}T{hour:02d}:00"


def world(seed) -> str:
    seed.timezone(ZONE)
    seed.account("youtube", "UC1", "Manish Clips")
    seed.account("instagram", "1784", "manish.makes")
    return seed.source(title="Creator Economy Podcast · Ep 42")


def add(seed, job: str, clip: str, platform: str = "youtube", *, days: int = 2, hour: int = 13,
        **extra) -> str:
    meta = dict(YT_META if platform == "youtube" else IG_META)
    fields = {"job_id": job, "clip_id": clip, "account_id": f"local-{platform}",
              "slot_at": at(days, hour), "slot_basis": "defaults", "meta": meta, "ai_meta": dict(meta),
              **extra}
    return seed.post(platform=platform, **fields)


def sensitive() -> list[dict]:
    return posts.make_holds(["sensitive"], now=time.time(), sensitive_categories=["financial claims"])


def row(page, pid: str):
    return page.locator(f'#apView .ap-q-row[data-id="{pid}"]')


def section(page, days: int):
    return page.locator(f'#apView section[aria-labelledby="ap-q-day-{day(days).isoformat()}"]')


def open_post(page, pid: str) -> None:
    row(page, pid).get_by_role("button", name=re.compile(r"^Open the ")).click()


@pytest.mark.req("N1.3", "6.5")
def test_200_posts_show_within_a_second(page, seed):
    job = world(seed)
    for i in range(100):
        clip = seed.clip(job, i + 1, score=95 - i % 40)
        add(seed, job, clip, "youtube", days=2 + i // 2, hour=11 + 4 * (i % 2))
        add(seed, job, clip, "instagram", days=2 + i // 2, hour=13 + 4 * (i % 2))
    page.goto("/")
    expect(page.locator("#apChip .ap-chip-counts")).to_contain_text("200 to approve")
    elapsed = page.evaluate(MEASURE)
    expect(page.locator("#apView .ap-q-row")).to_have_count(200)
    expect(page.locator("#apView .ap-q-day")).to_have_count(50)
    assert elapsed < 1000, f"The queue took {elapsed:.0f} ms to show 200 posts."
    assert page.locator("#apView .ap-q-row").first.evaluate(
        "r => getComputedStyle(r).contentVisibility") == "auto"


@pytest.mark.req("6.5", "5.5", "5.6", "8.7", "9.5")
def test_each_post_shows_what_it_is_and_opens_with_everything_to_check(page, seed):
    job = world(seed)
    c1 = seed.clip(job, 1, score=91, reason="Strong hook in the first 3 s")
    c2 = seed.clip(job, 2, score=72, reason="Good story, slow start")
    c3 = seed.clip(job, 3, score=84, reason="Clear payoff at 0:38")
    approved = add(seed, job, c1, days=2, hour=13, approval="approved")
    uploaded = add(seed, job, c2, days=2, hour=17, status="uploaded", approval="approved",
                   scheduled_publish_at=at(2, 17), platform_post_id="vid2")
    held = add(seed, job, c3, days=2, hour=19, slot_basis="user", user_locked=1, holds=sensitive())
    waiting = add(seed, job, c3, "instagram", days=2, hour=21)
    deferred = add(seed, job, c1, "instagram", days=3, hour=11, approval="approved",
                   last_error=LIMIT_NOTE, last_error_class="deferred")
    kept = add(seed, job, c2, days=-1, hour=18, status="kept_private", approval="approved",
               url="https://youtu.be/vid0", last_error="Kept private.")
    page.goto("/#/autopilot/queue")

    expect(page.locator("#apView .ap-lead")).to_have_text(
        "6 posts from 1 video · times in India Standard Time")
    expect(page.locator("#apView .ap-q-f")).to_have_text(
        ["All 6", "Needs approval 2", "Held 1", "Blocked 0", "Failed 0"])
    expect(page.locator("#apView .ap-q-note")).to_have_text(
        "Instagram posts go out only while Clip Maker is running.")
    expect(page.get_by_role("switch", name="Pause all publishing")).to_have_attribute("aria-checked", "false")

    busy_day = section(page, 2)
    expect(busy_day.get_by_role("heading", level=2)).to_have_text(day_label(2))
    expect(busy_day.locator(".ap-q-quota .ap-pill")).to_have_text(
        ["YouTube 3 of 2 a day, more than your daily maximum", "Instagram 1 of 2 a day"])
    expect(busy_day.locator(".ap-q-quota .ap-pill").first).to_have_class(re.compile(r"\bwarn\b"))
    expect(busy_day.locator(".ap-q-row .ap-q-time")).to_have_text(
        [re.compile(r"^13:00"), re.compile(r"^17:00"), re.compile(r"^19:00"), re.compile(r"^21:00")])
    expect(row(page, approved).locator(".ap-pill")).to_have_text("Approved")
    expect(row(page, approved).locator(".ap-q-plat")).to_have_text("YouTube: Manish Clips")
    expect(row(page, approved).locator(".ap-q-what")).to_have_text(
        "Stop chasing viral. Do this instead.Creator Economy Podcast · Ep 42 · clip 1")
    expect(row(page, approved).get_by_role("button", name=re.compile(r"^Don't post the "))).to_be_visible()
    expect(row(page, uploaded).locator(".ap-pill")).to_have_text("Uploaded · public at 17:00")
    expect(row(page, held).locator(".ap-q-flag")).to_have_text("2 h gap")
    expect(row(page, held).locator(".ap-pill")).to_have_text("Held · may be sensitive")
    expect(row(page, held).locator(".ap-q-score")).to_have_text("score 84 out of 100")
    expect(row(page, held).get_by_role("button", name=re.compile(r"^Approve anyway the "))).to_be_visible()
    expect(row(page, waiting).locator(".ap-pill")).to_have_text("Waiting for approval")
    expect(row(page, waiting).locator(".ap-q-plat")).to_have_text("Instagram: @manish.makes")
    expect(row(page, kept).locator(".ap-pill")).to_have_text("Kept private by YouTube")
    expect(row(page, deferred).locator(".ap-pill")).to_have_text("Approved")

    open_post(page, held)
    panel = page.get_by_role("region", name="Edit the YouTube post for clip 3")
    expect(panel.locator(f"#ap-q-{held}-title")).to_have_value(YT_META["title"])
    expect(panel.locator(f"#ap-q-{held}-title-count")).to_have_text("36 / 100")
    expect(panel.locator(f"#ap-q-{held}-slot")).to_have_value(slot_value(2, 19))
    expect(panel).to_contain_text("You chose this time")
    expect(panel.locator(".ap-q-callout")).to_have_text([
        f"Less than 4 h from another YouTube post at {planner.local_label(at(2, 17), ZONE)}.",
        "That day already has 2 YouTube posts; your daily maximum is 2.",
        "Held. The clip may be sensitive: financial claims. It won't publish until you approve it."])
    expect(panel.locator(".ap-q-reason")).to_have_text("84 Why this clip: Clear payoff at 0:38")
    expect(panel.locator(".ap-q-watch")).to_have_text("Watch the clip in the editor before you approve it.")
    expect(panel.get_by_role("link", name="Watch the clip in the editor")).to_have_attribute(
        "href", re.compile(r"^#/editor/[0-9a-f]{16}$"))
    expect(panel.get_by_role("button", name="Edit clip of the YouTube post for clip 3")).to_be_visible()
    expect(panel.get_by_role("button", name="Approve anyway the YouTube post for clip 3")).to_be_visible()
    expect(panel.get_by_role("button", name="Don't post the YouTube post for clip 3")).to_be_visible()
    expect(panel.get_by_role("group", name="Visibility").get_by_role("button", name="Public")).to_have_attribute(
        "aria-pressed", "true")

    open_post(page, kept)                                            # one post is open at a time
    expect(panel).to_have_count(0)
    kept_panel = page.get_by_role("region", name="Edit the YouTube post for clip 2")
    expect(kept_panel).to_contain_text("YouTube kept this video private. It keeps every video Clip Maker "
                                       "uploads private until your Google Cloud project")
    expect(kept_panel.get_by_role("link", name="YouTube's API audit form")).to_have_attribute(
        "href", readiness.YT_AUDIT_FORM)

    open_post(page, deferred)
    ig_panel = page.get_by_role("region", name="Edit the Instagram post for clip 1")
    expect(ig_panel.locator(".ap-q-callout")).to_have_text(
        [LIMIT_NOTE, "Instagram posts go out only while Clip Maker is running. If it's closed at the post's "
                     "time, the post goes out when Clip Maker starts again within 2 hours; later than that, it "
                     "moves to the next free time."])

    page.locator("#apView .ap-q-f", has_text="Held").click()
    expect(page.locator("#apView .ap-q-row")).to_have_count(1)
    expect(row(page, held)).to_be_visible()
    expect(page.locator("#apView .ap-q-f", has_text="Held")).to_be_focused()


@pytest.mark.req("6.2", "N5.1", "N5.2")
def test_approving_works_from_the_keyboard(page, seed):
    job = world(seed)
    first = add(seed, job, seed.clip(job, 1), days=2, hour=13)
    second = add(seed, job, seed.clip(job, 2), days=2, hour=18)
    page.goto("/#/autopilot/queue")
    expect(page.locator("#apChip .ap-chip-counts")).to_have_text("2 to approve")
    row(page, first).get_by_role("checkbox").focus()
    page.keyboard.press("Tab")
    expect(row(page, first).get_by_role("button", name=re.compile(
        r"^Approve the YouTube post for clip 1, "))).to_be_focused()
    page.keyboard.press("Enter")
    expect(row(page, first).locator(".ap-pill")).to_have_text("Approved")
    toggle = row(page, first).get_by_role("button", name=re.compile(r"^Open the YouTube post for clip 1, "))
    expect(toggle).to_be_focused()
    expect(page.locator("#apStatus")).to_have_text(
        f"Approved the YouTube post for clip 1; it's planned for {when(2, '13:00')}.")
    expect(page.locator("#apChip .ap-chip-counts")).to_have_text("1 to approve")
    assert seed.read_post(first)["approval"] == "approved"
    assert seed.read_post(second)["approval"] == "pending"

    page.keyboard.press("Enter")                                     # open it
    panel = page.get_by_role("region", name="Edit the YouTube post for clip 1")
    expect(panel).to_be_visible()
    expect(row(page, first).get_by_role("button", name=re.compile(r"^Close the "))).to_be_focused()
    page.keyboard.press("Escape")                                    # closes the post only
    expect(panel).to_have_count(0)
    expect(page).to_have_url(re.compile(r"/#/autopilot/queue$"))
    expect(toggle).to_be_focused()
    page.keyboard.press("Escape")                                    # then leaves the screen
    expect(page.locator("main.wrap")).to_be_visible()


@pytest.mark.req("6.5", "4.3", "N5.2")
def test_the_live_counters_follow_the_limits_the_server_sends(page, seed):
    job = world(seed)
    yt = add(seed, job, seed.clip(job, 1), days=2, hour=13)
    ig = add(seed, job, seed.clip(job, 2), "instagram", days=2, hour=19)

    def smaller_title_limit(route) -> None:
        response = route.fetch()
        body = response.json()
        body["limits"]["youtube"]["title_chars"] = 40
        route.fulfill(response=response, json=body)

    page.route(re.compile(r"/api/autopilot/posts\?"), smaller_title_limit)
    page.goto("/#/autopilot/queue")
    open_post(page, yt)
    title, count = page.locator(f"#ap-q-{yt}-title"), page.locator(f"#ap-q-{yt}-title-count")
    expect(count).to_have_text("36 / 40")                            # the server's number, not a copy
    title.fill(YT_META["title"] + " Now!")
    expect(count).to_have_text("41 / 40")
    expect(count).to_have_class(re.compile(r"\bover\b"))
    expect(title).to_have_attribute("aria-invalid", "true")
    description = page.locator(f"#ap-q-{yt}-description")
    description.fill("Café ☕")                                      # é is 2 bytes, ☕ is 3
    expect(page.locator(f"#ap-q-{yt}-description-count")).to_have_text("9 / 5,000 bytes")
    tags = page.locator(f"#ap-q-{yt}-tags")
    tags.fill("creator tips, growth")                                # as YouTube counts: quotes and commas
    expect(page.locator(f"#ap-q-{yt}-tags-count")).to_have_text("21 / 500")
    expect(title).to_have_attribute("aria-describedby", f"ap-q-{yt}-title-count ap-q-{yt}-title-error")

    open_post(page, ig)
    caption_count = page.locator(f"#ap-q-{ig}-caption-count")
    n = len(IG_META["caption"])                                      # code points, as the server counts
    expect(caption_count).to_have_text(f"{n} / 2,200 · 2 hashtags of 30 · 0 mentions of 20")
    page.locator(f"#ap-q-{ig}-caption").fill("Thanks @riya and @sam #podcast")
    expect(caption_count).to_have_text("30 / 2,200 · 1 hashtag of 30 · 2 mentions of 20")


@pytest.mark.req("6.6", "N5.2")
def test_a_bulk_approve_shows_a_result_for_each_post(page, seed):
    job = world(seed)
    vlog = seed.source(title="Weekend vlog: Jaipur", age=5)
    a = add(seed, job, seed.clip(job, 1, score=91), days=2, hour=11)
    b = add(seed, job, seed.clip(job, 2, score=84), days=2, hour=15, holds=sensitive())
    c = add(seed, job, seed.clip(job, 3, score=72), days=2, hour=19, approval="approved")
    d = add(seed, vlog, seed.clip(vlog, 1, score=88), "instagram", days=3, hour=12)
    page.goto("/#/autopilot/queue")

    section(page, 2).get_by_role("checkbox", name=re.compile(r"^Select all posts on ")).check()
    bar = page.get_by_role("region", name="Selected posts")
    expect(bar).to_contain_text("3 posts selected")
    expect(bar.locator(".ap-q-bulk-from")).to_be_hidden()            # all on one day: nothing to add
    bar.get_by_role("button", name="Approve 3").click()
    dialog = page.get_by_role("dialog", name="Approve 3 posts?")
    expect(dialog).to_contain_text("1 of them is held. Approving publishes it anyway:")
    expect(dialog.locator("li")).to_have_text(
        [f"YouTube post for clip 2, {when(2, '15:00')}: The clip may be sensitive: financial claims."])
    dialog.get_by_role("button", name="Approve anyway").click()

    results = page.locator("#apView .ap-q-results")
    expect(results.locator("p")).to_have_text("Approved 2 of 3 posts; 1 skipped.")
    expect(results.locator("li")).to_have_text([
        f"YouTube post for clip 1, {when(2, '11:00')}: approved",
        f"YouTube post for clip 2, {when(2, '15:00')}: approved",
        f"YouTube post for clip 3, {when(2, '19:00')}: skipped. Already approved."])
    expect(results.get_by_role("button", name="Dismiss")).to_be_focused()
    expect(page.locator("#apStatus")).to_have_text("Approved 2 of 3 posts; 1 skipped.")
    expect(bar).to_be_hidden()
    assert [seed.read_post(p)["approval"] for p in (a, b, c, d)] == [
        "approved", "approved", "approved", "pending"]

    page.get_by_label("Select all posts from a video").select_option(label="Weekend vlog: Jaipur (1 post)")
    expect(bar).to_contain_text("1 post selected")
    expect(row(page, d).get_by_role("checkbox")).to_be_checked()
    bar.get_by_role("button", name="Don't post").click()
    page.get_by_role("dialog", name="Don't post the Instagram post for clip 1?").get_by_role("button", name="Don't post").click()
    expect(results.locator("p")).to_have_text("1 post won't be posted.")
    expect(results.locator("li")).to_have_text([f"Instagram post for clip 1, {when(3, '12:00')}: won't be posted"])
    expect(row(page, d)).to_have_count(0)
    assert seed.read_post(d)["status"] == "rejected"


@pytest.mark.req("6.5", "8.5")
def test_dont_post_withdraws_a_post_and_says_so(page, seed):
    from autopilot import activity

    job = world(seed)
    plain = add(seed, job, seed.clip(job, 1), days=2, hour=13, approval="approved")
    uploaded = add(seed, job, seed.clip(job, 2), days=3, hour=13, approval="approved", status="uploaded",
                   platform_post_id="vidU", scheduled_publish_at=at(3, 13))
    page.goto("/#/autopilot/queue")
    status = page.locator("#apStatus")

    row(page, plain).get_by_role("button", name=re.compile(r"^Don't post the YouTube post for clip 1, ")).click()
    expect(status).to_have_text("The YouTube post for clip 1 won't be posted.")
    expect(row(page, plain)).to_have_count(0)
    assert seed.read_post(plain)["status"] == "rejected"

    # An uploaded video is deleted from YouTube, so the dialog asks first.
    row(page, uploaded).get_by_role("button", name=re.compile(r"^Don't post the YouTube post for clip 2, ")).click()
    dialog = page.get_by_role("dialog", name="Don't post the YouTube post for clip 2?")
    expect(dialog).to_contain_text("Not posting it deletes that upload, so it never goes public.")
    dialog.get_by_role("button", name="Don't post").click()
    expect(status).to_have_text("The YouTube post for clip 2 won't be posted.")
    assert (seed.read_post(uploaded)["status"], seed.read_post(uploaded)["step"]) == ("rejected", "yt_delete")
    summaries = [e["summary"] for e in activity.list_entries("local", {"kind": "post.rejected"})]
    assert "Decided not to post the YouTube post; its private YouTube upload will be deleted" in summaries


@pytest.mark.req("6.7", "6.5")
def test_reset_to_ai_version_and_a_change_the_server_refuses(page, seed):
    job = world(seed)
    pid = add(seed, job, seed.clip(job, 1), days=2, hour=13,
              meta=dict(YT_META, title="My own title"), ai_meta=dict(YT_META))
    page.goto("/#/autopilot/queue")
    expect(row(page, pid).locator(".ap-q-what b")).to_have_text("My own title")
    open_post(page, pid)
    panel = page.get_by_role("region", name="Edit the YouTube post for clip 1")
    reset = panel.get_by_role("button", name="Reset to AI version of the title")
    expect(reset).to_be_visible()
    expect(panel.get_by_role("button", name="Reset to AI version of the description")).to_be_hidden()
    reset.click()
    title = page.locator(f"#ap-q-{pid}-title")
    expect(title).to_have_value(YT_META["title"])
    expect(title).to_be_focused()
    expect(reset).to_be_hidden()
    expect(row(page, pid).locator(".ap-q-what b")).to_have_text(YT_META["title"])
    expect(page.locator("#apStatus")).to_have_text(
        "The title of the YouTube post for clip 1 is back to the AI version.")
    assert seed.read_post(pid)["meta"]["title"] == YT_META["title"]

    # A change the server refuses stays in its field with the reason, and
    # Approve sends the user to it instead of approving.
    title.fill("x" * 101)
    page.locator(f"#ap-q-{pid}-description").click()                 # leaving the field saves it
    error = page.locator(f"#ap-q-{pid}-title-error")
    expect(error).to_have_text("The title is 101 characters; YouTube allows 100.")
    expect(title).to_have_attribute("aria-invalid", "true")
    expect(reset).to_be_visible()
    panel.get_by_role("button", name="Approve the YouTube post for clip 1").click()
    expect(page.locator("#apView .ap-problem")).to_contain_text("has a change that didn't save")
    expect(title).to_be_focused()
    expect(title).to_have_value("x" * 101)
    assert seed.read_post(pid)["approval"] == "pending"
    reset.click()
    expect(title).to_have_value(YT_META["title"])
    expect(error).to_be_hidden()
    expect(title).to_have_attribute("aria-invalid", "false")
    panel.get_by_role("button", name="Approve the YouTube post for clip 1").click()
    expect(row(page, pid).locator(".ap-pill")).to_have_text("Approved")
    assert seed.read_post(pid)["approval"] == "approved"


@pytest.mark.req("5.5", "5.6")
def test_moving_a_slot_keeps_the_users_time_and_warns_about_the_gap(page, seed):
    job = world(seed)
    add(seed, job, seed.clip(job, 1), days=2, hour=13)
    moved = add(seed, job, seed.clip(job, 2), days=3, hour=13)
    page.goto("/#/autopilot/queue")
    open_post(page, moved)
    slot = page.locator(f"#ap-q-{moved}-slot")
    expect(slot).to_have_value(slot_value(3, 13))                    # in the user's zone
    slot.fill(slot_value(2, 15))
    page.get_by_role("button", name="Move the post to this time").click()
    expect(section(page, 2).locator(f'.ap-q-row[data-id="{moved}"]')).to_be_visible()
    expect(row(page, moved).locator(".ap-q-time")).to_contain_text("15:00")
    expect(row(page, moved).locator(".ap-q-flag")).to_have_text("2 h gap")
    panel = page.get_by_role("region", name="Edit the YouTube post for clip 2")
    expect(panel).to_contain_text("You chose this time")
    gap = f"Less than 4 h from another YouTube post at {planner.local_label(at(2, 13), ZONE)}."
    expect(panel.locator(".ap-q-callout")).to_have_text([gap])
    expect(slot).to_be_focused()
    expect(page.locator("#apStatus")).to_have_text(
        f"Moved the YouTube post for clip 2 to {when(2, '15:00')}. {gap}")
    stored = seed.read_post(moved)
    assert (stored["slot_at"], stored["user_locked"], stored["slot_basis"]) == (at(2, 15), 1, "user")


@pytest.mark.req("6.8")
def test_the_pause_switch_pauses_and_resumes_publishing(page, seed):
    job = world(seed)
    add(seed, job, seed.clip(job, 1), days=2, hour=13, approval="approved")
    page.goto("/#/autopilot/queue")
    switch = page.get_by_role("switch", name="Pause all publishing")
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(page.locator("#apView .ap-q-paused")).to_be_hidden()
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "true")
    expect(page.locator("#apView .ap-q-paused")).to_contain_text("Publishing is paused.")
    expect(page.locator("#apChip .ap-chip-counts")).to_have_text("Paused")
    expect(page.locator("#apStatus")).to_have_text("Publishing is paused.")
    assert seed.prefs()["paused"] is True
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(page.locator("#apView .ap-q-paused")).to_be_hidden()
    expect(page.locator("#apStatus")).to_have_text("Publishing resumed.")
    assert seed.prefs()["paused"] is False


@pytest.mark.req("10.10", "N5.1", "N5.2")
def test_a_failed_post_can_be_tried_again(page, seed):
    job = world(seed)
    nothing_sent = add(seed, job, seed.clip(job, 1), days=2, hour=13, status="failed",
                       approval="approved", retry_count=3, last_error_class="temporary",
                       last_error="Gave up after 3 retries: YouTube didn't answer.")
    container = add(seed, job, seed.clip(job, 2), "instagram", days=2, hour=18, status="failed",
                    approval="approved", step="ig_processing", step_data={"container": {"id": "c1"}},
                    last_error="Gave up after 3 retries: Instagram couldn't process the video.")
    page.goto("/#/autopilot/queue")
    expect(page.locator("#apChip .ap-chip-counts > span")).to_have_text(["2 failed"])

    # From the keyboard, with nothing on YouTube yet: back into the plan at its own time.
    again = row(page, nothing_sent).get_by_role(
        "button", name=f"Try again the YouTube post for clip 1, {when(2, '13:00')}")
    again.focus()
    page.keyboard.press("Enter")
    expect(row(page, nothing_sent).locator(".ap-pill")).to_have_text("Approved")
    expect(page.locator("#apStatus")).to_have_text(
        f"Trying the YouTube post for clip 1 again: it's planned for {when(2, '13:00')}.")
    expect(row(page, nothing_sent).get_by_role("button", name=re.compile(r"^Open the "))).to_be_focused()
    expect(row(page, nothing_sent).get_by_role("button", name=re.compile(r"^Don't post the "))).to_be_visible()
    expect(page.locator("#apChip .ap-chip-counts > span")).to_have_text(["1 failed"])
    stored = seed.read_post(nothing_sent)
    assert (stored["status"], stored["retry_count"], stored["last_error"], stored["slot_at"]) == (
        "scheduled", 0, None, at(2, 13))

    # From the open post, with an Instagram container already made: it carries on from there.
    open_post(page, container)
    panel = page.get_by_role("region", name="Edit the Instagram post for clip 2")
    expect(panel).to_contain_text("Gave up after 3 retries: Instagram couldn't process the video.")
    panel.get_by_role("button", name="Try again the Instagram post for clip 2").click()
    expect(row(page, container).locator(".ap-pill")).to_have_text("Retrying")
    expect(page.locator("#apStatus")).to_have_text("Trying the Instagram post for clip 2 again now.")
    expect(panel.get_by_role("button", name=re.compile(r"^Try again"))).to_have_count(0)
    stored = seed.read_post(container)
    assert (stored["status"], stored["step"], stored["step_data"]) == (
        "retrying", "ig_processing", {"container": {"id": "c1"}})
