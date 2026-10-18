/* The Videos screen, #/autopilot/sources (design §15, task 12.3, renamed from
   Sources after task 13.2; Requirements 1.3, 1.5, 1.7, 1.9, N8.1, N5.2).

   Each video given to Autopilot, newest first, with:
   - its stage and overall percentage while it's processed (1.3), read
     again every 2 s while any source is queued or running;
   - "Restarted once" when a restart of Clip Maker re-ran it (1.5);
   - its outcome: the posts planned, or why none were (1.7, with the best
     score and the profile's minimum), or the error;
   - the AI calls it used (N8.1);
   - Delete, which first says what deleting cancels (1.9). If the posts
     changed meanwhile, the server refuses (409) and the dialog shows the
     new count to confirm again;
   - Try again on a failed video (1.10, design §15), when the server says it
     can be (`retryable`): it's queued again with the settings it was given
     with, and polling resumes. A refusal (409: already queued, has posts,
     the uploaded file is gone) shows its reason and the row catches up.
   A source finishing or failing is announced in #apStatus (live.js). The
   composer reports what it just queued through noteAdded(), shown once on
   top. */
import { api } from "./api.js";
import { giveVideo } from "./composer.js";
import { modal } from "./dialog.js";
import { formatDuration, formatTime, plural } from "./format.js";
import { say } from "./live.js";
import { browserZone } from "./prefs.js";

const POLL_MS = 2000;

let report = null;        // what the composer queued, for the next render

/** The composer's report: {added: [{id, title}], failed: [{url, error}]}. */
export function noteAdded(value) {
  report = value;
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

const inFlight = source => source.status === "queued" || source.status === "running";

/** The status pill and the line beside it. */
export function stateOf(source) {
  const message = (source.message || "").trim();
  if (source.status === "queued") {
    return { label: "Queued", tone: "", detail: message && message !== "Queued" ? message : "" };
  }
  if (source.status === "running") {
    return { label: "Processing", tone: "busy", detail: message || "Starting" };
  }
  if (source.status === "error") {
    const why = message.replace(/^Error:\s*/, "") || source.error || "It couldn't be processed.";
    return { label: "Failed", tone: "err", detail: why };
  }
  const below = source.best_score !== null && source.best_score !== undefined &&
                source.min_score !== null && source.min_score !== undefined &&
                source.best_score < source.min_score;
  return { label: below ? "No posts" : "Done", tone: below ? "warn" : "ok", detail: message };
}

/** What #apLive says when a source finishes or fails. */
function announcement(source) {
  const { detail } = stateOf(source);
  if (source.status === "error") return `${source.title} failed: ${detail}`;
  return detail ? `${source.title}: ${detail}` : `${source.title} is done.`;
}

function factsOf(source) {
  const facts = [];
  const finished = source.status === "done" || source.status === "error";
  if (finished && source.best_score !== null && source.best_score !== undefined) {
    const floor = source.min_score !== null && source.min_score !== undefined
      ? ` (minimum ${source.min_score})` : "";
    facts.push(`Best score ${source.best_score}${floor}`);
  }
  if (finished) facts.push(plural(source.ai_calls || 0, "AI call"));
  if (source.posts) facts.push(plural(source.posts, "post"));
  return facts.join(" · ");
}

function metaOf(source, zone) {
  const where = source.kind === "file"
    ? "Uploaded file"
    : source.channel ? `${source.channel}${source.own_channel ? " (your channel)" : ""}` : "YouTube link";
  const parts = [where];
  if (source.duration > 0) parts.push(formatDuration(source.duration));
  if (source.profile && source.profile.name) parts.push(`Preset: ${source.profile.name}`);
  const added = formatTime(source.created_at, zone);
  if (added) parts.push(`Added ${added}`);
  return parts.join(" · ");
}

export async function render(container) {
  const heading = el("h1", { class: "ap-h1" }, "Videos");
  const lead = el("p", { class: "ap-lead" },
                  "Videos you gave Autopilot, newest first. Each is clipped, edited and planned in turn.");
  const flash = el("div", { class: "ap-flash", hidden: true });
  const problem = el("p", { class: "ap-problem", role: "alert", hidden: true });
  const list = el("ol", { class: "ap-sources", "aria-label": "Videos" });
  const addButton = el("button", { type: "button", class: "ap-btn" }, "Add a video");
  const empty = el("div", { class: "ap-empty", hidden: true },
                   el("p", { class: "ap-empty-text" }, "No videos yet. Press Add a video to give Autopilot one."));
  container.append(el("div", { class: "ap-q-head" }, el("div", {}, heading, lead), addButton),
                   flash, problem, list, empty);

  addButton.addEventListener("click", giveVideo);

  const given = report;
  report = null;
  if (given && (given.added.length || given.failed.length)) {
    const n = given.added.length;
    const started = n === 1 ? `Autopilot started on “${given.added[0].title}”.`
                            : `Autopilot started on ${n} videos.`;
    const parts = [el("p", {}, started)];
    let spoken = started;
    if (given.failed.length) {
      const f = given.failed.length;
      const left = `${plural(f, "link")} ${f === 1 ? "wasn't" : "weren't"} added:`;
      parts.push(el("p", { class: "ap-flash-warn" }, left),
                 el("ul", {}, ...given.failed.map(item => el("li", {}, `${item.url}: ${item.error}`))));
      spoken = `${started} ${left.slice(0, -1)}.`;
    }
    flash.replaceChildren(...parts);
    flash.hidden = false;
    say(spoken);              // focus lands on the heading, so say it as well
  }

  const rows = new Map();      // source id -> {node, parts, source}
  let zone = browserZone();
  let timer = 0;
  let stopped = false;
  let controller = null;

  function buildRow(source) {
    // The title takes focus when the row's Try again button goes away.
    const title = el("h2", { class: "ap-src-title", tabindex: "-1" });
    const meta = el("p", { class: "ap-src-meta" });
    const pill = el("span", { class: "ap-pill" });
    const detail = el("span", { class: "ap-src-detail" });
    const fill = el("i");
    const track = el("div", { class: "ap-meter-track", role: "progressbar", "aria-valuemin": "0",
                              "aria-valuemax": "100" }, fill);
    const pct = el("span", { class: "ap-meter-pct", "aria-hidden": "true" });
    const meter = el("div", { class: "ap-meter" }, track, pct);
    const note = el("p", { class: "ap-src-note" },
                    "Restarted once: Clip Maker restarted while this was being processed.");
    const facts = el("p", { class: "ap-src-facts" });
    const hidden = el("span", { class: "ap-sr" });
    const remove = el("button", { type: "button", class: "ap-btn ghost ap-danger" }, "Delete", hidden);
    const againHidden = el("span", { class: "ap-sr" });
    const again = el("button", { type: "button", class: "ap-btn", hidden: true }, "Try again", againHidden);
    const node = el("li", { class: "ap-src", "data-id": source.id },
                    el("div", { class: "ap-src-main" }, title, meta,
                       el("div", { class: "ap-src-state" }, pill, detail), meter, note, facts),
                    el("div", { class: "ap-src-actions" }, again, remove));
    const row = { node, source, parts: { title, meta, pill, detail, fill, track, pct, meter, note,
                                          facts, hidden, remove, again, againHidden } };
    remove.addEventListener("click", () => removeSource(row));
    again.addEventListener("click", () => retrySource(row));
    return row;
  }

  function fillRow(row, source) {
    const { parts } = row;
    const state = stateOf(source);
    parts.title.textContent = source.title;
    parts.meta.textContent = metaOf(source, zone);
    parts.pill.textContent = state.label;
    parts.pill.className = `ap-pill${state.tone ? ` ${state.tone}` : ""}`;
    parts.detail.textContent = state.detail;
    parts.detail.hidden = !state.detail;
    const running = source.status === "running";
    parts.meter.hidden = !running;
    if (running) {
      const value = Math.max(0, Math.min(100, Math.round(source.pct || 0)));
      parts.fill.style.width = `${value}%`;
      parts.pct.textContent = `${value}%`;
      parts.track.setAttribute("aria-valuenow", String(value));
      parts.track.setAttribute("aria-valuetext", `${value}%, ${state.detail}`);
      parts.track.setAttribute("aria-label", `Progress of ${source.title}`);
    }
    parts.note.hidden = !source.restarted;
    const facts = factsOf(source);
    parts.facts.textContent = facts;
    parts.facts.hidden = !facts;
    parts.hidden.textContent = ` ${source.title}`;
    const retryable = Boolean(source.retryable);
    if (!retryable && parts.again.contains(document.activeElement)) {
      parts.title.focus({ preventScroll: true });      // don't drop focus with the button
    }
    parts.again.hidden = !retryable;
    parts.againHidden.textContent = ` ${source.title}`;
    row.source = source;
  }

  function update(sources) {
    const seen = new Set();
    let before = null;
    for (let i = sources.length - 1; i >= 0; i -= 1) {     // keep the server's order
      const source = sources[i];
      seen.add(source.id);
      let row = rows.get(source.id);
      if (!row) {
        row = buildRow(source);
        rows.set(source.id, row);
      } else if (inFlight(row.source) && !inFlight(source)) {
        say(announcement(source));
      }
      fillRow(row, source);
      if (row.node.nextSibling !== before || row.node.parentNode !== list) list.insertBefore(row.node, before);
      before = row.node;
    }
    for (const [id, row] of rows) {
      if (!seen.has(id)) {
        row.node.remove();
        rows.delete(id);
      }
    }
    empty.hidden = sources.length > 0;
    list.hidden = sources.length === 0;
  }

  function schedule() {
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(refresh, POLL_MS);
  }

  async function refresh() {
    clearTimeout(timer);
    if (controller) controller.abort();
    // A reading a newer one replaced is dropped, even if it already
    // arrived: one taken before a Try again would show the video failed.
    const mine = new AbortController();
    controller = mine;
    let data;
    try {
      data = await api.get("/api/autopilot/sources", null, { signal: mine.signal });
    } catch (err) {
      if (stopped || mine !== controller || (err && err.name === "AbortError")) return;
      problem.textContent = `Couldn't read your videos: ${err.message}`;
      problem.hidden = false;
      schedule();
      return;
    }
    if (stopped || mine !== controller) return;
    problem.hidden = true;
    zone = data.timezone || browserZone();
    update(data.sources || []);
    if ((data.sources || []).some(inFlight)) schedule();
  }

  async function retrySource(row) {
    if (row.retrying) return;                // a second press while this runs
    row.retrying = true;
    const title = row.source.title;
    try {
      const data = await api.post(`/api/autopilot/sources/${encodeURIComponent(row.source.id)}/retry`);
      problem.hidden = true;
      fillRow(row, data.source);             // queued: the button goes, focus moves to the title
      say(`Trying “${title}” again.`);
    } catch (err) {
      if (err.status === 404) {
        forget(row);
        say(`“${title}” was already deleted.`);
        return;
      }
      problem.textContent = `Couldn't try “${title}” again: ${err.message}`;
      problem.hidden = false;
    } finally {
      row.retrying = false;
    }
    refresh();                               // and poll while it's queued or processing
  }

  async function removeSource(row) {
    // The button stays enabled, so focus can return to it when the dialog
    // closes; a click while this runs is ignored.
    if (row.deleting) return;
    row.deleting = true;
    const title = row.source.title;
    try {
      let impact = await api.get(`/api/autopilot/sources/${encodeURIComponent(row.source.id)}/delete-impact`);
      let changed = false;
      for (;;) {
        const content = [];
        if (changed) content.push(el("p", { class: "ap-dialog-warn" }, "Its posts changed while this was open."));
        content.push(el("p", {}, impact.message),
                     el("p", { class: "ap-dialog-quiet" },
                        "Its clips and files are deleted too. This can't be undone."));
        const ok = await modal({ title: `Delete “${title}”?`, content, confirm: "Delete video",
                                 danger: true });
        if (!ok) return;
        try {
          await api.del(`/api/autopilot/sources/${encodeURIComponent(row.source.id)}`,
                        { confirm: impact.unpublished });
          break;
        } catch (err) {
          if (err.status === 409 && err.body && err.body.impact) {
            impact = err.body.impact;
            changed = true;
            continue;
          }
          throw err;
        }
      }
      forget(row);
      say(`Deleted “${title}”.`);
      document.dispatchEvent(new Event("ap:changed"));       // its posts were cancelled
    } catch (err) {
      if (err.status === 404) {
        forget(row);
        say(`“${title}” was already deleted.`);
        return;
      }
      problem.textContent = `Couldn't delete “${title}”: ${err.message}`;
      problem.hidden = false;
    } finally {
      row.deleting = false;
    }
  }

  function forget(row) {
    row.node.remove();
    rows.delete(row.source.id);
    if (!rows.size) {
      empty.hidden = false;
      list.hidden = true;
    }
    heading.focus({ preventScroll: true });
  }

  function onVisible() {
    if (!document.hidden) refresh();
  }

  document.addEventListener("visibilitychange", onVisible);
  await refresh();
  return () => {
    stopped = true;
    clearTimeout(timer);
    if (controller) controller.abort();
    document.removeEventListener("visibilitychange", onVisible);
  };
}
