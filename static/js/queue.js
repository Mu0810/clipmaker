/* The review queue, #/autopilot/queue (design §7, §15, task 12.4;
   Requirements 5.5, 5.6, 6.2, 6.5-6.8, 8.7, 9.5, N1.3, N5.1, N5.2), in the
   Agenda layout chosen in task 12.1 (mockups/autopilot/queue-a.html).

   - Posts are rows under their day, in the user's time zone (5.6). A day's
     header counts each platform's posts that day against the daily maximum.
   - A row shows the slot, cover, title, source, account, score, status and
     its main action. Opening it edits the post in place: every generated
     field with a live counter driven by the server's limits, Reset to AI
     version (6.7), the slot with its gap and daily-limit warnings (5.5), the
     holds and their reasons, why the clip was picked, Approve or Don't post
     (the API's reject), and
     Edit clip. A field saves when it's left. Text that hasn't saved yet, or
     that the server refused, stays in its field with the reason beside it,
     through any redraw, and Approve waits for the save.
   - Filters (All, Needs approval, Held, Blocked, Failed) and the platform;
     the approval and failed counts match the header chip's. Ticking posts,
     a day or a whole video shows a bar that approves them, or marks them
     Don't post, together, then lists the result for each post (6.6).
   - A failed post's main action is Try again (10.10, design §15): with
     nothing on the platform yet it's planned again, otherwise it carries on
     where publishing stopped.
   - The "Pause all publishing" switch (6.8), the note that Instagram posts go
     out only while Clip Maker runs, the reason a post moved past Instagram's
     publishing limit (9.5), and why YouTube kept a video private, with the
     audit form (8.7).
   - Rows use content-visibility: auto, so 200 posts show within a second
     (N1.3). Everything works from the keyboard; Escape closes the open post
     before it leaves the screen (N5.1). */
import { api } from "./api.js";
import { giveVideo } from "./composer.js";
import { modal } from "./dialog.js";
import { addDays, dateLabel, fromInputValue, fromWallTime, plural, todayIn, wallInputValue,
         wallTime, zoneLongName } from "./format.js";
import { say } from "./live.js";
import * as router from "./router.js";
import { chars, hashtags, mentions, parseTags, tagsTotal, utf8Bytes } from "./textlimits.js";

// What the queue lists: posts not yet out, failures, and videos YouTube kept
// private in the last two weeks (8.7). Each of them may need the user.
const QUEUE_STATUSES = ["scheduled", "waiting_approval", "unscheduled", "blocked", "needs_reconnect",
                        "publishing", "retrying", "uploaded", "failed", "kept_private"];
const RECENT_S = 14 * 86400;
const POLL_MS = 60_000;
const APPROVABLE = new Set(["scheduled", "waiting_approval", "unscheduled", "blocked", "needs_reconnect"]);
const EDITABLE = new Set([...APPROVABLE, "uploaded"]);
const REJECTABLE = new Set([...EDITABLE, "failed"]);
// Posts that take a place in a day's count, as the planner counts them.
const OCCUPYING = new Set(["scheduled", "publishing", "retrying", "uploaded", "live", "private",
                           "kept_private"]);
const PLATFORM = { youtube: "YouTube", instagram: "Instagram" };
const FIELD_NAMES = { title: "title", description: "description", tags: "tags", category_id: "category",
                      caption: "caption", slot_at: "slot" };
const HOLD_SHORT = { sensitive: "may be sensitive", unsupported_claims: "unbacked claim",
                     avoid_words: "avoid-list word", heuristic_metadata: "keyword text",
                     text_shaping: "subtitle shaping" };
const BLOCKED_SHORT = { setup: "setup needed", rights: "rights", format: "video format" };
const KEPT_PRIVATE = "YouTube kept this video private. It keeps every video Clip Maker uploads private " +
  "until your Google Cloud project, the one set up for Autopilot, passes YouTube's API audit.";
const INSTAGRAM_NOTE = "Instagram posts go out only while Clip Maker is running.";
const INSTAGRAM_LATE = `${INSTAGRAM_NOTE} If it's closed at the post's time, the post goes out when Clip ` +
  "Maker starts again within 2 hours; later than that, it moves to the next free time.";

const needsApproval = post => post.approval === "pending" && APPROVABLE.has(post.status);
const held = post => needsApproval(post) && (post.holds || []).length > 0;
// 10.10: a failed post can be tried again (the queue lists only Autopilot's own posts).
const retryable = post => post.status === "failed";

/** The filters. "Needs approval" and "Failed" count what the header chip counts. */
export const FILTERS = [
  { id: "all", label: "All", test: () => true },
  { id: "approval", label: "Needs approval", test: needsApproval },
  { id: "held", label: "Held", test: held },
  { id: "blocked", label: "Blocked", test: p => p.status === "blocked" || p.status === "needs_reconnect" },
  { id: "failed", label: "Failed", test: p => p.status === "failed" },
];

const SVG = {
  youtube: '<svg viewBox="0 0 24 24"><rect x="1.5" y="5" width="21" height="14" rx="4" fill="#FF0033"/><path d="M10 9.2v5.6l4.8-2.8z" fill="#fff"/></svg>',
  instagram: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="5.5" fill="none" stroke="#E1306C" stroke-width="2"/><circle cx="12" cy="12" r="4.2" fill="none" stroke="#E1306C" stroke-width="2"/><circle cx="17.4" cy="6.6" r="1.2" fill="#E1306C"/></svg>',
  down: '<svg class="icon-sm" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>',
  up: '<svg class="icon-sm" viewBox="0 0 24 24"><path d="M6 15l6-6 6 6"/></svg>',
  alert: '<svg class="icon-sm" viewBox="0 0 24 24"><path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.5v.01"/></svg>',
  hold: '<svg class="icon-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M10 9v6M14 9v6"/></svg>',
  info: '<svg class="icon-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.01"/></svg>',
  lock: '<svg class="icon-sm" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>',
  cut: '<svg class="icon-sm" viewBox="0 0 24 24"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M8.6 7.6L20 18M8.6 16.4L20 6"/></svg>',
  reset: '<svg class="icon-sm" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>',
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === true) node.setAttribute(name, "");
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(name, value);
  }
  node.append(...children.filter(c => c !== null && c !== undefined && c !== false && c !== ""));
  return node;
}

function icon(name, extra = "") {
  const span = el("span", { class: `ap-ico${extra ? ` ${extra}` : ""}`, "aria-hidden": "true" });
  span.innerHTML = SVG[name];
  return span;
}

const sr = text => el("span", { class: "ap-sr" }, text);

function callout(iconName, tone, ...content) {
  return el("div", { class: `ap-q-callout ${tone}` }, icon(iconName), el("span", {}, ...content));
}

/** "YouTube post for clip 3". */
export function labelOf(post) {
  const name = PLATFORM[post.platform] || "Autopilot";
  return post.clip_idx !== null && post.clip_idx !== undefined
    ? `${name} post for clip ${post.clip_idx}` : `${name} post`;
}

/** "Mon 19 Oct 13:00 IST", or "no slot". */
function whenOf(post) {
  return post.slot_day ? `${dateLabel(post.slot_day)} ${post.slot_time} ${post.slot_zone}` : "no slot";
}

function titleOf(post) {
  const meta = post.meta || {};
  const text = post.platform === "youtube" ? meta.title : (meta.caption || "").split("\n")[0];
  return (text || "").trim() || "Untitled";
}

/** The status pill: {tone, text}. */
export function statusOf(post, zone) {
  const holds = post.holds || [];
  switch (post.status) {
    case "scheduled":
      if (post.approval === "pending") {
        return holds.length ? { tone: "warn", text: `Held · ${HOLD_SHORT[holds[0].code] || "check it"}` }
                            : { tone: "terra", text: "Waiting for approval" };
      }
      return { tone: "ok", text: post.approval === "auto" ? "Posts automatically" : "Approved" };
    case "waiting_approval":
      return { tone: "terra", text: "Slot passed · approve to replan" };
    case "unscheduled":
      return { tone: "", text: "No slot" };
    case "blocked":
      return { tone: "err", text: `Blocked · ${BLOCKED_SHORT[post.last_error_class] || "see why"}` };
    case "needs_reconnect":
      return { tone: "err", text: "Waiting for a reconnect" };
    case "publishing":
      return { tone: "terra", text: "Publishing…" };
    case "retrying":
      return { tone: "warn", text: "Retrying" };
    case "uploaded": {
      if (post.paused_hold) return { tone: "warn", text: "Uploaded · held by the pause" };
      if (!post.scheduled_publish_at) return { tone: "terra", text: "Uploaded" };
      const w = wallTime(post.scheduled_publish_at, zone);
      const at = `${String(w.hour).padStart(2, "0")}:${String(w.minute).padStart(2, "0")}`;
      return { tone: "terra", text: `Uploaded · public at ${at}` };
    }
    case "failed":
      return { tone: "err", text: "Failed" };
    case "kept_private":
      return { tone: "warn", text: "Kept private by YouTube" };
    default:
      return { tone: "", text: post.status };
  }
}

function scoreClass(score) {
  if (score === null || score === undefined) return "";
  return score >= 75 ? "hi" : score >= 60 ? "mid" : "lo";
}

function fileUrl(post, name) {
  return name && post.job_id
    ? `/api/clip/${encodeURIComponent(post.job_id)}/${encodeURIComponent(name)}` : null;
}
const coverOf = post => fileUrl(post, (post.files || {}).cover || (post.files || {}).thumb);
const videoOf = post => fileUrl(post, (post.files || {}).vertical);

function gapText(seconds) {
  return seconds < 3600 ? `${Math.max(1, Math.round(seconds / 60))} min gap`
                        : `${Math.round(seconds / 360) / 10} h gap`;
}

export async function render(container) {
  const state = { posts: [], published: [], limits: null, paused: false, zone: "UTC", filter: "all",
                  platform: "all", selected: new Set(), open: null, busy: false };
  const saving = new Map();     // post id -> promise of its last save: true when it worked
  const drafts = new Map();     // post id -> {field: text typed but not saved yet}
  const refused = new Map();    // post id -> [{field, message}] the server refused
  const controls = new Map();   // post id -> field -> {control, error, check}, for the open post
  let stopped = false;
  let timer = 0;

  /* ── the frame ──────────────────────────────────────────────────────── */
  const heading = el("h1", { class: "ap-h1" }, "Review ", el("em", {}, "queue"));
  const lead = el("p", { class: "ap-lead" });
  const platforms = el("div", { class: "ap-seg", role: "group", "aria-label": "Platform" });
  for (const [id, label] of [["all", "Both"], ["youtube", "YouTube"], ["instagram", "Instagram"]]) {
    const button = el("button", { type: "button", "data-platform": id, "aria-pressed": String(id === "all") },
                      label);
    button.addEventListener("click", () => {
      state.platform = id;
      renderAll("keep");
    });
    platforms.append(button);
  }
  const bySource = el("select", { class: "ap-q-bysource", "aria-label": "Select all posts from a video" });
  bySource.addEventListener("change", () => {
    const job = bySource.value;
    bySource.value = "";
    if (!job) return;
    const picked = visible().filter(p => p.job_id === job);
    for (const post of picked) state.selected.add(post.id);
    renderAll("keep");
    const title = (picked[0] && picked[0].source_title) || "the video";
    say(`Selected ${plural(picked.length, "post")} from ${title}.`);
  });
  const pause = el("button", { type: "button", class: "ap-pause", role: "switch", "aria-checked": "false" },
                   el("span", { class: "ap-pause-sw", "aria-hidden": "true" }), "Pause all publishing");
  pause.addEventListener("click", () => setPaused(!state.paused));
  const pausedNote = callout("hold", "warn ap-q-paused", el("b", {}, "Publishing is paused. "),
                             "Nothing is published until you resume, and YouTube videos already " +
                             "uploaded stay private.");
  pausedNote.hidden = true;
  const filters = el("div", { class: "ap-q-filters", role: "group", "aria-label": "Show" });
  const igNote = el("span", { class: "ap-q-note" }, icon("info"), INSTAGRAM_NOTE);
  const results = el("div", { class: "ap-q-results", hidden: true });
  const alertLine = el("p", { class: "ap-problem", role: "alert", hidden: true });
  const days = el("div", { class: "ap-q-days" });
  const empty = el("div", { class: "ap-empty", hidden: true });
  const bulkCount = el("b", {});
  const bulkFrom = el("span", { class: "ap-q-bulk-from" });
  const bulkClear = el("button", { type: "button", class: "ap-btn ghost sm ap-on-ink" }, "Clear");
  const bulkReject = el("button", { type: "button", class: "ap-btn ghost sm ap-on-ink" }, "Don't post");
  const bulkApprove = el("button", { type: "button", class: "ap-btn sm" }, "Approve");
  const bulkBar = el("div", { class: "ap-q-bulk", role: "region", "aria-label": "Selected posts", hidden: true },
                     bulkCount, bulkFrom, bulkClear, bulkReject, bulkApprove);
  bulkClear.addEventListener("click", () => {
    state.selected.clear();
    renderAll();
    heading.focus({ preventScroll: true });
    say("Nothing is selected.");
  });
  bulkReject.addEventListener("click", () => runBulk("reject"));
  bulkApprove.addEventListener("click", () => runBulk("approve"));
  container.append(el("div", { class: "ap-q-head" },
                      el("div", {}, heading, lead),
                      el("div", { class: "ap-q-tools" }, platforms, bySource, pause)),
                   pausedNote, filters, results, alertLine, days, empty, bulkBar);

  function showAlert(text) {
    alertLine.textContent = text || "";
    alertLine.hidden = !text;
  }

  /* ── data ───────────────────────────────────────────────────────────── */
  const parseDay = iso => {
    const [year, month, day] = iso.split("-").map(Number);
    return { year, month, day };
  };

  async function load() {
    let data;
    try {
      data = await api.get("/api/autopilot/posts", { status: QUEUE_STATUSES.join(",") });
    } catch (err) {
      if (!stopped) showAlert(`Couldn't read the queue: ${err.message}`);
      return false;
    }
    if (stopped) return false;
    state.limits = data.limits;
    state.paused = !!data.paused;
    state.zone = data.timezone || "UTC";
    const now = Date.now() / 1000;
    state.posts = (data.posts || []).filter(p => p.method === "official" &&
      (p.status !== "kept_private" || !p.slot_at || p.slot_at > now - RECENT_S));
    // Posts already out today still count toward today's daily maximum.
    try {
      const start = fromWallTime({ ...parseDay(todayIn(state.zone)), hour: 0, minute: 0 }, state.zone);
      const out = await api.get("/api/autopilot/posts", { status: "live,private", from: start });
      state.published = (out.posts || []).filter(p => p.method === "official");
    } catch {
      state.published = [];
    }
    showAlert("");
    keepVisibleSelection();
    return true;
  }

  const byId = id => state.posts.find(p => p.id === id);

  function forgetPost(id) {
    state.selected.delete(id);
    drafts.delete(id);
    refused.delete(id);
    saving.delete(id);
    if (state.open === id) state.open = null;
  }

  function setPost(post) {
    const i = state.posts.findIndex(p => p.id === post.id);
    if (QUEUE_STATUSES.includes(post.status)) {
      if (i >= 0) state.posts[i] = post;
      else state.posts.push(post);
    } else {
      if (i >= 0) state.posts.splice(i, 1);
      forgetPost(post.id);
    }
  }

  const ofPlatform = p => state.platform === "all" || p.platform === state.platform;

  function visible() {
    const filter = FILTERS.find(f => f.id === state.filter) || FILTERS[0];
    return state.posts.filter(p => ofPlatform(p) && filter.test(p));
  }

  function keepVisibleSelection() {
    const shown = new Set(visible().map(p => p.id));
    for (const id of state.selected) if (!shown.has(id)) state.selected.delete(id);
  }

  /* ── drafts and refused fields ──────────────────────────────────────── */
  function draftOf(pid, name, fallback) {
    const d = drafts.get(pid);
    return d && Object.prototype.hasOwnProperty.call(d, name) ? d[name] : fallback;
  }

  function setDraft(pid, name, text) {
    drafts.set(pid, { ...(drafts.get(pid) || {}), [name]: text });
  }

  function dropDraft(pid, name, sent) {
    // Only what was sent: text typed while the save ran stays a draft.
    const d = drafts.get(pid);
    if (!d || !Object.prototype.hasOwnProperty.call(d, name)) return;
    if (sent === undefined || d[name] === sent) delete d[name];
    if (!Object.keys(d).length) drafts.delete(pid);
  }

  const fieldOf = problem => (problem.field || "").replace(/^meta\./, "");
  const refusedFor = (pid, name) => (refused.get(pid) || []).filter(p => p.field === name);

  /** Show the refused fields' reasons beside them; returns those with no field on screen. */
  function placeRefused(pid) {
    const fields = controls.get(pid);
    const list = refused.get(pid) || [];
    if (fields) {
      for (const [name, f] of fields) {
        const mine = list.filter(p => p.field === name);
        f.error.textContent = mine.map(p => p.message).join(" ");
        f.error.hidden = mine.length === 0;
        f.check();
      }
    }
    return list.filter(p => !fields || !fields.has(p.field));
  }

  /** Record what the server refused. The problems shown beside their own
      fields are returned; the others go to the alert line. */
  function setRefused(pid, problems, names) {
    const kept = (refused.get(pid) || []).filter(p => !names.includes(p.field));
    const fresh = (problems || []).map(p => ({ field: fieldOf(p), message: p.message }));
    refused.set(pid, [...kept, ...fresh]);
    const elsewhere = placeRefused(pid);
    if (elsewhere.length) showAlert(elsewhere.map(p => p.message).join(" "));
    return fresh.filter(p => !elsewhere.includes(p));
  }

  /** A refusal shows beside its field, which focus has usually left by the
      time the save comes back, so it's said as well (N5.2). */
  function sayRefused(pid, placed) {
    if (placed.length) say(`Didn't save the ${labelOf(byId(pid) || {})}: ${placed.map(p => p.message).join(" ")}`);
  }

  function clearRefused(pid, names) {
    const kept = (refused.get(pid) || []).filter(p => !names.includes(p.field));
    if (kept.length) refused.set(pid, kept);
    else refused.delete(pid);
    placeRefused(pid);
  }

  /* ── rendering ──────────────────────────────────────────────────────── */
  function renderHead(shown) {
    const all = state.posts.filter(ofPlatform);
    const videos = new Set(all.map(p => p.job_id).filter(Boolean)).size;
    const zone = `times in ${zoneLongName(state.zone)}`;
    lead.textContent = all.length
      ? `${plural(all.length, "post")} from ${plural(videos, "video")} · ${zone}`
      : `Nothing to review · ${zone}`;
    for (const button of platforms.querySelectorAll("button")) {
      button.setAttribute("aria-pressed", String(button.dataset.platform === state.platform));
    }
    pause.setAttribute("aria-checked", String(state.paused));
    pause.classList.toggle("on", state.paused);
    pausedNote.hidden = !state.paused;

    filters.replaceChildren();
    for (const filter of FILTERS) {
      const button = el("button", { type: "button", class: "ap-q-f", "data-focus": `filter-${filter.id}`,
                                    "aria-pressed": String(filter.id === state.filter) },
                        filter.label, " ", el("span", { class: "ap-q-c" }, String(all.filter(filter.test).length)));
      button.addEventListener("click", () => {
        state.filter = filter.id;
        keepVisibleSelection();
        renderAll({ key: `filter-${filter.id}` });
      });
      filters.append(button);
    }
    filters.append(el("span", { class: "ap-spacer" }), igNote);
    igNote.hidden = !all.some(p => p.platform === "instagram");

    const videosShown = new Map();
    for (const post of shown) {
      if (!post.job_id) continue;
      const entry = videosShown.get(post.job_id) || { title: post.source_title || "Untitled video", n: 0 };
      entry.n += 1;
      videosShown.set(post.job_id, entry);
    }
    bySource.replaceChildren(el("option", { value: "" }, "Select all from a video…"),
                             ...[...videosShown].map(([job, v]) =>
                               el("option", { value: job }, `${v.title} (${plural(v.n, "post")})`)));
    bySource.disabled = videosShown.size === 0;
  }

  function dayQuota(day) {
    const box = el("div", { class: "ap-q-quota" });
    for (const platform of ["youtube", "instagram"]) {
      if (state.platform !== "all" && state.platform !== platform) continue;
      const onDay = [...state.posts, ...state.published].filter(p => p.platform === platform &&
        p.slot_day === day && (OCCUPYING.has(p.status) || p.user_locked));
      if (!onDay.length) continue;
      const limit = Math.min(...onDay.map(p => (p.posting && p.posting.daily_max) || 2));
      const over = onDay.length > limit;
      box.append(el("span", { class: `ap-pill${over ? " warn" : ""}` },
                    `${PLATFORM[platform]} ${onDay.length} of ${limit} a day`,
                    over ? sr(", more than your daily maximum") : null));
    }
    return box;
  }

  /** The short warning under a moved slot's time: "2 h gap" or "over daily max". */
  function slotFlag(post) {
    const warnings = post.warnings || [];
    if (!warnings.length) return null;
    if (!warnings.some(w => w.startsWith("Less than"))) return "over daily max";
    const gaps = [...state.posts, ...state.published]
      .filter(o => o.id !== post.id && o.platform === post.platform && o.slot_at &&
                   (OCCUPYING.has(o.status) || o.user_locked))
      .map(o => Math.abs(o.slot_at - post.slot_at));
    return gaps.length ? gapText(Math.min(...gaps)) : "short gap";
  }

  function editClipButton(post, small = true) {
    const button = el("button", { type: "button", class: `ap-btn ghost${small ? " sm" : ""}` },
                      icon("cut"), "Edit clip", sr(` of the ${labelOf(post)}`));
    button.addEventListener("click", () => router.go(`#/editor/${encodeURIComponent(post.clip_id)}`));
    return button;
  }

  function primaryButton(post) {
    const context = sr(` the ${labelOf(post)}, ${whenOf(post)}`);
    if (post.status === "blocked" || post.status === "needs_reconnect") {
      if (post.status === "blocked" && post.last_error_class === "format" && post.clip_id) {
        return editClipButton(post);
      }
      if (post.status === "needs_reconnect" || post.last_error_class === "setup") {
        const button = el("button", { type: "button", class: "ap-btn ghost sm" }, "Open Setup");
        button.addEventListener("click", () => router.go("#/autopilot/setup"));
        return button;
      }
    } else if (needsApproval(post)) {
      const button = el("button", { type: "button", class: "ap-btn sm", "data-focus": `approve-${post.id}` },
                        held(post) ? "Approve anyway" : "Approve", context);
      button.addEventListener("click", () => approve(post.id));
      return button;
    } else if (retryable(post)) {
      const button = el("button", { type: "button", class: "ap-btn sm", "data-focus": `retry-${post.id}` },
                        "Try again", context);
      button.addEventListener("click", () => retry(post.id));
      return button;
    }
    if (!REJECTABLE.has(post.status)) return null;
    const button = el("button", { type: "button", class: "ap-btn ghost sm", "data-focus": `reject-${post.id}` },
                      "Don't post", context);
    button.addEventListener("click", () => reject(post.id));
    return button;
  }

  function rowNode(post) {
    const open = state.open === post.id;
    const selected = state.selected.has(post.id);
    const box = el("input", { type: "checkbox", "aria-label": `Select the ${labelOf(post)}, ${whenOf(post)}`,
                              "data-focus": `select-${post.id}` });
    box.checked = selected;
    box.addEventListener("change", () => {
      if (box.checked) state.selected.add(post.id);
      else state.selected.delete(post.id);
      renderAll({ key: `select-${post.id}` });
    });
    const time = el("div", { class: "ap-q-time" });
    if (post.slot_time) {
      const flag = slotFlag(post);
      time.append(post.slot_time, flag ? el("small", { class: "ap-q-flag" }, icon("alert"), flag)
                                       : el("small", {}, post.slot_zone || ""));
    } else {
      time.append(el("span", { class: "ap-q-noslot", "aria-hidden": "true" }, "—"), sr("No slot"));
    }
    const cover = el("div", { class: "ap-q-cover" });
    const coverUrl = coverOf(post);
    if (coverUrl) {
      const img = el("img", { src: coverUrl, alt: "", loading: "lazy", decoding: "async", width: "36",
                              height: "64" });
      img.addEventListener("error", () => img.remove(), { once: true });
      cover.append(img);
    }
    const from = [post.source_title,
                  post.clip_idx !== null && post.clip_idx !== undefined ? `clip ${post.clip_idx}` : null]
      .filter(Boolean).join(" · ");
    const what = el("div", { class: "ap-q-what" }, el("b", {}, titleOf(post)),
                    el("span", {}, from || PLATFORM[post.platform]));
    what.addEventListener("click", () => toggle(post.id));
    const name = post.account_name
      ? (post.platform === "instagram" ? `@${post.account_name}` : post.account_name)
      : PLATFORM[post.platform];
    const account = el("span", { class: "ap-q-plat" }, icon(post.platform, "ap-q-logo"),
                       sr(`${PLATFORM[post.platform]}: `), name);
    const known = post.score !== null && post.score !== undefined;
    const score = el("span", { class: `ap-q-score ${scoreClass(post.score)}`,
                               title: known ? `Score ${post.score} out of 100${post.score_reason ? `: ${post.score_reason}` : ""}`
                                 : null },
                     known ? sr("score ") : null, known ? String(post.score) : "–",
                     sr(known ? " out of 100" : " no score"));
    const status = statusOf(post, state.zone);
    const pill = el("span", { class: `ap-pill${status.tone ? ` ${status.tone}` : ""}` }, status.text);
    const toggleButton = el("button", { type: "button", class: "ap-q-toggle", "aria-expanded": String(open),
                                        "aria-controls": `ap-q-edit-${post.id}`,
                                        "aria-label": `${open ? "Close" : "Open"} the ${labelOf(post)}, ${whenOf(post)}`,
                                        "data-focus": `toggle-${post.id}` });
    toggleButton.innerHTML = open ? SVG.up : SVG.down;
    toggleButton.firstElementChild.setAttribute("aria-hidden", "true");     // the label says it
    toggleButton.addEventListener("click", () => toggle(post.id));
    return el("div", { class: `ap-q-row${selected ? " sel" : ""}${open ? " open" : ""}`, "data-id": post.id },
              box, time, cover, what, account, score, pill,
              el("div", { class: "ap-q-acts" }, primaryButton(post), toggleButton));
  }

  function renderAll(focus = null) {
    const keepKey = focus === "keep" && document.activeElement
      ? document.activeElement.getAttribute("data-focus") : null;
    controls.clear();
    const shown = visible();
    if (state.open && !shown.some(p => p.id === state.open)) state.open = null;
    renderHead(shown);
    const groups = new Map();
    for (const post of shown) {
      const key = post.slot_day || "none";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(post);
    }
    const keys = [...groups.keys()].sort((a, b) => (a === "none") - (b === "none") || a.localeCompare(b));
    const today = todayIn(state.zone);
    const tomorrow = addDays(today, 1);
    const fragment = document.createDocumentFragment();
    for (const key of keys) {
      const posts = groups.get(key);
      const id = `ap-q-day-${key}`;
      const h2 = el("h2", { id }, key === "none" ? "No slot yet"
        : key === today ? "Today" : key === tomorrow ? "Tomorrow" : dateLabel(key));
      if (key === today || key === tomorrow) h2.append(el("span", {}, ` · ${dateLabel(key)}`));
      const which = key === "none" ? "with no slot" : `on ${dateLabel(key, { long: true })}`;
      const dayBox = el("input", { type: "checkbox", "aria-label": `Select all posts ${which}`,
                                   "data-focus": `day-${key}` });
      const picked = posts.filter(p => state.selected.has(p.id)).length;
      dayBox.checked = picked === posts.length;
      dayBox.indeterminate = picked > 0 && picked < posts.length;
      dayBox.addEventListener("change", () => {
        for (const post of posts) {
          if (dayBox.checked) state.selected.add(post.id);
          else state.selected.delete(post.id);
        }
        renderAll({ key: `day-${key}` });
      });
      const list = el("div", { class: "ap-q-list" });
      for (const post of posts) {
        list.append(rowNode(post));
        if (state.open === post.id) list.append(panelNode(post));
      }
      fragment.append(el("section", { class: "ap-q-day", "aria-labelledby": id },
                         el("div", { class: "ap-q-dayhead" }, dayBox, h2, key === "none" ? null : dayQuota(key)),
                         list));
    }
    days.replaceChildren(fragment);
    for (const pid of controls.keys()) placeRefused(pid);      // reasons survive a redraw
    renderEmpty(shown);
    renderBulk();
    restoreFocus(focus, keepKey);
  }

  function restoreFocus(focus, keepKey) {
    const find = key => (key ? container.querySelector(`[data-focus="${CSS.escape(key)}"]`) : null);
    let target = focus === "keep" ? find(keepKey) : focus ? find(focus.key) : null;
    if (!target && focus && focus.fallback) {
      // The post left the list: focus what took its place, or the heading.
      for (const id of focus.fallback) {
        target = find(`toggle-${id}`);
        if (target) break;
      }
      target = target || heading;
    }
    if (target) target.focus({ preventScroll: true });
  }

  function renderEmpty(shown) {
    empty.hidden = shown.length > 0;
    if (shown.length) return;
    if (state.posts.length) {
      empty.replaceChildren(el("p", { class: "ap-empty-text" }, "No posts match these filters."));
      return;
    }
    const start = el("button", { type: "button", class: "ap-btn" }, "Give Autopilot a video");
    start.addEventListener("click", giveVideo);
    empty.replaceChildren(
      el("p", { class: "ap-empty-text" }, "Nothing to review yet. Posts appear here when Autopilot finishes a " +
         "video you gave it; follow each video's progress under ",
         el("a", { href: "#/autopilot/sources", class: "ap-textlink" }, "Videos"), "."),
      el("p", { class: "ap-empty-act" }, start));
  }

  function renderBulk() {
    const chosen = [...state.selected].map(byId).filter(Boolean);
    bulkBar.hidden = chosen.length === 0;
    if (!chosen.length) return;
    bulkCount.textContent = `${plural(chosen.length, "post")} selected`;
    const days = new Set(chosen.map(p => p.slot_day || "none")).size;
    bulkFrom.textContent = days > 1 ? `on ${days} days` : "";
    bulkFrom.hidden = days < 2;
    bulkApprove.textContent = `Approve ${chosen.length}`;
    bulkApprove.disabled = !chosen.some(needsApproval);
    bulkReject.disabled = !chosen.some(p => REJECTABLE.has(p.status));
  }

  /* ── the open post ──────────────────────────────────────────────────── */
  function toggle(id) {
    state.open = state.open === id ? null : id;
    renderAll({ key: `toggle-${id}` });
    if (state.open) {
      const panel = container.querySelector(`#ap-q-edit-${CSS.escape(id)}`);
      if (panel) panel.scrollIntoView({ block: "nearest" });
    }
  }

  function counter(kind, value) {
    const yt = (state.limits || {}).youtube || {};
    const ig = (state.limits || {}).instagram || {};
    switch (kind) {
      case "title": return { used: chars(value), max: yt.title_chars, unit: "" };
      case "description": return { used: utf8Bytes(value), max: yt.description_bytes, unit: " bytes" };
      case "tags": return { used: tagsTotal(parseTags(value)), max: yt.tags_chars, unit: "" };
      case "caption": return { used: chars(value), max: ig.caption_chars, unit: "" };
      default: return null;
    }
  }

  /** Fill a live counter; returns whether the text is past a limit. */
  function fillCounter(node, kind, value) {
    const c = counter(kind, value);
    const number = n => (typeof n === "number" ? n.toLocaleString() : "?");
    let over = typeof c.max === "number" && c.used > c.max;
    let text = `${number(c.used)} / ${number(c.max)}${c.unit}`;
    if (kind === "caption") {
      const ig = (state.limits || {}).instagram || {};
      const tags = hashtags(value);
      const people = mentions(value);
      text += ` · ${plural(tags, "hashtag")} of ${ig.hashtags} · ${plural(people, "mention")} of ${ig.mentions}`;
      over = over || tags > ig.hashtags || people > ig.mentions;
    }
    node.textContent = text;
    node.classList.toggle("over", over);
    node.classList.toggle("near", !over && typeof c.max === "number" && c.used > c.max * 0.9);
    return over;
  }

  function panelNode(post) {
    const pid = post.id;
    const editable = EDITABLE.has(post.status);
    const idOf = name => `ap-q-${pid}-${name}`;
    const fields = new Map();
    controls.set(pid, fields);
    // Reset shows for a field the user changed, and for one whose change the
    // server refused, since putting the AI's text back is one way out.
    const showReset = name => ((byId(pid) || post).edited_fields || []).includes(name) ||
                              refusedFor(pid, name).length > 0;

    function field(name, label, control, kind) {
      control.id = idOf(name);
      const tools = el("span", { class: "ap-q-field-tools" });
      const reset = el("button", { type: "button", class: "ap-linkbtn ap-q-reset", "data-reset": name,
                                   hidden: !showReset(name) },
                       icon("reset"), "Reset to AI version", sr(` of the ${FIELD_NAMES[name]}`));
      reset.addEventListener("click", () => resetField(pid, name));
      tools.append(reset);
      const count = kind ? el("span", { class: "ap-q-count", id: idOf(`${name}-count`) }) : null;
      if (count) tools.append(count);
      const error = el("p", { class: "ap-q-error", id: idOf(`${name}-error`), hidden: true });
      control.setAttribute("aria-describedby", [count && count.id, error.id].filter(Boolean).join(" "));
      const check = () => {
        const over = count ? fillCounter(count, kind, control.value) : false;
        control.setAttribute("aria-invalid", String(over || refusedFor(pid, name).length > 0));
        reset.hidden = !showReset(name);
      };
      fields.set(name, { control, error, check });
      control.addEventListener("input", () => {
        setDraft(pid, name, control.value);
        check();
      });
      check();
      const wide = name !== "tags" && name !== "category_id";
      return el("div", { class: `ap-q-field${wide ? " wide" : ""}` },
                el("div", { class: "top" }, el("label", { for: control.id }, label), tools), control, error);
    }

    const left = el("div", { class: "ap-q-left" });
    const video = videoOf(post);
    left.append(el("div", { class: "ap-q-preview" }, video
      ? el("video", { src: video, poster: coverOf(post), controls: true, preload: "none", playsinline: true,
                      "aria-label": `Preview of the ${labelOf(post)}` })
      : el("div", { class: "ap-q-nopreview" }, "No preview yet")));
    if (post.clip_id) left.append(editClipButton(post));

    const form = el("div", { class: "ap-q-fields" });
    const meta = post.meta || {};
    if (post.platform === "youtube") {
      const title = el("input", { type: "text", autocomplete: "off" });
      title.value = draftOf(pid, "title", meta.title || "");
      const description = el("textarea", { rows: "4" });
      description.value = draftOf(pid, "description", meta.description || "");
      const tags = el("input", { type: "text", autocomplete: "off", placeholder: "Separate tags with commas" });
      tags.value = draftOf(pid, "tags", (meta.tags || []).join(", "));
      const category = el("select", {});
      const names = ((state.limits || {}).youtube || {}).categories || {};
      for (const [id, name] of Object.entries(names).sort((a, b) => a[1].localeCompare(b[1]))) {
        category.append(el("option", { value: id }, name));
      }
      category.value = draftOf(pid, "category_id", String(meta.category_id || "22"));
      title.addEventListener("change", () => saveText(pid, "title", title.value, title.value));
      description.addEventListener("change", () =>
        saveText(pid, "description", description.value, description.value));
      tags.addEventListener("change", () => saveText(pid, "tags", tags.value, parseTags(tags.value)));
      category.addEventListener("change", () => saveText(pid, "category_id", category.value, category.value));
      form.append(field("title", "Title", title, "title"),
                  field("description", "Description", description, "description"),
                  field("tags", "Tags", tags, "tags"),
                  field("category_id", "Category", category, null));
      const visibility = el("div", { class: "ap-seg", role: "group", "aria-labelledby": idOf("vis") });
      for (const [value, label] of [["public", "Public"], ["private", "Private"]]) {
        const button = el("button", { type: "button", "data-vis": value,
                                      "aria-pressed": String(post.target_visibility === value) }, label);
        button.addEventListener("click", () => {
          const current = byId(pid);
          if (current && current.target_visibility !== value) {
            save(pid, { target_visibility: value }, "target_visibility");
          }
        });
        visibility.append(button);
      }
      form.append(el("div", { class: "ap-q-field" },
                     el("div", { class: "top" }, el("span", { class: "ap-q-label", id: idOf("vis") }, "Visibility")),
                     visibility));
    } else {
      const caption = el("textarea", { rows: "5" });
      caption.value = draftOf(pid, "caption", meta.caption || "");
      caption.addEventListener("change", () => saveText(pid, "caption", caption.value, caption.value));
      form.append(field("caption", "Caption", caption, "caption"));
    }
    const ai = el("input", { type: "checkbox", id: idOf("ai") });
    ai.checked = !!post.disclosure_ai;
    ai.addEventListener("change", () => save(pid, { disclosure_ai: ai.checked }, "disclosure_ai"));
    const aiHint = el("span", { class: "ap-q-hint", id: `${ai.id}-hint` },
                      "Tick it when the clip shows realistic people, places or events made or changed with AI.");
    ai.setAttribute("aria-describedby", aiHint.id);
    form.append(el("div", { class: "ap-q-ai" },
                   el("label", { class: "ap-q-check", for: ai.id }, ai, el("span", {}, "Label as AI-altered content")),
                   aiHint));
    if (!editable) {
      for (const control of form.querySelectorAll("input, textarea, select, button")) control.disabled = true;
    }

    const side = el("div", { class: "ap-q-side" }, slotField(post, editable, fields));
    if (post.status === "uploaded") {
      side.append(callout("info", "info", "It's already uploaded to YouTube for this time. To post it at " +
                                          "another time, choose Don't post, then add the clip again."));
    }
    for (const warning of post.warnings || []) side.append(callout("alert", "warn", warning));
    if (post.approval === "pending") {
      for (const hold of post.holds || []) {
        side.append(callout("hold", "warn", el("b", {}, "Held. "), hold.message,
                            " It won't publish until you approve it."));
      }
      // Approving anyway means overruling the check: watch the clip first.
      if ((post.holds || []).length && post.clip_id) {
        side.append(el("p", { class: "ap-q-watch" },
                       el("a", { href: `#/editor/${encodeURIComponent(post.clip_id)}`, class: "ap-textlink" },
                          "Watch the clip in the editor"), " before you approve it."));
      }
    }
    if (post.status === "kept_private") {
      side.append(callout("alert", "warn", KEPT_PRIVATE, post.audit_form ? " Apply through " : null,
                          post.audit_form ? el("a", { href: post.audit_form, target: "_blank",
                                                      rel: "noopener noreferrer", class: "ap-textlink" },
                                               "YouTube's API audit form") : null,
                          post.audit_form ? "; once it passes, you can make this video public in YouTube Studio."
                            : null));
    } else if (post.last_error) {
      const tone = ["failed", "blocked", "needs_reconnect"].includes(post.status) ? "err"
        : post.status === "retrying" ? "warn" : "info";
      side.append(callout(tone === "info" ? "info" : "alert", tone, post.last_error));
    }
    if (post.platform === "instagram") side.append(callout("info", "info", INSTAGRAM_LATE));
    if (post.url) {
      side.append(el("p", { class: "ap-q-reason" },
                     el("a", { href: post.url, target: "_blank", rel: "noopener noreferrer", class: "ap-textlink" },
                        `Open on ${PLATFORM[post.platform]}`)));
    }
    if (post.score !== null && post.score !== undefined) {
      side.append(el("p", { class: "ap-q-reason" },
                     el("span", { class: `ap-q-score ${scoreClass(post.score)}` }, String(post.score)), " ",
                     el("b", {}, "Why this clip:"), ` ${post.score_reason || "no reason was given."}`));
    }
    const acts = el("div", { class: "ap-q-panel-acts" });
    if (REJECTABLE.has(post.status)) {
      const button = el("button", { type: "button", class: "ap-btn ghost ap-danger" }, "Don't post",
                        sr(` the ${labelOf(post)}`));
      button.addEventListener("click", () => reject(pid));
      acts.append(button);
    }
    if (needsApproval(post)) {
      const button = el("button", { type: "button", class: "ap-btn" }, held(post) ? "Approve anyway" : "Approve",
                        sr(` the ${labelOf(post)}`));
      button.addEventListener("click", () => approve(pid));
      acts.append(button);
    }
    if (retryable(post)) {
      const button = el("button", { type: "button", class: "ap-btn" }, "Try again", sr(` the ${labelOf(post)}`));
      button.addEventListener("click", () => retry(pid));
      acts.append(button);
    }
    if (acts.childElementCount) side.append(acts);

    return el("div", { class: "ap-q-edit", id: `ap-q-edit-${pid}`, role: "region",
                       "aria-label": `Edit the ${labelOf(post)}`, "data-ap-escape": "" }, left, form, side);
  }

  function slotField(post, editable, fields) {
    const pid = post.id;
    const input = el("input", { type: "datetime-local", id: `ap-q-${pid}-slot`, "data-focus": `slot-${pid}` });
    if (post.slot_at) input.value = wallInputValue(post.slot_at, state.zone);
    const movable = editable && post.status !== "uploaded";
    input.disabled = !movable;
    const move = el("button", { type: "button", class: "ap-btn ghost sm", disabled: true }, "Move",
                    sr(" the post to this time"));
    const top = el("div", { class: "top" }, el("label", { for: input.id }, "Posting time"));
    if (post.slot_at && post.user_locked) {
      top.append(el("span", { class: "ap-q-count" }, icon("lock"), "You chose this time"));
    } else if (post.slot_at && post.slot_basis) {
      top.append(el("span", { class: "ap-q-count" }, post.slot_basis === "defaults"
        ? "Picked from your preset's posting hours" : `Planned from ${post.slot_basis}`));
    }
    const error = el("p", { class: "ap-q-error", id: `ap-q-${pid}-slot-error`, hidden: true });
    input.setAttribute("aria-describedby", error.id);
    fields.set("slot_at", { control: input, error,
                            check: () => input.setAttribute("aria-invalid",
                                                            String(refusedFor(pid, "slot_at").length > 0)) });
    const changed = () => {
      const t = fromInputValue(input.value, state.zone);
      move.disabled = !movable || t === null || t === (byId(pid) || post).slot_at;
    };
    input.addEventListener("input", changed);
    input.addEventListener("change", changed);
    input.addEventListener("keydown", event => {
      if (event.key === "Enter" && !move.disabled) {
        event.preventDefault();
        moveSlot(pid, input.value);
      }
    });
    move.addEventListener("click", () => moveSlot(pid, input.value));
    return el("div", { class: "ap-q-field" }, top,
              el("div", { class: "ap-q-slot" }, input, el("span", { class: "ap-pill" }, post.slot_zone || state.zone),
                 move),
              error);
  }

  /* ── actions ────────────────────────────────────────────────────────── */
  function refreshRow(post) {
    // Swap the row only: the open panel beside it keeps its fields and focus
    // while a field saves. Focus on the row itself comes back.
    const row = container.querySelector(`.ap-q-row[data-id="${CSS.escape(post.id)}"]`);
    if (row) {
      const active = document.activeElement;
      const key = active && row.contains(active) ? active.getAttribute("data-focus") : null;
      const fresh = rowNode(post);
      row.replaceWith(fresh);
      const target = key ? fresh.querySelector(`[data-focus="${CSS.escape(key)}"]`) : null;
      if (target) target.focus({ preventScroll: true });
    }
    const panel = container.querySelector(`#ap-q-edit-${CSS.escape(post.id)}`);
    if (panel) {
      for (const f of (controls.get(post.id) || new Map()).values()) f.check();
      for (const button of panel.querySelectorAll("[data-vis]")) {
        button.setAttribute("aria-pressed", String(post.target_visibility === button.dataset.vis));
      }
    }
    renderHead(visible());
  }

  const settled = pid => saving.get(pid) || Promise.resolve(true);

  /** PATCH one change, after the post's earlier saves. `text` is what the
      field held, so the draft is dropped only if nothing was typed since. */
  function save(pid, body, name, text = undefined) {
    const run = settled(pid).then(async () => {
      try {
        const { post } = await api.patch(`/api/autopilot/posts/${encodeURIComponent(pid)}`, body);
        setPost(post);
        dropDraft(pid, name, text);
        clearRefused(pid, [name]);
        refreshRow(post);
        say(`Saved the ${labelOf(post)}.`);
        return true;
      } catch (err) {
        if (err.status === 422) sayRefused(pid, setRefused(pid, err.problems, [name]));
        else showAlert(`Couldn't save the ${labelOf(byId(pid) || {})}: ${err.message}`);
        return false;
      }
    });
    saving.set(pid, run);
    return run;
  }

  function saveText(pid, name, text, value) {
    return save(pid, { meta: { [name]: value } }, name, text);
  }

  async function resetField(pid, name) {
    await settled(pid);
    try {
      const post = await api.post(`/api/autopilot/posts/${encodeURIComponent(pid)}/reset`, { field: name });
      setPost(post);
      dropDraft(pid, name);
      saving.set(pid, Promise.resolve(true));     // the field is the AI's again, so nothing is unsaved
      clearRefused(pid, [name]);
      const f = (controls.get(pid) || new Map()).get(name);
      if (f) {
        const value = (post.meta || {})[name];
        f.control.value = Array.isArray(value) ? value.join(", ") : value ?? "";
        f.check();
        f.control.focus({ preventScroll: true });
      }
      refreshRow(post);
      say(`The ${FIELD_NAMES[name]} of the ${labelOf(post)} is back to the AI version.`);
    } catch (err) {
      showAlert(`Couldn't reset the ${FIELD_NAMES[name]}: ${err.message}`);
    }
  }

  async function moveSlot(pid, value) {
    const t = fromInputValue(value, state.zone);
    if (t === null || state.busy) return;
    await settled(pid);
    state.busy = true;
    try {
      const { post, warnings } = await api.patch(`/api/autopilot/posts/${encodeURIComponent(pid)}`, { slot_at: t });
      setPost(post);
      clearRefused(pid, ["slot_at"]);
      renderAll({ key: `slot-${pid}` });
      say(`Moved the ${labelOf(post)} to ${whenOf(post)}.${warnings.length ? ` ${warnings.join(" ")}` : ""}`);
    } catch (err) {
      if (err.status === 422) sayRefused(pid, setRefused(pid, err.problems, ["slot_at"]));
      else showAlert(`Couldn't move the ${labelOf(byId(pid) || {})}: ${err.message}`);
    } finally {
      state.busy = false;
    }
  }

  function orderAfter(pid) {
    const ids = visible().map(p => p.id);
    const i = ids.indexOf(pid);
    return [...ids.slice(i + 1), ...ids.slice(0, Math.max(0, i)).reverse()];
  }

  async function approve(pid) {
    if (state.busy) return;
    const saved = await settled(pid);
    const before = byId(pid);
    if (!before) return;
    const pending = refused.get(pid) || [];
    if (pending.length) {
      // A change the server refused is still in its field: fix it first.
      showAlert(`The ${labelOf(before)} has a change that didn't save. Fix the field that shows why, ` +
                "or reset it to the AI version, then approve.");
      if (state.open !== pid) {
        state.open = pid;
        renderAll();
      }
      const f = (controls.get(pid) || new Map()).get(pending[0].field);
      if (f) f.control.focus();
      return;
    }
    if (!saved) {
      saving.delete(pid);
      showAlert("Your last change didn't save. Change the field again to retry it, or approve again " +
                "to approve without it.");
      return;
    }
    const fallback = orderAfter(pid);
    state.busy = true;
    try {
      const post = await api.post(`/api/autopilot/posts/${encodeURIComponent(pid)}/approve`);
      setPost(post);
      showAlert("");
      say(`Approved the ${labelOf(post)}${post.slot_day ? `; it's planned for ${whenOf(post)}` : ""}.`);
      document.dispatchEvent(new Event("ap:changed"));
      renderAll({ key: `toggle-${pid}`, fallback });
    } catch (err) {
      showAlert(`Couldn't approve the ${labelOf(before)}: ${err.message}`);
      if (err.status === 404 || err.status === 409) {
        await load();
        renderAll({ key: `toggle-${pid}`, fallback });
      }
    } finally {
      state.busy = false;
    }
  }

  /** What Try again did, as the server put the post back (10.10). */
  function retriedText(post) {
    const label = labelOf(post);
    if (post.status === "retrying" || post.status === "publishing") return `Trying the ${label} again now.`;
    if (post.status === "scheduled" && post.slot_day) {
      return `Trying the ${label} again: it's planned for ${whenOf(post)}.`;
    }
    return `The ${label} is back in the queue without a posting time` +
           (post.last_error ? `: ${post.last_error}.` : ".");
  }

  async function retry(pid) {
    if (state.busy) return;
    const before = byId(pid);
    if (!before) return;
    const fallback = orderAfter(pid);
    state.busy = true;
    try {
      const post = await api.post(`/api/autopilot/posts/${encodeURIComponent(pid)}/retry`);
      setPost(post);
      showAlert("");
      say(retriedText(post));
      document.dispatchEvent(new Event("ap:changed"));          // the chip's failed count
      renderAll({ key: `toggle-${pid}`, fallback });
    } catch (err) {
      showAlert(`Couldn't try the ${labelOf(before)} again: ${err.message}`);
      if (err.status === 404 || err.status === 409) {
        await load();
        renderAll({ key: `toggle-${pid}`, fallback });
      }
    } finally {
      state.busy = false;
    }
  }

  async function reject(pid) {
    if (state.busy) return;
    await settled(pid);
    const before = byId(pid);
    if (!before) return;
    if (before.status === "uploaded") {
      const ok = await modal({ title: `Don't post the ${labelOf(before)}?`, confirm: "Don't post", danger: true,
                               content: [el("p", {}, "It's already uploaded to YouTube as a private video. " +
                                                     "Not posting it deletes that upload, so it never goes public.")] });
      if (!ok) return;
    }
    const fallback = orderAfter(pid);
    state.busy = true;
    try {
      const post = await api.post(`/api/autopilot/posts/${encodeURIComponent(pid)}/reject`);
      setPost(post);
      showAlert("");
      say(`The ${labelOf(before)} won't be posted.`);
      document.dispatchEvent(new Event("ap:changed"));
      renderAll({ key: `toggle-${pid}`, fallback });
    } catch (err) {
      showAlert(`Couldn't stop the ${labelOf(before)} from posting: ${err.message}`);
    } finally {
      state.busy = false;
    }
  }

  async function runBulk(action) {
    if (state.busy) return;
    const chosen = [...state.selected].map(byId).filter(Boolean);
    if (!chosen.length) return;
    await Promise.all(chosen.map(p => settled(p.id)));
    const n = chosen.length;
    if (action === "reject") {
      const uploads = chosen.filter(p => p.status === "uploaded").length;
      const content = [el("p", {}, `${n === 1 ? "It" : "They"} won't be published.`)];
      if (uploads) {
        content.push(el("p", {}, `${plural(uploads, "of them is", "of them are")} already uploaded to YouTube as ` +
                                 `private ${uploads === 1 ? "video" : "videos"}; not posting deletes ` +
                                 `${uploads === 1 ? "it" : "them"} there.`));
      }
      const title = n === 1 ? `Don't post the ${labelOf(chosen[0])}?` : `Don't post these ${n} posts?`;
      if (!await modal({ title, content, confirm: "Don't post", danger: true })) return;
    } else {
      const heldOnes = chosen.filter(held);
      if (heldOnes.length) {
        const content = [
          el("p", {}, `${plural(heldOnes.length, "of them is", "of them are")} held. Approving publishes ` +
                      `${heldOnes.length === 1 ? "it" : "them"} anyway:`),
          el("ul", { class: "ap-q-holds" }, ...heldOnes.map(p =>
            el("li", {}, `${labelOf(p)}, ${whenOf(p)}: ${(p.holds[0] || {}).message || "held"}`))),
        ];
        if (!await modal({ title: `Approve ${plural(n, "post")}?`, content, confirm: "Approve anyway" })) return;
      }
    }
    state.busy = true;
    try {
      const reply = await api.post("/api/autopilot/posts/bulk", { ids: chosen.map(p => p.id), action });
      state.selected.clear();
      await load();
      renderAll();
      showResults(action, chosen, reply.results || []);
      document.dispatchEvent(new Event("ap:changed"));
    } catch (err) {
      showAlert(`Couldn't ${action} them: ${err.message}`);
    } finally {
      state.busy = false;
    }
  }

  function showResults(action, chosen, replies) {
    const done = action === "approve" ? "approved" : "won't be posted";
    const byPost = new Map(chosen.map(p => [p.id, p]));
    const ok = replies.filter(r => r.ok).length;
    const all = ok === replies.length;
    const summary = action === "approve"
      ? (all ? `Approved ${plural(ok, "post")}.` : `Approved ${ok} of ${plural(replies.length, "post")}; ` +
                                                   `${replies.length - ok} skipped.`)
      : (all ? `${plural(ok, "post")} won't be posted.` : `${ok} of ${plural(replies.length, "post")} won't be ` +
                                                         `posted; ${replies.length - ok} skipped.`);
    const items = replies.map(r => {
      const post = byPost.get(r.id) || { id: r.id };
      return el("li", { class: r.ok ? "ok" : "skip" }, el("b", {}, `${labelOf(post)}, ${whenOf(post)}`), ": ",
                r.ok ? done : `skipped. ${r.error || ""}`.trim());
    });
    const dismiss = el("button", { type: "button", class: "ap-btn ghost sm" }, "Dismiss");
    dismiss.addEventListener("click", () => {
      results.hidden = true;
      results.replaceChildren();
      heading.focus({ preventScroll: true });
    });
    results.replaceChildren(el("div", { class: "ap-q-results-head" }, el("p", {}, summary), dismiss),
                            el("ul", {}, ...items));
    results.hidden = false;
    dismiss.focus({ preventScroll: true });
    say(summary);
  }

  async function setPaused(paused) {
    if (state.busy) return;
    state.busy = true;
    try {
      await api.post("/api/autopilot/pause", { paused });
      await load();
      renderAll();
      pause.focus({ preventScroll: true });
      say(state.paused ? "Publishing is paused." : "Publishing resumed.");
      document.dispatchEvent(new Event("ap:changed"));
    } catch (err) {
      showAlert(`Couldn't ${paused ? "pause" : "resume"} publishing: ${err.message}`);
    } finally {
      state.busy = false;
    }
  }

  /* ── keeping it current ─────────────────────────────────────────────── */
  // A redraw never happens under the user: not while a post is open,
  // anything is ticked, an action runs or a dialog is up.
  const quiet = () => !state.open && !state.selected.size && !state.busy && !document.querySelector("dialog[open]");

  async function refresh() {
    if (quiet() && await load() && quiet()) renderAll("keep");
  }

  function schedule() {
    clearTimeout(timer);
    if (stopped) return;
    timer = setTimeout(async () => {
      await refresh();
      schedule();
    }, POLL_MS);
  }

  function onVisible() {
    if (!document.hidden) refresh();
  }

  // Escape closes the open post first. Its panel is marked data-ap-escape,
  // so the router leaves Escape alone while it's open; a text field, a
  // dialog and the main page's own dialogs keep Escape for themselves.
  function onKey(event) {
    if (event.key !== "Escape" || event.defaultPrevented || !state.open) return;
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.isComposing) return;
    if (document.querySelector("dialog[open], .scrim.open")) return;
    const t = event.target;
    if (t && (t.isContentEditable || (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) && t.type !== "checkbox"))) return;
    event.preventDefault();
    toggle(state.open);
  }

  document.addEventListener("keydown", onKey);
  document.addEventListener("visibilitychange", onVisible);
  if (await load()) renderAll();
  else renderHead(visible());
  schedule();
  return () => {
    stopped = true;
    clearTimeout(timer);
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("visibilitychange", onVisible);
  };
}
