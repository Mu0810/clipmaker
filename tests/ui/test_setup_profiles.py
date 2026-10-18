"""Setup, Profiles, History and Activity in the browser (task 12.5;
Requirements 3.9, 11.1-11.8, 12.1, 12.4, 12.5, 14.1, 14.4, N5.1, N5.2).
Run with `pytest -m ui`.

Connections are seeded straight into the live server's data folder. Meta is
the fake Graph API from tests/fakes/instagram.py, which the live server
reaches because it runs in this process; the browser's own trip to Meta's
sign-in page is answered empty by the page fixture and recorded."""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from autopilot import connections_api, profiles, store
from tests.fakes.instagram import APP, FakeInstagram, page as meta_page

pytestmark = pytest.mark.ui

DAY = 86400


def setup_page(page, seed, zone: str = "Asia/Kolkata"):
    seed.timezone(zone)
    page.goto("/#/autopilot/setup")
    expect(page.locator("#apView h1")).to_have_text("Setup")
    expect(page.locator("#apView .ap-checklist")).to_be_visible()


def item(page, key: str):
    return page.locator(f'#apView .ap-ck[data-key="{key}"]')


@pytest.mark.req("11.1", "11.2", "11.3", "11.5", "11.8", "N5.1")
def test_the_checklist_follows_the_seeded_accounts(page, seed):
    setup_page(page, seed)
    ready = page.locator("#apView .ap-ready-line")
    expect(ready).to_have_text(["YouTube: publishing is off until 2 required items are done.",
                                "Instagram: publishing is off until 1 required item is done."])
    expect(item(page, "youtube_connected").locator(".ap-pill")).to_have_text("Needed")
    expect(item(page, "youtube_connected").locator(".ap-ck-msg")).to_have_text(
        "Connect your YouTube channel for Autopilot.")
    expect(item(page, "ai_engine").locator(".ap-pill")).to_have_text("Recommended")
    expect(item(page, "ai_engine").locator(".ap-ck-kind")).to_have_text("Advisory")
    for key in ("youtube_connected", "ai_engine"):                # the page's own .err/.ok don't leak in
        assert item(page, key).locator(".ap-ck-label b").evaluate("b => getComputedStyle(b).color") == \
            "rgb(31, 36, 33)"
    expect(item(page, "youtube_audit").locator(".ap-ck-msg")).to_have_text(
        "YouTube uploads will stay private until your Google Cloud project passes YouTube's API audit.")
    expect(item(page, "timezone").locator(".ap-ck-msg")).to_have_text("Posting times use Asia/Kolkata.")
    youtube = page.get_by_role("region", name="YouTube")
    expect(youtube.get_by_text("Manage your YouTube videos", exact=True)).to_be_visible()
    expect(youtube.locator(".ap-perms li").first).to_have_text(
        "Manage your YouTube videosAutopilotNeeded to upload, schedule, update or remove the videos "
        "Autopilot posts, and to read their status and views.")

    seed.youtube(autopilot=False)                                  # today's manual-upload grant only
    seed.instagram(publish=False)
    page.reload()
    expect(item(page, "youtube_connected").locator(".ap-ck-msg")).to_have_text(
        "Connected for manual uploads only; Autopilot needs permission to manage the videos it posts.")
    expect(item(page, "instagram_connected").locator(".ap-ck-msg")).to_have_text(
        "Connected, but permission to publish content is missing.")
    expect(youtube.locator(".ap-conn")).to_contain_text("Connected for manual uploads only")

    seed.youtube(autopilot=True)
    seed.instagram(publish=True)
    page.reload()
    expect(item(page, "youtube_connected").locator(".ap-pill")).to_have_text("Done")
    expect(item(page, "instagram_connected").locator(".ap-pill")).to_have_text("Done")
    expect(ready).to_have_text(["YouTube: publishing is off until 1 required item is done.",
                                "Instagram: Autopilot can publish."])
    expect(youtube.locator(".ap-conn").first).to_have_text("Connected to Manish Clips for Autopilot.")
    details = youtube.locator(".ap-details")
    expect(details).to_contain_text("ChannelManish Clips")
    expect(details).to_contain_text(re.compile(r"Connected\d{1,2} [A-Z][a-z]{2,3} \d{4}"))
    expect(details).to_contain_text("Last publishedNothing published yet")
    expect(page.get_by_role("region", name="Instagram").locator(".ap-details")).to_contain_text(
        "Sign-in expiresDoesn't expire")

    # The fix-it step takes the user to the audience choice (11.2).
    item(page, "youtube_audience").get_by_role("button").click()
    audience = page.locator('[data-focus="yt-audience"]')
    expect(audience).to_be_focused()
    audience.get_by_label("Not made for kids").check()
    expect(ready.first).to_have_text("YouTube: Autopilot can publish.")
    expect(item(page, "youtube_audience").locator(".ap-pill")).to_have_text("Done")
    expect(page.locator("#apStatus")).to_have_text("Channel audience: not made for kids.")
    with store.connect() as conn:
        assert conn.execute("SELECT made_for_kids FROM accounts WHERE platform='youtube'").fetchone()[0] == 0

    youtube.get_by_label("My Google Cloud project passed YouTube's API audit").check()
    expect(item(page, "youtube_audit").locator(".ap-pill")).to_have_text("Done")


@pytest.mark.req("11.4", "11.8")
def test_instagram_connect_opens_metas_sign_in(page, seed, live_server, page_log):
    setup_page(page, seed)
    instagram = page.get_by_role("region", name="Instagram")
    connect = instagram.get_by_role("button", name="Connect Instagram")
    expect(connect).to_be_disabled()                               # the Meta app comes first
    item(page, "instagram_connected").get_by_role("button", name="Connect Instagram").click()
    expect(instagram.get_by_label("App ID")).to_be_focused()
    expect(instagram.locator(".ap-perms")).to_contain_text("instagram_content_publishPublish the Reels Autopilot posts.")

    instagram.get_by_label("App ID").fill(APP["app_id"])
    instagram.get_by_label("Login configuration ID").fill(APP["config_id"])
    instagram.get_by_role("button", name="Save app settings").click()
    expect(instagram.get_by_label("App secret")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#ap-setup-ig-secret-error")).to_have_text("Enter the app secret.")
    instagram.get_by_label("App secret").fill(APP["app_secret"])
    instagram.get_by_role("button", name="Save app settings").click()
    expect(page.locator("#apStatus")).to_have_text("Saved the Meta app settings.")
    connect = page.get_by_role("region", name="Instagram").get_by_role("button", name="Connect Instagram")
    expect(connect).to_be_enabled()
    expect(connect).to_be_focused()

    connect.click()
    page.wait_for_url(re.compile(r"^https://www\.facebook\.com/v26\.0/dialog/oauth\?"))
    query = parse_qs(urlparse(page.url).query)
    port = urlparse(live_server).port
    assert query["client_id"] == [APP["app_id"]] and query["config_id"] == [APP["config_id"]]
    assert query["redirect_uri"] == [f"http://localhost:{port}/api/connections/instagram/callback"]
    assert query["response_type"] == ["code"] and query["state"][0]
    assert any(url.startswith("https://www.facebook.com/v26.0/dialog/oauth") for url in page_log["outside"])


@pytest.mark.req("11.4")
def test_coming_back_from_meta_opens_setup_with_what_happened(page, seed):
    seed.timezone("Asia/Kolkata")
    seed.meta_app()
    connections_api._record_error("local", "You cancelled the Meta sign-in, so Instagram wasn't connected.")
    page.goto("/?instagram=error")
    expect(page.locator("#apView h1")).to_have_text("Setup")
    expect(page).to_have_url(re.compile(r"/#/autopilot/setup$"))        # the query is gone
    expect(page.get_by_role("region", name="Instagram").locator(".ap-q-callout.err")).to_have_text(
        "Instagram wasn't connected. You cancelled the Meta sign-in, so Instagram wasn't connected.")


@pytest.mark.req("11.4", "11.5")
def test_a_pasted_token_with_several_pages_asks_which_account(page, seed, net):
    fake = FakeInstagram(net)
    fake.pages = [meta_page("p1"), meta_page("p2")]
    seed.meta_app()
    setup_page(page, seed)
    instagram = page.get_by_role("region", name="Instagram")
    instagram.get_by_text("Sign-in can't come back to Clip Maker? Paste a token instead").click()
    instagram.get_by_role("button", name="Connect with this token").click()
    expect(page.locator("#ap-setup-ig-token-error")).to_have_text("Paste the token first.")
    instagram.get_by_label("User access token").fill("EAAfromTheExplorer")
    instagram.get_by_role("button", name="Connect with this token").click()

    picker = page.get_by_role("group", name="Choose the Instagram account Autopilot posts to")
    expect(picker.get_by_role("radio")).to_have_count(2)
    expect(page.get_by_role("button", name="Use this account")).to_be_focused()
    picker.get_by_label(re.compile(r"^@userp2 ")).check()
    page.get_by_role("button", name="Use this account").click()
    instagram = page.get_by_role("region", name="Instagram")
    expect(instagram.locator(".ap-conn").first).to_have_text("Connected to @userp2 through Meta.")
    expect(instagram.locator(".ap-details")).to_contain_text("Account@userp2")
    expect(instagram.locator(".ap-details")).to_contain_text("Data access until")
    expect(item(page, "instagram_connected").locator(".ap-pill")).to_have_text("Done")
    expect(instagram.get_by_role("button", name="Disconnect Instagram")).to_be_visible()


@pytest.mark.req("14.1", "N5.2")
def test_a_profile_validation_error_shows_next_to_its_field(page, seed):
    page.goto("/#/autopilot/profiles")
    form = page.locator("#apView form.ap-prof-form")
    expect(form.get_by_role("heading", level=2)).to_have_text("Default")
    expect(form.get_by_label("Subtitle style")).to_have_value("words")
    save = form.get_by_role("button", name="Save changes")
    expect(save).to_be_disabled()
    form.get_by_label("Shortest clip, in seconds").fill("70")          # longer than the longest, 60
    form.get_by_label("Hashtags on every post").fill("#tips #bad-tag")
    expect(form.locator(".ap-prof-status")).to_have_text("Unsaved changes.")
    expect(form.get_by_role("button", name="Re-plan pending posts")).to_be_disabled()
    save.click()

    longest = form.get_by_label("Longest clip, in seconds")
    expect(page.locator("#ap-prof-length-error")).to_have_text("The longest clip must be longer than the shortest.")
    expect(longest).to_have_attribute("aria-invalid", "true")
    expect(longest).to_be_focused()
    expect(page.locator("#ap-prof-tags-error")).to_have_text(
        "'bad-tag' is not a valid hashtag (letters, numbers and _ only).")
    expect(form.get_by_label("Hashtags on every post")).to_have_attribute("aria-describedby",
                                                                          "ap-prof-tags-hint ap-prof-tags-error")
    expect(page.locator("#apStatus")).to_have_text("Not saved: 2 problems to fix.")

    # Posting hours are picked from chips (YouTube's Tuesday defaults are 12:00 and 18:00).
    youtube = form.get_by_role("grid", name="YouTube posting hours")
    tuesday = youtube.get_by_role("row", name=re.compile(r"^Tuesday"))
    nine = tuesday.get_by_role("button", name="9:00", exact=True)
    expect(nine).to_have_attribute("aria-pressed", "false")
    expect(tuesday.get_by_role("button", name="12:00", exact=True)).to_have_attribute("aria-pressed", "true")
    nine.click()
    expect(nine).to_have_attribute("aria-pressed", "true")
    expect(nine).to_be_focused()
    # The keyboard moves along the grid: Right along the day, Up and Down between days.
    for key in ("ArrowRight",) * 5:
        page.keyboard.press(key)
    fourteen = tuesday.get_by_role("button", name="14:00", exact=True)
    expect(fourteen).to_be_focused()
    page.keyboard.press("Space")
    expect(fourteen).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("ArrowUp")
    expect(youtube.get_by_role("row", name=re.compile(r"^Monday")).get_by_role("button", name="14:00", exact=True)).to_be_focused()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("End")
    expect(tuesday.get_by_role("button", name="23:00", exact=True)).to_be_focused()
    # Each grid is one Tab stop, and it remembers the chip you left from.
    page.keyboard.press("Tab")
    instagram = form.get_by_role("grid", name="Instagram posting hours")
    expect(instagram.get_by_role("row", name=re.compile(r"^Monday")).get_by_role("button", name="11:00", exact=True)).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(tuesday.get_by_role("button", name="23:00", exact=True)).to_be_focused()

    form.get_by_label("Shortest clip, in seconds").fill("15")
    form.get_by_label("Hashtags on every post").fill("#tips #growth")
    save.click()
    expect(page.locator("#apStatus")).to_have_text("Saved Default.")
    expect(page.locator("#ap-prof-length-error")).to_be_hidden()
    saved = profiles.ensure_default("local")["settings"]
    assert (saved["length_min_s"], saved["brand"]["always_hashtags"]) == (15, ["tips", "growth"])
    assert saved["windows"]["youtube"]["1"] == [9, 12, 14, 18]
    assert saved["windows"]["youtube"]["0"] == [15, 18]                 # Monday unchanged


@pytest.mark.req("14.1", "2.7", "4.4", "N5.2")
def test_the_languages_are_picked_from_menus(page, seed):
    seed.profile("Lisbon", metadata_language="pt-BR")             # a code typed before the menus
    page.goto("/#/autopilot/profiles")
    form = page.locator("#apView form.ap-prof-form")
    spoken = form.get_by_label("Spoken language")
    titles = form.get_by_label("Language of titles and captions")
    expect(spoken).to_have_value("")
    expect(spoken.locator("option").first).to_have_text("Detect it automatically")
    expect(titles.locator("option").first).to_have_text("Same as the spoken language")
    expect(spoken.locator("option")).to_have_count(101)
    expect(spoken.locator("option").nth(1)).to_have_text("Afrikaans")
    spoken.select_option(label="Hindi")
    titles.select_option(label="English")
    expect(form.locator(".ap-prof-status")).to_have_text("Unsaved changes.")
    form.get_by_role("button", name="Save changes").click()
    expect(page.locator("#apStatus")).to_have_text("Saved Default.")
    saved = profiles.ensure_default("local")["settings"]
    assert (saved["language"], saved["metadata_language"]) == ("hi", "en")
    expect(form.get_by_label("Spoken language")).to_have_value("hi")

    # A saved code the list doesn't have stays chosen, named by its language.
    page.get_by_role("list", name="Presets").get_by_role("button", name="Lisbon").click()
    titles = form.get_by_label("Language of titles and captions")
    expect(titles).to_have_value("pt-BR")
    expect(titles.locator("option:checked")).to_have_text("Portuguese (pt-BR)")


@pytest.mark.req("14.1", "14.4", "3.9")
def test_profiles_are_created_duplicated_made_default_and_deleted(page, seed):
    seed.track("beat.mp3")
    page.goto("/#/autopilot/profiles")
    side = page.get_by_role("list", name="Presets")
    expect(side.get_by_role("button")).to_have_text(["Default"])
    page.get_by_role("button", name="New preset").click()
    name = page.locator("#apView form.ap-prof-form").get_by_label("Name", exact=True)
    expect(name).to_be_focused()
    expect(name).to_have_value("New preset")
    name.fill("Podcast")
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#apStatus")).to_have_text("Saved Podcast.")
    expect(side.get_by_role("button")).to_have_text(["Default", "Podcast"])

    page.get_by_role("button", name="Make default").click()
    expect(side.get_by_role("button")).to_have_text(["PodcastDefault", "Default"])
    expect(page.get_by_role("button", name="This is the default")).to_be_disabled()
    page.get_by_role("button", name="Duplicate").click()
    expect(side.get_by_role("button")).to_have_text(["PodcastDefault", "Default", "Copy of Podcast"])
    page.get_by_role("button", name="Delete Copy of Podcast").click()
    page.get_by_role("dialog", name="Delete Copy of Podcast?").get_by_role("button", name="Delete preset").click()
    expect(side.get_by_role("button")).to_have_text(["PodcastDefault", "Default"])
    expect(page.locator("#apStatus")).to_have_text("Deleted Copy of Podcast.")

    page.get_by_role("button", name="Re-plan pending posts").click()
    expect(page.locator("#apView .ap-prof-status")).to_have_text("No planned posts needed moving.")

    # Music cleared for publishing (3.9): only cleared tracks can be chosen.
    music = page.locator("#apView").get_by_label("Background music")
    expect(music.locator("option")).to_have_text(["None"])
    page.get_by_label("beat.mp3 · I have the rights to publish it").check()
    expect(page.locator("#apStatus")).to_have_text("beat.mp3 is cleared for publishing.")
    expect(music.locator("option")).to_have_text(["None", "beat.mp3"])
    music.select_option("beat.mp3")
    page.get_by_role("button", name="Save changes").click()
    expect(page.locator("#apStatus")).to_have_text("Saved Podcast.")
    podcast = next(p for p in profiles.list_profiles("local") if p["name"] == "Podcast")
    assert podcast["is_default"] and podcast["settings"]["music_track"] == "beat.mp3"


@pytest.mark.req("14.1")
def test_unsaved_profile_changes_wait_while_another_screen_is_open(page, seed):
    page.goto("/#/autopilot/profiles")
    form = page.locator("#apView form.ap-prof-form")
    form.get_by_label("Clips per video").fill("9")
    page.get_by_role("navigation", name="Autopilot").get_by_role("link", name="Activity").click()
    expect(page.locator("#apView h1")).to_have_text("Activity")
    page.get_by_role("navigation", name="Autopilot").get_by_role("link", name="Presets").click()
    form = page.locator("#apView form.ap-prof-form")
    expect(form.get_by_label("Clips per video")).to_have_value("9")
    expect(form.locator(".ap-prof-status")).to_have_text("Your unsaved changes are back.")
    form.get_by_role("button", name="Discard changes").click()
    expect(form.get_by_label("Clips per video")).to_have_value("6")


@pytest.mark.req("12.4", "12.5", "8.7")
def test_history_rows_link_out(page, seed):
    seed.timezone("Asia/Kolkata")
    seed.youtube()
    seed.instagram()
    job = seed.source(title="Creator Economy Podcast")
    now = time.time()
    yt = seed.post(job_id=job, status="live", approval="approved", platform="youtube",
                   published_at=now - 3 * DAY, slot_at=now - 3 * DAY,
                   url="https://youtu.be/vid1", meta={"title": "Why the first 1,000 are the hardest"})
    seed.metric(yt, views=12345, likes=678, comments=9)
    ig = seed.post(job_id=job, status="live", approval="approved", platform="instagram",
                   published_at=now - 2 * DAY, slot_at=now - 2 * DAY,
                   url="https://www.instagram.com/reel/abc/", meta={"caption": "Stop chasing viral\n#tips"})
    seed.post(job_id=job, status="failed", approval="approved", platform="youtube", slot_at=now - DAY,
              last_error="YouTube refused the upload: the file is too long.", meta={"title": "Too long"})
    seed.post(job_id=job, status="kept_private", approval="approved", platform="youtube", slot_at=now - 4 * DAY,
              published_at=now - 4 * DAY, url="https://youtu.be/vid0", meta={"title": "Kept private"})
    page.goto("/#/autopilot/history")

    rows = page.locator("#apView .ap-table tbody tr")
    expect(rows).to_have_count(4)
    expect(rows.locator("td:nth-child(2) b")).to_have_text(
        ["Too long", "Stop chasing viral", "Why the first 1,000 are the hardest", "Kept private"])
    first_live = page.locator(f'#apView tr[data-id="{yt}"]')
    link = first_live.get_by_role("link", name=re.compile(r"^Open on YouTube"))
    expect(link).to_have_attribute("href", "https://youtu.be/vid1")
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    expect(first_live.locator("td.num")).to_have_text(["12,345", "678", "9"])
    expect(first_live.locator("td.num").first).to_have_attribute("title", "3 days after publishing")
    expect(first_live).to_contain_text("Manish Clips")
    expect(first_live.locator("td").first).to_have_text(re.compile(r"^\w{3} \d{1,2} \w{3} \d{4}, \d\d:\d\d IST$"))
    expect(page.locator(f'#apView tr[data-id="{ig}"]').get_by_role("link")).to_have_attribute(
        "href", "https://www.instagram.com/reel/abc/")
    expect(page.locator(f'#apView tr[data-id="{ig}"]')).to_contain_text("@manish.makes")
    expect(rows.nth(0)).to_contain_text("YouTube refused the upload: the file is too long.")
    expect(rows.nth(3).get_by_role("link", name=re.compile(r"^API audit form"))).to_have_attribute(
        "href", "https://support.google.com/youtube/contact/yt_api_form")
    expect(page.locator("#apView .ap-lead")).to_contain_text("Times in India Standard Time.")

    page.get_by_label("Platform").select_option("instagram")
    expect(rows).to_have_count(1)
    expect(rows.locator(".ap-pill")).to_have_text("Posted")
    expect(page.get_by_label("Status").locator("option")).to_have_text(
        ["Every status", "Posted", "Private", "Kept private by YouTube", "Failed"])
    page.get_by_label("Platform").select_option("")                 # straight on: the newer filter wins
    page.get_by_label("Status").select_option("failed")
    expect(rows.locator(".ap-pill")).to_have_text(["Failed"])
    page.wait_for_timeout(300)
    expect(rows).to_have_count(1)


@pytest.mark.req("12.1", "N5.1")
def test_activity_pages_load(page, seed):
    seed.timezone("Asia/Kolkata")
    start = time.time() - 3600
    for i in range(60):
        kind, actor = (("post.approved", "user") if i % 3 == 0 else
                       ("clips.chosen", "ai") if i % 3 == 1 else ("publish.live", "system"))
        seed.activity(kind, f"Entry {i:02d}", actor=actor, at=start + i)
    page.goto("/#/autopilot/activity")
    entries = page.locator("#apView .ap-act")
    expect(entries).to_have_count(50)
    expect(entries.first.locator(".ap-act-what")).to_have_text("Entry 59")      # newest first
    expect(entries.first.locator(".ap-pill")).to_have_text("Clip Maker")
    expect(entries.nth(1).locator(".ap-pill")).to_have_text("AI")
    expect(entries.nth(2).locator(".ap-pill")).to_have_text("You")
    expect(entries.first.locator("time")).to_have_text(re.compile(r"\d{4}, \d\d:\d\d IST$"))
    more = page.get_by_role("button", name="Show more")
    more.click()
    expect(entries).to_have_count(60)
    expect(entries.nth(50)).to_be_focused()
    expect(entries.last.locator(".ap-act-what")).to_have_text("Entry 00")
    expect(more).to_be_hidden()
    expect(page.locator("#apStatus")).to_have_text("Showing 60 entries.")

    page.get_by_label("Show").select_option("post.")
    expect(entries).to_have_count(20)
    expect(entries.locator(".ap-pill")).to_have_text(["You"] * 20)
