/* Posting hours picked from chips (design §15, after task 13.2; Requirements
   14.1, 5.2, N5.2): a grid per platform with a row per weekday and a chip
   per hour of the 24-hour clock, in the user's time zone. Each chip is a
   toggle button.

   The grid is one Tab stop, the ARIA grid pattern: Left and Right move
   along a day, Up and Down to the same hour on the day before or after,
   Home and End to the day's first and last hour, Ctrl+Home and Ctrl+End to
   the grid's first and last chip. Space or Enter turns the hour on or off.
   A change fires a bubbling "change" event on the grid, as a form field
   would. */
import { el, sr } from "./dom.js";

const DAYS = [["Monday", "Mon"], ["Tuesday", "Tue"], ["Wednesday", "Wed"], ["Thursday", "Thu"],
              ["Friday", "Fri"], ["Saturday", "Sat"], ["Sunday", "Sun"]];
const HOURS = 24;
const BREAKS = new Set([6, 12, 18]);           // a little space before morning, afternoon, evening

/** One platform's grid. `saved` is {"0": [hours], …, "6": [hours]}, Monday
    first. Returns {node, read(), active()}: read() gives the same shape,
    hours ascending; active() is the chip that takes the Tab stop. */
export function hourGrid({ label, saved = {}, describedBy = "" }) {
  const chips = [];                             // chips[day][hour]
  const tbody = el("tbody", {});
  DAYS.forEach(([day, short], d) => {
    const on = new Set(((saved || {})[String(d)] || []).map(Number));
    const row = el("tr", {}, el("th", { scope: "row" }, el("span", { "aria-hidden": "true" }, short), sr(day)));
    chips.push([]);
    for (let h = 0; h < HOURS; h += 1) {
      const chip = el("button", { type: "button", class: "ap-hour", tabindex: "-1", "aria-label": `${h}:00`,
                                  "aria-pressed": String(on.has(h)) }, String(h));
      chip.dataset.day = String(d);
      chip.dataset.hour = String(h);
      chips[d].push(chip);
      row.append(el("td", { class: BREAKS.has(h) ? "ap-hour-break" : null }, chip));
    }
    tbody.append(row);
  });
  const node = el("table", { class: "ap-hours", role: "grid", "aria-label": label,
                             "aria-describedby": describedBy || null }, tbody);

  const all = () => chips.flat();
  const pressed = chip => chip.getAttribute("aria-pressed") === "true";
  let stop = all().find(pressed) || chips[0][0];
  stop.tabIndex = 0;

  function moveStop(chip, focus = true) {
    if (chip !== stop) {
      stop.tabIndex = -1;
      chip.tabIndex = 0;
      stop = chip;
    }
    if (focus) chip.focus();
  }

  node.addEventListener("click", event => {
    const chip = event.target.closest(".ap-hour");
    if (!chip || !node.contains(chip)) return;
    chip.setAttribute("aria-pressed", String(!pressed(chip)));
    moveStop(chip, false);
    node.dispatchEvent(new Event("change", { bubbles: true }));
  });
  node.addEventListener("focusin", event => {
    const chip = event.target.closest(".ap-hour");
    if (chip) moveStop(chip, false);
  });
  node.addEventListener("keydown", event => {
    const chip = event.target.closest(".ap-hour");
    if (!chip || event.altKey || event.metaKey) return;
    const d = Number(chip.dataset.day);
    const h = Number(chip.dataset.hour);
    const last = DAYS.length - 1;
    const to = {
      ArrowLeft: [d, Math.max(0, h - 1)],
      ArrowRight: [d, Math.min(HOURS - 1, h + 1)],
      ArrowUp: [Math.max(0, d - 1), h],
      ArrowDown: [Math.min(last, d + 1), h],
      Home: event.ctrlKey ? [0, 0] : [d, 0],
      End: event.ctrlKey ? [last, HOURS - 1] : [d, HOURS - 1],
    }[event.key];
    if (!to) return;
    event.preventDefault();                     // the page doesn't scroll
    moveStop(chips[to[0]][to[1]]);
  });

  return {
    node,
    read() {
      const out = {};
      chips.forEach((row, d) => {
        out[String(d)] = row.filter(pressed).map(chip => Number(chip.dataset.hour));
      });
      return out;
    },
    active: () => stop,
  };
}
