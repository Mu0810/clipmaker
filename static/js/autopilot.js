/* The Autopilot shell (design §15, task 12.2; Requirements 6.8, 12.2, 12.3,
   N5.2), loaded by static/index.html as a module.

   - The header chip (#apChip) shows what needs the user: posts waiting for
     approval, failed posts, posts waiting for a reconnect, and whether
     publishing is paused. It reads /api/autopilot/counts on load, every
     30 s, when the tab comes back into view, and on an "ap:changed" event,
     which a screen dispatches after changing posts.
   - A change after the first reading is announced in #apLive (polite).
   - When a count rises and the user allowed browser notifications, a
     notification says what changed; clicking it opens the review queue.
   - On the Autopilot screens, a one-time prompt offers to allow
     notifications. Either answer hides it for good.
   - The first Autopilot screen opened stores the browser's time zone when
     none is set yet (prefs.js).
   - It starts the router (router.js) and wires the composer's Autopilot
     controls (composer.js). */
import { api } from "./api.js";
import "./cards.js";
import "./composer.js";
import { ensureTimezone } from "./prefs.js";
import { setupLeft, setupText } from "./readiness.js";
import * as router from "./router.js";

const POLL_MS = 30_000;
const ASKED_KEY = "cm-ap-notifications-asked";
const NOTE_TITLE = "Clip Maker Autopilot";
const NOTE_TAG = "clipmaker-autopilot";

let last = null;          // the last counts read
let reading = false;

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  node.append(...children);
  return node;
}

/** What the counts mean, in words: the chip's short parts and a sentence. */
export function describe(counts) {
  const waiting = counts.waiting_approval || 0;
  const failed = counts.failed || 0;
  const reconnect = counts.needs_reconnect || 0;
  const parts = [];
  const said = [];
  if (waiting) {
    parts.push({ n: waiting, text: "to approve" });
    said.push(plural(waiting, "post needs your approval", "posts need your approval"));
  }
  if (failed) {
    parts.push({ n: failed, text: "failed", bad: true });
    said.push(plural(failed, "post failed", "posts failed"));
  }
  if (reconnect) {
    parts.push({ n: reconnect, text: "to reconnect", bad: true });
    said.push(plural(reconnect, "post waits for a reconnect", "posts wait for a reconnect"));
  }
  let sentence = said.length ? `Autopilot: ${said.join(", ")}.` : "Autopilot: nothing needs you.";
  if (counts.paused) {
    sentence = said.length ? `${sentence} Publishing is paused.` : "Autopilot: publishing is paused.";
  }
  return { parts, sentence, attention: said.length > 0, paused: !!counts.paused };
}

function say(text) {
  const live = document.getElementById("apLive");
  if (live) live.textContent = text;
}

function renderChip(counts, previous) {
  const chip = document.getElementById("apChip");
  if (!chip) return;
  const meaning = describe(counts);
  const box = chip.querySelector(".ap-chip-counts");
  box.replaceChildren();
  for (const part of meaning.parts) {
    box.append(el("span", part.bad ? { class: "ap-bad" } : {},
                  el("span", { class: "ap-n" }, String(part.n)), ` ${part.text}`));
  }
  if (meaning.paused) box.append(el("span", { class: "ap-paused" }, "Paused"));
  chip.classList.toggle("live", meaning.attention);
  chip.setAttribute("aria-label", meaning.sentence);
  chip.setAttribute("title", meaning.sentence);
  if (previous && describe(previous).sentence !== meaning.sentence) say(meaning.sentence);
}

function notifyRise(previous, counts) {
  if (!previous || !("Notification" in window) || Notification.permission !== "granted") return;
  const rise = key => Math.max(0, (counts[key] || 0) - (previous[key] || 0));
  const failed = rise("failed");
  const waiting = rise("waiting_approval");
  const reconnect = rise("needs_reconnect");
  const lines = [];
  if (failed) lines.push(`${plural(failed, "post", "posts")} failed.`);
  if (waiting) lines.push(`${plural(waiting, "more post needs", "more posts need")} your approval.`);
  if (reconnect) lines.push(`${plural(reconnect, "post waits", "posts wait")} for a reconnect.`);
  if (!lines.length) return;
  try {
    const note = new Notification(NOTE_TITLE, { body: lines.join(" "), tag: NOTE_TAG });
    note.onclick = () => {
      window.focus();
      router.go("#/autopilot/queue");
      note.close();
    };
  } catch (err) {
    console.warn("Couldn't show a notification:", err);
  }
}

/** Read the counts now and update the chip. */
export async function refreshCounts() {
  if (reading) return;
  reading = true;
  try {
    const counts = await api.get("/api/autopilot/counts");
    const previous = last;
    last = counts;
    renderChip(counts, previous);
    notifyRise(previous, counts);
    document.dispatchEvent(new CustomEvent("ap:counts", { detail: counts }));
  } catch {
    // Keep showing the last reading; the next one tries again.
  } finally {
    reading = false;
  }
}

/* ── the one-time "Allow notifications" prompt ───────────────────────── */

function shouldAsk() {
  if (!("Notification" in window) || Notification.permission !== "default") return false;
  try { return localStorage.getItem(ASKED_KEY) !== "1"; } catch { return false; }
}

function remember() {
  try { localStorage.setItem(ASKED_KEY, "1"); } catch { /* private mode: ask again next time */ }
}

const BELL = '<svg class="icon-sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>';

function renderNotice(target) {
  const box = document.getElementById("apNotice");
  if (!box) return;
  box.hidden = true;
  box.replaceChildren();
  if (!target || target.kind !== "autopilot") return;
  if (target.section !== "setup") showSetupNotice(box);
  if (!shouldAsk()) return;
  const allow = el("button", { type: "button", class: "ap-btn" }, "Allow notifications");
  const later = el("button", { type: "button", class: "ap-btn ghost" }, "Not now");
  const text = el("span", {}, "Get a browser notification when a post fails or needs your approval.");
  const card = el("div", { class: "ap-notice-in", role: "region", "aria-label": "Notifications" });
  card.insertAdjacentHTML("afterbegin", BELL);
  card.append(text, allow, later);
  box.append(card);
  box.hidden = false;
  const close = () => {
    card.remove();
    box.hidden = box.childElementCount === 0;
  };
  allow.addEventListener("click", async () => {
    remember();
    let answer = "default";
    try { answer = await Notification.requestPermission(); } catch { answer = "default"; }
    close();
    say(answer === "granted"
      ? "Notifications are on."
      : "Notifications stay off. You can allow them later in the browser's settings for this site.");
  });
  later.addEventListener("click", () => {
    remember();
    close();
  });
}

const ALERT = '<svg class="icon-sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5l9.5 16.5h-19z"/>' +
  '<path d="M12 10v4.5M12 17.5v.01"/></svg>';

/** Until some platform can publish, each Autopilot screen but Setup says so
    first, with the way there: a new user's way in is the chip, not Setup. */
async function showSetupNotice(box) {
  let left = 0;
  try { left = await setupLeft(); } catch { return; }      // Setup shows a failed reading itself
  if (!left || !box.isConnected) return;                   // ready, or another screen opened meanwhile
  const card = el("div", { class: "ap-notice-in", role: "region", "aria-label": "Setup" },
                  el("span", {}, setupText(left)),
                  el("a", { href: "#/autopilot/setup", class: "ap-btn" }, "Open Setup"));
  card.insertAdjacentHTML("afterbegin", ALERT);
  box.prepend(card);
  box.hidden = false;
}

function markChip(target) {
  const chip = document.getElementById("apChip");
  if (!chip) return;
  if (target && target.kind === "autopilot") chip.setAttribute("aria-current", "page");
  else chip.removeAttribute("aria-current");
}

document.addEventListener("ap:route", event => {
  renderNotice(event.detail.route);
  markChip(event.detail.route);
  if (event.detail.route) ensureTimezone();
});
document.addEventListener("ap:changed", refreshCounts);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshCounts();
});

router.start();
refreshCounts();
setInterval(refreshCounts, POLL_MS);
