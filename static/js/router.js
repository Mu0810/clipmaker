/* Hash routes for the Autopilot screens (design §15, task 12.2; N5.1, N5.2).

     #/autopilot/queue | sources | history | activity | profiles | setup
     #/editor/<clip>      (a clip id, or <job id>-<clip number>)

   A route hides the main page (main.wrap) and shows its screen in #apView,
   under the Autopilot sub-navigation; the editor takes the whole window.
   Any other hash shows the main page again.

   Each screen is an ES module in this folder, loaded the first time it's
   opened. It exports render(container, route), which may be async and may
   return a cleanup function, called when the screen is left.

   Escape, like the browser's Back, returns to the previous route; on a
   route opened directly, Escape goes to the main page. Escape is left alone
   while a dialog of the main page is open, while focus is in a text field,
   and while an element marked data-ap-escape is shown (a screen's own
   popover, which closes first). After each change the screen's heading
   takes focus, and an "ap:route" event reports the route on screen. */

export const SECTIONS = [
  { id: "queue", label: "Queue", title: "Review queue", module: "./queue.js" },
  { id: "sources", label: "Videos", title: "Videos", module: "./sources.js" },   // the address keeps "sources"
  { id: "history", label: "History", title: "Post history", module: "./history.js" },
  { id: "activity", label: "Activity", title: "Activity", module: "./activity.js" },
  { id: "profiles", label: "Presets", title: "Presets", module: "./profiles.js" },   // the address keeps "profiles"
  { id: "setup", label: "Setup", title: "Setup", module: "./setup.js" },
];
const EDITOR = { title: "Clip editor", module: "./editor.js" };
const BY_ID = new Map(SECTIONS.map(section => [section.id, section]));
const CLIP_REF = /^[A-Za-z0-9-]{1,64}$/;
const ORIGINAL_TITLE = document.title;

const visited = [];       // hashes seen in this tab, for Escape
let current = null;       // the route on screen, or null on the main page
let cleanup = null;
let renderCount = 0;
let mainScroll = 0;

const norm = hash => (hash === "#" ? "" : hash || "");

/** The route a hash names, or null for the main page. */
export function parse(hash = location.hash) {
  const section = /^#\/autopilot\/([a-z]+)\/?$/.exec(hash);
  if (section && BY_ID.has(section[1])) {
    return { kind: "autopilot", section: section[1], hash, ...BY_ID.get(section[1]) };
  }
  const editor = /^#\/editor\/([^/?#]+)$/.exec(hash);
  if (editor) {
    let ref = "";
    try { ref = decodeURIComponent(editor[1]); } catch { ref = ""; }
    if (CLIP_REF.test(ref)) return { kind: "editor", section: "editor", ref, hash, ...EDITOR };
  }
  return null;
}

export const route = () => current;

/** Open a route, e.g. go("#/autopilot/queue"). */
export function go(hash) {
  if (norm(location.hash) === norm(hash)) return;
  location.hash = hash;
}

/** What Escape does: the previous route, or the main page. */
export function back() {
  if (visited.length >= 2) history.back();
  else leave();
}

/** Back to Clip Maker: show the main page. Autopilot keeps running. */
export function leave() {
  if (!current) return;
  history.pushState(null, "", location.pathname + location.search);
  visited.push("");
  show();
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === true) node.setAttribute(name, "");
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(name, value);
  }
  node.append(...children);
  return node;
}

function frame(view, target) {
  view.replaceChildren();
  if (target.kind === "autopilot") {
    const inner = el("div", { class: "ap-sub-in" });
    for (const section of SECTIONS) {
      inner.append(el("a", { href: `#/autopilot/${section.id}`,
                             "aria-current": section.id === target.section ? "page" : null },
                      section.label));
    }
    const close = el("button", { type: "button", class: "ap-close" }, "Back to Clip Maker");
    close.addEventListener("click", leave);
    inner.append(el("span", { class: "ap-spacer" }), close);
    view.append(el("nav", { class: "ap-sub", "aria-label": "Autopilot" }, inner));
  }
  view.append(el("div", { id: "apNotice", class: "ap-notice", hidden: true }));
  const page = el("div", { id: "apPage", class: target.kind === "editor" ? "ap-editor" : "ap-page" });
  view.append(page);
  return page;
}

function failed(page, target, err) {
  console.error(`Couldn't open ${target.title}:`, err);
  const parts = [el("h1", { class: "ap-h1", tabindex: "-1" }, target.title),
                 el("p", { class: "ap-empty", role: "alert" },
                    "This screen couldn't be opened. Reload the page to try again.")];
  if (target.kind === "editor") {
    const backButton = el("button", { type: "button", class: "ap-btn ghost" }, "Back");
    backButton.addEventListener("click", back);
    parts.push(el("p", {}, backButton));
    page.replaceChildren(el("div", { class: "ap-page" }, ...parts));   // the editor has no padding of its own
    return;
  }
  page.replaceChildren(...parts);
}

function focusHeading(page) {
  const heading = page.querySelector("h1");
  if (!heading) return;
  if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
  heading.focus({ preventScroll: true });
}

function announce(target) {
  document.dispatchEvent(new CustomEvent("ap:route", { detail: { route: target } }));
}

async function show() {
  const target = parse();
  const view = document.getElementById("apView");
  const main = document.querySelector("main.wrap");
  if (!view) return;
  if (cleanup) {
    try { cleanup(); } catch (err) { console.error(err); }
    cleanup = null;
  }
  const count = ++renderCount;
  if (!target) {
    const wasOpen = current !== null;
    current = null;
    view.hidden = true;
    view.replaceChildren();
    document.body.classList.remove("ap-full");
    if (main) main.hidden = false;
    document.title = ORIGINAL_TITLE;
    if (wasOpen) {
      window.scrollTo(0, mainScroll);
      const chip = document.getElementById("apChip");
      if (chip && chip.offsetParent !== null) chip.focus({ preventScroll: true });
    }
    announce(null);
    return;
  }
  if (!current) mainScroll = window.scrollY;
  current = target;
  if (main) main.hidden = true;
  view.hidden = false;
  document.body.classList.toggle("ap-full", target.kind === "editor");
  document.title = `${target.title} · Clip Maker`;
  const page = frame(view, target);
  window.scrollTo(0, 0);
  announce(target);
  try {
    const screen = await import(target.module);
    if (count !== renderCount) return;               // another route opened meanwhile
    if (typeof screen.render !== "function") throw new Error(`${target.module} has no render()`);
    const result = await screen.render(page, target);
    if (count !== renderCount) {
      if (typeof result === "function") result();
      return;
    }
    cleanup = typeof result === "function" ? result : null;
  } catch (err) {
    if (count !== renderCount) return;
    failed(page, target, err);
  }
  focusHeading(page);
}

function onKey(event) {
  if (event.key !== "Escape" || !current || event.defaultPrevented) return;
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.isComposing) return;
  if (document.querySelector(".scrim.open")) return;            // the dialog closes first
  if (document.querySelector("[data-ap-escape]:not([hidden])")) return;
  const target = event.target;
  if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
  event.preventDefault();
  back();
}

let started = false;

/** The sign-ins that leave the page (Instagram, and YouTube with sign-in on)
    come back to /?instagram=… or /?youtube=…. That opens Setup, which reads
    the outcome from sessionStorage, and the query is dropped from the
    address, so a reload doesn't show it again (task 12.5). */
export const SIGNIN_RETURN_KEY = "cm-ap-signin-return";
const SIGNIN_OUTCOMES = new Set(["connected", "choose", "error"]);

function takeSignInReturn() {
  const query = new URLSearchParams(location.search);
  for (const platform of ["youtube", "instagram"]) {
    const outcome = query.get(platform);
    if (outcome === null) continue;
    query.delete(platform);
    try {
      sessionStorage.setItem(SIGNIN_RETURN_KEY, JSON.stringify(
        { platform, outcome: SIGNIN_OUTCOMES.has(outcome) ? outcome : "error" }));
    } catch { /* private mode: Setup still shows the connection's state */ }
    const rest = query.toString();
    history.replaceState(null, "", `${location.pathname}${rest ? `?${rest}` : ""}#/autopilot/setup`);
    return;
  }
}

/** Start routing: show the route in the address bar, and follow changes. */
export function start() {
  if (started) return;
  started = true;
  takeSignInReturn();
  visited.push(norm(location.hash));
  window.addEventListener("hashchange", () => {
    const hash = norm(location.hash);
    if (visited.length >= 2 && visited[visited.length - 2] === hash) visited.pop();
    else visited.push(hash);
    show();
  });
  // Capture phase: the main page's dialogs close on Escape in their own
  // listeners, and by the bubble phase they would already look closed.
  document.addEventListener("keydown", onKey, true);
  show();
}
