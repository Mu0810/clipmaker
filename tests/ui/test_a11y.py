"""Accessibility of the Autopilot screens in the browser (task 12.7;
Requirements N5.1, N5.2). Run with `pytest -m ui`.

`test_the_screen_passes_the_checks` opens each Autopilot screen, and the
Autopilot parts of the main page, on realistic seeded data and checks that:
- every control has an accessible name, read from Chrome's own accessibility
  tree over the DevTools protocol, and no text field is named only by its
  placeholder (WCAG 4.1.2, 3.3.2);
- every Tab stop is on screen and shows an outline at least 2 px wide with
  3:1 against what is around it (2.4.7, 1.4.11). The whole page is tabbed
  through once, and the stops inside the screen are checked;
- text meets 4.5:1, or 3:1 when large (1.4.3). Disabled controls are exempt,
  as WCAG allows, and so is the editor's preview, whose captions stand in for
  the ones burned into the video;
- no text-bearing button has the --terra background (#C4612F, 4.1:1 with
  white), at rest or from a hover or focus rule: they use --terra-hover;
- nothing that takes a click (it shows the pointer cursor) is out of the
  keyboard's reach, unless TWINS names the control that does the same from
  the keyboard.

The keyboard-only tests then do the main jobs of the review queue, the
editor and the readiness checklist without the mouse, starting from the
header chip, checking the focus at every stop and that each status change
is announced in #apStatus, the screens' polite live region."""
from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import expect

from tests.ui.test_composer import turn_on
from tests.ui.test_editor import (TITLE, add_post, clip, media_files, open_editor,  # noqa: F401 (fixtures)
                                  word, worker)
from tests.ui.test_queue import ZONE, add, open_post, row, section, sensitive, when, world
from tests.ui.test_setup_profiles import item, setup_page

pytestmark = pytest.mark.ui

DAY = 86400
VIEW = ["#apView", "#apChip", ".ap-dialog"]        # an Autopilot screen, the header chip and any dialog
EDITOR = ["#apView", ".ap-dialog"]                 # the editor takes the whole window

# ── the checks, in the page ─────────────────────────────────────────────

LIB = r"""
const SCOPES = args.scopes || [], SKIP = args.skip || [];
const parse = c => {
  if (!c || c === "transparent") return [0, 0, 0, 0];
  const m = /rgba?\(([^)]*)\)/.exec(c);
  if (!m) return null;
  const p = m[1].split(/[\s,\/]+/).filter(Boolean).map(Number);
  return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
};
const over = (top, under) => [0, 1, 2].map(i => top[i] * top[3] + under[i] * (1 - top[3])).concat([1]);
const channel = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
const luminance = c => 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2]);
const ratio = (a, b) => {
  const x = luminance(a), y = luminance(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
};
const inScope = el => SCOPES.some(s => el.closest(s));
const skipped = el => SKIP.some(s => el.closest(s));
const roots = () => SCOPES.flatMap(s => [...document.querySelectorAll(s)]);
const shown = el => {
  if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) || el.closest(".ap-sr")) return false;
  const r = el.getBoundingClientRect();
  return r.width >= 2 && r.height >= 2;
};
const describe = el => {
  let s = el.tagName.toLowerCase();
  if (el.id) s += `#${el.id}`;
  else if (el.classList.length) s += `.${[...el.classList].slice(0, 2).join(".")}`;
  const words = (el.getAttribute("aria-label") || el.textContent || el.value || el.getAttribute("placeholder") || "")
    .replace(/\s+/g, " ").trim().slice(0, 48);
  return words ? `${s} "${words}"` : s;
};
const opacity = el => {
  let o = 1;
  for (let n = el; n; n = n.parentElement) o *= Number(getComputedStyle(n).opacity);
  return o;
};
// The colour behind an element: its own background and its ancestors',
// composited; null where a picture or a gradient is behind it.
const backdrop = el => {
  const layers = [];
  for (let n = el; n; n = n.parentElement) {
    const style = getComputedStyle(n);
    if (style.backgroundImage !== "none") return null;
    const c = parse(style.backgroundColor);
    if (c && c[3] > 0) {
      layers.push(c);
      if (c[3] >= 1) break;
    }
  }
  return layers.reverse().reduce((under, c) => over(c, under), [255, 255, 255, 1]);
};
const isTerra = c => Boolean(c) && c[3] > 0 && c[0] === 196 && c[1] === 97 && c[2] === 47;
const DISABLED = ":disabled, [aria-disabled=true]";
const BUTTONS = "button, [role=button], a[href], input[type=submit], input[type=button], summary";
const FOCUSABLE = "a[href], button, input, select, textarea, summary, label, [tabindex], [contenteditable]";
// Colours are read once entrances, fades and focus transitions have ended
// (the spinners' endless animations aside).
await Promise.all(document.getAnimations().filter(a => {
  const end = a.effect ? a.effect.getComputedTiming().endTime : Infinity;
  return a.playState === "running" && Number.isFinite(end);
}).map(a => a.finished.catch(() => null)));
"""

CHECKS = LIB + r"""
const TWINS = args.twins || [];
const problems = [];
const seen = new Set();
const contrast = (el, colour, what) => {
  const bg = backdrop(el), fg = parse(colour);
  if (!bg || !fg) return;
  const style = getComputedStyle(el);
  const size = parseFloat(style.fontSize);
  const need = size >= 24 || (size >= 18.66 && Number(style.fontWeight) >= 700) ? 3 : 4.5;
  const r = ratio(over([fg[0], fg[1], fg[2], fg[3] * opacity(el)], bg), bg);
  if (r < need) {
    problems.push(`${what}: text at ${r.toFixed(2)}:1 (${colour} on rgb(${bg.slice(0, 3).map(Math.round).join(", ")})), ` +
                  `under ${need}:1`);
  }
};
for (const root of roots()) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    const el = t.parentElement;
    if (!el || !t.data.trim() || seen.has(el)) continue;
    seen.add(el);
    if (skipped(el) || el.closest(DISABLED) || !shown(el)) continue;
    contrast(el, getComputedStyle(el).color, describe(el));
  }
  for (const field of root.querySelectorAll("input, textarea, select")) {
    if (skipped(field) || field.matches(DISABLED) || !shown(field)) continue;
    if (["checkbox", "radio", "range", "file", "color", "hidden", "button", "submit"].includes(field.type)) continue;
    if (field.value) contrast(field, getComputedStyle(field).color, describe(field));
    else if (field.placeholder) {
      contrast(field, getComputedStyle(field, "::placeholder").color, `${describe(field)}, its placeholder`);
    }
  }
  for (const button of root.querySelectorAll(BUTTONS)) {
    if (shown(button) && button.textContent.trim() && isTerra(parse(getComputedStyle(button).backgroundColor))) {
      problems.push(`${describe(button)}: text on --terra; use --terra-hover`);
    }
  }
  for (const n of root.querySelectorAll("*")) {
    if (getComputedStyle(n).cursor !== "pointer" || n.closest(FOCUSABLE) || skipped(n) || !shown(n)) continue;
    if (TWINS.some(s => n.closest(s))) continue;
    const up = n.parentElement;
    if (up && getComputedStyle(up).cursor === "pointer" && !up.closest(FOCUSABLE)) continue;   // reported once
    problems.push(`${describe(n)}: takes a click but the keyboard can't reach it`);
  }
}
// Hover and focus rules that would put a text-bearing button on --terra.
const TERRA_RULE = /background(-color)?\s*:\s*(var\(--terra\)|#c4612f\b|rgb\(196,\s*97,\s*47\))/i;
const STATE = /:(hover|focus-visible|focus-within|focus|active)\b/;
const walk = rules => {
  for (const rule of rules) {
    if (rule.cssRules) walk(rule.cssRules);
    if (!rule.selectorText || !TERRA_RULE.test(rule.style.cssText)) continue;
    for (const selector of rule.selectorText.split(",")) {
      if (!STATE.test(selector)) continue;
      const base = selector.replace(new RegExp(STATE.source, "g"), "").trim() || "*";
      let hits = [];
      try { hits = [...document.querySelectorAll(base)]; } catch { continue; }
      for (const b of hits) {
        if (inScope(b) && b.matches(BUTTONS) && shown(b) && b.textContent.trim()) {
          problems.push(`${describe(b)}: "${selector.trim()}" puts its text on --terra; use --terra-hover`);
        }
      }
    }
  }
};
for (const sheet of document.styleSheets) {
  try { walk(sheet.cssRules); } catch { /* a stylesheet from another origin */ }
}
return problems;
"""

STOP = LIB + r"""
const el = document.activeElement;
if (!el || el === document.body || el === document.documentElement) return { kind: "none" };
if (args.mark) {
  const seen = window.__a11yStops || (window.__a11yStops = new WeakSet());
  if (seen.has(el)) return { kind: "repeat" };
  seen.add(el);
}
if (!inScope(el)) return { kind: "outside", what: describe(el) };
// A native control drawn as something else, like a switch's hidden
// checkbox and its track, shows its focus on what is drawn.
let box = el;
const own = el.getBoundingClientRect();
if (own.width < 2 || own.height < 2 || Number(getComputedStyle(el).opacity) === 0) {
  box = el.nextElementSibling || el.parentElement;
}
const style = getComputedStyle(box);
const r = box.getBoundingClientRect();
const onScreen = r.width >= 2 && r.height >= 2 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
let ring = null;
const width = parseFloat(style.outlineWidth) || 0;
if (style.outlineStyle !== "none" && width > 0) {
  const around = parseFloat(style.outlineOffset) < 0 ? backdrop(box) : backdrop(box.parentElement || box);
  const colour = parse(style.outlineColor);
  ring = { width, auto: style.outlineStyle === "auto",
           contrast: style.outlineStyle === "auto" || !around || !colour ? null : ratio(over(colour, around), around) };
} else {
  const m = /(rgba?\([^)]*\))\s+0px\s+0px\s+0px\s+([\d.]+)px/.exec(style.boxShadow || "");
  if (m) {
    const around = backdrop(box.parentElement || box);
    ring = { width: Number(m[2]), auto: false, contrast: around ? ratio(over(parse(m[1]), around), around) : null };
  }
}
const terra = Boolean(el.textContent.trim()) && isTerra(parse(getComputedStyle(el).backgroundColor));
return { kind: "stop", what: describe(el), onScreen, ring, terra };
"""

NAMED_ROLES = {"button", "link", "textbox", "searchbox", "checkbox", "radio", "switch", "slider", "spinbutton",
               "combobox", "listbox", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "disclosuretriangle",
               "progressbar", "meter", "dialog", "alertdialog", "image", "img", "radiogroup", "datetime",
               "inputtime", "date", "colorwell"}
TEXT_ROLES = {"textbox", "searchbox", "combobox", "spinbutton"}
# Pointer shortcuts with a control that does the same from the keyboard.
TWINS = {".ap-q-what": "a click opens the post, as the row's Open button does",
         ".ap-ed-strip": "a click moves the playhead, as the Position in the clip slider does"}
# The page scrolls smoothly; while tabbing it scrolls at once, so a stop is
# measured where it ends up rather than on its way there.
INSTANT = """if (!document.getElementById("a11y-instant")) {
  document.head.append(Object.assign(document.createElement("style"),
    { id: "a11y-instant", textContent: "html{scroll-behavior:auto!important}" }));
}"""


def run(page, script: str, **args):
    return page.evaluate(f"async (args) => {{{script}}}", args)


def unnamed(page, scopes: list[str]) -> list[str]:
    """Controls in `scopes` (each an #id or a .class) that Chrome's
    accessibility tree gives no name, or names only by a placeholder."""
    ids = {s[1:] for s in scopes if s.startswith("#")}
    classes = {s[1:] for s in scopes if s.startswith(".")}
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send("DOM.enable")
        cdp.send("Accessibility.enable")
        document = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})["root"]
        nodes = cdp.send("Accessibility.getFullAXTree")["nodes"]
    finally:
        cdp.detach()
    parent: dict = {}
    attrs: dict = {}
    stack = [(document, None)]
    while stack:
        node, up = stack.pop()
        key = node["backendNodeId"]
        parent[key] = up
        flat = node.get("attributes", [])
        attrs[key] = (node.get("localName", ""), dict(zip(flat[::2], flat[1::2])))
        stack += [(child, key) for child in node.get("children", []) + node.get("shadowRoots", [])]
        if "contentDocument" in node:
            stack.append((node["contentDocument"], key))

    def inside(key) -> bool:
        while key is not None:
            _, a = attrs.get(key, ("", {}))
            if a.get("id") in ids or classes & set(a.get("class", "").split()):
                return True
            key = parent.get(key)
        return False

    found = []
    for node in nodes:
        role = str((node.get("role") or {}).get("value", "")).lower()
        key = node.get("backendDOMNodeId")
        if node.get("ignored") or role not in NAMED_ROLES or key is None or not inside(key):
            continue
        tag, a = attrs.get(key, ("?", {}))
        what = f"{role} <{tag}" + "".join(f' {k}="{a[k]}"' for k in ("id", "class") if k in a) + ">"
        name = node.get("name") or {}
        text = str(name.get("value", "")).strip()
        if not text:
            found.append(f"{what}: no accessible name")
        elif role in TEXT_ROLES:
            used = next((s for s in name.get("sources", []) if s.get("value") and not s.get("superseded")), None)
            if used and (used.get("type") == "placeholder" or used.get("attribute") in ("placeholder", "aria-placeholder")):
                found.append(f"{what}: named only by its placeholder, {text!r}")
    return found


def ring_problems(stop: dict) -> list[str]:
    what, ring, found = stop["what"], stop["ring"], []
    if not stop["onScreen"]:
        found.append(f"{what}: has focus but isn't on screen")
    if ring is None:
        found.append(f"{what}: no focus outline")
    elif not ring["auto"]:
        if ring["width"] < 2:
            found.append(f"{what}: a focus outline only {ring['width']:g} px wide")
        if ring["contrast"] is not None and ring["contrast"] < 3:
            found.append(f"{what}: a focus outline at {ring['contrast']:.2f}:1 against what's around it, under 3:1")
    if stop["terra"]:
        found.append(f"{what}: on --terra while focused; use --terra-hover")
    return found


def tab_round(page, scopes: list[str], limit: int = 400) -> tuple[int, list[str]]:
    """Tab through the whole page once, checking each stop inside `scopes`."""
    run(page, INSTANT + "delete window.__a11yStops;")
    stops, found = 0, []
    for _ in range(limit):
        page.keyboard.press("Tab")
        stop = run(page, STOP, scopes=scopes, mark=True)
        if stop["kind"] == "repeat":
            return stops, found
        if stop["kind"] == "stop":
            stops += 1
            found += ring_problems(stop)
    return stops, [*found, f"focus didn't come round again within {limit} presses of Tab"]


def audit(page, scopes: list[str]) -> list[str]:
    found = unnamed(page, scopes)
    found += run(page, CHECKS, scopes=scopes, skip=[".ap-pv"], twins=list(TWINS))
    stops, rings = tab_round(page, scopes)
    assert stops, "Tab never reached the screen"
    return found + rings


def tab_to(page, target, scopes: list[str], *, back: bool = False, limit: int = 120) -> None:
    """Press Tab (Shift+Tab with `back`) until `target` has focus, checking
    the focus on every stop on the way."""
    run(page, INSTANT)
    found = []
    for _ in range(limit):
        if target.evaluate("el => el === document.activeElement"):
            assert not found, "\n".join(found)
            return
        page.keyboard.press("Shift+Tab" if back else "Tab")
        stop = run(page, STOP, scopes=scopes, mark=False)
        if stop["kind"] == "stop":
            found += ring_problems(stop)
    raise AssertionError(f"{limit} presses of {'Shift+Tab' if back else 'Tab'} didn't reach {target}")


# ── each screen, seeded ─────────────────────────────────────────────────

def on_queue(page, seed, request) -> list[str]:
    job = world(seed)
    first = add(seed, job, seed.clip(job, 1), days=2, hour=13)
    add(seed, job, seed.clip(job, 2), "instagram", days=2, hour=18, approval="approved")
    held = add(seed, job, seed.clip(job, 3), days=3, hour=13, holds=sensitive())
    add(seed, job, seed.clip(job, 4), days=4, hour=13, status="failed", approval="approved",
        last_error="YouTube refused the upload: the file is too long.")
    page.goto("/#/autopilot/queue")
    open_post(page, held)                                       # the panel, with its hold and every field
    row(page, first).get_by_role("checkbox").check()            # and the bar for a selection
    expect(page.get_by_role("region", name="Selected posts")).to_be_visible()
    return VIEW


def on_sources(page, seed, request) -> list[str]:
    seed.timezone(ZONE)
    seed.source(title="Failed talk", status="error", age=1, ai_calls=2,
                message="Error: Couldn't download the video.", error="Couldn't download the video.")
    planned = seed.source(title="Talk with posts", age=2, ai_calls=7, best_score=82,
                          message="Done: 2 clips, 2 posts planned.")
    seed.post(job_id=planned)
    seed.source(title="Long interview", status="running", age=3, stage="rendering", pct=70,
                message="Rendering clip 2 of 6")
    page.goto("/#/autopilot/sources")
    expect(page.locator("#apView .ap-src")).to_have_count(3)
    return VIEW


def on_history(page, seed, request) -> list[str]:
    seed.timezone(ZONE)
    seed.youtube()
    seed.instagram()
    job = seed.source(title="Creator Economy Podcast")
    now = time.time()
    live = seed.post(job_id=job, status="live", approval="approved", platform="youtube",
                     published_at=now - 3 * DAY, slot_at=now - 3 * DAY, url="https://youtu.be/vid1",
                     meta={"title": "Why the first 1,000 are the hardest"})
    seed.metric(live, views=12345, likes=678, comments=9)
    seed.post(job_id=job, status="live", approval="approved", platform="instagram", published_at=now - 2 * DAY,
              slot_at=now - 2 * DAY, url="https://www.instagram.com/reel/abc/", meta={"caption": "Stop chasing viral"})
    seed.post(job_id=job, status="failed", approval="approved", platform="youtube", slot_at=now - DAY,
              last_error="YouTube refused the upload: the file is too long.", meta={"title": "Too long"})
    seed.post(job_id=job, status="kept_private", approval="approved", platform="youtube", slot_at=now - 4 * DAY,
              published_at=now - 4 * DAY, url="https://youtu.be/vid0", meta={"title": "Kept private"})
    page.goto("/#/autopilot/history")
    expect(page.locator("#apView .ap-table tbody tr")).to_have_count(4)
    return VIEW


def on_activity(page, seed, request) -> list[str]:
    seed.timezone(ZONE)
    start = time.time() - 600
    for i, (kind, actor, summary) in enumerate([
            ("clips.chosen", "ai", "Picked 4 clips from “Creator Economy Podcast”."),
            ("post.approved", "user", "Approved the YouTube post for clip 2."),
            ("publish.live", "system", "The Instagram post for clip 1 is posted.")]):
        seed.activity(kind, summary, actor=actor, at=start + i)
    page.goto("/#/autopilot/activity")
    expect(page.locator("#apView .ap-act")).to_have_count(3)
    return VIEW


def on_profiles(page, seed, request) -> list[str]:
    seed.timezone(ZONE)
    seed.track("beat.mp3")
    seed.profile("Podcast", clips_per_source=3)
    page.goto("/#/autopilot/profiles")
    expect(page.get_by_role("button", name="Save changes")).to_be_visible()
    return VIEW


def on_setup(page, seed, request) -> list[str]:
    seed.youtube(autopilot=False)                               # connected for manual uploads only
    seed.meta_app()
    setup_page(page, seed)
    return VIEW


def on_editor(page, seed, request) -> list[str]:
    clip = request.getfixturevalue("clip")
    add_post(clip, platform="instagram", status="live", url="https://www.instagram.com/reel/abc/",
             published_at=time.time() - 3600)
    add_post(clip, platform="youtube", status="uploaded", platform_post_id="vid1",
             scheduled_publish_at=time.time() + DAY)
    open_editor(page, clip)
    word(page, "first").click()
    page.keyboard.press("Shift+ArrowRight")
    page.keyboard.press("Delete")                               # an edit, so Render and its choices show
    expect(page.get_by_label("Replace the uploaded YouTube video with version 2")).to_be_visible()
    return EDITOR


def on_composer(page, seed, request) -> list[str]:
    seed.profile("Weekly", clips_per_source=3)
    page.goto("/")
    turn_on(page)
    page.get_by_role("button", name="Several links").click()
    expect(page.locator("#apLinks")).to_be_visible()
    return ["#apBar", "#apLinksField", "#apMore", "#apSummary", "#apSetupNote", "#go", "#apMsg", "#apChip",
            ".ap-when-on", ".ap-dialog"]


def clip_page(page, request):
    clip = request.getfixturevalue("clip")
    add_post(clip, platform="youtube", status="scheduled", slot_at=time.time() + 3 * DAY)
    add_post(clip, platform="instagram", status="live", url="https://www.instagram.com/reel/abc/",
             published_at=time.time() - 3600)
    page.goto("/")
    page.locator("#libBtn").click()
    library_row = page.locator(f'#libBody .lib-row[data-job="{clip["job"]}"]')
    expect(library_row.locator(".ap-card-post")).to_have_count(2)
    return library_row, clip


def on_library(page, seed, request) -> list[str]:
    clip_page(page, request)
    return [".ap-card-posts"]


def on_cards(page, seed, request) -> list[str]:
    library_row, clip = clip_page(page, request)
    library_row.get_by_role("button", name="Open").click()
    card = page.locator(f'#clips .clip[data-job="{clip["job"]}"]')
    expect(card.locator(".ap-card-post")).to_have_count(2)
    return [".ap-card-posts", ".ap-card-edit"]


SCREENS = {"queue": on_queue, "sources": on_sources, "history": on_history, "activity": on_activity,
           "profiles": on_profiles, "setup": on_setup, "editor": on_editor, "composer": on_composer,
           "library": on_library, "cards": on_cards}


@pytest.mark.req("N5.2", "N5.1")
@pytest.mark.parametrize("screen", list(SCREENS))
def test_the_screen_passes_the_checks(page, seed, request, screen):
    scopes = SCREENS[screen](page, seed, request)
    problems = audit(page, scopes)
    assert not problems, "\n".join(problems)


@pytest.mark.req("N5.2")
def test_the_live_regions_are_on_the_page_before_anything_changes(page):
    html = page.request.get("/").text()                          # as served, before any script runs
    for region in ("apLive", "apStatus"):
        assert re.search(rf'<div id="{region}" class="ap-sr" role="status" aria-live="polite"></div>', html)


# ── the main jobs, with the keyboard alone ──────────────────────────────

def open_from_the_chip(page, title: str) -> None:
    """From the main page: Tab to the header chip and press Enter."""
    page.goto("/")
    tab_to(page, page.locator("#apChip"), VIEW)
    page.keyboard.press("Enter")
    heading = page.locator("#apView h1")
    expect(heading).to_have_text(title)
    expect(heading).to_be_focused()


@pytest.mark.req("N5.1", "N5.2", "6.2", "6.6", "6.8")
def test_the_queue_works_from_the_keyboard_alone(page, seed):
    job = world(seed)
    first = add(seed, job, seed.clip(job, 1), days=2, hour=13)
    second = add(seed, job, seed.clip(job, 2), days=2, hour=18)
    held = add(seed, job, seed.clip(job, 3), days=3, hour=13, holds=sensitive())
    status = page.locator("#apStatus")
    open_from_the_chip(page, "Review queue")

    tab_to(page, row(page, first).get_by_role("button", name=re.compile(r"^Approve the YouTube post for clip 1, ")),
           VIEW)
    page.keyboard.press("Enter")
    expect(status).to_have_text(f"Approved the YouTube post for clip 1; it's planned for {when(2, '13:00')}.")

    tab_to(page, row(page, second).get_by_role("button", name=re.compile(r"^Open the YouTube post for clip 2, ")),
           VIEW)
    page.keyboard.press("Enter")
    title = page.locator(f"#ap-q-{second}-title")
    tab_to(page, title, VIEW)
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type("x" * 101)
    page.keyboard.press("Tab")                                   # leaving the field saves it
    expect(status).to_have_text(
        "Didn't save the YouTube post for clip 2: The title is 101 characters; YouTube allows 100.")
    tab_to(page, title, VIEW, back=True)
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type("A clearer title")
    page.keyboard.press("Tab")
    expect(status).to_have_text("Saved the YouTube post for clip 2.")
    assert seed.read_post(second)["meta"]["title"] == "A clearer title"
    close = row(page, second).get_by_role("button", name=re.compile(r"^Close the "))
    tab_to(page, close, VIEW, back=True)                         # Escape is the text field's while in one
    page.keyboard.press("Enter")
    expect(row(page, second).get_by_role("button", name=re.compile(r"^Open the "))).to_be_focused()

    pause = page.get_by_role("switch", name="Pause all publishing")
    tab_to(page, pause, VIEW, back=True)
    page.keyboard.press("Space")
    expect(status).to_have_text("Publishing is paused.")
    expect(pause).to_have_attribute("aria-checked", "true")
    page.keyboard.press("Space")
    expect(status).to_have_text("Publishing resumed.")

    # A bulk approval of a held post, through its dialog (6.6).
    tab_to(page, section(page, 3).get_by_role("checkbox", name=re.compile(r"^Select all posts on ")), VIEW)
    page.keyboard.press("Space")
    bar = page.get_by_role("region", name="Selected posts")
    tab_to(page, bar.get_by_role("button", name="Approve 1"), VIEW)
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="Approve 1 post?")
    expect(dialog).to_be_visible()
    tab_to(page, dialog.get_by_role("button", name="Approve anyway"), VIEW)
    page.keyboard.press("Enter")
    expect(status).to_have_text("Approved 1 post.")
    assert seed.read_post(held)["approval"] == "approved"
    expect(page.locator("#apView .ap-q-results").get_by_role("button", name="Dismiss")).to_be_focused()


@pytest.mark.req("N5.1", "N5.2", "7.1", "7.3", "7.7", "7.10")
def test_the_editor_works_from_the_keyboard_alone(page, clip, worker):
    status = page.locator("#apStatus")
    open_editor(page, clip)
    expect(page.locator("#apView h1")).to_be_focused()

    # The transcript is one Tab stop; the arrows move between its words.
    tab_to(page, page.locator("#apView .ap-w[tabindex='0']"), EDITOR)
    expect(word(page, "Your")).to_be_focused()                    # the clip's first word
    page.keyboard.press("ArrowRight")
    expect(word(page, "first")).to_be_focused()
    page.keyboard.press("Shift+ArrowRight")
    page.keyboard.press("Delete")
    expect(status).to_have_text("Cut 2 words.")
    expect(word(page, "thousand")).to_have_class(re.compile(r"\bcut\b"))
    page.keyboard.press("ControlOrMeta+z")
    expect(status).to_have_text("Undid: Cut 2 words.")
    page.keyboard.press("ControlOrMeta+Shift+z")
    expect(status).to_have_text("Redid: Cut 2 words.")

    # The strip's handles: 0.1 s an arrow, 1 s with Shift (N5.1).
    start = page.get_by_role("slider", name="Clip start, fine trim")
    tab_to(page, start, EDITOR, back=True)
    expect(start).to_have_attribute("aria-valuenow", "10.0")
    page.keyboard.press("ArrowRight")
    expect(start).to_have_attribute("aria-valuenow", "10.1")
    page.keyboard.press("Shift+ArrowRight")
    expect(start).to_have_attribute("aria-valuenow", "11.1")
    page.keyboard.press("Shift+ArrowLeft")
    page.keyboard.press("ArrowLeft")
    expect(start).to_have_attribute("aria-valuenow", "10.0")

    # A problem an edit causes is announced, after the edit itself.
    end = page.get_by_role("slider", name="Clip end, fine trim")
    tab_to(page, end, EDITOR)
    page.keyboard.press("Home")
    expect(status).to_have_text(re.compile(r"^The clip is \d+\.\d s; clips must be at least 3 s\."))
    page.keyboard.press("ControlOrMeta+z")
    expect(status).to_have_text(re.compile(r"^Undid: "))

    render = page.get_by_role("button", name="Save as version 2")
    tab_to(page, render, EDITOR, back=True)
    page.keyboard.press("Enter")
    expect(status).to_have_text("Rendering version 2.")
    expect(status).to_have_text("Version 2 is ready and in use.", timeout=10_000)
    expect(page.locator("#apView .ap-ed-versions summary")).to_have_text("Version 2 · in use")


@pytest.mark.req("N5.1", "N5.2", "11.2", "11.3")
def test_the_checklist_works_from_the_keyboard_alone(page, seed):
    seed.timezone(ZONE)
    seed.youtube(autopilot=True)
    seed.instagram()
    status = page.locator("#apStatus")
    open_from_the_chip(page, "Review queue")
    tab_to(page, page.get_by_role("navigation", name="Autopilot").get_by_role("link", name="Setup"), VIEW, back=True)
    page.keyboard.press("Enter")
    expect(page.locator("#apView h1")).to_have_text("Setup")
    expect(page.locator("#apView h1")).to_be_focused()

    # The fix-it step goes to the audience, which the keyboard sets (11.2).
    tab_to(page, item(page, "youtube_audience").get_by_role("button"), VIEW)
    page.keyboard.press("Enter")
    expect(page.locator('[data-focus="yt-audience"]')).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_label("Not made for kids")).to_be_focused()
    page.keyboard.press("Space")
    expect(status).to_have_text("Channel audience: not made for kids.")
    expect(item(page, "youtube_audience").locator(".ap-pill")).to_have_text("Done")

    audit_box = page.get_by_label("My Google Cloud project passed YouTube's API audit")
    tab_to(page, audit_box, VIEW)
    page.keyboard.press("Space")
    expect(status).to_have_text("My Google Cloud project passed YouTube's API audit: yes.")
    expect(item(page, "youtube_audit").locator(".ap-pill")).to_have_text("Done")
    expect(audit_box).to_be_focused()
