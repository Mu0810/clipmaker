"""Unofficial YouTube upload by automating YouTube Studio with Playwright.

No Google Cloud project or OAuth client needed: you log into Google once in a
real browser window, and the saved session cookies are reused for subsequent
headless uploads.

⚠️  This drives the Studio web UI directly, which is unofficial and against
YouTube's Terms of Service. Selectors can break when the UI changes, it may hit
captchas/verification, and it risks your account. Use one you're willing to risk.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import paths


def _profile_dir() -> Path:
    """Persistent Chrome profile (cookies)."""
    return paths.data_file("yt_pw_profile")


def _meta_file() -> Path:
    """Cached channel display info."""
    return paths.data_file("yt_pw_account.json")


STUDIO = "https://studio.youtube.com"
UPLOAD_URL = "https://www.youtube.com/upload"

# Strip the flags Google uses to fingerprint automation, so the real Chrome
# login isn't rejected with "this browser may not be secure".
_STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
_IGNORE_ARGS = ["--enable-automation"]


class YTBrowserError(RuntimeError):
    pass


def available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def is_connected() -> bool:
    # A non-empty profile dir means a Chrome session has been established.
    return _profile_dir().exists() and any(_profile_dir().iterdir())


def account_status() -> dict:
    acct = None
    if _meta_file().exists():
        try:
            acct = json.loads(_meta_file().read_text())
        except Exception:
            acct = None
    return {"available": available(), "connected": is_connected(),
            "channel": acct}


def disconnect() -> None:
    import shutil
    if _profile_dir().exists():
        shutil.rmtree(_profile_dir(), ignore_errors=True)
    if _meta_file().exists():
        _meta_file().unlink()


def _sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception as e:
        raise YTBrowserError(
            "Browser automation needs Playwright. Install it:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        ) from e


def _launch_context(pw, headless: bool):
    """Launch a persistent context using the user's *real* installed Chrome.

    Real Chrome (channel="chrome") + a persistent profile + stripped automation
    flags is what gets past Google's "browser may not be secure" block. Falls
    back to bundled Chromium only if Chrome isn't installed (login may fail).
    """
    _profile_dir().mkdir(exist_ok=True)
    common = dict(
        user_data_dir=str(_profile_dir()),
        headless=headless,
        args=_STEALTH_ARGS,
        ignore_default_args=_IGNORE_ARGS,
        viewport={"width": 1280, "height": 800},
    )
    try:
        return pw.chromium.launch_persistent_context(channel="chrome", **common)
    except Exception:
        # No system Chrome — try the bundled Chromium (less likely to pass login).
        try:
            return pw.chromium.launch_persistent_context(**common)
        except Exception as e:
            raise YTBrowserError(
                f"Couldn't start a browser ({e}). Install Google Chrome, or run "
                "python -m playwright install chromium."
            ) from e


def _read_channel(page) -> dict | None:
    """Best-effort channel name + avatar from the Studio nav."""
    info = {}
    try:
        name = page.locator("#channel-name").first
        if name.count():
            info["title"] = (name.inner_text(timeout=4000) or "").strip()
    except Exception:
        pass
    try:
        img = page.locator("#avatar-btn img, #account-button img").first
        if img.count():
            info["thumbnail"] = img.get_attribute("src", timeout=4000)
    except Exception:
        pass
    return info or None


def connect(timeout_s: int = 240) -> dict:
    """Open a real browser, let the user log into Google, then cache the
    session. Blocks until login completes (or times out). Returns channel info.
    """
    sp = _sync_playwright()
    channel = None
    logged_in = False
    with sp() as pw:
        ctx = _launch_context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(STUDIO, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass

        # Logged in once Studio lands on the channel dashboard URL.
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if "studio.youtube.com/channel/" in page.url:
                logged_in = True
                break
            page.wait_for_timeout(1500)

        if logged_in:
            page.wait_for_timeout(1500)
            channel = _read_channel(page)
        ctx.close()       # persists cookies into _profile_dir()

    if not logged_in:
        raise YTBrowserError(
            "Login wasn't completed in the browser window. Try Connect again "
            "and finish signing into Google (including any 2FA)."
        )
    if channel:
        try:
            _meta_file().write_text(json.dumps(channel))
        except Exception:
            pass
    return {"connected": True, "channel": channel}


# Visibility radio names in the Studio publish step.
_VIS = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}


def _set_file(page, file_path: Path, timeout: int = 8000) -> bool:
    """Try to feed the clip into a hidden <input type=file>. Returns success.

    The input lives inside Studio's web components (sometimes shadow DOM); we
    try a plain query first, then a DOM-piercing one.
    """
    for sel in ('input[type="file"]', 'ytcp-uploads-file-picker input[type="file"]'):
        try:
            inp = page.locator(sel).first
            inp.wait_for(state="attached", timeout=timeout)
            inp.set_input_files(str(file_path))
            return True
        except Exception:
            continue
    return False


def _click_create_upload(page) -> bool:
    """Click Create → Upload videos. Returns True if both clicks landed."""
    try:
        page.locator('#create-icon, ytcp-button#create-icon, '
                     'tp-yt-paper-icon-button#create-icon').first.click(timeout=8000)
    except Exception:
        try:
            page.get_by_role("button", name="Create").first.click(timeout=8000)
        except Exception:
            return False
    page.wait_for_timeout(900)
    for getter in (
        lambda: page.get_by_text("Upload videos", exact=False).first,
        lambda: page.locator('tp-yt-paper-item:has-text("Upload")').first,
        lambda: page.get_by_role("menuitem", name="Upload videos").first,
    ):
        try:
            getter().click(timeout=4000)
            return True
        except Exception:
            continue
    return False


def _open_uploader(page, file_path: Path) -> None:
    """Open the Studio upload dialog and select the file, trying several routes
    so we don't depend on a single selector."""
    # 1) Land on the dashboard (this is what loads the upload components).
    try:
        page.goto(STUDIO, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
    except Exception:
        pass

    # 2) Fast path: hidden file input already present.
    if _set_file(page, file_path, timeout=6000):
        return

    # 3) Create → Upload videos. Prefer grabbing the hidden input the dialog
    #    mounts; fall back to a native file chooser if the click triggers one.
    try:
        with page.expect_file_chooser(timeout=4000) as fc:
            opened = _click_create_upload(page)
            if not opened:
                raise RuntimeError("create/upload menu not found")
        fc.value.set_files(str(file_path))
        return
    except Exception:
        # No native chooser fired — the dialog likely mounted a hidden input.
        if _set_file(page, file_path, timeout=10000):
            return

    # 4) Last resort: /upload page, hidden input, then SELECT FILES chooser.
    try:
        page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        if _set_file(page, file_path, timeout=10000):
            return
        with page.expect_file_chooser(timeout=15000) as fc:
            page.locator('#select-files-button, ytcp-button#select-files-button'
                         ).first.click(timeout=10000)
        fc.value.set_files(str(file_path))
        return
    except Exception as e:
        raise YTBrowserError(
            "Couldn't open the Studio uploader (the file picker never "
            f"appeared: {e}). Reconnect, or use the official API method."
        ) from e


def upload_video(file_path: Path, title: str, description: str = "",
                 tags: list[str] | None = None, privacy: str = "unlisted",
                 progress=None) -> dict:
    """Upload via the Studio UI using the cached session. Returns {video_id, url}."""
    if not is_connected():
        raise YTBrowserError("No browser session. Connect YouTube first.")
    if not file_path.exists():
        raise YTBrowserError(f"Clip not found: {file_path}")
    privacy = privacy if privacy in _VIS else "unlisted"

    def _say(msg):
        if progress:
            progress(msg)

    sp = _sync_playwright()
    with sp() as pw:
        # Headed: headless Chrome is more likely to be bot-flagged or served a
        # stripped uploader where the file input never renders.
        ctx = _launch_context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(60000)
        try:
            _say("Opening uploader…")
            _open_uploader(page, file_path)
            _say("Uploading file…")

            # Title (contenteditable). It pre-fills with the filename — clear it.
            title_box = page.locator(
                'ytcp-social-suggestions-textbox#title-textarea div#textbox, '
                '#title-textarea div#textbox').first
            title_box.wait_for(timeout=120000)
            title_box.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            title_box.type(title[:100] or "Clip")

            if description:
                try:
                    desc_box = page.locator(
                        'ytcp-social-suggestions-textbox#description-textarea '
                        'div#textbox, #description-textarea div#textbox').first
                    desc_box.click()
                    desc_box.type(description[:4900])
                except Exception:
                    pass

            # Not made for kids.
            try:
                page.locator(
                    'tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]'
                ).first.click(timeout=8000)
            except Exception:
                pass

            # Details → Video elements → Checks → Visibility.
            _say("Configuring…")
            for _ in range(3):
                page.locator('#next-button').click()
                page.wait_for_timeout(700)

            # Capture the share link before publishing.
            url = ""
            try:
                url = page.locator('a.ytcp-video-info').first.get_attribute(
                    "href", timeout=15000) or ""
            except Exception:
                url = ""

            # Set visibility, then wait until the file finishes uploading so the
            # publish button is enabled.
            page.locator(
                f'tp-yt-paper-radio-button[name="{_VIS[privacy]}"]').first.click()

            _say("Waiting for upload to finish…")
            done = page.locator('#done-button')
            for _ in range(120):                    # up to ~4 minutes
                disabled = done.get_attribute("aria-disabled")
                if disabled in (None, "false"):
                    break
                page.wait_for_timeout(2000)

            _say("Publishing…")
            done.click()
            page.wait_for_timeout(4000)
            ctx.close()
        except YTBrowserError:
            ctx.close()
            raise
        except Exception as e:
            ctx.close()
            raise YTBrowserError(
                f"Studio upload failed ({e}). The Studio UI may have changed — "
                "reconnect, or use the official API method."
            ) from e

    vid = url.rstrip("/").split("/")[-1] if url else ""
    return {"video_id": vid, "url": url or "https://studio.youtube.com"}
