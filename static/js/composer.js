/* The Autopilot part of the composer on the main page (design §15, task
   12.3; Requirements 1.1, 1.2, 13.1, 13.2).

   - An Autopilot switch and a profile picker sit at the top of the form.
     Both are remembered in the preferences (autopilot_default and
     last_profile_id), so the form opens the way the user left it.
   - With the switch on, the form's option rows give way to a one-line
     summary of the chosen profile, the button reads "Start Autopilot", and
     "Several links" swaps the link field for a box taking up to 20 links,
     one per line.
   - Starting reads each link's details (POST /sources/probe), then asks the
     user to confirm their rights in a dialog naming each video's channel
     (13.1), unless every video is on the user's own connected channel
     (13.2). An uploaded file always needs the confirmation. The sources are
     then queued in order (POST /sources or /sources/file) and the Sources
     screen opens, saying what was added and which links weren't.
   - With the switch off, the form is today's and "Make clips" runs the
     manual flow in index.html, whose handler steps aside only while
     <body data-autopilot="on">. */
import { api } from "./api.js";
import { modal } from "./dialog.js";
import { formatBytes, formatDuration, listOf, plural } from "./format.js";
import { ensureTimezone, prefs, savePrefs } from "./prefs.js";
import { setupLeft, setupText } from "./readiness.js";
import * as router from "./router.js";
import { noteAdded } from "./sources.js";

export const MAX_LINKS = 20;

const CAPTIONS = { words: "word-by-word subtitles", phrases: "phrase subtitles",
                   karaoke: "karaoke subtitles" };
const PLATFORMS = { youtube: "YouTube", instagram: "Instagram" };

const byId = id => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === true) node.setAttribute(name, "");
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(name, value);
  }
  node.append(...children);
  return node;
}

const trimNumber = n => (Number.isInteger(n) ? String(n) : String(Math.round(n * 10) / 10));

/** The one-line summary of a profile's settings shown in place of the options. */
export function summarize(settings = {}) {
  const clips = settings.clips_per_source ?? 6;
  const shortest = settings.length_min_s ?? 20;
  const longest = settings.length_max_s ?? 60;
  const captions = CAPTIONS[settings.caption_style] || CAPTIONS.words;
  const where = listOf((settings.platforms || ["youtube", "instagram"]).map(p => PLATFORMS[p] || p));
  const asks = settings.mode === "full_auto" ? "Posts go out without asking you." : "You approve each post.";
  return `Up to ${plural(clips, "clip")} per video, ${trimNumber(shortest)}–${trimNumber(longest)} s each, ` +
         `with ${captions}, for ${where}. ${asks}`;
}

/** The links in the "Several links" box: whitespace separates them. */
export function linksIn(text) {
  return (text || "").split(/\s+/).map(s => s.trim()).filter(Boolean);
}

const state = { on: false, several: false, busy: false, profiles: [], profileId: null };

let parts = null;          // the composer's elements, once found

function find() {
  const names = ["apBar", "apSwitch", "apProfileWrap", "apProfile", "apSummary", "apSummaryText",
                 "apLinksField", "apLinks", "apLinksCount", "apMore", "apSeveral", "apMsg",
                 "goLabel", "urlField", "url", "fileInput", "fileName", "dropZone", "go"];
  const found = {};
  for (const name of names) {
    found[name] = byId(name);
    if (!found[name]) return null;          // not this page: nothing to wire up
  }
  found.composer = found.apBar.closest(".composer");
  return found.composer ? found : null;
}

const fileMode = () => document.body.dataset.mode === "file";

function setMessage(content, tone = "") {
  const box = parts.apMsg;
  box.replaceChildren();
  if (tone === "busy" && content) box.append(el("span", { class: "spin", "aria-hidden": "true" }));
  if (content) {
    const nodes = Array.isArray(content) ? content : [content];
    box.append(el("span", { class: "ap-msg-body" }, ...nodes));
  }
  box.className = `ap-composer-msg${tone ? ` ap-${tone}` : ""}`;
}

function busy(on, text = "") {
  // aria-disabled, not disabled: a disabled button drops focus, and focus
  // must stay on Go so it can come back there when the dialog closes.
  state.busy = on;
  if (on) parts.go.setAttribute("aria-disabled", "true");
  else parts.go.removeAttribute("aria-disabled");
  parts.apSwitch.disabled = on;          // the form can't change under a submission
  if (on) setMessage(text, text ? "busy" : "");
}

function sync() {
  const link = !fileMode();
  parts.composer.classList.toggle("ap-on", state.on);
  document.body.dataset.autopilot = state.on ? "on" : "off";
  parts.apProfileWrap.hidden = !state.on;
  parts.apSummary.hidden = !state.on;
  parts.apMore.hidden = !(state.on && link);
  parts.apLinksField.hidden = !(state.on && link && state.several);
  parts.apLinksCount.hidden = parts.apLinksField.hidden;
  parts.urlField.classList.toggle("ap-gone", state.on && link && state.several);
  parts.apSeveral.textContent = state.several ? "Use one link" : "Several links";
  parts.goLabel.textContent = state.on ? "Start Autopilot" : "Make clips";
  if (!state.on && !state.busy) setMessage("");
}

function currentProfile() {
  return state.profiles.find(p => p.id === state.profileId) || null;
}

/** "Give Autopilot a video" on the Autopilot screens: back to the main
    page's form, with Autopilot on and focus where the video goes. */
export function giveVideo() {
  if (!parts) return;
  if (!state.on && !state.busy) {
    parts.apSwitch.checked = true;
    parts.apSwitch.dispatchEvent(new Event("change"));
  }
  router.leave();
  const field = fileMode() ? parts.dropZone : state.several ? parts.apLinks : parts.url;
  field.scrollIntoView({ block: "center" });
  field.focus({ preventScroll: true });
}

function renderSummary() {
  const profile = currentProfile();
  parts.apSummaryText.textContent = profile
    ? summarize(profile.settings || {})
    : "Uses your default preset.";
}

/** With Autopilot on, say so when it can't publish anywhere yet (11.1). */
async function showSetupNote() {
  const note = byId("apSetupNote");
  if (!note) return;
  let left = 0;
  if (state.on) {
    try { left = await setupLeft(); } catch { left = 0; }        // Setup shows a failed reading itself
  }
  note.hidden = !(state.on && left);
  note.replaceChildren(...(note.hidden ? [] : [`${setupText(left)} `,
    el("a", { class: "ap-textlink", href: "#/autopilot/setup" }, "Open Setup")]));
}

function fillPicker() {
  const picker = parts.apProfile;
  picker.replaceChildren(...state.profiles.map(p => el("option", { value: p.id },
    p.is_default && p.name.toLowerCase() !== "default" ? `${p.name} (default)` : p.name)));
  if (state.profileId) picker.value = state.profileId;
  picker.disabled = state.profiles.length === 0;
}

function updateCount() {
  const n = linksIn(parts.apLinks.value).length;
  const over = n > MAX_LINKS;
  parts.apLinksCount.textContent = n === 0
    ? `Up to ${MAX_LINKS} links.`
    : over ? `${n} links: at most ${MAX_LINKS} at a time.` : `${n} of ${MAX_LINKS} links`;
  parts.apLinksCount.classList.toggle("ap-over", over);
  parts.apLinks.setAttribute("aria-invalid", over ? "true" : "false");
}

function remember(changes) {
  savePrefs(changes).catch(err => {
    setMessage(`Couldn't remember this choice: ${err.message}`, "error");
  });
}

function notAdded(items) {
  return el("ul", {}, ...items.map(item => el("li", {}, `${item.url}: ${item.error}`)));
}

/** The rights confirmation (13.1). `items` are {title, meta}. */
function confirmRights(items, skipped = []) {
  const many = items.length > 1;
  const box = el("input", { type: "checkbox" });
  const content = [
    el("p", {}, `Autopilot will cut clips from ${many ? "these videos" : "this video"} and post ` +
                "them to your accounts. Posting videos you don't have the rights to can get your " +
                "channel struck or your account blocked."),
    el("ul", { class: "ap-rights", "aria-label": many ? "Videos" : "Video" },
       ...items.map(item => el("li", {}, el("strong", {}, item.title),
                               el("span", { class: "ap-rights-meta" }, item.meta)))),
  ];
  if (skipped.length) {
    content.push(el("p", { class: "ap-rights-skipped" },
                    `${plural(skipped.length, "link")} can't be added and ` +
                    `${skipped.length === 1 ? "is" : "are"} left out:`),
                 el("ul", { class: "ap-rights-skip" },
                    ...skipped.map(item => el("li", {}, `${item.url}: ${item.error}`))));
  }
  content.push(el("label", { class: "ap-check" }, box,
                  el("span", {}, many ? "I own these videos or have the right to republish them."
                                      : "I own this video or have the right to republish it.")));
  return modal({
    title: "Confirm your rights",
    content,
    confirm: "Start Autopilot",
    focus: box,
    check: () => (box.checked ? null : "Tick the box to confirm, or cancel."),
  });
}

function videoMeta(source) {
  const channel = source.channel
    ? `${source.channel}${source.own_channel ? " (your channel)" : ""}`
    : "Unknown channel";
  const length = formatDuration(source.duration);
  return length ? `${channel} · ${length}` : channel;
}

/** All went well: clear the form and show the Sources screen. */
function finish(added, failed) {
  busy(false);
  if (!added.length) {
    setMessage([el("span", {}, "Nothing was added:"), notAdded(failed)], "error");
    return;
  }
  parts.url.value = "";
  parts.apLinks.value = "";
  updateCount();
  setMessage("");
  noteAdded({ added: added.map(a => ({ id: a.job_id, title: a.title })),
              failed: failed.map(f => ({ url: f.url, error: f.error })) });
  router.go("#/autopilot/sources");
}

async function startLinks() {
  const field = state.several ? parts.apLinks : parts.url;
  const urls = state.several ? linksIn(parts.apLinks.value)
                             : [parts.url.value.trim()].filter(Boolean);
  if (!urls.length) {
    setMessage(state.several ? "Paste at least one YouTube link." : "Paste a YouTube link first.", "error");
    field.focus();
    return;
  }
  if (urls.length > MAX_LINKS) {
    const extra = urls.length - MAX_LINKS;
    setMessage(`At most ${MAX_LINKS} links at a time; there are ${urls.length}. ` +
               `Remove ${extra} and try again.`, "error");
    field.focus();
    return;
  }
  busy(true, urls.length > 1 ? `Reading the details of ${urls.length} videos…`
                             : "Reading the video's details…");
  try {
    const { sources } = await api.post("/api/autopilot/sources/probe", { urls });
    const good = sources.filter(s => !s.error);
    const bad = sources.filter(s => s.error);
    if (!good.length) {
      busy(false);
      setMessage([el("span", {}, urls.length > 1 ? "None of these links can be added:"
                                                 : "This link can't be added:"),
                  notAdded(bad)], "error");
      field.focus();
      return;
    }
    // 13.2: no confirmation when every video is on the user's own channel.
    const confirming = good.some(s => !s.own_channel);
    if (confirming) {
      setMessage("");
      const ok = await confirmRights(good.map(s => ({ title: s.title, meta: videoMeta(s) })), bad);
      if (!ok) {
        busy(false);
        return;
      }
    }
    busy(true, good.length > 1 ? `Adding ${good.length} videos…` : "Adding the video…");
    await ensureTimezone();
    const { jobs } = await api.post("/api/autopilot/sources", {
      urls: good.map(s => s.url), profile_id: state.profileId, rights_confirmed: confirming,
    });
    finish(jobs.filter(j => j.job_id), [...bad, ...jobs.filter(j => j.error)]);
  } catch (err) {
    busy(false);
    setMessage(err.message, "error");
  }
}

async function startFile() {
  const file = parts.fileInput.files && parts.fileInput.files[0];
  if (!file) {
    setMessage("Choose a video file first.", "error");
    parts.dropZone.focus();
    return;
  }
  busy(true);
  const ok = await confirmRights([{ title: file.name, meta: `Uploaded file · ${formatBytes(file.size)}` }]);
  if (!ok) {
    busy(false);
    return;
  }
  busy(true, `Uploading “${file.name}”…`);
  try {
    await ensureTimezone();
    const form = new FormData();
    form.append("file", file);
    if (state.profileId) form.append("profile_id", state.profileId);
    form.append("rights_confirmed", "true");
    const job = await api.post("/api/autopilot/sources/file", form);
    parts.fileInput.value = "";
    parts.fileName.textContent = "";
    finish([{ job_id: job.job_id, title: job.title || file.name }], []);
  } catch (err) {
    busy(false);
    setMessage(err.message, "error");
  }
}

function start() {
  if (state.busy) return;
  setMessage("");
  if (fileMode()) startFile();
  else startLinks();
}

async function load() {
  const [stored, listed] = await Promise.allSettled([prefs(), api.get("/api/autopilot/profiles")]);
  const preferences = stored.status === "fulfilled" ? stored.value : {};
  state.profiles = listed.status === "fulfilled" ? listed.value.profiles || [] : [];
  const remembered = state.profiles.find(p => p.id === preferences.last_profile_id);
  const fallback = state.profiles.find(p => p.is_default) || state.profiles[0];
  state.profileId = (remembered || fallback || {}).id || null;
  state.on = preferences.autopilot_default === true;
  parts.apSwitch.checked = state.on;
  fillPicker();
  renderSummary();
  updateCount();
  sync();
  parts.apBar.hidden = false;
  showSetupNote();
}

function wire() {
  parts = find();
  if (!parts) return;
  parts.apSwitch.addEventListener("change", () => {
    state.on = parts.apSwitch.checked;
    sync();
    showSetupNote();
    if (state.on) ensureTimezone();
    remember({ autopilot_default: state.on });
  });
  parts.apProfile.addEventListener("change", () => {
    state.profileId = parts.apProfile.value;
    renderSummary();
    remember({ last_profile_id: state.profileId });
  });
  parts.apSeveral.addEventListener("click", () => {
    if (!state.several) {
      if (!parts.apLinks.value.trim()) parts.apLinks.value = parts.url.value.trim();
      state.several = true;
      sync();
      updateCount();
      parts.apLinks.focus();
    } else {
      if (!parts.url.value.trim()) parts.url.value = linksIn(parts.apLinks.value)[0] || "";
      state.several = false;
      sync();
      parts.url.focus();
    }
  });
  parts.apLinks.addEventListener("input", updateCount);
  parts.go.addEventListener("click", () => {
    if (state.on) start();
  });
  // The source tabs (link or file) are index.html's; follow them.
  new MutationObserver(sync).observe(document.body, { attributes: true, attributeFilter: ["data-mode"] });
  document.addEventListener("ap:route", event => {
    if (!event.detail.route) showSetupNote();          // back from Setup, say
  });
  load();
}

wire();
