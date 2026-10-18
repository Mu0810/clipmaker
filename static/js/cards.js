/* Autopilot on the main page's clip cards and library rows (design §15,
   task 12.6; Requirements 6.5, 12.4).

   index.html draws the cards and rows, with an "Edit clip" link to the
   editor on each card, then dispatches "cm:clips" or "cm:library". This
   module adds a chip for each platform's post: on a card, that post's state
   ("YouTube · Scheduled Mon 19 Oct 13:00 IST", "Instagram · Posted"), linking
   to the post when it's out and to the review queue otherwise; on a
   library row, a summary for the source ("YouTube · 2 planned, 1 posted").
   Withdrawn posts (rejected, or cancelled with their source) aren't shown. */
import { api } from "./api.js";
import { el, sr } from "./dom.js";
import { dateLabel } from "./format.js";

const PLATFORM = { youtube: "YouTube", instagram: "Instagram" };
const OUT = new Set(["live", "private", "kept_private"]);
const WITHDRAWN = new Set(["rejected", "cancelled"]);

/** {text, tone} for one post's chip. */
export function stateOf(post) {
  const when = post.slot_day ? ` ${dateLabel(post.slot_day)} ${post.slot_time} ${post.slot_zone}` : "";
  switch (post.status) {
    case "scheduled":
      return post.approval === "pending" ? { text: "Needs approval", tone: "warn" }
        : { text: `Scheduled${when}`, tone: "" };
    case "waiting_approval": return { text: "Needs approval", tone: "warn" };
    case "unscheduled": return { text: "No slot", tone: "" };
    case "blocked": return { text: "Blocked", tone: "err" };
    case "needs_reconnect": return { text: "Waiting for a reconnect", tone: "err" };
    case "publishing":
    case "retrying": return { text: "Publishing", tone: "" };
    case "uploaded": return { text: "Uploaded, not public yet", tone: "" };
    case "live": return { text: "Posted", tone: "ok" };
    case "private": return { text: "Private", tone: "" };
    case "kept_private": return { text: "Kept private by YouTube", tone: "warn" };
    case "failed": return { text: "Failed", tone: "err" };
    default: return { text: post.status, tone: "" };
  }
}

function chip(post) {
  const { text, tone } = stateOf(post);
  const name = PLATFORM[post.platform] || post.platform;
  const out = OUT.has(post.status) && post.url;
  const attrs = out ? { href: post.url, target: "_blank", rel: "noopener noreferrer" } : { href: "#/autopilot/queue" };
  return el("a", { class: `ap-card-post${tone ? ` ${tone}` : ""}`, ...attrs },
            `${name} · ${text}`, sr(out ? " (opens the post in a new tab)" : " (opens the review queue)"));
}

const shown = posts => posts.filter(p => p.method === "official" && !WITHDRAWN.has(p.status));

async function decorateCards(jobId) {
  const cards = [...document.querySelectorAll("#clips .clip[data-job]")].filter(c => c.dataset.job === jobId);
  if (!cards.length) return;
  let posts;
  try {
    posts = shown((await api.get("/api/autopilot/posts", { source: jobId })).posts || []);
  } catch {
    return;                                     // no Autopilot posts to show; the card works as before
  }
  for (const card of cards) {
    const box = card.querySelector("[data-posts]");
    if (!box) continue;
    const mine = posts.filter(p => String(p.clip_idx) === card.dataset.index)
      .sort((a, b) => a.platform.localeCompare(b.platform));
    box.replaceChildren(...mine.map(chip));
  }
}

async function decorateLibrary() {
  const rows = [...document.querySelectorAll("#libBody .lib-row[data-job]")];
  if (!rows.length) return;
  let posts;
  try {
    posts = shown((await api.get("/api/autopilot/posts")).posts || []);
  } catch {
    return;
  }
  for (const row of rows) {
    const box = row.querySelector("[data-posts]");
    if (!box) continue;
    const mine = posts.filter(p => p.job_id === row.dataset.job);
    const chips = [];
    for (const platform of ["youtube", "instagram"]) {
      const of = mine.filter(p => p.platform === platform);
      if (!of.length) continue;
      const out = of.filter(p => OUT.has(p.status)).length;
      const failed = of.filter(p => p.status === "failed").length;
      const coming = of.length - out - failed;
      const parts = [coming && `${coming} planned`, out && `${out} posted`, failed && `${failed} failed`].filter(Boolean);
      chips.push(el("a", { class: `ap-card-post${failed ? " err" : ""}`, href: "#/autopilot/queue" },
                    `${PLATFORM[platform]} · ${parts.join(", ")}`, sr(" (opens the review queue)")));
    }
    box.replaceChildren(...chips);
    // The library is a dialog of the main page: close it, so the queue shows.
    for (const link of chips) link.addEventListener("click", () => window.closeLibrary && window.closeLibrary());
  }
}

document.addEventListener("cm:clips", event => decorateCards(event.detail.jobId));
document.addEventListener("cm:library", () => decorateLibrary());
