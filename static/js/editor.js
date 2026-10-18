/* The clip editor, #/editor/<clip> (design §10, §15, task 12.6;
   Requirements 7.1-7.10, 8.6, N5.1, N5.2), in the Script layout chosen in
   task 12.1 (mockups/autopilot/editor-b.html).

   - The transcript of the window around the clip is the main surface: one
     paragraph per sentence with its start time. Words outside the clip are
     dimmed. START and END pins sit on words; drag them, or press [ and ] on
     a word. Select words (click, Shift+click, or arrows with Shift) and
     press Delete to cut them, or again to bring them back (7.3). Press
     Enter or double-click a word to correct its subtitle (7.4). Pauses and
     filler words that auto-trim removes are marked, with a way to keep
     every pause.
   - Above it, a strip of the source window with the clip, the cuts, the
     playhead and two handles for trims finer than a word: 0.1 s per arrow
     press, 1 s with Shift (N5.1). Near the window's edge a wider preview
     is fetched (§10.2), so any range of the source can be chosen (7.2).
   - On the right, the 9:16 preview (preview.js) with play, the time and a
     scrubber, then Subtitles, Hook, Layout (drag the preview to move the
     Fill crop), Music (cleared tracks only, 3.9) and Cover (7.5), and the
     clip's posts with the "replace the uploaded YouTube video" choice (8.6).
   - Every change is undoable, up to 200 steps (7.10): the buttons, or
     Cmd/Ctrl+Z and Shift+Cmd/Ctrl+Z. Edits not rendered yet are kept while
     the editor is closed, and come back when it opens again.
   - Render makes a new version in the background and shows its progress
     (7.7); a failure keeps the previous version and says why (7.8). Before
     rendering it warns about platform length limits (7.2), posts already
     published (7.9) and uploaded YouTube videos (8.6). */
import { api } from "./api.js";
import { modal } from "./dialog.js";
import { el, icon, sr } from "./dom.js";
import { plural } from "./format.js";
import { say } from "./live.js";
import * as P from "./preview.js";
import * as router from "./router.js";

const UNDO_MAX = 200;
const GROUP_MS = 800;                 // repeated presses within this make one undo step
const POLL_MS = 1000;
const WIDEN_NEAR_S = 5;               // §10.2: a handle this close to the window's edge
const WIDEN_BY_S = 60;
const MAX_WINDOW_S = 20 * 60;
const HOOK_MAX = 48;
const EDIT_MAX = 60;
const MIN_GAP_S = 0.1;                // the start stays this far before the end
const STYLES = [["words", "Word by word"], ["phrases", "Full phrases"], ["karaoke", "Karaoke"]];
const PLATFORM = { youtube: "YouTube", instagram: "Instagram" };
const PUBLISHED = new Set(["live", "private", "kept_private"]);

// Edits not rendered yet, per clip: {base (version id), spec, undo, redo}.
const drafts = new Map();

const ICONS = {
  back: '<path d="M19 12H5M11 18l-6-6 6-6"/>',
  undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
  redo: '<path d="M15 14l5-5-5-5"/><path d="M20 9H9a5 5 0 0 0 0 10h3"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z" fill="currentColor" stroke="none"/>',
  pause: '<path d="M8 5h3v14H8zM13 5h3v14h-3z" fill="currentColor" stroke="none"/>',
  spark: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/>',
  alert: '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.5v.01"/>',
};

function svg(name) {
  const span = el("span", { class: "ap-ico", "aria-hidden": "true" });
  span.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24">${ICONS[name]}</svg>`;
  return span;
}

/** Canonical JSON, so two specs compare equal whatever their key order. */
function canon(value) {
  if (Array.isArray(value)) return value.map(canon);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map(k => [k, canon(value[k])]));
  }
  return value;
}
const same = (a, b) => JSON.stringify(canon(a)) === JSON.stringify(canon(b));
const clone = spec => JSON.parse(JSON.stringify(spec));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/** A source or clip time: m:ss.d, or h:mm:ss.d from an hour; `tenths: false` drops the tenth. */
export function clock(seconds, { tenths = true } = {}) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "–";
  const total = tenths ? Math.round(Math.max(0, seconds) * 10) / 10 : Math.floor(Math.max(0, seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total - h * 3600 - m * 60;
  const ss = tenths ? s.toFixed(1).padStart(4, "0") : String(Math.floor(s)).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** `spans` minus `holes`, both sorted lists of [a, b]. */
function subtract(spans, holes) {
  const out = [];
  for (const [a, b] of spans) {
    let cur = a;
    for (const [c, d] of holes) {
      if (d <= cur || c >= b) continue;
      if (c > cur) out.push([cur, c]);
      cur = Math.max(cur, d);
    }
    if (cur < b) out.push([cur, b]);
  }
  return out.filter(([a, b]) => b - a > 1e-6);
}

function failedScreen(container, err) {
  const message = err.status === 404
    ? "This clip isn't there any more. The video it came from may have been deleted."
    : `The clip couldn't be opened: ${err.message}`;
  const backButton = el("button", { type: "button", class: "ap-btn ghost" }, "Back");
  backButton.addEventListener("click", router.back);
  container.append(el("div", { class: "ap-page" }, el("h1", { class: "ap-h1" }, "Clip editor"),
                      el("p", { class: "ap-problem", role: "alert" }, message), el("p", {}, backButton)));
}

export async function render(container, route) {
  let view;
  try {
    view = await api.get(`/api/editor/clips/${encodeURIComponent(route.ref)}`);
  } catch (err) {
    failedScreen(container, err);
    return undefined;
  }
  if (!view.version) {
    failedScreen(container, { status: 0, message: "it has no version to start from." });
    return undefined;
  }

  const S = {
    view,
    id: view.clip.id,
    base: view.version.spec,               // the version in use
    spec: clone(view.version.spec),        // what's on screen
    undo: [],
    redo: [],
    words: view.transcript.words || [],
    sents: [],
    sep: P.separator(view.transcript.language),
    ws: view.window.start,
    we: view.window.end,
    duration: view.source.duration,
    anchor: null,                          // the selection: anchor..focus, word indices
    focus: null,
    spans: [],
    length: 0,
    track: null,                           // the version's crop track (Fill, auto)
    music: { tracks: [], cleared: [] },
    rendering: null,                       // {id, version, progress}
    sent: null,                            // the spec last sent to render
    replace: false,                        // 8.6: replace uploaded YouTube videos
    widening: false,
    problems: [],
    warnings: [],
    noted: null,                           // the problems and warnings already shown, once drawn
    status: "",
  };
  S.sents = P.sentences(S.words);
  let stopped = false;
  const timers = new Set();
  const later = (fn, ms) => {
    const t = setTimeout(() => {
      timers.delete(t);
      if (!stopped) fn();
    }, ms);
    timers.add(t);
  };

  const kept = drafts.get(S.id);
  drafts.delete(S.id);
  let restored = false;
  if (kept && kept.base === view.version.id) {
    Object.assign(S, { spec: kept.spec, undo: kept.undo, redo: kept.redo });
    restored = true;
  }

  /* ── the frame ──────────────────────────────────────────────────────── */
  const backButton = el("button", { type: "button", class: "ap-btn ghost sm" }, svg("back"), "Back");
  backButton.addEventListener("click", router.back);
  const where = el("span", { class: "ap-ed-where" },
                   [view.clip.source_title, `clip ${view.clip.idx}`].filter(Boolean).join(" · "));
  const undoButton = el("button", { type: "button", class: "ap-btn ghost ap-ed-icon", "aria-label": "Undo",
                                    title: "Undo (Cmd+Z)" }, svg("undo"));
  const redoButton = el("button", { type: "button", class: "ap-btn ghost ap-ed-icon", "aria-label": "Redo",
                                    title: "Redo (Shift+Cmd+Z)" }, svg("redo"));
  const versions = el("details", { class: "ap-ed-versions" });
  const renderButton = el("button", { type: "button", class: "ap-btn", "aria-describedby": "ap-ed-render-why" });
  const renderWhy = el("span", { class: "ap-sr", id: "ap-ed-render-why" });
  const pending = el("span", { class: "ap-ed-pending", hidden: true }, "Edits not saved yet");
  const meter = el("div", { class: "ap-ed-meter", hidden: true });
  const top = el("div", { class: "ap-ed-top" }, backButton, where, el("span", { class: "ap-spacer" }),
                 undoButton, redoButton, versions, meter, pending, renderButton, renderWhy);

  const eyebrow = el("span", { class: "ap-ed-eyebrow" });
  const heading = el("h1", { class: "ap-ed-h1" }, view.clip.title);
  const kbd = text => el("kbd", { class: "ap-kbd" }, text);
  const helpLine = (...parts) => el("span", { class: "ap-ed-help-line" }, ...parts);
  const help = el("p", { class: "ap-ed-help", id: "ap-ed-help" },
                  helpLine(el("b", {}, "Trim:"), " drag the ", el("b", {}, "START"), " or ", el("b", {}, "END"),
                           " pin to a word, or press ", kbd("["), " or ", kbd("]"), " on a word."),
                  helpLine(el("b", {}, "Cut:"), " select words and press ", kbd("Delete"),
                           ". Press it again on cut words to bring them back."),
                  helpLine(el("b", {}, "Fix a subtitle:"), " double-click a word, or press ", kbd("Enter"), " on it."));
  const strip = el("div", { class: "ap-ed-strip" });
  const stripLabels = el("div", { class: "ap-ed-strip-lab" });
  const hint = el("div", { class: "ap-ed-hint" });
  const statusLine = el("p", { class: "ap-ed-status", role: "status" });
  const problemLine = el("p", { class: "ap-problem", role: "alert", hidden: true });
  const transcriptNote = el("div", { class: "ap-ed-tnote", hidden: true });
  const script = el("div", { class: "ap-ed-script", role: "group", "aria-label": "Transcript",
                             "aria-describedby": "ap-ed-help" });
  const paper = el("div", { class: "ap-ed-paper" }, eyebrow, heading, help, strip, stripLabels, hint, statusLine,
                   problemLine, transcriptNote, script);
  const doc = el("section", { class: "ap-ed-doc", "aria-label": "Script" }, paper);

  const preview = new P.Preview({ onTime: t => onTime(t), onPlay: playing => onPlay(playing) });
  const playButton = el("button", { type: "button", class: "ap-btn ap-ed-play", "aria-label": "Play" }, svg("play"));
  const timeNow = el("span", { class: "ap-ed-time" });
  const scrub = el("input", { type: "range", class: "ap-ed-scrub", min: "0", step: "0.1", value: "0",
                              "aria-label": "Position in the clip" });
  const controls = el("div", { class: "ap-ed-ctl" }, playButton, timeNow,
                      el("span", { class: "ap-ed-keys" }, el("kbd", { class: "ap-kbd" }, "Space"), " play"));
  const settings = el("div", { class: "ap-ed-insp" });
  const postsBox = el("div", { class: "ap-ed-posts" });
  const side = el("aside", { class: "ap-ed-side", "aria-label": "Preview and settings" },
                  preview.frame, controls, scrub, settings, postsBox);

  const root = el("div", { class: "ap-ed" }, top, el("div", { class: "ap-ed-body" }, doc, side));
  container.append(root);

  function showProblem(text) {
    problemLine.textContent = text || "";
    problemLine.hidden = !text;
  }

  function setStatus(text) {
    S.status = text || "";
    statusLine.textContent = S.status;
  }

  /* ── edits and undo (7.10) ──────────────────────────────────────────── */

  /** Apply `next` as one undoable edit. Edits with the same `group` within
      GROUP_MS make one undo step ("drag:" groups always do). */
  function commit(next, label, group = null) {
    const now = performance.now();
    const last = S.undo[S.undo.length - 1];
    const sticky = group && group.startsWith("drag:");
    if (group && last && last.group === group && (sticky || now - last.at < GROUP_MS)) {
      last.at = now;
    } else {
      S.undo.push({ spec: S.spec, label, group, at: now });
      if (S.undo.length > UNDO_MAX) S.undo.shift();
    }
    S.redo = [];
    S.spec = next;
    update();
  }

  function undo() {
    const entry = S.undo.pop();
    if (!entry) return;
    S.redo.push({ spec: S.spec, label: entry.label, group: null, at: 0 });
    S.spec = entry.spec;
    update();
    say(`Undid: ${entry.label}.`);
  }

  function redo() {
    const entry = S.redo.pop();
    if (!entry) return;
    S.undo.push({ spec: S.spec, label: entry.label, group: null, at: 0 });
    S.spec = entry.spec;
    update();
    say(`Redid: ${entry.label}.`);
  }

  const edited = () => !same(S.spec, S.base);
  const mid = w => (w.s + w.e) / 2;
  const insideClip = w => mid(w) >= S.spec.in && mid(w) <= S.spec.out;
  const inKeep = t => S.spans.some(([a, b]) => a <= t && t <= b);
  const firstStart = () => (S.spans.length ? S.spans[0][0] : S.spec.in);
  const lastEnd = () => (S.spans.length ? S.spans[S.spans.length - 1][1] : S.spec.out);

  /** Keep the cuts inside [in, out], as the server requires. */
  function clipCuts(spec) {
    spec.cuts = P.merge(spec.cuts)
      .map(([a, b]) => [Math.max(a, spec.in), Math.min(b, spec.out)])
      .filter(([a, b]) => b - a > 1e-6)
      .map(([a, b]) => [P.round3(a), P.round3(b)]);
    return spec;
  }

  function setIn(t, label, group) {
    t = P.round3(Math.min(Math.max(t, S.ws), S.spec.out - MIN_GAP_S));
    if (t === S.spec.in) return;
    const next = clone(S.spec);
    next.in = t;
    commit(clipCuts(next), label, group);
    preview.seek(firstStart());
    maybeWiden();
  }

  function setOut(t, label, group) {
    t = P.round3(Math.max(Math.min(t, S.we), S.spec.in + MIN_GAP_S));
    if (t === S.spec.out) return;
    const next = clone(S.spec);
    next.out = t;
    commit(clipCuts(next), label, group);
    preview.seek(Math.max(firstStart(), lastEnd() - 0.04));
    maybeWiden();
  }

  /** A pin set on a word: padded like a kept span (0.1 s, never more than
      half-way to the neighbouring word). */
  const startAt = k => {
    const w = S.words[k];
    const prev = S.words[k - 1];
    return Math.max(S.ws, w.s - P.PAD_S, prev ? (prev.e + w.s) / 2 : -Infinity);
  };
  const endAt = k => {
    const w = S.words[k];
    const next = S.words[k + 1];
    return Math.min(S.we, w.e + P.PAD_S, next ? (w.e + next.s) / 2 : Infinity);
  };

  function wordsInClip() {
    let first = -1;
    let last = -1;
    S.words.forEach((w, k) => {
      if (insideClip(w)) {
        if (first < 0) first = k;
        last = k;
      }
    });
    return [first, last];
  }

  function range() {
    if (S.anchor === null) return null;
    return [Math.min(S.anchor, S.focus), Math.max(S.anchor, S.focus)];
  }

  const isCut = w => P.merge(S.spec.cuts).some(([a, b]) => a <= mid(w) && mid(w) <= b);

  /** Delete on a selection: cut its words, or bring them back when they're all cut (7.3). */
  function toggleCut() {
    const sel = range();
    if (!sel) return;
    const chosen = S.words.slice(sel[0], sel[1] + 1).filter(insideClip);
    if (!chosen.length) {
      say("Those words are outside the clip. Move the START or END pin to include them.");
      return;
    }
    const first = chosen[0];
    const last = chosen[chosen.length - 1];
    const next = clone(S.spec);
    const n = plural(chosen.length, "word");
    if (chosen.every(isCut)) {
      next.cuts = subtract(P.merge(next.cuts), [[first.s, last.e]]).map(([a, b]) => [P.round3(a), P.round3(b)]);
      commit(clipCuts(next), `Restored ${n}`);
      say(`Restored ${n}.`);
    } else {
      next.cuts.push([Math.max(next.in, first.s), Math.min(next.out, last.e)]);
      commit(clipCuts(next), `Cut ${n}`);
      say(`Cut ${n}.`);
    }
  }

  /** Correct a word's caption (7.4); its timing stays. */
  function setWord(k, text) {
    const w = S.words[k];
    const key = String(w.i);
    const before = S.spec.captions.edits[key] ?? w.w;
    if (text === before) return;
    const next = clone(S.spec);
    if (text === w.w) delete next.captions.edits[key];
    else next.captions.edits[key] = text;
    commit(next, `Changed “${before}” to “${text}”`);
    say(`Changed “${before}” to “${text}”.`);
    if (inKeep(mid(w))) preview.seek(Math.max(w.s, firstStart()) + 0.001);
  }

  function setSpec(change, label, group) {
    const next = clone(S.spec);
    change(next);
    commit(next, label, group);
  }

  /* ── what follows from the spec ─────────────────────────────────────── */

  function hookText() {
    const h = S.spec.hook;
    return h.enabled ? P.stripEmoji(h.text).split(/\s+/).filter(Boolean).join(" ").toUpperCase() : "";
  }

  function targets() {
    const used = new Set(S.view.posts.map(p => p.platform));
    return used.size ? used : new Set(["youtube", "instagram"]);
  }

  /** Problems keep Render off; warnings don't (7.2, 7.9, 8.6). Mirrors media/editspec.validate. */
  function check() {
    const sp = S.spec;
    const lim = S.view.limits;
    const min = S.view.min_length;
    const len = S.length;
    const problems = [];
    const warnings = [];
    if (len < min - 1e-6) {
      const cutLength = (sp.out - sp.in) - P.keptDuration(P.merge(sp.cuts));
      problems.push(len < cutLength - 1e-6
        ? `After trimming pauses and filler the clip is ${len.toFixed(1)} s; clips must be at least ${min} s. ` +
          "Keep every pause, or make the clip longer."
        : `The clip is ${len.toFixed(1)} s; clips must be at least ${min} s.`);
    }
    if (sp.hook.enabled && !sp.hook.text.trim()) problems.push("Write the hook text, or turn the hook off.");
    if (sp.hook.text.trim().length > HOOK_MAX) problems.push(`The hook can be at most ${HOOK_MAX} characters.`);
    if (sp.cover.t !== null && sp.cover.t >= len) {
      problems.push("The cover frame is past the end of the clip. Use another frame, or let it be chosen when rendering.");
    }
    if (sp.music.track && !S.music.cleared.includes(sp.music.track)) {
      problems.push(`The track “${sp.music.track}” isn't marked as cleared for publishing.`);
    }
    const aim = targets();
    if (aim.has("youtube") && len > lim.youtube_max_s) {
      warnings.push({ code: "youtube_length",
                      text: `The clip is ${len.toFixed(1)} s; YouTube Shorts can be at most ${lim.youtube_max_s} s.` });
    }
    if (aim.has("instagram") && (len < lim.instagram_min_s || len > lim.instagram_max_s)) {
      warnings.push({ code: "instagram_length",
                      text: `The clip is ${len.toFixed(1)} s; Instagram Reels must be 3 s to 15 min long.` });
    }
    for (const note of S.view.notes || []) warnings.push({ code: note.code, text: note.message });
    S.problems = problems;
    S.warnings = warnings;
    // A problem or warning a change brings is announced, after what said the
    // change itself (live.js joins them). The ones there on opening are read
    // with the page.
    const notes = [...problems, ...warnings.map(w => w.text)];
    if (S.noted) {
      const fresh = notes.filter(text => !S.noted.has(text));
      if (fresh.length) queueMicrotask(() => say(fresh.join(" ")));
    }
    S.noted = new Set(notes);
  }

  function cropPercent(ct) {
    const { width: W, height: H } = S.view.source;
    if (!W || !H) return null;
    const cw = P.cropWidth(W, H);
    if (!cw) return null;
    let left = null;
    if (S.spec.crop.mode === "manual" && S.spec.crop.x !== null) {
      left = P.manualLeft(S.spec.crop.x, W, H);
    } else if (S.track && S.track.src_w) {
      const at = P.trackLeft(S.track, ct);
      if (at !== null) left = at * (W / S.track.src_w);
    }
    if (left === null) return 50;
    return Math.max(0, Math.min(100, (left / (W - cw)) * 100));
  }

  /** The crop centre now, as a fraction of the width (for dragging and keys). */
  function cropCentre() {
    const { width: W, height: H } = S.view.source;
    const cw = P.cropWidth(W, H);
    if (S.spec.crop.mode === "manual" && S.spec.crop.x !== null) return S.spec.crop.x;
    const p = cropPercent(preview.clipTime());
    return p === null ? 0.5 : ((p / 100) * (W - cw) + cw / 2) / W;
  }

  function fileUrl(name) {
    return name ? `/api/clip/${encodeURIComponent(S.view.clip.job_id)}/${encodeURIComponent(name)}` : null;
  }

  /** Recompute everything that follows from the spec and redraw. The 200 ms
      path (7.2): no server call, and the preview seeks synchronously. */
  function update() {
    const sp = S.spec;
    S.spans = P.keepSpans(S.words, sp.in, sp.out, sp.cuts, sp.auto_trim);
    S.length = P.keptDuration(S.spans);
    const kept = sp.captions.enabled ? P.keptWords(S.words, S.spans, sp.captions.edits) : [];
    const states = sp.captions.enabled ? P.captionStates(kept, S.sents, sp.captions.style, S.sep, P.measureText) : [];
    const hook = hookText();
    const timeline = P.captionTimeline(states, S.length, hook ? Math.min(sp.hook.duration, S.length) : 0);
    const files = S.view.version.files || {};
    preview.setModel({ spans: S.spans, states, timeline, hookText: hook, sep: S.sep, layout: sp.layout,
                       cropPercent, backdrop: fileUrl(files.cover || files.thumb) });
    check();
    drawStrip();
    drawScript();
    drawHint();
    drawSettings();
    drawTop();
    drawPosts();
  }

  /* ── the strip of the source window ─────────────────────────────────── */
  const ticks = el("canvas", { class: "ap-ed-ticks", "aria-hidden": "true" });
  const removedLayer = el("div", { class: "ap-ed-removed", "aria-hidden": "true" });
  const spanBox = el("div", { class: "ap-ed-span", "aria-hidden": "true" });
  const playhead = el("div", { class: "ap-ed-ph", "aria-hidden": "true" });
  const handleIn = el("span", { class: "ap-ed-h", role: "slider", tabindex: "0", "aria-label": "Clip start, fine trim",
                                "data-focus": "h-in" });
  const handleOut = el("span", { class: "ap-ed-h", role: "slider", tabindex: "0", "aria-label": "Clip end, fine trim",
                                 "data-focus": "h-out" });
  strip.append(ticks, removedLayer, spanBox, playhead, handleIn, handleOut);
  const pct = t => ((t - S.ws) / Math.max(1e-6, S.we - S.ws)) * 100;

  function drawTicks() {
    const width = strip.clientWidth;
    const height = strip.clientHeight;
    if (!width || !height) return;
    const ratio = window.devicePixelRatio || 1;
    ticks.width = Math.round(width * ratio);
    ticks.height = Math.round(height * ratio);
    const ctx = ticks.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#B2AA9E";
    for (const w of S.words) {             // one tick a word, taller for longer words
      if (w.e < S.ws || w.s > S.we) continue;
      const x = (pct(w.s) / 100) * width;
      const h = height * Math.min(0.85, 0.25 + 0.07 * [...w.w].length);
      ctx.fillRect(x, height - h, Math.max(1.5, ((w.e - w.s) / (S.we - S.ws)) * width), h);
    }
  }

  function drawStrip() {
    const sp = S.spec;
    spanBox.style.left = `${pct(sp.in)}%`;
    spanBox.style.width = `${pct(sp.out) - pct(sp.in)}%`;
    const cuts = P.merge(sp.cuts);
    const trimmed = subtract(subtract(P.baseSpans(sp.in, sp.out, []), cuts), S.spans);
    removedLayer.replaceChildren(
      ...cuts.map(([a, b]) => el("i", { class: "cut", style: `left:${pct(a)}%;width:${pct(b) - pct(a)}%` })),
      ...trimmed.map(([a, b]) => el("i", { class: "trim", style: `left:${pct(a)}%;width:${pct(b) - pct(a)}%` })));
    for (const [handle, t, lo, hi] of [[handleIn, sp.in, S.ws, sp.out - MIN_GAP_S],
                                       [handleOut, sp.out, sp.in + MIN_GAP_S, S.we]]) {
      handle.style.left = `${pct(t)}%`;
      handle.setAttribute("aria-valuemin", lo.toFixed(1));
      handle.setAttribute("aria-valuemax", hi.toFixed(1));
      handle.setAttribute("aria-valuenow", t.toFixed(1));
      handle.setAttribute("aria-valuetext", clock(t));
    }
    stripLabels.replaceChildren(
      el("span", {}, clock(S.ws, { tenths: false })),
      el("span", {}, `start ${clock(sp.in)} · end ${clock(sp.out)} · the handles move 0.1 s with `,
         el("kbd", { class: "ap-kbd" }, "←"), el("kbd", { class: "ap-kbd" }, "→"), ", 1 s with ",
         el("kbd", { class: "ap-kbd" }, "Shift"), S.widening ? " · loading more of the video…" : ""),
      el("span", {}, clock(S.we, { tenths: false })));
  }

  /* ── the transcript ─────────────────────────────────────────────────── */
  let wordEls = [];
  const pinIn = el("span", { class: "ap-pin", role: "slider", tabindex: "0", "aria-label": "Clip start pin",
                             "data-pin": "in" }, "▸ START");
  const pinOut = el("span", { class: "ap-pin end", role: "slider", tabindex: "0", "aria-label": "Clip end pin",
                              "data-pin": "out" }, "END ◂");

  function buildScript() {
    wordEls = [];
    const paragraphs = S.sents.map(sent => {
      const text = el("span", { class: "ap-ed-tx" });
      for (let k = sent.w0; k < sent.w1; k += 1) {
        const button = el("button", { type: "button", class: "ap-w", tabindex: "-1", "data-k": String(k) },
                          el("span", { class: "ap-w-t" }), el("span", { class: "ap-sr" }));
        wordEls[k] = button;
        text.append(button);
        if (S.sep && k + 1 < sent.w1) text.append(S.sep);
      }
      return el("p", { class: "ap-ed-p" }, el("span", { class: "ap-ed-ts" }, clock(sent.s, { tenths: false })), text);
    });
    script.replaceChildren(...paragraphs);
    if (S.focus === null) {
      const [first] = wordsInClip();
      S.anchor = S.focus = first >= 0 ? first : (S.words.length ? 0 : null);
    }
  }

  function drawScript() {
    const sp = S.spec;
    const sel = range();
    const edits = sp.captions.edits;
    const active = document.activeElement;
    const keepFocus = [pinIn, pinOut].includes(active) ? active : null;
    script.querySelectorAll(".ap-gap").forEach(n => n.remove());
    let fillers = 0;
    let pauses = 0;
    S.words.forEach((w, k) => {
      const node = wordEls[k];
      if (!node) return;
      const out = !insideClip(w);
      const cut = !out && isCut(w);
      const trim = !out && !cut && sp.auto_trim && !inKeep(mid(w));
      const key = String(w.i);
      const fixed = Object.prototype.hasOwnProperty.call(edits, key);
      const chosen = sel !== null && k >= sel[0] && k <= sel[1];
      if (trim) fillers += 1;
      node.className = `ap-w${out ? " out" : ""}${cut ? " cut" : ""}${trim ? " trim" : ""}${fixed ? " fixed" : ""}` +
                       `${chosen ? " sel" : ""}`;
      node.tabIndex = k === S.focus ? 0 : -1;
      node.setAttribute("aria-pressed", String(chosen));
      node.firstChild.textContent = fixed ? edits[key] : w.w;
      node.title = fixed ? `The transcript says “${w.w}”` : "";
      node.lastChild.textContent = cut ? " (cut)" : trim ? " (trimmed as filler)" : out ? " (outside the clip)"
        : fixed ? ` (corrected; the transcript says ${w.w})` : "";
      // A long pause between two kept words of the clip (§4.5).
      const next = S.words[k + 1];
      if (!out && !cut && next && insideClip(next) && !isCut(next) && next.s - w.e > P.GAP_S) {
        if (sp.auto_trim) pauses += 1;
        node.after(el("span", { class: `ap-gap${sp.auto_trim ? "" : " kept"}` },
                      `${(next.s - w.e).toFixed(1)} s pause`, sr(sp.auto_trim ? ", trimmed" : ", kept")));
      }
    });
    const [first, last] = wordsInClip();
    if (first >= 0) {
      wordEls[first].before(pinIn);
      wordEls[last].after(pinOut);
    } else {
      pinIn.remove();
      pinOut.remove();
    }
    for (const [pin, t, label] of [[pinIn, sp.in, "Clip start"], [pinOut, sp.out, "Clip end"]]) {
      pin.setAttribute("aria-valuetext", `${label} at ${clock(t)}, ${first >= 0 ? `“${S.words[pin === pinIn ? first : last].w}”` : "no word"}`);
      pin.setAttribute("aria-valuenow", t.toFixed(1));
      pin.setAttribute("aria-valuemin", S.ws.toFixed(1));
      pin.setAttribute("aria-valuemax", S.we.toFixed(1));
    }
    if (keepFocus && document.activeElement !== keepFocus) keepFocus.focus({ preventScroll: true });
    script.toggleAttribute("data-ap-escape", sel !== null && sel[0] !== sel[1]);
    S.counts = { fillers, pauses };
  }

  /* ── the hint, the warnings and the problems ────────────────────────── */
  const trimToggle = el("button", { type: "button", class: "ap-linkbtn" });
  trimToggle.addEventListener("click", () => {
    const on = !S.spec.auto_trim;
    setSpec(s => { s.auto_trim = on; }, on ? "Trimmed pauses and filler" : "Kept every pause");
    say(on ? "Pauses and filler words are trimmed again." : "Every pause and filler word is kept.");
  });

  function drawHint() {
    const sp = S.spec;
    const { fillers = 0, pauses = 0 } = S.counts || {};
    const len = S.length;
    const lim = S.view.limits;
    const fitsYT = len <= lim.youtube_max_s;
    const fitsIG = len >= lim.instagram_min_s && len <= lim.instagram_max_s;
    const fits = fitsYT && fitsIG ? "fits Shorts and Reels"
      : [fitsYT ? "fits Shorts" : "too long for Shorts", fitsIG ? "fits Reels" : len < lim.instagram_min_s
        ? "too short for Reels" : "too long for Reels"].join(", ");
    eyebrow.textContent = `Clip ${S.view.clip.idx} · ${len.toFixed(1)} s · ${fits}`;
    trimToggle.textContent = sp.auto_trim ? "Keep every pause" : "Trim pauses and filler";
    const summary = sp.auto_trim
      ? el("span", {}, el("b", {}, fillers ? plural(fillers, "filler word") : "No filler words"), " and ",
           el("b", {}, plural(pauses, "long pause")), " are trimmed automatically.")
      : el("span", {}, "Pauses and filler words are kept.");
    const pills = S.warnings.map(w => el("span", { class: "ap-pill warn", "data-code": w.code }, svg("alert"), w.text));
    const errors = S.problems.map(p => el("span", { class: "ap-pill err", "data-code": "problem" }, svg("alert"), p));
    hint.replaceChildren(el("div", { class: "ap-ed-hint-row" }, svg("spark"), summary, trimToggle),
                         ...(pills.length || errors.length ? [el("div", { class: "ap-ed-pills" }, ...errors, ...pills)] : []));
  }

  /* ── settings: captions, hook, layout, music, cover (7.5) ───────────── */
  function toggleSwitch(label) {
    return el("button", { type: "button", class: "ap-ed-sw", role: "switch", "aria-checked": "false", "aria-label": label },
              el("span", { "aria-hidden": "true" }));
  }

  const capSwitch = toggleSwitch("Subtitles");
  const capStyle = el("select", { "aria-label": "Subtitle style" },
                      ...STYLES.map(([value, label]) => el("option", { value }, label)));
  const hookSwitch = toggleSwitch("Hook");
  const hookInput = el("input", { type: "text", maxlength: String(HOOK_MAX), "aria-label": "Hook text",
                                  "aria-describedby": "ap-ed-hook-count", autocomplete: "off" });
  const hookCount = el("span", { class: "ap-ed-count", id: "ap-ed-hook-count" });
  const layoutSeg = el("div", { class: "ap-seg", role: "group", "aria-label": "Layout" });
  const layoutButtons = [["fill", "Fill"], ["fit", "Fit"]].map(([value, label]) => {
    const button = el("button", { type: "button", "data-layout": value, "aria-pressed": "false" }, label);
    button.addEventListener("click", () => {
      if (S.spec.layout !== value) setSpec(s => { s.layout = value; }, `Switched to ${label}`);
    });
    layoutSeg.append(button);
    return button;
  });
  const cropHint = el("p", { class: "ap-ed-sub" });
  const cropAuto = el("button", { type: "button", class: "ap-linkbtn" }, "Automatic crop");
  const musicSelect = el("select", { "aria-label": "Background music" });
  const volume = el("input", { type: "range", min: "0", max: "100", step: "1", "aria-label": "Music volume" });
  const volumeOut = el("output", { class: "ap-ed-count" });
  const coverValue = el("span", { class: "ap-ed-val" });
  const coverUse = el("button", { type: "button", class: "ap-linkbtn" }, "Use this frame");
  const coverAuto = el("button", { type: "button", class: "ap-linkbtn" }, "Choose when rendering");

  const row = (name, label, ...parts) => el("div", { class: "ap-ed-row", "data-row": name },
                                            el("span", { class: "ap-ed-row-lab" }, label), el("div", { class: "ap-ed-row-v" }, ...parts));
  settings.append(
    row("captions", "Subtitles", capStyle, capSwitch),
    row("hook", "Hook", el("span", { class: "ap-ed-hook" }, hookInput, hookCount), hookSwitch),
    row("layout", "Layout", layoutSeg, el("div", { class: "ap-ed-row-more" }, cropHint, cropAuto)),
    row("music", "Music", musicSelect, el("span", { class: "ap-ed-vol" }, volume, volumeOut)),
    row("cover", "Cover", coverValue, coverUse, coverAuto));

  capSwitch.addEventListener("click", () => {
    const on = !S.spec.captions.enabled;
    setSpec(s => { s.captions.enabled = on; }, on ? "Turned subtitles on" : "Turned subtitles off");
  });
  capStyle.addEventListener("change", () => {
    const label = STYLES.find(([v]) => v === capStyle.value)[1];
    setSpec(s => { s.captions.style = capStyle.value; }, `Changed the subtitle style to ${label}`);
  });
  hookSwitch.addEventListener("click", () => {
    const on = !S.spec.hook.enabled;
    setSpec(s => { s.hook.enabled = on; }, on ? "Turned the hook on" : "Turned the hook off");
  });
  let hookTyping = 0;
  hookInput.addEventListener("focus", () => { hookTyping += 1; });
  hookInput.addEventListener("input", () => {
    setSpec(s => { s.hook.text = hookInput.value; }, "Changed the hook text", `hook:${hookTyping}`);
  });
  cropAuto.addEventListener("click", () => setSpec(s => { s.crop = { mode: "auto", x: null }; }, "Went back to the automatic crop"));
  musicSelect.addEventListener("change", () => {
    const track = musicSelect.value || null;
    setSpec(s => { s.music.track = track; }, track ? `Chose the music “${track}”` : "Removed the music");
  });
  volume.addEventListener("input", () => {
    setSpec(s => { s.music.volume = Number(volume.value) / 100; }, "Changed the music volume", "music-volume");
  });
  coverUse.addEventListener("click", () => {
    const t = Math.min(Math.round(preview.clipTime() * 10) / 10, Math.max(0, S.length - 0.1));
    setSpec(s => { s.cover.t = t; }, `Set the cover frame at ${clock(t)}`);
    say(`The cover is the frame at ${clock(t)}.`);
  });
  coverAuto.addEventListener("click", () => setSpec(s => { s.cover.t = null; }, "Left the cover to be chosen when rendering"));

  function setSwitch(button, on) {
    button.setAttribute("aria-checked", String(on));
    button.classList.toggle("on", on);
  }

  function drawSettings() {
    const sp = S.spec;
    setSwitch(capSwitch, sp.captions.enabled);
    capStyle.value = sp.captions.style;
    capStyle.disabled = !sp.captions.enabled;
    setSwitch(hookSwitch, sp.hook.enabled);
    if (document.activeElement !== hookInput && hookInput.value !== sp.hook.text) hookInput.value = sp.hook.text;
    hookInput.disabled = !sp.hook.enabled;
    hookCount.textContent = `${[...sp.hook.text].length} / ${HOOK_MAX}`;
    for (const button of layoutButtons) button.setAttribute("aria-pressed", String(button.dataset.layout === sp.layout));
    const croppable = sp.layout === "fill" && P.cropWidth(S.view.source.width, S.view.source.height);
    cropHint.textContent = sp.layout === "fit" ? "The whole frame, on a blurred background."
      : croppable ? (sp.crop.mode === "manual" ? "You placed the crop. Drag the preview sideways to move it."
        : "The crop follows the speaker. Drag the preview sideways to place it yourself.")
        : "The video is already vertical, so there's nothing to crop.";
    cropAuto.hidden = !(sp.layout === "fill" && sp.crop.mode === "manual");
    if (croppable) {
      preview.frame.setAttribute("role", "slider");
      preview.frame.setAttribute("tabindex", "0");
      preview.frame.setAttribute("aria-label", "Crop position; drag sideways or use the arrow keys");
      const x = Math.round(cropCentre() * 100);
      preview.frame.setAttribute("aria-valuemin", "0");
      preview.frame.setAttribute("aria-valuemax", "100");
      preview.frame.setAttribute("aria-valuenow", String(x));
      preview.frame.setAttribute("aria-valuetext", `${x}% from the left${sp.crop.mode === "auto" ? ", automatic" : ""}`);
    } else {
      for (const name of ["role", "tabindex", "aria-label", "aria-valuemin", "aria-valuemax", "aria-valuenow", "aria-valuetext"]) {
        preview.frame.removeAttribute(name);
      }
    }
    const options = [el("option", { value: "" }, "None")];
    for (const name of S.music.cleared) options.push(el("option", { value: name }, name));
    if (sp.music.track && !S.music.cleared.includes(sp.music.track)) {
      options.push(el("option", { value: sp.music.track }, `${sp.music.track} (not cleared)`));
    }
    musicSelect.replaceChildren(...options);
    musicSelect.value = sp.music.track || "";
    volume.value = String(Math.round(sp.music.volume * 100));
    volume.disabled = !sp.music.track;
    volumeOut.textContent = `${volume.value}%`;
    const chosen = (S.view.version.cover || {}).t;
    coverValue.textContent = sp.cover.t !== null ? `${clock(sp.cover.t)} · you chose it`
      : typeof chosen === "number" ? `Chosen when rendering (now ${clock(chosen)})` : "Chosen when rendering";
    coverAuto.hidden = sp.cover.t === null;
  }

  /* ── the clip's posts (7.9, 8.6) ─────────────────────────────────────── */
  const replaceBox = el("input", { type: "checkbox", id: "ap-ed-replace" });
  replaceBox.addEventListener("change", () => {
    S.replace = replaceBox.checked;
    drawPosts();
  });

  function postLine(post) {
    const name = PLATFORM[post.platform] || post.platform;
    if (PUBLISHED.has(post.status)) return `${name} post is published and won't change.`;
    if (post.status === "uploaded") return `${name} video is uploaded and not public yet.`;
    if (post.status === "publishing" || post.status === "retrying") return `${name} post is being published and keeps the current version.`;
    if (post.status === "failed") return `${name} post failed and won't change.`;
    if (post.status === "rejected" || post.status === "cancelled") return null;
    return `${name} post isn't out yet and will use the new version.`;
  }

  function drawPosts() {
    const lines = S.view.posts.map(p => [p, postLine(p)]).filter(([, text]) => text);
    const uploaded = S.view.posts.filter(p => p.status === "uploaded" && p.platform === "youtube").length;
    const next = nextVersion();
    const parts = lines.map(([p, text]) => el("p", { class: "ap-ed-post", "data-status": p.status }, text));
    if (!lines.length) parts.push(el("p", { class: "ap-ed-post" }, "This clip has no posts yet."));
    if (uploaded) {
      replaceBox.checked = S.replace;
      parts.push(el("label", { class: "ap-check-row ap-ed-replace", for: replaceBox.id }, replaceBox,
                    el("span", {}, uploaded === 1 ? `Replace the uploaded YouTube video with version ${next}`
                                                  : `Replace the ${uploaded} uploaded YouTube videos with version ${next}`)));
    }
    postsBox.replaceChildren(el("h2", { class: "ap-ed-sub-h" }, "Posts of this clip"), ...parts);
  }

  /* ── the top bar ─────────────────────────────────────────────────────── */
  function nextVersion() {
    return Math.max(0, ...S.view.versions.map(v => v.version)) + 1;
  }

  function drawTop() {
    undoButton.disabled = !S.undo.length;
    redoButton.disabled = !S.redo.length;
    undoButton.title = S.undo.length ? `Undo: ${S.undo[S.undo.length - 1].label} (Cmd+Z)` : "Nothing to undo";
    redoButton.title = S.redo.length ? `Redo: ${S.redo[S.redo.length - 1].label} (Shift+Cmd+Z)` : "Nothing to redo";
    const current = S.view.version;
    versions.replaceChildren(
      el("summary", {}, `Version ${current.version} · in use`),
      el("ul", {}, ...S.view.versions.map(v => el("li", { "data-status": v.status },
        `Version ${v.version}`, v.id === current.id ? " · in use" : v.status === "failed" ? ` · failed: ${v.error || "unknown error"}`
          : v.status === "rendering" ? " · rendering" : ""))));
    const next = nextVersion();
    const busy = S.rendering !== null;
    renderButton.textContent = `Save as version ${busy ? S.rendering.version : next}`;
    const why = busy ? `Version ${S.rendering.version} is rendering.`
      : S.view.transcript.state === "preparing" ? "The word timings are still being prepared."
        : S.problems.length ? S.problems.join(" ")
          : !edited() ? `Nothing to save: no changes since version ${current.version}.` : "";
    renderButton.disabled = Boolean(why);
    // Edits aren't saved anywhere but here until a render: say so, so Back
    // isn't taken for saving.
    pending.hidden = busy || !edited();
    renderWhy.textContent = why || `Not saved yet: the clip's posts keep version ${current.version} until you save.`;
    renderButton.title = why;
    meter.hidden = !busy;
    if (busy) {
      const value = Math.round((S.rendering.progress || 0) * 100);
      meter.replaceChildren(
        el("div", { class: "ap-ed-meter-track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
                    "aria-valuenow": String(value), "aria-label": `Rendering version ${S.rendering.version}` },
           el("i", { style: `width:${value}%` })),
        el("span", {}, `Rendering version ${S.rendering.version} · ${value}%`));
    }
  }

  /* ── playback ────────────────────────────────────────────────────────── */
  let nowWord = null;
  let scrubbing = false;

  function onTime(st) {
    const ct = P.toClipTime(S.spans, st);
    timeNow.replaceChildren(clock(ct), el("span", {}, ` / ${clock(S.length)}`));
    scrub.max = String(Math.max(0.1, Math.round(S.length * 10) / 10));
    if (!scrubbing) scrub.value = String(Math.round(ct * 10) / 10);
    scrub.setAttribute("aria-valuetext", `${clock(ct)} of ${clock(S.length)}`);
    playhead.style.left = `${pct(st)}%`;
    const k = S.words.findIndex(w => w.s <= st && st < w.e);
    const node = k >= 0 ? wordEls[k] : null;
    if (node !== nowWord) {
      if (nowWord) nowWord.classList.remove("now");
      if (node) node.classList.add("now");
      nowWord = node;
    }
  }

  function onPlay(playing) {
    playButton.replaceChildren(svg(playing ? "pause" : "play"));
    playButton.setAttribute("aria-label", playing ? "Pause" : "Play");
  }

  playButton.addEventListener("click", () => preview.toggle());
  scrub.addEventListener("input", () => {
    scrubbing = true;
    preview.seek(P.toSourceTime(S.spans, Number(scrub.value)));
  });
  scrub.addEventListener("change", () => { scrubbing = false; });

  /* ── the words: select, cut, correct ────────────────────────────────── */
  function focusWord(k, extend = false) {
    if (k < 0 || k >= S.words.length) return;
    if (!extend || S.anchor === null) S.anchor = k;
    S.focus = k;
    drawScript();
    wordEls[k].focus({ preventScroll: false });
  }

  function editWord(k) {
    const button = wordEls[k];
    const w = S.words[k];
    const before = S.spec.captions.edits[String(w.i)] ?? w.w;
    const input = el("input", { type: "text", class: "ap-w-edit", maxlength: String(EDIT_MAX), spellcheck: "false",
                                "aria-label": `Subtitle for “${w.w}”; Enter to save, Escape to cancel` });
    input.value = before;
    input.style.width = `${Math.max(4, [...before].length + 2)}ch`;
    button.hidden = true;
    button.after(input);
    input.focus();
    input.select();
    let done = false;
    const finish = save => {
      if (done) return;
      done = true;
      const text = input.value.split(/\s+/).filter(Boolean).join(" ");
      input.remove();
      button.hidden = false;
      button.focus({ preventScroll: true });
      if (!save) return;
      if (!text) say("A word can't be empty. To remove it, cut it instead.");
      else setWord(k, text);
    };
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        finish(true);
      } else if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      }
      event.stopPropagation();
    });
    input.addEventListener("blur", () => finish(true));
  }

  const wordIndex = target => {
    const node = target && target.closest ? target.closest(".ap-w") : null;
    return node ? Number(node.dataset.k) : null;
  };

  script.addEventListener("click", event => {
    const k = wordIndex(event.target);
    if (k === null || event.detail === 0) return;          // keys are handled below
    focusWord(k, event.shiftKey);
    const w = S.words[k];
    if (!event.shiftKey && inKeep(mid(w))) preview.seek(Math.max(w.s, firstStart()));
  });
  script.addEventListener("dblclick", event => {
    const k = wordIndex(event.target);
    if (k !== null) editWord(k);
  });
  script.addEventListener("keydown", event => {
    const pin = event.target.closest && event.target.closest(".ap-pin");
    if (pin) return pinKey(event, pin.dataset.pin);
    const k = wordIndex(event.target);
    if (k === null || event.metaKey || event.ctrlKey || event.altKey) return;
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -1, ArrowDown: 1 }[event.key];
    if (step) {
      event.preventDefault();
      focusWord(Math.max(0, Math.min(S.words.length - 1, k + step)), event.shiftKey);
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      focusWord(event.key === "Home" ? 0 : S.words.length - 1, event.shiftKey);
    } else if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      toggleCut();
    } else if (event.key === "Enter" || event.key === "F2") {
      event.preventDefault();
      editWord(k);
    } else if (event.key === "[") {
      event.preventDefault();
      setIn(startAt(k), "Moved the start");
      say(`The clip starts at “${S.words[k].w}”.`);
    } else if (event.key === "]") {
      event.preventDefault();
      setOut(endAt(k), "Moved the end");
      say(`The clip ends at “${S.words[k].w}”.`);
    } else if (event.key === "Escape" && S.anchor !== S.focus) {
      event.preventDefault();
      focusWord(k);
    }
    return undefined;
  });

  /* ── the pins: a word at a time ─────────────────────────────────────── */
  function pinKey(event, which) {
    const [first, last] = wordsInClip();
    if (first < 0) return;
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -1, ArrowDown: 1 }[event.key];
    let k = null;
    if (step) k = (which === "in" ? first : last) + step * (event.shiftKey ? 5 : 1);
    else if (event.key === "Home") k = 0;
    else if (event.key === "End") k = S.words.length - 1;
    if (k === null) return;
    event.preventDefault();
    k = Math.max(0, Math.min(S.words.length - 1, k));
    if (which === "in") setIn(startAt(Math.min(k, last)), "Moved the start", "pin-in");
    else setOut(endAt(Math.max(k, first)), "Moved the end", "pin-out");
  }

  for (const pin of [pinIn, pinOut]) {
    pin.addEventListener("pointerdown", event => {
      event.preventDefault();
      pin.setPointerCapture(event.pointerId);
      pin.classList.add("drag");
      let target = null;
      const move = e => {
        const hit = document.elementFromPoint(e.clientX, e.clientY);
        const k = wordIndex(hit);
        if (target !== null && wordEls[target]) wordEls[target].classList.remove("drop");
        target = k;
        if (k !== null) wordEls[k].classList.add("drop");
      };
      const up = () => {
        pin.removeEventListener("pointermove", move);
        pin.classList.remove("drag");
        if (target === null) return;
        wordEls[target].classList.remove("drop");
        const [first, last] = wordsInClip();
        if (pin === pinIn) setIn(startAt(last >= 0 ? Math.min(target, last) : target), "Moved the start");
        else setOut(endAt(first >= 0 ? Math.max(target, first) : target), "Moved the end");
        pin.focus({ preventScroll: true });
      };
      pin.addEventListener("pointermove", move);
      pin.addEventListener("pointerup", up, { once: true });
      pin.addEventListener("pointercancel", up, { once: true });
    });
  }

  /* ── the strip: fine trims (N5.1) and seeking ───────────────────────── */
  function handleKey(event, which) {
    const sp = S.spec;
    const big = event.shiftKey ? 1 : 0.1;
    const delta = { ArrowLeft: -big, ArrowDown: -big, ArrowRight: big, ArrowUp: big, PageDown: -1, PageUp: 1 }[event.key];
    let t = null;
    const now = which === "in" ? sp.in : sp.out;
    if (delta !== undefined) t = now + delta;
    else if (event.key === "Home") t = which === "in" ? S.ws : sp.in + MIN_GAP_S;
    else if (event.key === "End") t = which === "in" ? sp.out - MIN_GAP_S : S.we;
    if (t === null) return;
    event.preventDefault();
    if (which === "in") setIn(t, "Moved the start", "handle-in");
    else setOut(t, "Moved the end", "handle-out");
  }

  handleIn.addEventListener("keydown", event => handleKey(event, "in"));
  handleOut.addEventListener("keydown", event => handleKey(event, "out"));
  const timeAt = clientX => {
    const box = strip.getBoundingClientRect();
    return S.ws + ((clientX - box.left) / Math.max(1, box.width)) * (S.we - S.ws);
  };
  let dragCount = 0;
  for (const [handle, which] of [[handleIn, "in"], [handleOut, "out"]]) {
    handle.addEventListener("pointerdown", event => {
      event.preventDefault();
      event.stopPropagation();
      handle.setPointerCapture(event.pointerId);
      handle.focus({ preventScroll: true });
      const group = `drag:${which}:${(dragCount += 1)}`;
      const move = e => {
        const t = timeAt(e.clientX);
        if (which === "in") setIn(t, "Moved the start", group);
        else setOut(t, "Moved the end", group);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", () => handle.removeEventListener("pointermove", move), { once: true });
      handle.addEventListener("pointercancel", () => handle.removeEventListener("pointermove", move), { once: true });
    });
  }
  strip.addEventListener("pointerdown", event => {
    if (event.target === handleIn || event.target === handleOut) return;
    const t = Math.min(Math.max(timeAt(event.clientX), firstStart()), lastEnd());
    preview.seek(t);
  });

  /* ── the crop: drag the preview sideways, or arrow keys (7.5) ───────── */
  function setCrop(x, group, label = "Moved the crop") {
    const { width: W, height: H } = S.view.source;
    const cw = P.cropWidth(W, H);
    if (!cw) return;
    const half = cw / 2 / W;
    x = Math.round(Math.min(1 - half, Math.max(half, x)) * 1000) / 1000;
    if (S.spec.crop.mode === "manual" && S.spec.crop.x === x) return;
    setSpec(s => { s.crop = { mode: "manual", x }; }, label, group);
  }

  preview.frame.addEventListener("pointerdown", event => {
    if (S.spec.layout !== "fill" || !P.cropWidth(S.view.source.width, S.view.source.height)) {
      preview.toggle();
      return;
    }
    const { width: W, height: H } = S.view.source;
    const cw = P.cropWidth(W, H);
    const start = { x: event.clientX, centre: cropCentre() };
    const frameWidth = preview.frame.getBoundingClientRect().width;
    const group = `drag:crop:${(dragCount += 1)}`;
    let moved = false;
    preview.frame.setPointerCapture(event.pointerId);
    const move = e => {
      const dx = e.clientX - start.x;
      if (Math.abs(dx) < 3 && !moved) return;
      moved = true;
      setCrop(start.centre - (dx * cw) / (frameWidth * W), group);
    };
    const up = () => {
      preview.frame.removeEventListener("pointermove", move);
      if (!moved) preview.toggle();
    };
    preview.frame.addEventListener("pointermove", move);
    preview.frame.addEventListener("pointerup", up, { once: true });
    preview.frame.addEventListener("pointercancel", up, { once: true });
  });
  preview.frame.addEventListener("keydown", event => {
    if (preview.frame.getAttribute("role") !== "slider") return;
    const step = event.shiftKey ? 0.1 : 0.01;
    const delta = { ArrowLeft: -step, ArrowDown: -step, ArrowRight: step, ArrowUp: step }[event.key];
    if (delta !== undefined) {
      event.preventDefault();
      setCrop(cropCentre() + delta, "crop-keys");
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      setCrop(event.key === "Home" ? 0 : 1, null);
    }
  });

  /* ── keys anywhere in the editor ────────────────────────────────────── */
  function onKey(event) {
    if (!root.isConnected || document.querySelector("dialog[open]")) return;
    const t = event.target;
    const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    const mod = event.metaKey || event.ctrlKey;
    const key = event.key.toLowerCase();
    if (mod && !event.altKey && (key === "z" || key === "y")) {
      if (typing) return;                       // a text field undoes its own typing
      event.preventDefault();
      if (key === "y" || event.shiftKey) redo();
      else undo();
      return;
    }
    if (event.key === " " && !mod && !typing) {
      const control = t && t.closest ? t.closest("button, summary, a, [role=switch]") : null;
      if (control && !control.classList.contains("ap-w")) return;      // Space presses that control
      event.preventDefault();
      preview.toggle();
    }
  }

  undoButton.addEventListener("click", undo);
  redoButton.addEventListener("click", redo);
  document.addEventListener("keydown", onKey);

  /* ── rendering (7.7-7.9, 8.6) ────────────────────────────────────────── */
  const PROBLEM_FIELDS = { length: "Length", out: "End", in: "Start", "hook.text": "Hook", "music.track": "Music",
                           "cover.t": "Cover", "crop.x": "Crop" };

  async function startRender() {
    if (renderButton.disabled) return;
    const next = nextVersion();
    const uploaded = S.view.posts.filter(p => p.status === "uploaded" && p.platform === "youtube").length;
    const notes = (S.view.notes || []).filter(note => note.code !== "uploaded").map(note => note.message);
    if (notes.length || uploaded) {
      // Say how many posts the new version reaches, and offer the replace
      // choice here as well, where its effect is spelled out (7.9, 8.6).
      const box = el("input", { type: "checkbox", id: "ap-ed-replace-confirm" });
      box.checked = S.replace;
      const lead = el("p", {});
      const sayReach = () => {
        const reach = (S.view.switching || 0) + (box.checked ? uploaded : 0);
        lead.textContent = reach ? `${reach === 1 ? "1 of the clip's posts" : `${reach} of the clip's posts`} ` +
                                   `will use version ${next}.`
          : `None of the clip's posts will use version ${next}.`;
      };
      sayReach();
      box.addEventListener("change", sayReach);
      const content = [lead];
      if (notes.length) content.push(el("ul", { class: "ap-ed-notes" }, ...notes.map(text => el("li", {}, text))));
      if (uploaded) {
        content.push(el("label", { class: "ap-check", for: box.id }, box,
                        el("span", {}, uploaded === 1 ? `Replace the uploaded YouTube video with version ${next}`
                                                      : `Replace the ${uploaded} uploaded YouTube videos with version ${next}`)));
      }
      const ok = await modal({ title: `Save as version ${next}?`, confirm: `Save as version ${next}`, content });
      if (!ok) return;
      if (S.replace !== box.checked) {
        S.replace = box.checked;
        drawPosts();
      }
    }
    showProblem("");
    try {
      const sent = clone(S.spec);
      const reply = await api.post(`/api/editor/clips/${encodeURIComponent(S.id)}/render`,
                                   { spec: sent, confirm_replace: S.replace });
      S.sent = sent;
      say(`Rendering version ${reply.version}.`);
      setStatus("");
      follow(reply.version_id, reply.version);
    } catch (err) {
      if (err.status === 422 && err.problems.length) {
        showProblem(err.problems.map(p => `${PROBLEM_FIELDS[p.field] || "Edit"}: ${p.message}`).join(" "));
      } else {
        showProblem(`Couldn't save version ${next}: ${err.message}`);
      }
    }
  }

  function follow(versionId, version) {
    S.rendering = { id: versionId, version, progress: 0 };
    drawTop();
    const tick = async () => {
      let reply;
      try {
        reply = await api.get(`/api/editor/renders/${encodeURIComponent(versionId)}`);
      } catch {
        later(tick, POLL_MS);
        return;
      }
      if (stopped) return;
      if (reply.status === "rendering") {
        S.rendering.progress = reply.progress || 0;
        drawTop();
        later(tick, POLL_MS);
        return;
      }
      S.rendering = null;
      const before = S.view.version.version;
      await reload();
      if (reply.status === "ready") {
        setStatus(`Version ${version} is ready and in use. The clip's posts that aren't out yet use it now.`);
        say(`Version ${version} is ready and in use.`);
      } else {
        const reason = (reply.error || "unknown error").trim();
        const why = /[.!?]$/.test(reason) ? reason : `${reason}.`;
        showProblem(`Couldn't render version ${version}: ${why} Version ${before} stays in use.`);
        say(`Version ${version} failed. Version ${before} stays in use.`);
      }
    };
    later(tick, 400);
  }

  /** Read the clip again: a new version, its posts and notes. */
  async function reload() {
    try {
      const fresh = await api.get(`/api/editor/clips/${encodeURIComponent(S.id)}`);
      if (stopped) return;
      S.view = fresh;
      S.base = fresh.version.spec;
      // The server tidies what it renders (cuts merged, text on one line):
      // when nothing changed since, show its version.
      if (S.sent && same(S.spec, S.sent) && fresh.version.status === "ready") S.spec = clone(fresh.version.spec);
      S.sent = null;
      loadTrack();
      update();
    } catch (err) {
      showProblem(`Couldn't read the clip again: ${err.message}`);
    }
  }

  renderButton.addEventListener("click", startRender);

  /* ── the proxy, a wider one near the edge (§10.2), and the transcript ─ */
  async function requestProxy(body, onReady) {
    for (;;) {
      let reply;
      try {
        reply = await api.post(`/api/editor/clips/${encodeURIComponent(S.id)}/proxy`, body);
      } catch (err) {
        if (!stopped) preview.waiting(`The preview couldn't be prepared: ${err.message}`);
        return false;
      }
      if (stopped) return false;
      if (reply.state === "ready") {
        onReady(reply);
        return true;
      }
      if (reply.state === "failed") {
        preview.waiting(`The preview couldn't be prepared: ${reply.error || "unknown error"}`);
        return false;
      }
      if (!S.widening) preview.waiting(`Preparing the preview… ${Math.round((reply.progress || 0) * 100)}%`);
      await sleep(POLL_MS);
      if (stopped) return false;
    }
  }

  function maybeWiden() {
    if (S.widening) return;
    let a = S.ws;
    let b = S.we;
    if (S.spec.in - S.ws < WIDEN_NEAR_S && S.ws > 0) a = Math.max(0, S.ws - WIDEN_BY_S);
    if (S.we - S.spec.out < WIDEN_NEAR_S && S.we < S.duration) b = Math.min(S.duration, S.we + WIDEN_BY_S);
    if (a === S.ws && b === S.we) return;
    if (b - a > MAX_WINDOW_S) {
      if (a < S.ws) b = Math.max(S.spec.out, a + MAX_WINDOW_S);
      else a = Math.min(S.spec.in, b - MAX_WINDOW_S);
    }
    S.widening = true;
    drawStrip();
    requestProxy({ start: P.round3(a), end: P.round3(b) }, reply => {
      S.ws = reply.start;
      S.we = reply.end;
      preview.setSource(reply.url, S.ws);
      say("More of the video is in the timeline now.");
    }).finally(() => {
      S.widening = false;
      if (stopped) return;
      drawTicks();
      drawStrip();
      maybeWiden();
    });
  }

  function describeTranscript() {
    const t = S.view.transcript;
    const notes = {
      preparing: `Preparing the word timings for this clip… ${Math.round((t.progress || 0) * 100)}%. ` +
                 "You can trim it with the handles meanwhile.",
      estimated: "These word timings are estimated from YouTube's subtitles, so a cut can be off by a little.",
      unavailable: "This clip has no transcript, so trim it with the handles above." +
                   (t.error ? ` (The transcription failed: ${t.error})` : ""),
    };
    transcriptNote.textContent = notes[t.state] || "";
    transcriptNote.hidden = !notes[t.state];
  }

  async function waitForTranscript() {
    while (!stopped && S.view.transcript.state === "preparing") {
      await sleep(2000);
      if (stopped) return;
      try {
        S.view = await api.get(`/api/editor/clips/${encodeURIComponent(S.id)}`);
      } catch {
        continue;
      }
      if (stopped) return;
      describeTranscript();
      if (S.view.transcript.state !== "preparing") {
        S.words = S.view.transcript.words || [];
        S.sents = P.sentences(S.words);
        S.sep = P.separator(S.view.transcript.language);
        S.focus = S.anchor = null;
        buildScript();
        drawTicks();
        update();
        say("The word timings are ready.");
      }
    }
  }

  async function loadTrack() {
    const name = (S.view.version.files || {}).crop_track;
    S.track = null;
    if (!name) return;
    try {
      const response = await fetch(fileUrl(name), { credentials: "same-origin" });
      if (response.ok) S.track = await response.json();
    } catch {
      S.track = null;
    }
    if (!stopped) preview.draw();
  }

  async function loadMusic() {
    try {
      const data = await api.get("/api/music");
      S.music = { tracks: data.tracks || [], cleared: data.cleared || [] };
    } catch {
      S.music = { tracks: [], cleared: [] };
    }
    if (!stopped) update();
  }

  /* ── start ───────────────────────────────────────────────────────────── */
  buildScript();
  describeTranscript();
  update();
  const resize = new ResizeObserver(() => drawTicks());
  resize.observe(strip);
  if (restored) setStatus("Your edits that weren't saved yet are back.");
  if (view.proxy.state === "ready") preview.setSource(view.proxy.url, S.ws, firstStart());
  else requestProxy({}, reply => preview.setSource(reply.url, S.ws, firstStart()));
  if (view.rendering) follow(view.rendering.version_id, view.rendering.version);
  loadTrack();
  loadMusic();
  waitForTranscript();
  const beforeUnload = event => {
    if (edited()) event.preventDefault();
  };
  window.addEventListener("beforeunload", beforeUnload);

  return () => {
    stopped = true;
    for (const t of timers) clearTimeout(t);
    preview.pause();
    preview.video.removeAttribute("src");
    preview.video.load();
    resize.disconnect();
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("beforeunload", beforeUnload);
    if (edited()) drafts.set(S.id, { base: S.view.version.id, spec: S.spec, undo: S.undo, redo: S.redo });
  };
}
