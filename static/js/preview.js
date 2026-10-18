/* The clip editor's preview (design §10.3, task 12.6; Requirements 7.2, 7.6).

   Two parts:

   1. The model, ported line by line from the renderer so the preview shows
      what a render will make:
      - keepSpans() and the time mapping from media/keep.py: the clip's
        [in, out] minus the cuts and, with auto-trim on, minus pauses over
        0.5 s and filler-only phrases, each kept span padded by 0.1 s.
        round3() rounds the way Python's round(x, 3) does (exactly, ties to
        even), so the spans match the server's to the last bit;
        tests/ui/test_editor.py compares them in the page;
      - sentences() from media/transcript.py, and the caption chunks and
        timing from media/overlays.py: words (at most 3 words and 1.2 s),
        phrases (a sentence in pieces that fit 3 lines) and karaoke (groups
        of 4, the spoken word highlighted). A caption never crosses a
        sentence, and stays until the next one starts or 0.5 s after its
        last word.
   2. Preview: a <video> playing the proxy of the source window, skipping
      the removed stretches as it plays (a requestAnimationFrame loop), with
      the captions and the hook drawn over it in HTML, and the Fill crop or
      the Fit layout. It's marked as a preview: fonts and line breaks can
      differ a little from the render. */

export const GAP_S = 0.5;
export const PAD_S = 0.1;
const SCAN_BACK_S = 60.0;
// editing._FILLER, which media/keep.py uses.
export const FILLER = new Set(["um", "umm", "uh", "uhh", "uhm", "uh huh", "er", "erm", "hmm", "mm",
                               "mhm", "mmhmm", "you know", "i mean"]);

/* ── numbers, the way Python rounds them ─────────────────────────────── */

const scratch = new DataView(new ArrayBuffer(8));

/** Python's round(x, 3): the exact value rounded half to even, then the
    nearest double. Math.round(x * 1000) / 1000 differs on exact ties
    (12.0625 → 12.062 in Python) and where x * 1000 isn't exact. */
export function round3(x) {
  if (!Number.isFinite(x) || x === 0) return x;
  const sign = x < 0 ? -1 : 1;
  scratch.setFloat64(0, Math.abs(x));
  const hi = scratch.getUint32(0);
  const lo = scratch.getUint32(4);
  const bits = (hi >>> 20) & 0x7ff;
  let mantissa = (BigInt(hi & 0xfffff) << 32n) | BigInt(lo);
  let exponent;
  if (bits === 0) {
    exponent = -1074;                            // subnormal
  } else {
    mantissa |= 1n << 52n;
    exponent = bits - 1075;
  }
  if (exponent >= 0) return x;                   // a whole number already
  const shift = BigInt(-exponent);
  const scaled = mantissa * 1000n;               // |x| * 1000 = scaled / 2^shift, exactly
  let q = scaled >> shift;
  const twice = (scaled - (q << shift)) * 2n;
  const half = 1n << shift;
  if (twice > half || (twice === half && (q & 1n) === 1n)) q += 1n;
  return sign * (Number(q) / 1000);
}

/** Python's round(x) to a whole number: ties to even. */
function roundEven(x) {
  const floor = Math.floor(x);
  const diff = x - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/* ── keep spans (media/keep.py) ──────────────────────────────────────── */

/** Intervals sorted, overlapping or touching ones merged, empty ones dropped. */
export function merge(intervals) {
  const sorted = intervals.map(([a, b]) => [Number(a), Number(b)])
    .sort((p, q) => (p[0] - q[0]) || (p[1] - q[1]));
  const out = [];
  for (const [a, b] of sorted) {
    if (b <= a) continue;
    if (out.length && a <= out[out.length - 1][1]) out[out.length - 1][1] = Math.max(out[out.length - 1][1], b);
    else out.push([a, b]);
  }
  return out;
}

/** [tIn, tOut] minus the cuts. */
export function baseSpans(tIn, tOut, cuts = []) {
  const spans = [];
  let cur = Number(tIn);
  for (let [a, b] of merge(cuts)) {
    a = Math.max(a, tIn);
    b = Math.min(b, tOut);
    if (b <= a) continue;
    if (a > cur) spans.push([cur, a]);
    cur = Math.max(cur, b);
  }
  if (cur < tOut) spans.push([cur, Number(tOut)]);
  return spans;
}

const LETTER = /[\p{L}\p{M}\p{N}]/u;

/** Lower-cased letters, marks, digits and apostrophes, in any script. */
export function normalise(text) {
  let out = "";
  for (const ch of (text || "").toLowerCase()) out += LETTER.test(ch) || ch === "'" ? ch : " ";
  return out.split(" ").filter(Boolean).join(" ");
}

export function isFiller(text) {
  const norm = normalise(text);
  return Boolean(norm) && FILLER.has(norm);
}

function bisectLeft(list, x) {
  let lo = 0;
  let hi = list.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid] < x) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function bisectRight(list, x) {
  let lo = 0;
  let hi = list.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (x < list[mid]) hi = mid;
    else lo = mid + 1;
  }
  return lo;
}

function phrases(words, indices, gap) {
  const out = [];
  for (const k of indices) {
    const last = out.length ? out[out.length - 1] : null;
    if (last && words[k].s - words[last[last.length - 1]].e <= gap) last.push(k);
    else out.push([k]);
  }
  return out;
}

const rounded = spans => spans.map(([a, b]) => [round3(a), round3(b)]).filter(([a, b]) => b > a);

/** Source-time [start, end] spans the clip keeps, in order. `words` are
    {w, s, e} in time order. */
export function keepSpans(words, tIn, tOut, cuts = [], autoTrim = true, { gap = GAP_S, pad = PAD_S } = {}) {
  const base = baseSpans(tIn, tOut, cuts);
  if (!autoTrim || !base.length) return rounded(base);
  const starts = words.map(w => w.s);
  const kept = [];
  for (const [a, b] of base) {
    const lo = bisectLeft(starts, a - SCAN_BACK_S);
    const hi = bisectRight(starts, b);
    const inside = [];
    for (let k = lo; k < hi; k += 1) {
      const mid = (words[k].s + words[k].e) / 2;      // a word belongs to the span its middle is in
      if (a <= mid && mid <= b) inside.push(k);
    }
    for (const phrase of phrases(words, inside, gap)) {
      if (isFiller(phrase.map(k => words[k].w).join(" "))) continue;
      const first = phrase[0];
      const last = phrase[phrase.length - 1];
      const s = Math.max(a, words[first].s);
      const e = Math.min(b, words[last].e);
      let lower = a;
      let upper = b;
      if (first > 0) lower = Math.max(lower, Math.min(s, (words[first - 1].e + s) / 2));
      if (last + 1 < words.length) upper = Math.min(upper, Math.max(e, (e + words[last + 1].s) / 2));
      kept.push([Math.max(lower, s - pad), Math.min(upper, e + pad)]);
    }
  }
  if (!kept.length) return rounded(base);
  return rounded(merge(kept));
}

/** The clip's length. Python 3.12's sum() adds floats with Neumaier's
    compensation, so this does too: a plain loop can differ in the last bit. */
export function keptDuration(spans) {
  let total = 0;
  let c = 0;
  for (const [a, b] of spans) {
    const x = b - a;
    const t = total + x;
    if (Math.abs(total) >= Math.abs(x)) c += (total - t) + x;
    else c += (x - t) + total;
    total = t;
  }
  return c && Number.isFinite(c) ? total + c : total;
}

/** Source time to clip time; a removed stretch maps to where the next kept span begins. */
export function toClipTime(spans, t) {
  let acc = 0;
  for (const [a, b] of spans) {
    if (t < a) return acc;
    if (t <= b) return acc + (t - a);
    acc += b - a;
  }
  return acc;
}

/** Clip time to source time; a join maps to the later span, the end to the last frame. */
export function toSourceTime(spans, ct) {
  let acc = 0;
  for (const [a, b] of spans) {
    if (ct < acc + (b - a)) return a + Math.max(0, ct - acc);
    acc += b - a;
  }
  return spans.length ? spans[spans.length - 1][1] : 0;
}

/* ── sentences (media/transcript.py) ─────────────────────────────────── */

const SENTENCE_END = new Set([...".!?।॥؟。！？"]);
const CLOSERS = "\"'”’»)]}」』）";
const OPENERS = "\"'“‘«([{「『（";
const ABBREVIATIONS = new Set(["mr.", "mrs.", "ms.", "dr.", "prof.", "e.g.", "i.e.", "vs."]);
export const PAUSE_BREAK_S = 0.8;
export const MAX_SENTENCE_S = 25.0;
const NO_SPACE = new Set(["zh", "ja", "th", "lo", "my", "yue"]);

export const separator = language => (NO_SPACE.has((language || "").split("-")[0]) ? "" : " ");

function stripEnd(text, chars) {
  let end = text.length;
  while (end > 0 && chars.includes(text[end - 1])) end -= 1;
  return text.slice(0, end);
}

function stripStart(text, chars) {
  let start = 0;
  while (start < text.length && chars.includes(text[start])) start += 1;
  return text.slice(start);
}

function endsSentence(text) {
  const core = stripEnd(text, `${CLOSERS} `);
  if (!core || !SENTENCE_END.has(core[core.length - 1])) return false;
  return !ABBREVIATIONS.has(stripStart(core, OPENERS).toLowerCase());
}

function splitLong(words, a, b, maxLen) {
  const out = [];
  const stack = [[a, b]];
  while (stack.length) {
    const [lo, hi] = stack.pop();
    if (hi - lo < 2 || words[hi - 1].e - words[lo].s <= maxLen) {
      out.push([lo, hi]);
      continue;
    }
    const mid = (words[lo].s + words[hi - 1].e) / 2;
    let cut = lo + 1;
    let best = null;
    for (let k = lo + 1; k < hi; k += 1) {        // the longest pause; ties: nearest the middle, then first
      const key = [words[k].s - words[k - 1].e, -Math.abs(words[k].s - mid)];
      if (!best || key[0] > best[0] || (key[0] === best[0] && key[1] > best[1])) {
        best = key;
        cut = k;
      }
    }
    stack.push([cut, hi]);
    stack.push([lo, cut]);
  }
  return out;
}

/** [{s, e, w0, w1}]: w0..w1 (exclusive) index `words`. */
export function sentences(words, { pause = PAUSE_BREAK_S, maxLen = MAX_SENTENCE_S } = {}) {
  const n = words.length;
  const groups = [];
  let start = 0;
  for (let i = 0; i < n; i += 1) {
    if (i === n - 1 || endsSentence(words[i].w) || words[i + 1].s - words[i].e > pause) {
      groups.push([start, i + 1]);
      start = i + 1;
    }
  }
  const spans = [];
  for (const [a, b] of groups) spans.push(...splitLong(words, a, b, maxLen));
  return spans.map(([a, b]) => ({ s: words[a].s, e: words[b - 1].e, w0: a, w1: b }));
}

/* ── captions (media/overlays.py) ────────────────────────────────────── */

export const WORDS_MAX = 3;
export const WORDS_MAX_S = 1.2;
export const KARAOKE_GROUP = 4;
export const PHRASE_MAX_LINES = 3;
export const HOLD_S = 0.5;
export const FRAME_W = 1080;

export const captionFontSize = n => (n <= 3 ? 88 : 64);
export const karaokeFontSize = n => (n <= 3 ? 92 : n <= 5 ? 76 : 60);

const EMOJI = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{2190}-\u{21FF}\u{20E3}\u{E0020}-\u{E007F}\u{FE0F}]/gu;
const EMOJI_JOINER = /(?<=[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{2190}-\u{21FF}\u{20E3}\u{E0020}-\u{E007F}\u{FE0F}])\u200D|\u200D(?=[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{2190}-\u{21FF}\u{20E3}\u{E0020}-\u{E007F}])/gu;

export const stripEmoji = text => (text || "").replace(EMOJI_JOINER, "").replace(EMOJI, "");

/** The words the clip keeps, in clip time, with caption edits applied:
    [{i (transcript word number), k (index in `words`), text, s, e}]. */
export function keptWords(words, spans, edits = {}) {
  const out = [];
  if (!spans.length) return out;
  const lo = spans[0][0];
  const hi = spans[spans.length - 1][1];
  words.forEach((w, k) => {
    const mid = (w.s + w.e) / 2;
    if (mid < lo || mid > hi || !spans.some(([a, b]) => a <= mid && mid <= b)) return;
    const raw = Object.prototype.hasOwnProperty.call(edits, String(w.i)) ? edits[String(w.i)] : w.w;
    const text = String(raw).split(/\s+/).filter(Boolean).join(" ");
    if (!text) return;
    const s = toClipTime(spans, w.s);
    out.push({ i: w.i, k, text, s, e: Math.max(s, toClipTime(spans, w.e)) });
  });
  return out;
}

function sentenceRuns(kept, sentenceList) {
  const starts = sentenceList.map(s => s.w0);
  const sentenceOf = k => {
    const at = bisectRight(starts, k) - 1;
    return at >= 0 && k < sentenceList[at].w1 ? `s${at}` : `w${k}`;
  };
  const runs = [];
  let last = null;
  for (const w of kept) {
    const id = sentenceOf(w.k);
    if (runs.length && id === last) runs[runs.length - 1].push(w);
    else runs.push([w]);
    last = id;
  }
  return runs;
}

function wordChunks(run) {
  const chunks = [];
  let i = 0;
  while (i < run.length) {
    let j = i + 1;
    while (j < run.length && j - i < WORDS_MAX && run[j].e - run[i].s <= WORDS_MAX_S + 1e-9) j += 1;
    chunks.push(run.slice(i, j));
    i = j;
  }
  return chunks;
}

/** Greedy line count for `text` at `size` px in a FRAME_W-wide frame. */
export function lineCount(text, size, maxWidth, measure) {
  const words = stripEmoji(text).split(/\s+/).filter(Boolean);
  const space = measure(" ", size);
  let lines = 0;
  let width = 0;
  for (const word of words) {
    const w = measure(word, size);
    if (lines === 0) {
      lines = 1;
      width = w;
    } else if (width + space + w <= maxWidth) {
      width += space + w;
    } else {
      lines += 1;
      width = w;
    }
  }
  return lines;
}

function phrasePieces(run, sep, maxWidth, measure) {
  const pieces = [];
  let start = 0;
  for (let k = 1; k < run.length; k += 1) {
    const candidate = run.slice(start, k + 1);
    const text = candidate.map(w => w.text).join(sep).toUpperCase();
    if (lineCount(text, captionFontSize(candidate.length), maxWidth, measure) > PHRASE_MAX_LINES) {
      pieces.push(run.slice(start, k));
      start = k;
    }
  }
  pieces.push(run.slice(start));
  return pieces;
}

/** What the captions show, in order: one state per chunk, or per spoken
    word for karaoke: {start, naturalEnd, words, active, fontSize}. */
export function captionStates(kept, sentenceList, style, sep, measure) {
  const states = [];
  for (const run of sentenceRuns(kept, sentenceList)) {
    if (style === "karaoke") {
      for (let g = 0; g < run.length; g += KARAOKE_GROUP) {
        const group = run.slice(g, g + KARAOKE_GROUP);
        group.forEach((w, pos) => {
          const natural = pos + 1 < group.length ? group[pos + 1].s : w.e;
          states.push({ start: w.s, naturalEnd: natural, words: group, active: pos,
                        fontSize: karaokeFontSize(group.length) });
        });
      }
      continue;
    }
    const pieces = style === "words" ? wordChunks(run)
      : phrasePieces(run, sep, Math.floor(FRAME_W * 0.88), measure);
    for (const piece of pieces) {
      states.push({ start: piece[0].s, naturalEnd: piece[piece.length - 1].e, words: piece, active: null,
                    fontSize: captionFontSize(piece.length) });
    }
  }
  return states;
}

const us = t => roundEven(t * 1_000_000);

/** [{a, b (microseconds), state (index or null), hook}]: the clip's
    stretches of identical captions, as overlays._timeline makes them. */
export function captionTimeline(states, total, hookEnd) {
  const totalUs = us(total);
  const hookEndUs = Math.min(us(hookEnd), totalUs);
  states.forEach((st, k) => {
    let end = st.naturalEnd + HOLD_S;
    if (k + 1 < states.length) end = Math.min(end, states[k + 1].start);
    st.startUs = Math.min(us(st.start), totalUs);
    st.endUs = Math.min(us(end), totalUs);
  });
  const live = states.map((st, k) => k).filter(k => states[k].endUs > states[k].startUs);
  const points = new Set([0, totalUs, hookEndUs]);
  for (const k of live) {
    points.add(states[k].startUs);
    points.add(states[k].endUs);
  }
  const sorted = [...points].filter(p => p >= 0 && p <= totalUs).sort((p, q) => p - q);
  const out = [];
  let pos = 0;
  for (let i = 0; i + 1 < sorted.length; i += 1) {
    const a = sorted[i];
    const b = sorted[i + 1];
    while (pos < live.length && states[live[pos]].endUs <= a) pos += 1;
    const state = pos < live.length && states[live[pos]].startUs <= a ? live[pos] : null;
    const hook = a < hookEndUs;
    const last = out[out.length - 1];
    if (last && last.state === state && last.hook === hook) last.b = b;
    else out.push({ a, b, state, hook });
  }
  return out;
}

/* ── the crop (media/render.manual_track, media/reframe) ─────────────── */

/** Width of a full-height 9:16 crop, or null when there's nothing to crop sideways. */
export function cropWidth(srcW, srcH) {
  const cw = roundEven(srcH * 9 / 16 / 2) * 2;
  return cw > 0 && cw < srcW ? cw : null;
}

/** The crop's left edge for a centre at fraction `x` of the width. */
export function manualLeft(x, srcW, srcH) {
  const cw = cropWidth(srcW, srcH);
  if (!cw) return null;
  return Math.min(Math.max(0, roundEven(x * srcW - cw / 2)), srcW - cw);
}

/** The crop's left edge at clip time `t` from a crop track's keyframes. */
export function trackLeft(track, t) {
  const keys = (track && track.keyframes) || [];
  if (!keys.length) return null;
  let at = 0;
  for (let i = 0; i < keys.length; i += 1) if (keys[i][0] <= t) at = i;
  return keys[at][1];
}

/* ── the preview itself ──────────────────────────────────────────────── */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === true) node.setAttribute(name, "");
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(name, value);
  }
  node.append(...children.filter(c => c !== null && c !== undefined && c !== false));
  return node;
}

const widths = new Map();
let canvas = null;

/** Text width in px at `size` px, in the caption font. */
export function measureText(text, size) {
  const key = `${size}|${text}`;
  if (!widths.has(key)) {
    canvas = canvas || document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    ctx.font = `bold ${size}px Arial, "Helvetica Neue", Helvetica, sans-serif`;
    widths.set(key, ctx.measureText(text).width);
  }
  return widths.get(key);
}

const unit = size => `${(size / FRAME_W) * 100}cqw`;      // 1080-wide frame units

/** The 9:16 preview. `onTime(sourceTime)` reports the playhead. */
export class Preview {
  constructor({ onTime = () => {}, onPlay = () => {} } = {}) {
    this.onTime = onTime;
    this.onPlay = onPlay;
    this.spans = [];
    this.windowStart = 0;
    this.timeline = [];
    this.states = [];
    this.playing = false;
    this.raf = 0;
    this.shown = "";
    this.video = el("video", { class: "ap-pv-video", preload: "auto", playsinline: true, muted: false,
                               "aria-hidden": "true", tabindex: "-1" });
    this.backdrop = el("img", { class: "ap-pv-backdrop", alt: "", hidden: true });
    this.hook = el("div", { class: "ap-pv-hook", hidden: true });
    this.caption = el("div", { class: "ap-pv-caption", hidden: true });
    this.wait = el("div", { class: "ap-pv-wait" }, "Preparing the preview…");
    this.badge = el("span", { class: "ap-pv-badge" }, "Preview · the rendered video may look slightly different");
    this.frame = el("div", { class: "ap-pv" }, this.backdrop, this.video, this.hook, this.caption,
                    this.badge, this.wait);
    this.video.addEventListener("pause", () => this.stopLoop());
    this.video.addEventListener("ended", () => this.pause());
    this.video.addEventListener("seeked", () => {
      if (!this.playing) this.draw();
    });
    this.video.addEventListener("loadeddata", () => {
      this.wait.hidden = true;
      this.draw();
    });
  }

  /** Show `text` while the proxy isn't ready (a progress message). */
  waiting(text) {
    this.wait.textContent = text;
    this.wait.hidden = !text;
  }

  /** The proxy: its URL and the source time it starts at. Once it loads,
      it seeks to `at` (a source time; by default the playhead, so swapping
      in a wider proxy keeps the place). */
  setSource(url, windowStart, at = this.sourceTime()) {
    this.windowStart = windowStart;
    if (this.video.getAttribute("src") !== url) {
      this.wait.hidden = false;
      this.video.addEventListener("loadedmetadata", () => this.seek(at ?? windowStart), { once: true });
      this.video.src = url;
    }
  }

  sourceTime() {
    return Number.isFinite(this.video.currentTime) ? this.video.currentTime + this.windowStart : null;
  }

  /** What the preview shows. `model`: {spans, timeline, states, hookText,
      sep, layout, cropPercent(clipTime) | null, backdrop (an image URL for
      Fit, or null)} */
  setModel(model) {
    for (const key of ["spans", "timeline", "states", "hookText", "sep", "layout", "cropPercent"]) {
      if (key in model) this[key] = model[key];
    }
    this.video.style.objectFit = model.layout === "fit" ? "contain" : "cover";
    this.backdrop.hidden = model.layout !== "fit" || !model.backdrop;
    if (model.backdrop && this.backdrop.getAttribute("src") !== model.backdrop) this.backdrop.src = model.backdrop;
    this.hook.textContent = model.hookText || "";
    this.shown = "";
    this.draw();
  }

  /** Seek to a source time (the 200 ms path: no server call). */
  seek(sourceTime) {
    const t = Math.max(0, sourceTime - this.windowStart);
    if (Number.isFinite(t)) this.video.currentTime = t;
    this.draw(sourceTime);
  }

  clipTime(sourceTime = this.sourceTime()) {
    return sourceTime === null ? 0 : toClipTime(this.spans, sourceTime);
  }

  async play() {
    if (!this.spans.length) return;
    const st = this.sourceTime();
    const end = this.spans[this.spans.length - 1][1];
    if (st === null || st >= end - 0.05 || st < this.spans[0][0]) this.seek(this.spans[0][0]);
    this.playing = true;
    this.onPlay(true);
    try {
      await this.video.play();
    } catch {
      this.playing = false;
      this.onPlay(false);
      return;
    }
    this.loop();
  }

  pause() {
    this.playing = false;
    this.video.pause();
    this.stopLoop();
    this.onPlay(false);
    this.draw();
  }

  toggle() {
    if (this.playing) this.pause();
    else this.play();
  }

  stopLoop() {
    cancelAnimationFrame(this.raf);
    this.raf = 0;
    if (this.playing && this.video.paused) {
      this.playing = false;
      this.onPlay(false);
    }
  }

  loop() {
    cancelAnimationFrame(this.raf);
    const step = () => {
      if (!this.playing) return;
      const st = this.sourceTime();
      const spans = this.spans;
      if (st !== null && spans.length) {
        const end = spans[spans.length - 1][1];
        if (st >= end) {
          this.pause();
          this.seek(end);
          return;
        }
        // Inside a removed stretch: jump to where the next kept span begins.
        const next = spans.find(([, b]) => st < b);
        if (next && st < next[0] - 0.02) this.video.currentTime = next[0] - this.windowStart;
      }
      this.draw(st);
      this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  }

  /** Captions, hook and crop at a source time (the playhead by default). */
  draw(sourceTime = this.sourceTime()) {
    if (sourceTime === null) return;
    const ct = this.clipTime(sourceTime);
    const ctUs = us(ct);
    const entry = this.timeline.find(e => e.a <= ctUs && ctUs < e.b) ||
                  (this.timeline.length && ctUs >= this.timeline[this.timeline.length - 1].b
                    ? this.timeline[this.timeline.length - 1] : null);
    const key = entry ? `${entry.state}|${entry.hook}` : "none";
    if (key !== this.shown) {
      this.shown = key;
      this.hook.hidden = !(entry && entry.hook && this.hookText);
      const state = entry && entry.state !== null ? this.states[entry.state] : null;
      this.caption.replaceChildren();
      this.caption.hidden = !state;
      if (state) {
        const karaoke = state.active !== null;
        this.caption.className = `ap-pv-caption${karaoke ? " karaoke" : ""}`;
        this.caption.style.fontSize = unit(state.fontSize);
        state.words.forEach((w, pos) => {
          if (pos) this.caption.append(this.sep ?? " ");
          this.caption.append(el("span", { class: karaoke && pos === state.active ? "on" : null,
                                           "data-i": String(w.i) }, stripEmoji(w.text).toUpperCase()));
        });
      }
    }
    if (this.cropPercent) {
      const p = this.cropPercent(ct);
      this.video.style.objectPosition = p === null ? "50% 50%" : `${p}% 50%`;
    }
    this.onTime(sourceTime);
  }
}
