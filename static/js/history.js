/* Post history, #/autopilot/history (design §11, §15, task 12.5;
   Requirements 12.4, 12.5, 8.7, 5.6, N5.2).

   Posts that went out or failed, newest first: when, what, where, the
   status, the latest views, likes and comments (updated once a day for 30
   days after publishing), and a link to the post on the platform. Filters
   by platform and status; "Show more" reads the next page and moves focus
   to the first new row. Times are in the preferred zone, named in the lead. */
import { api } from "./api.js";
import { el, icon, sr } from "./dom.js";
import { plural, zoneLongName } from "./format.js";
import { say } from "./live.js";

const PLATFORM = { youtube: "YouTube", instagram: "Instagram" };
const STATUS = { live: ["Posted", "ok"], private: ["Private", ""], kept_private: ["Kept private by YouTube", "warn"],
                 failed: ["Failed", "err"] };
const AUDIT = "YouTube keeps every video Clip Maker uploads private until your Google Cloud project, the one " +
              "set up for Autopilot, passes YouTube's API audit; then you can make it public in YouTube Studio.";

const count = n => (typeof n === "number" ? n.toLocaleString() : "–");

/** "Sat 17 Oct 2026, 15:00 IST": the server's label without the zone id,
    which the lead already names. */
const shortLabel = label => (label || "").replace(/\s*\([^)]*\)\s*$/, "");

function ageOf(metrics) {
  if (!metrics || typeof metrics.age_hours !== "number") return "";
  const h = metrics.age_hours;
  return h < 48 ? `${Math.round(h)} h after publishing` : `${Math.round(h / 24)} days after publishing`;
}

export async function render(container) {
  const state = { platform: "", status: "", next: null, zone: null, rows: 0 };

  const heading = el("h1", { class: "ap-h1" }, "Post history");
  const lead = el("p", { class: "ap-lead" });
  const platform = el("select", { id: "ap-hist-platform" },
                      el("option", { value: "" }, "Both platforms"), el("option", { value: "youtube" }, "YouTube"),
                      el("option", { value: "instagram" }, "Instagram"));
  const status = el("select", { id: "ap-hist-status" },
                    el("option", { value: "" }, "Every status"),
                    ...Object.entries(STATUS).map(([value, [label]]) => el("option", { value }, label)));
  const filters = el("div", { class: "ap-filters" },
                     el("label", { for: platform.id }, "Platform"), platform,
                     el("label", { for: status.id }, "Status"), status);
  const problem = el("p", { class: "ap-problem", role: "alert", hidden: true });
  const tbody = el("tbody", {});
  const table = el("table", { class: "ap-table", hidden: true },
                   el("caption", { class: "ap-sr" }, "Published and failed posts, newest first"),
                   el("thead", {}, el("tr", {}, ...["When", "Post", "Where", "Status", "Views", "Likes",
                                                    "Comments", "Link"].map((t, i) =>
                     el("th", { scope: "col", class: i >= 4 && i <= 6 ? "num" : null }, t)))),
                   tbody);
  const empty = el("p", { class: "ap-empty", hidden: true });
  const more = el("button", { type: "button", class: "ap-btn ghost", hidden: true }, "Show more");
  container.append(heading, lead, filters, problem, el("div", { class: "ap-table-wrap" }, table), empty,
                   el("div", { class: "ap-more-row" }, more));

  function row(post) {
    const [label, tone] = STATUS[post.status] || [post.status, ""];
    const what = el("td", { class: "ap-hist-what" }, el("b", {}, post.title || "Untitled"),
                    el("span", {}, [post.source_deleted ? "source deleted" : post.source_title,
                                    post.clip_idx !== null && post.clip_idx !== undefined ? `clip ${post.clip_idx}` : null]
                      .filter(Boolean).join(" · ")));
    if (post.status === "failed" && post.last_error) what.append(el("span", { class: "ap-hist-error" }, post.last_error));
    if (post.status === "kept_private") {
      what.append(el("span", { class: "ap-hist-note" }, AUDIT, " ",
                     post.audit_form ? el("a", { href: post.audit_form, target: "_blank", rel: "noopener noreferrer",
                                                 class: "ap-textlink" }, "API audit form", sr(" (opens in a new tab)")) : null));
    }
    const account = post.account ? (post.platform === "instagram" ? `@${post.account.name}` : post.account.name) : null;
    const metrics = post.metrics;
    const age = ageOf(metrics);
    const metric = value => el("td", { class: "num", title: age || null }, count(value));
    const link = post.url
      ? el("a", { href: post.url, target: "_blank", rel: "noopener noreferrer", class: "ap-textlink ap-hist-link" },
           icon("link"), `Open on ${PLATFORM[post.platform]}`, sr(`: ${post.title || "post"} (opens in a new tab)`))
      : el("span", { class: "ap-hist-none" }, "–");
    return el("tr", { "data-id": post.id, tabindex: "-1" },
              el("td", { class: "ap-hist-when" }, shortLabel(post.publish_time_label)),
              what,
              el("td", {}, PLATFORM[post.platform] || post.platform,
                 account ? el("span", { class: "ap-hist-acct" }, account) : null,
                 post.method === "legacy" ? el("span", { class: "ap-hist-acct" }, "Scheduled by hand") : null),
              el("td", {}, el("span", { class: `ap-pill${tone ? ` ${tone}` : ""}` }, label)),
              metric(metrics && metrics.views), metric(metrics && metrics.likes), metric(metrics && metrics.comments),
              el("td", {}, link));
  }

  let controller = null;          // the load in flight; a newer filter aborts it

  async function loadPage({ append = false } = {}) {
    if (append && controller) return;
    if (controller) controller.abort();
    const mine = new AbortController();
    controller = mine;
    more.setAttribute("aria-disabled", "true");
    try {
      const query = { platform: state.platform, status: state.status };
      if (append && state.next) Object.assign(query, { before: state.next.before, before_id: state.next.before_id });
      const data = await api.get("/api/autopilot/history", query, { signal: mine.signal });
      if (mine !== controller) return;
      state.zone = data.timezone;
      state.next = data.next;
      const rows = (data.posts || []).map(row);
      if (!append) {
        tbody.replaceChildren(...rows);
        state.rows = rows.length;
      } else {
        tbody.append(...rows);
        state.rows += rows.length;
        if (rows[0]) rows[0].focus();
        say(`Showing ${plural(state.rows, "post")}.`);
      }
      problem.hidden = true;
      lead.textContent = "Posts that went out or failed, newest first. Views, likes and comments are updated " +
                         `once a day for 30 days after publishing. Times in ${zoneLongName(state.zone)}.`;
      table.hidden = state.rows === 0;
      empty.hidden = state.rows > 0;
      empty.textContent = state.platform || state.status ? "No posts match these filters."
                                                         : "Nothing has gone out yet. Published posts appear here.";
      more.hidden = !state.next;
    } catch (err) {
      if (err.name === "AbortError" || mine !== controller) return;
      problem.textContent = `Couldn't read the history: ${err.message}`;
      problem.hidden = false;
    } finally {
      if (mine === controller) {
        controller = null;
        more.removeAttribute("aria-disabled");
      }
    }
  }

  for (const [select, key] of [[platform, "platform"], [status, "status"]]) {
    select.addEventListener("change", () => {
      state[key] = select.value;
      state.next = null;
      loadPage();
    });
  }
  more.addEventListener("click", () => loadPage({ append: true }));
  await loadPage();
}
