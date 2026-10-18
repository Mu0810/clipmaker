/* The Profiles screen, #/autopilot/profiles (design §12, §15, task 12.5;
   Requirements 14.1, 14.2, 14.4, 3.9, N5.1, N5.2).

   - The profiles, the default first, and an editor for the one chosen: every
     setting of 14.1, grouped as on the way through Autopilot (clips,
     language, editing, music, publishing, posting times, brand).
   - Save sends the whole form; each problem the server finds shows beside
     its field (422 [{field, message}]), and focus goes to the first one.
   - Posting hours are picked from chips, a week grid per platform
     (hours.js, design §15), so they can't be mistyped.
   - New profile, Duplicate, Delete (not the only one) and Make default.
   - "Re-plan pending posts" applies the saved posting settings to the
     profile's planned posts the user hasn't changed (14.4).
   - Music cleared for publishing: Autopilot adds background music only from
     tracks marked here (3.9), and the music choice lists only those. */
import { api } from "./api.js";
import { modal } from "./dialog.js";
import { el, field, setError, sr } from "./dom.js";
import { plural } from "./format.js";
import { hourGrid } from "./hours.js";
import { say } from "./live.js";

const PLATFORMS = [["youtube", "YouTube"], ["instagram", "Instagram"]];
const CAPTIONS = [["words", "Word by word (Shorts style)"], ["phrases", "Full phrases"],
                  ["karaoke", "Karaoke (word highlight)"]];

const splitList = text => (text || "").split(",").map(s => s.trim()).filter(Boolean);
const splitTags = text => (text || "").split(/[\s,]+/).map(s => s.replace(/^#+/, "")).filter(Boolean);

function numberOrNull(input) {
  const value = input.value.trim();
  return value === "" ? null : Number(value);
}

// Changes not saved when the screen is left (another Autopilot screen, or
// Escape): {id, data}. They come back the next time the screen opens.
let unsaved = null;

export async function render(container) {
  const state = { profiles: [], languages: [], tracks: [], cleared: [], selected: null, busy: false, saved: "" };

  const heading = el("h1", { class: "ap-h1" }, "Presets");
  const lead = el("p", { class: "ap-lead" },
                  "Each video you give Autopilot uses a preset: how many clips, how they're edited and when " +
                  "they're posted. A change applies to videos you add afterwards; Re-plan pending posts applies " +
                  "its posting settings to posts already planned.");
  const problem = el("p", { class: "ap-problem", role: "alert", hidden: true });
  const listBox = el("ul", { class: "ap-prof-list", "aria-label": "Presets" });
  const addButton = el("button", { type: "button", class: "ap-btn ghost sm" }, "New preset");
  const editor = el("div", { class: "ap-prof-editor" });
  const music = el("section", { class: "ap-card", "aria-labelledby": "ap-prof-music" });
  container.append(heading, lead, problem,
                   el("div", { class: "ap-prof" },
                      el("div", { class: "ap-prof-side" }, listBox, addButton), editor),
                   music);
  addButton.addEventListener("click", () => leaveThen(createProfile));

  function showProblem(text) {
    problem.textContent = text || "";
    problem.hidden = !text;
  }

  async function load() {
    const [list, tracks] = await Promise.allSettled([api.get("/api/autopilot/profiles"), api.get("/api/music")]);
    if (list.status === "rejected") {
      showProblem(`Couldn't read your presets: ${list.reason.message}`);
      return false;
    }
    state.profiles = list.value.profiles || [];
    state.languages = list.value.languages || [];
    state.tracks = tracks.status === "fulfilled" ? tracks.value.tracks || [] : [];
    state.cleared = tracks.status === "fulfilled" ? tracks.value.cleared || [] : [];
    if (!state.profiles.some(p => p.id === state.selected)) {
      state.selected = (state.profiles.find(p => p.is_default) || state.profiles[0] || {}).id || null;
    }
    showProblem("");
    return true;
  }

  const current = () => state.profiles.find(p => p.id === state.selected) || null;

  /* ── the list ─────────────────────────────────────────────────────────── */
  function renderList() {
    listBox.replaceChildren(...state.profiles.map(profile => {
      const chosen = profile.id === state.selected;
      const button = el("button", { type: "button", class: "ap-prof-item", "aria-current": chosen ? "true" : null,
                                    "data-focus": `pick-${profile.id}` },
                        el("span", {}, profile.name),
                        profile.is_default && profile.name.toLowerCase() !== "default"
                          ? el("span", { class: "ap-pill terra" }, "Default") : null);
      button.addEventListener("click", () => {
        if (profile.id !== state.selected) leaveThen(() => select(profile.id, `pick-${profile.id}`));
      });
      return el("li", {}, button);
    }));
  }

  async function select(id, focus) {
    state.selected = id;
    renderList();
    renderEditor();
    const target = container.querySelector(`[data-focus="${CSS.escape(focus || "name")}"]`);
    if (target) target.focus({ preventScroll: true });
  }

  /* ── the editor ───────────────────────────────────────────────────────── */
  let form = null;            // {node, read(), place(problems), dirty()}

  function renderEditor(draft = null) {
    const profile = current();
    editor.replaceChildren();
    form = null;
    if (!profile) return;
    // What "unchanged" means is the saved profile, also when a kept draft is shown.
    state.saved = JSON.stringify(buildForm(profile).read());
    form = buildForm(draft ? { ...profile, name: draft.name, settings: draft.settings } : profile);
    editor.append(form.node);
    form.update();
  }

  function buildForm(profile) {
    const s = profile.settings || {};
    const brand = s.brand || {};
    const fields = new Map();          // server field -> {control, error}
    const id = name => `ap-prof-${name}`;
    const put = (name, f) => {
      fields.set(name, f);
      return f.wrap;
    };
    const number = (value, attrs) => el("input", { type: "number", inputmode: "decimal", ...attrs,
                                                   value: value ?? "" });

    const name = field(id("name"), "Name", el("input", { type: "text", maxlength: "60", autocomplete: "off",
                                                          value: profile.name, "data-focus": "name" }));
    fields.set("name", name);

    // Clips
    const clips = field(id("clips"), "Clips per video", number(s.clips_per_source, { min: "1", max: "20", step: "1" }),
                        { hint: "1 to 20." });
    const shortest = number(s.length_min_s, { min: "3", max: "180", step: "1", id: id("min"),
                                              "aria-label": "Shortest clip, in seconds" });
    const longest = number(s.length_max_s, { min: "3", max: "180", step: "1", id: id("max"),
                                             "aria-label": "Longest clip, in seconds" });
    const lengthError = el("p", { class: "ap-field-error", id: id("length-error"), hidden: true });
    shortest.setAttribute("aria-describedby", lengthError.id);
    longest.setAttribute("aria-describedby", lengthError.id);
    const length = { control: shortest, error: lengthError, also: longest };
    fields.set("length_min_s", length);
    fields.set("length_max_s", { control: longest, error: lengthError, also: shortest });
    const lengthWrap = el("div", { class: "ap-field" },
                          el("span", { class: "ap-label", id: id("length-label") }, "Clip length (seconds)"),
                          el("div", { class: "ap-range", role: "group", "aria-labelledby": id("length-label") },
                             shortest, el("span", { "aria-hidden": "true" }, "to"), longest),
                          el("p", { class: "ap-hint-line" }, "3 to 180 seconds; YouTube Shorts allow up to 180."),
                          lengthError);
    const minScore = field(id("score"), "Minimum score", number(s.min_score, { min: "0", max: "100", step: "1" }),
                           { hint: "Autopilot scores each clip from 0 to 100 for how well it's likely to do. " +
                                   "Clips below this get no posts; they stay in the library." });

    // Language (2.7, 4.4): menus of the languages the server lists, by name.
    const language = field(id("lang"), "Spoken language", languageMenu(s.language, "Detect it automatically"),
                           { hint: "Detecting works for most videos. Pick the language if a video mixes " +
                                   "languages or its transcript comes out in the wrong one." });
    const metaLanguage = field(id("metalang"), "Language of titles and captions",
                               languageMenu(s.metadata_language, "Same as the spoken language"),
                               { hint: "Autopilot writes each post's title, description and caption in it." });

    // Editing
    const layout = radios(id("layout"), "Layout", "layout", [
      ["fill", "Fill", "Crops to 9:16 and follows the speaker's face."],
      ["fit", "Fit", "Keeps the whole frame on a blurred background."]], s.layout);
    const captions = field(id("captions"), "Subtitle style", el("select", {},
      ...CAPTIONS.map(([value, label]) => el("option", { value }, label))));
    captions.control.value = s.caption_style || "words";
    const hook = check(id("hook"), "On-screen hook in the first 2 seconds", s.hook);
    const trim = check(id("trim"), "Trim silence and filler words", s.auto_trim);

    // Music (3.9)
    const track = field(id("track"), "Background music", el("select", {}),
                        { hint: "Only tracks marked as cleared for publishing below." });
    const volume = el("input", { type: "range", min: "0", max: "100", step: "1", id: id("volume"),
                                 value: String(Math.round((s.music_volume ?? 0.25) * 100)) });
    const volumeOut = el("output", { for: volume.id, class: "ap-volume-out" });
    const volumeError = el("p", { class: "ap-field-error", id: id("volume-error"), hidden: true });
    volume.setAttribute("aria-describedby", volumeError.id);
    fields.set("music_volume", { control: volume, error: volumeError });
    const volumeWrap = el("div", { class: "ap-field" }, el("label", { for: volume.id }, "Music volume"),
                          el("div", { class: "ap-volume" }, volume, volumeOut), volumeError);
    const syncVolume = () => {
      volumeOut.textContent = `${volume.value}%`;
      volume.setAttribute("aria-valuetext", `${volume.value}%`);
    };
    volume.addEventListener("input", syncVolume);
    syncVolume();
    fillTracks(track.control, s.music_track);

    // Publishing
    const platformsBox = el("fieldset", { class: "ap-checks", id: id("platforms") },
                            el("legend", {}, "Post to"));
    const platformInputs = PLATFORMS.map(([value, label]) => {
      const box = el("input", { type: "checkbox", value });
      box.checked = (s.platforms || []).includes(value);
      platformsBox.append(el("label", { class: "ap-check-row" }, box, el("span", {}, label)));
      return box;
    });
    const platformsError = el("p", { class: "ap-field-error", id: id("platforms-error"), hidden: true });
    platformsBox.setAttribute("aria-describedby", platformsError.id);
    platformsBox.append(platformsError);
    fields.set("platforms", { control: platformInputs[0], error: platformsError, group: platformsBox });
    const visibility = radios(id("visibility"), "YouTube visibility", "youtube_visibility", [
      ["public", "Public", "Uploaded private, then made public at its slot."],
      ["private", "Private", "Stays private on YouTube."]], s.youtube_visibility);
    const mode = radios(id("mode"), "Mode", "mode", [
      ["review", "Review", "Nothing is published until you approve it."],
      ["full_auto", "Full auto", "Posts go out on their own, unless they're held."]], s.mode);
    const disclosure = check(id("ai"), "Label posts as AI-altered content", s.ai_disclosure);

    // Posting times (5.2, 5.3)
    const windows = s.windows || {};
    const windowsError = el("p", { class: "ap-field-error", id: id("windows-error"), hidden: true });
    const windowsHint = el("p", { class: "ap-hint-line", id: id("windows-hint") },
                           "Pick the hours Autopilot may post at, in your time zone. It chooses among them, " +
                           "keeping to the gap and the daily limit below.");
    const grids = PLATFORMS.map(([platform, label]) => ({
      platform, label,
      grid: hourGrid({ label: `${label} posting hours`, saved: windows[platform] || {},
                       describedBy: `${windowsHint.id} ${windowsError.id}` }),
    }));
    const hoursNode = el("div", { class: "ap-windows-wrap" },
                         ...grids.map(({ label, grid }) => el("div", { class: "ap-hours-block" },
                                                              el("h3", { class: "ap-hours-title" }, label),
                                                              grid.node)),
                         windowsHint, windowsError);
    // The server can only name `windows`: its message goes under the grids,
    // and focus to the YouTube grid's chip.
    fields.set("windows", { get control() { return grids[0].grid.active(); }, error: windowsError,
                            group: hoursNode });
    const gap = field(id("gap"), "Minimum gap between posts (hours)", number(s.min_gap_hours, { min: "0", max: "48", step: "0.5" }),
                      { hint: "Per account; 0 to 48." });
    const daily = field(id("daily"), "Most posts a day", number(s.daily_max, { min: "1", max: "10", step: "1" }),
                        { hint: "Per account; 1 to 10." });

    // Brand
    const tone = field(id("tone"), "Tone", el("textarea", { rows: "2", maxlength: "300" }),
                       { hint: "How titles and captions should sound, for example: warm, plain, no hype." });
    tone.control.value = brand.tone || "";
    const avoid = field(id("avoid"), "Words to avoid", el("input", { type: "text", autocomplete: "off",
                                                                   value: (brand.avoid_words || []).join(", ") }),
                        { hint: "Separate them with commas. A post that still has one waits for your approval." });
    const tags = field(id("tags"), "Hashtags on every post", el("input", { type: "text", autocomplete: "off",
                                                                         value: (brand.always_hashtags || []).map(t => `#${t}`).join(" ") }),
                       { hint: "Separate them with spaces, like #tips #growth. Each can have letters, numbers and _." });

    for (const [key, f] of [["clips_per_source", clips], ["min_score", minScore], ["language", language],
                            ["metadata_language", metaLanguage], ["caption_style", captions], ["music_track", track],
                            ["min_gap_hours", gap], ["daily_max", daily], ["brand.tone", tone],
                            ["brand.avoid_words", avoid], ["brand.always_hashtags", tags]]) {
      fields.set(key, f);
    }
    for (const [key, r] of [["layout", layout], ["youtube_visibility", visibility], ["mode", mode]]) {
      fields.set(key, { control: r.inputs[0], error: r.error, group: r.node });
    }
    for (const [key, c] of [["hook", hook], ["auto_trim", trim], ["ai_disclosure", disclosure]]) {
      fields.set(key, { control: c.control, error: c.error });
    }

    // Actions
    const status = el("p", { class: "ap-prof-status", role: "status" });
    const save = el("button", { type: "submit", class: "ap-btn" }, "Save changes");
    const discard = el("button", { type: "button", class: "ap-btn ghost" }, "Discard changes");
    const replan = el("button", { type: "button", class: "ap-btn ghost", "aria-describedby": id("replan-hint") },
                      "Re-plan pending posts");
    const replanHint = el("p", { class: "ap-hint-line", id: id("replan-hint") },
                          "Moves this preset's planned posts that you haven't changed to fit its posting " +
                          "hours, gap and daily maximum. Posts you moved stay where you put them.");
    const makeDefault = el("button", { type: "button", class: "ap-btn ghost", disabled: profile.is_default },
                           profile.is_default ? "This is the default" : "Make default");
    const duplicate = el("button", { type: "button", class: "ap-btn ghost" }, "Duplicate");
    const remove = el("button", { type: "button", class: "ap-btn ghost ap-danger",
                                  disabled: state.profiles.length < 2 }, "Delete", sr(` ${profile.name}`));

    const node = el("form", { class: "ap-card ap-prof-form", novalidate: true, "aria-labelledby": id("title") },
      el("h2", { id: id("title"), tabindex: "-1" }, profile.name),
      el("div", { class: "ap-form-grid" }, name.wrap),
      group("Clips", clips.wrap, lengthWrap, minScore.wrap),
      group("Language", language.wrap, metaLanguage.wrap),
      group("Editing", layout.node, captions.wrap, hook.wrap, trim.wrap),
      group("Music", track.wrap, volumeWrap),
      group("Publishing", platformsBox, visibility.node, mode.node, disclosure.wrap),
      el("fieldset", { class: "ap-group ap-group-wide" }, el("legend", {}, "Posting times"),
         hoursNode,
         el("div", { class: "ap-form-grid" }, gap.wrap, daily.wrap)),
      group("Brand", tone.wrap, avoid.wrap, tags.wrap),
      el("div", { class: "ap-prof-acts" }, save, discard, status),
      el("div", { class: "ap-prof-more" }, el("div", {}, replan, replanHint),
         el("div", { class: "ap-prof-more-row" }, makeDefault, duplicate, remove)));

    function read() {
      const hours = Object.fromEntries(grids.map(({ platform, grid }) => [platform, grid.read()]));
      return {
        name: name.control.value,
        settings: {
          clips_per_source: numberOrNull(clips.control), length_min_s: numberOrNull(shortest),
          length_max_s: numberOrNull(longest), min_score: numberOrNull(minScore.control),
          language: language.control.value || null,
          metadata_language: metaLanguage.control.value || null,
          layout: layout.value(), caption_style: captions.control.value, hook: hook.control.checked,
          auto_trim: trim.control.checked, music_track: track.control.value || null,
          music_volume: Number(volume.value) / 100,
          platforms: platformInputs.filter(b => b.checked).map(b => b.value),
          youtube_visibility: visibility.value(), windows: hours,
          min_gap_hours: numberOrNull(gap.control), daily_max: numberOrNull(daily.control),
          mode: mode.value(), ai_disclosure: disclosure.control.checked,
          brand: { tone: tone.control.value, avoid_words: splitList(avoid.control.value),
                   always_hashtags: splitTags(tags.control.value) },
        },
      };
    }

    function clear() {
      for (const f of fields.values()) {
        f.error.textContent = "";
        f.error.hidden = true;
        f.control.removeAttribute("aria-invalid");
        if (f.also) f.also.removeAttribute("aria-invalid");
      }
    }

    /** Show each problem beside its field; returns the first control to fix. */
    function place(problems) {
      clear();
      let first = null;
      const loose = [];
      for (const p of problems) {
        const f = fields.get(p.field);
        if (!f) {
          loose.push(p.message);
          continue;
        }
        f.error.textContent = f.error.textContent ? `${f.error.textContent} ${p.message}` : p.message;
        f.error.hidden = false;
        f.control.setAttribute("aria-invalid", "true");
        first = first || f.control;
      }
      showProblem(loose.join(" "));
      return first;
    }

    function dirty() {
      return JSON.stringify(read()) !== state.saved;
    }

    function update() {
      const changed = dirty();
      save.disabled = !changed;
      discard.disabled = !changed;
      replan.disabled = changed;
      status.textContent = changed ? "Unsaved changes." : "";
      replanHint.textContent = changed
        ? "Save your changes first: re-planning uses the saved settings."
        : "Moves this preset's planned posts that you haven't changed to fit its posting hours, gap and " +
          "daily maximum. Posts you moved stay where you put them.";
    }

    node.addEventListener("input", update);
    node.addEventListener("change", update);
    node.addEventListener("submit", async event => {
      event.preventDefault();
      if (state.busy) return;
      state.busy = true;
      try {
        const saved = await api.patch(`/api/autopilot/profiles/${encodeURIComponent(profile.id)}`, read());
        replace(saved);
        say(`Saved ${saved.name}.`);
        renderList();
        renderEditor();
        editor.querySelector("h2").focus({ preventScroll: true });
        editor.querySelector(".ap-prof-status").textContent = "Saved.";
      } catch (err) {
        if (err.status === 422 && err.problems.length) {
          const first = place(err.problems);
          if (first) first.focus();
          say(`Not saved: ${plural(err.problems.length, "problem")} to fix.`);
        } else {
          showProblem(`Couldn't save ${profile.name}: ${err.message}`);
        }
      } finally {
        state.busy = false;
      }
    });
    discard.addEventListener("click", () => {
      renderEditor();
      say("Changes discarded.");
      container.querySelector('[data-focus="name"]').focus({ preventScroll: true });
    });
    replan.addEventListener("click", () => replanProfile(profile, replan));
    makeDefault.addEventListener("click", () => leaveThen(() => setDefault(profile)));
    duplicate.addEventListener("click", () => leaveThen(() => duplicateProfile(profile)));
    remove.addEventListener("click", () => deleteProfile(profile));

    return { node, read, place, dirty, update, fillTracks: () => fillTracks(track.control, track.control.value) };
  }

  function group(title, ...children) {
    return el("fieldset", { class: "ap-group" }, el("legend", {}, title),
              el("div", { class: "ap-form-grid" }, ...children));
  }

  function radios(id, legend, name, options, value) {
    const error = el("p", { class: "ap-field-error", id: `${id}-error`, hidden: true });
    const node = el("fieldset", { class: "ap-radios", id, "aria-describedby": error.id }, el("legend", {}, legend));
    const inputs = options.map(([option, label, hint]) => {
      const input = el("input", { type: "radio", name: `${id}-${name}`, value: option });
      input.checked = value === option;
      node.append(el("label", { class: "ap-radio" }, input,
                     el("span", {}, el("b", {}, label), hint ? el("span", { class: "ap-radio-hint" }, ` ${hint}`) : null)));
      return input;
    });
    if (!inputs.some(i => i.checked)) inputs[0].checked = true;
    node.append(error);
    return { node, inputs, error, value: () => (inputs.find(i => i.checked) || inputs[0]).value };
  }

  function check(id, label, value) {
    const control = el("input", { type: "checkbox", id });
    control.checked = !!value;
    const error = el("p", { class: "ap-field-error", id: `${id}-error`, hidden: true });
    control.setAttribute("aria-describedby", error.id);
    return { control, error, wrap: el("div", { class: "ap-field" },
                                      el("label", { class: "ap-check-row", for: id }, control, el("span", {}, label)), error) };
  }

  /** A menu of the languages by name, after `first` (no language set). A
      saved code the list doesn't have, such as "pt-BR", stays as an option. */
  function languageMenu(value, first) {
    const select = el("select", {}, el("option", { value: "" }, first));
    const known = new Map(state.languages.map(l => [l.code, l.name]));
    if (value && !known.has(value)) {
      const base = known.get(value.split("-")[0].toLowerCase());
      select.append(el("option", { value }, base ? `${base} (${value})` : value));
    }
    select.append(...state.languages.map(l => el("option", { value: l.code }, l.name)));
    select.value = value || "";
    return select;
  }

  function fillTracks(select, chosen) {
    const options = [el("option", { value: "" }, "None")];
    for (const name of state.tracks) {
      const cleared = state.cleared.includes(name);
      if (cleared || name === chosen) {
        options.push(el("option", { value: name }, cleared ? name : `${name} (not cleared)`));
      }
    }
    select.replaceChildren(...options);
    select.value = chosen && options.some(o => o.value === chosen) ? chosen : "";
  }

  function replace(profile) {
    const i = state.profiles.findIndex(p => p.id === profile.id);
    if (i >= 0) state.profiles[i] = profile;
    else state.profiles.push(profile);
  }

  /** Run `then` after the user agrees to drop unsaved changes, if any. */
  async function leaveThen(then) {
    if (state.busy) return;
    if (form && form.dirty()) {
      const profile = current();
      const ok = await modal({ title: "Discard your changes?", confirm: "Discard changes", danger: true,
                               content: [el("p", {}, `Your changes to ${profile ? profile.name : "this preset"} ` +
                                                     "haven't been saved.")] });
      if (!ok) return;
    }
    await then();
  }

  /* ── actions ──────────────────────────────────────────────────────────── */
  async function run(work) {
    if (state.busy) return;
    state.busy = true;
    try {
      await work();
    } catch (err) {
      showProblem(err.message);
    } finally {
      state.busy = false;
    }
  }

  function freeName(base) {
    const names = new Set(state.profiles.map(p => p.name));
    if (!names.has(base)) return base;
    for (let n = 2; ; n += 1) if (!names.has(`${base} ${n}`)) return `${base} ${n}`;
  }

  const createProfile = () => run(async () => {
    const profile = await api.post("/api/autopilot/profiles", { name: freeName("New preset"), settings: {} });
    replace(profile);
    say(`Created ${profile.name}, with the default settings.`);
    await select(profile.id, "name");
  });

  const duplicateProfile = profile => run(async () => {
    const copy = await api.post(`/api/autopilot/profiles/${encodeURIComponent(profile.id)}/duplicate`,
                                { name: freeName(`Copy of ${profile.name}`).slice(0, 60) });
    replace(copy);
    say(`Created ${copy.name}.`);
    await select(copy.id, "name");
  });

  const setDefault = profile => run(async () => {
    await api.patch(`/api/autopilot/profiles/${encodeURIComponent(profile.id)}`, { is_default: true });
    await load();
    say(`${profile.name} is now the default preset.`);
    await select(profile.id, `pick-${profile.id}`);
  });

  async function deleteProfile(profile) {
    const ok = await modal({ title: `Delete ${profile.name}?`, confirm: "Delete preset", danger: true,
                             content: [el("p", {}, "Videos already given to Autopilot keep the settings they " +
                                                   "were submitted with.")] });
    if (!ok) return;
    await run(async () => {
      await api.del(`/api/autopilot/profiles/${encodeURIComponent(profile.id)}`);
      state.selected = null;
      await load();
      say(`Deleted ${profile.name}.`);
      await select(state.selected, `pick-${state.selected}`);
    });
  }

  function replanProfile(profile, button) {
    return run(async () => {
      const reply = await api.post(`/api/autopilot/profiles/${encodeURIComponent(profile.id)}/replan`);
      const text = reply.replanned
        ? `Re-planned ${plural(reply.replanned, "post")}: ${reply.scheduled} with a slot` +
          `${reply.unscheduled ? `, ${reply.unscheduled} without one` : ""}.`
        : "No planned posts needed moving.";
      editor.querySelector(".ap-prof-status").textContent = text;
      say(text);
      document.dispatchEvent(new Event("ap:changed"));
      button.focus({ preventScroll: true });
    });
  }

  /* ── music cleared for publishing (3.9) ───────────────────────────────── */
  function renderMusic() {
    const rows = state.tracks.map((name, i) => {
      const box = el("input", { type: "checkbox", id: `ap-prof-clear-${i}` });
      box.checked = state.cleared.includes(name);
      box.addEventListener("change", () => setCleared(name, box));
      return el("li", {}, el("label", { class: "ap-check-row", for: box.id }, box,
                             el("span", {}, el("b", {}, name), " · I have the rights to publish it")));
    });
    music.replaceChildren(
      el("h2", { id: "ap-prof-music" }, "Music cleared for publishing"),
      el("p", { class: "ap-lead" }, "Autopilot adds background music only from tracks ticked here. Tick a track " +
                                    "only if you have the rights to publish it. With none ticked, clips get no " +
                                    "music. These are the tracks added on the main page, and every preset " +
                                    "shares this list."),
      rows.length ? el("ul", { class: "ap-tracks", "aria-label": "Music tracks" }, ...rows)
                  : el("p", { class: "ap-empty-text" }, "No tracks yet. Add one under Background music on the main page."));
  }

  async function setCleared(name, box) {
    try {
      const reply = await api.post(`/api/autopilot/music/${encodeURIComponent(name)}/clearance`, { cleared: box.checked });
      state.cleared = reply.cleared_tracks || [];
      say(box.checked ? `${name} is cleared for publishing.` : `${name} is no longer cleared for publishing.`);
      if (form) {
        const before = form.dirty();
        form.fillTracks();
        if (!before) state.saved = JSON.stringify(form.read());
        form.update();
      }
    } catch (err) {
      box.checked = !box.checked;
      showProblem(err.message);
    }
  }

  if (await load()) {
    const kept = unsaved && state.profiles.some(p => p.id === unsaved.id) ? unsaved : null;
    unsaved = null;
    if (kept) state.selected = kept.id;
    renderList();
    renderEditor(kept ? kept.data : null);
    renderMusic();
    if (kept && form && form.dirty()) {
      editor.querySelector(".ap-prof-status").textContent = "Your unsaved changes are back.";
    }
  }
  // Leaving with unsaved changes: the browser asks before the page goes,
  // and another Autopilot screen keeps them for when this one opens again.
  const beforeUnload = event => {
    if (form && form.dirty()) event.preventDefault();
  };
  window.addEventListener("beforeunload", beforeUnload);
  return () => {
    window.removeEventListener("beforeunload", beforeUnload);
    if (form && form.dirty() && state.selected) unsaved = { id: state.selected, data: form.read() };
  };
}
