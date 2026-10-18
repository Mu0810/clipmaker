"""The Autopilot shell in the browser (task 12.2; Requirements 6.8, 12.2, 12.3,
N5.1, N5.2): the header chip, its counts and announcements, notifications,
and the routes with Escape. Run with `pytest -m ui`."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.ui

T0 = datetime(2026, 11, 2, 9, 0, 0)
MAIN_TITLE = "Clip Maker — turn long videos into shorts"

FAKE_NOTIFICATIONS = """
  window.__notes = [];
  window.__asked = 0;
  class FakeNotification {
    constructor(title, options = {}) {
      this.title = title;
      window.__notes.push({ title, body: options.body, tag: options.tag });
      window.__lastNote = this;
    }
    close() { this.closed = true; }
  }
  FakeNotification.permission = "%s";
  FakeNotification.requestPermission = async () => {
    window.__asked += 1;
    FakeNotification.permission = "granted";
    return "granted";
  };
  Object.defineProperty(window, "Notification", { value: FakeNotification, configurable: true });
"""


def chip(page):
    return page.locator("#apChip")


def parts(page):
    return page.locator("#apChip .ap-chip-counts > span")


def count_requests(page) -> list[str]:
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url) if "/api/autopilot/counts" in r.url else None)
    return seen


@pytest.mark.req("12.2", "6.8", "N5.2")
def test_the_chip_shows_the_counts_and_follows_them_every_30_seconds(page, seed, page_log):
    seed.posts(waiting=2, failed=1)
    requests = count_requests(page)
    page.clock.install(time=T0)
    page.goto("/")
    expect(chip(page)).to_be_visible()
    expect(chip(page)).to_have_attribute("href", "#/autopilot/queue")
    expect(parts(page)).to_have_text(["2 to approve", "1 failed"])
    expect(chip(page)).to_have_attribute(
        "aria-label", "Autopilot: 2 posts need your approval, 1 post failed.")
    expect(chip(page)).to_have_class(re.compile(r"\blive\b"))
    expect(page.locator("#apLive")).to_have_text("")              # nothing announced on load
    assert len(requests) == 1

    page.clock.pause_at(T0 + timedelta(seconds=5))
    seed.posts(waiting=3, reconnect=1)
    seed.paused(True)
    page.clock.run_for(24_000)                                      # 29 s in: not yet
    page.wait_for_timeout(300)
    assert len(requests) == 1
    expect(parts(page)).to_have_text(["2 to approve", "1 failed"])
    page.clock.run_for(10_000)                                      # past 30 s: read again
    expect(parts(page)).to_have_text(["5 to approve", "1 failed", "1 to reconnect", "Paused"])
    assert len(requests) == 2
    sentence = ("Autopilot: 5 posts need your approval, 1 post failed, 1 post waits for a "
                "reconnect. Publishing is paused.")
    expect(page.locator("#apLive")).to_have_text(sentence)          # announced (aria-live)
    expect(page.locator("#apLive")).to_have_attribute("aria-live", "polite")
    expect(chip(page)).to_have_attribute("aria-label", sentence)
    assert page_log["outside"] and all(url.startswith("https://fonts.googleapis.com/")
                                       for url in page_log["outside"])


@pytest.mark.req("12.2", "6.8")
def test_with_nothing_to_do_the_chip_shows_its_name_and_the_pause(page, seed):
    page.goto("/")
    expect(chip(page)).to_have_attribute("aria-label", "Autopilot: nothing needs you.")
    expect(parts(page)).to_have_count(0)
    expect(chip(page)).not_to_have_class(re.compile(r"\blive\b"))
    expect(chip(page)).to_contain_text("Autopilot")
    seed.paused(True)
    page.evaluate("document.dispatchEvent(new Event('ap:changed'))")   # a screen changed posts
    expect(parts(page)).to_have_text(["Paused"])
    expect(chip(page)).to_have_attribute("aria-label", "Autopilot: publishing is paused.")


@pytest.mark.req("12.3")
def test_a_rising_count_shows_a_browser_notification_when_allowed(page, seed):
    page.add_init_script(FAKE_NOTIFICATIONS % "granted")
    seed.posts(waiting=1)
    page.clock.install(time=T0)
    page.goto("/")
    expect(parts(page)).to_have_text(["1 to approve"])
    assert page.evaluate("window.__notes") == []                    # not for the first reading
    page.clock.pause_at(T0 + timedelta(seconds=5))
    seed.posts(failed=1, waiting=2)
    page.clock.run_for(30_000)
    page.wait_for_function("window.__notes.length === 1")
    assert page.evaluate("window.__notes[0]") == {
        "title": "Clip Maker Autopilot", "tag": "clipmaker-autopilot",
        "body": "1 post failed. 2 more posts need your approval."}
    page.evaluate("window.__lastNote.onclick()")                    # the user clicks it
    expect(page).to_have_url(re.compile(r"/#/autopilot/queue$"))
    assert page.evaluate("window.__lastNote.closed") is True


@pytest.mark.req("12.3")
def test_no_notification_without_permission_or_when_counts_fall(page, seed):
    page.add_init_script(FAKE_NOTIFICATIONS % "denied")
    ids = seed.posts(waiting=2)
    page.goto("/")
    expect(parts(page)).to_have_text(["2 to approve"])
    seed.posts(failed=1)
    page.evaluate("document.dispatchEvent(new Event('ap:changed'))")
    expect(parts(page)).to_have_text(["2 to approve", "1 failed"])
    page.evaluate("Notification.permission = 'granted'")
    seed.update(ids[0], approval="approved")                         # approved: one fewer
    page.evaluate("document.dispatchEvent(new Event('ap:changed'))")
    expect(parts(page)).to_have_text(["1 to approve", "1 failed"])
    assert page.evaluate("window.__notes") == []


@pytest.mark.req("12.3", "N5.2")
def test_the_allow_notifications_prompt_shows_once(page):
    page.add_init_script(FAKE_NOTIFICATIONS % "default")
    allow = page.get_by_role("button", name="Allow notifications")
    page.goto("/")
    expect(chip(page)).to_have_attribute("aria-label", re.compile("^Autopilot"))
    expect(allow).to_have_count(0)                                   # not on the main page
    page.goto("/#/autopilot/history")
    expect(allow).to_be_visible()
    allow.click()
    expect(allow).to_be_hidden()
    assert page.evaluate("window.__asked") == 1
    expect(page.locator("#apLive")).to_have_text("Notifications are on.")
    page.reload()                                                    # still "default" in the fake
    expect(page.locator("#apView h1")).to_have_text("Post history")
    expect(allow).to_have_count(0)

    page.evaluate("localStorage.clear()")
    page.reload()
    page.get_by_role("button", name="Not now").click()
    expect(allow).to_be_hidden()
    page.reload()
    expect(page.locator("#apView h1")).to_have_text("Post history")
    expect(allow).to_have_count(0)
    assert page.evaluate("window.__asked") == 0


@pytest.mark.req("N5.1", "N5.2")
def test_escape_returns_to_the_previous_route_then_to_the_main_page(page, live_server):
    page.goto("/")
    main, view = page.locator("main.wrap"), page.locator("#apView")
    chip(page).click()
    expect(page).to_have_url(re.compile(r"/#/autopilot/queue$"))
    expect(view).to_be_visible()
    expect(main).to_be_hidden()
    sub = page.get_by_role("navigation", name="Autopilot")
    expect(sub.get_by_role("link", name="Queue")).to_have_attribute("aria-current", "page")
    expect(page.locator("#apView h1")).to_have_text("Review queue")
    expect(page.locator("#apView h1")).to_be_focused()               # the new screen's heading
    expect(page).to_have_title("Review queue · Clip Maker")
    expect(chip(page)).to_have_attribute("aria-current", "page")

    sub.get_by_role("link", name="History").click()
    expect(page.locator("#apView h1")).to_have_text("Post history")
    page.keyboard.press("Escape")
    expect(page).to_have_url(re.compile(r"/#/autopilot/queue$"))      # the previous route
    expect(page.locator("#apView h1")).to_have_text("Review queue")
    page.keyboard.press("Escape")
    expect(page).to_have_url(live_server + "/")
    expect(main).to_be_visible()
    expect(view).to_be_hidden()
    expect(page).to_have_title(MAIN_TITLE)
    expect(chip(page)).to_be_focused()
    expect(chip(page)).not_to_have_attribute("aria-current", "page")


@pytest.mark.req("N5.1")
def test_escape_on_a_direct_link_leaves_and_spares_an_open_dialog(page, live_server):
    page.goto("/#/autopilot/setup")
    view = page.locator("#apView")
    expect(page.locator("#apView h1")).to_have_text("Setup")
    page.locator("#schedChip").click()                               # a main-page dialog on top
    dialog = page.locator("#schedScrim")
    expect(dialog).to_have_class(re.compile(r"\bopen\b"))
    page.keyboard.press("Escape")
    expect(dialog).not_to_have_class(re.compile(r"\bopen\b"))          # the dialog closed...
    expect(view).to_be_visible()                                     # ...and the route stayed
    expect(page).to_have_url(re.compile(r"/#/autopilot/setup$"))
    page.keyboard.press("Escape")
    expect(page).to_have_url(live_server + "/")
    expect(page.locator("main.wrap")).to_be_visible()

    page.goto("/#/autopilot/activity")
    page.get_by_role("button", name="Back to Clip Maker").click()
    expect(page.locator("main.wrap")).to_be_visible()
    expect(view).to_be_hidden()


@pytest.mark.req("N5.1")
def test_the_editor_route_takes_the_whole_window(page, live_server):
    page.goto("/#/editor/0123456789ab-2")
    expect(page.locator("#apView h1")).to_have_text("Clip editor")
    expect(page.locator("header.nav")).to_be_hidden()
    expect(page.get_by_role("navigation", name="Autopilot")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.locator("header.nav")).to_be_visible()
    expect(page.locator("main.wrap")).to_be_visible()

    page.goto("/#/editor/..%2Fsecret")                               # not a clip: no route
    expect(page.locator("main.wrap")).to_be_visible()
    expect(page.locator("#apView")).to_be_hidden()


# ── found in the usability review (task 12.7) ───────────────────────────

LEFT_3 = "Autopilot can't publish yet: Setup has 3 required items left."


@pytest.mark.req("11.1", "N5.2")
def test_until_something_can_publish_every_screen_but_setup_says_so(page, seed):
    seed.timezone("Asia/Kolkata")
    page.goto("/#/autopilot/queue")
    notice = page.get_by_role("region", name="Setup")
    expect(notice).to_have_text(re.compile(rf"^{re.escape(LEFT_3)}\s*Open Setup$"))
    notice.get_by_role("link", name="Open Setup").click()
    expect(page.locator("#apView h1")).to_have_text("Setup")
    expect(notice).to_have_count(0)                                  # Setup lists the items itself

    page.goto("/")                                                   # and the composer, with Autopilot on
    page.locator("label.ap-switch").click()
    expect(page.locator("#apSetupNote")).to_have_text(f"{LEFT_3} Open Setup")
    page.locator("label.ap-switch").click()
    expect(page.locator("#apSetupNote")).to_be_hidden()

    seed.youtube(made_for_kids=False)                                # one platform ready is enough
    page.goto("/#/autopilot/history")
    expect(page.locator("#apView h1")).to_have_text("Post history")
    page.wait_for_load_state("networkidle")
    expect(notice).to_have_count(0)


@pytest.mark.req("1.1", "N5.1")
def test_the_empty_queue_leads_back_to_the_form_with_autopilot_on(page, seed):
    seed.timezone("Asia/Kolkata")
    page.goto("/#/autopilot/queue")
    expect(page.locator("#apView .ap-empty-text")).to_contain_text("Nothing to review yet.")
    page.get_by_role("button", name="Give Autopilot a video").click()
    expect(page.locator("main.wrap")).to_be_visible()
    expect(page.get_by_role("switch", name="Autopilot")).to_be_checked()
    expect(page.locator("#url")).to_be_focused()
    expect(page.locator("#go")).to_have_text("Start Autopilot")
    expect(page.locator("#emptyState .ap-when-on")).to_be_visible()   # where Autopilot's results go
    expect(page.locator("#emptyState .ap-when-off")).to_be_hidden()
