/* Activity, #/autopilot/activity (design §11, §15, task 12.5;
   Requirements 12.1, 5.6, N5.2).

   Everything Autopilot decided and did, newest first: when, who (the AI,
   you, or Clip Maker itself) and what happened. A filter narrows it to one
   part of the work; "Show more" reads the next 50 and moves focus to the
   first new entry. Entries are kept for 400 days. */
import { api } from "./api.js";
import { el } from "./dom.js";
import { plural, zoneLongName } from "./format.js";
import { say } from "./live.js";

const PAGE = 50;
// The kinds to filter by: one prefix each (the server matches "post." and so on).
const KINDS = [["", "Everything"], ["source.", "Videos"], ["clips.", "Clips chosen"],
               ["clip.", "Clip edits and renders"], ["post.", "Posts"], ["publish.", "Publishing"],
               ["account.", "Accounts"], ["pause.", "Pause"], ["profile.", "Presets"], ["music.", "Music"],
               ["prefs.", "Preferences"], ["metrics.", "Views and likes"], ["maintenance.", "Maintenance"]];
const ACTORS = { ai: ["AI", "terra"], user: ["You", "ok"], system: ["Clip Maker", ""] };

const shortLabel = label => (label || "").replace(/\s*\([^)]*\)\s*$/, "");

export async function render(container) {
  const state = { kind: "", next: null, zone: null, count: 0 };

  const heading = el("h1", { class: "ap-h1" }, "Activity");
  const lead = el("p", { class: "ap-lead" });
  const kind = el("select", { id: "ap-act-kind" }, ...KINDS.map(([value, label]) => el("option", { value }, label)));
  const filters = el("div", { class: "ap-filters" }, el("label", { for: kind.id }, "Show"), kind);
  const problem = el("p", { class: "ap-problem", role: "alert", hidden: true });
  const list = el("ol", { class: "ap-activity", "aria-label": "Activity, newest first" });
  const empty = el("p", { class: "ap-empty", hidden: true });
  const more = el("button", { type: "button", class: "ap-btn ghost", hidden: true }, "Show more");
  container.append(heading, lead, filters, problem, list, empty, el("div", { class: "ap-more-row" }, more));

  function item(entry) {
    const [who, tone] = ACTORS[entry.actor] || [entry.actor, ""];
    return el("li", { class: "ap-act", "data-id": String(entry.id), tabindex: "-1" },
              el("time", { class: "ap-act-when", datetime: new Date(entry.at * 1000).toISOString() },
                 shortLabel(entry.at_label)),
              el("span", { class: `ap-pill${tone ? ` ${tone}` : ""}` }, who),
              el("p", { class: "ap-act-what" }, entry.summary));
  }

  let controller = null;          // the load in flight; a newer filter aborts it

  async function loadPage({ append = false } = {}) {
    if (append && controller) return;
    if (controller) controller.abort();
    const mine = new AbortController();
    controller = mine;
    more.setAttribute("aria-disabled", "true");
    try {
      const data = await api.get("/api/autopilot/activity", {
        kind: state.kind, limit: PAGE, before_id: append && state.next ? state.next : null }, { signal: mine.signal });
      if (mine !== controller) return;
      state.zone = data.timezone;
      state.next = data.next_before_id;
      const items = (data.entries || []).map(item);
      if (append) {
        list.append(...items);
        state.count += items.length;
        if (items[0]) items[0].focus();
        say(`Showing ${plural(state.count, "entry", "entries")}.`);
      } else {
        list.replaceChildren(...items);
        state.count = items.length;
      }
      problem.hidden = true;
      lead.textContent = "Everything Autopilot decided and did, newest first, kept for 400 days. " +
                         `Times in ${zoneLongName(state.zone)}.`;
      list.hidden = state.count === 0;
      empty.hidden = state.count > 0;
      empty.textContent = state.kind ? "Nothing of this kind yet." : "Nothing yet. Autopilot's work shows up here.";
      more.hidden = !state.next;
    } catch (err) {
      if (err.name === "AbortError" || mine !== controller) return;
      problem.textContent = `Couldn't read the activity: ${err.message}`;
      problem.hidden = false;
    } finally {
      if (mine === controller) {
        controller = null;
        more.removeAttribute("aria-disabled");
      }
    }
  }

  kind.addEventListener("change", () => {
    state.kind = kind.value;
    state.next = null;
    loadPage();
  });
  more.addEventListener("click", () => loadPage({ append: true }));
  await loadPage();
}
