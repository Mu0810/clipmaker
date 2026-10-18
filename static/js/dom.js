/* Building screen markup without HTML strings (design §15; N5.2).

     el("p", { class: "ap-lead" }, "Text ", el("b", {}, "bold"))
     sr(" for clip 3")             text for screen readers only
     icon("check")                 an inline icon, hidden from screen readers

   Attribute values set to true become empty attributes; false, null and
   undefined leave the attribute out, and the same values are skipped as
   children. Text always goes in as text, never as markup. */

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === true) node.setAttribute(name, "");
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(name, value);
  }
  node.append(...children.flat().filter(c => c !== null && c !== undefined && c !== false && c !== ""));
  return node;
}

export const sr = text => el("span", { class: "ap-sr" }, text);

const ICONS = {
  check: '<path d="M5 12.5l4.2 4.2L19 7"/>',
  cross: '<path d="M6 6l12 12M18 6L6 18"/>',
  alert: '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.5v.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.01"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  link: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/>',
};

/** An inline icon (stroke style, 15 px), hidden from screen readers. */
export function icon(name, extra = "") {
  const span = el("span", { class: `ap-ico${extra ? ` ${extra}` : ""}`, "aria-hidden": "true" });
  span.innerHTML = `<svg class="icon-sm" viewBox="0 0 24 24">${ICONS[name] || ""}</svg>`;
  return span;
}

/** A labelled field: label, control, an optional hint and an error line
    tied to the control with aria-describedby. Returns {wrap, control, error}. */
export function field(id, label, control, { hint = "", wide = false } = {}) {
  control.id = id;
  const hintNode = hint ? el("p", { class: "ap-hint-line", id: `${id}-hint` }, hint) : null;
  const error = el("p", { class: "ap-field-error", id: `${id}-error`, hidden: true });
  control.setAttribute("aria-describedby", [hintNode && hintNode.id, error.id].filter(Boolean).join(" "));
  const wrap = el("div", { class: `ap-field${wide ? " wide" : ""}` },
                  el("label", { for: id }, label), control, hintNode, error);
  return { wrap, control, error };
}

/** Show or clear a field's error; the control is marked invalid with it. */
export function setError(f, message) {
  f.error.textContent = message || "";
  f.error.hidden = !message;
  if (message) f.control.setAttribute("aria-invalid", "true");
  else f.control.removeAttribute("aria-invalid");
}
