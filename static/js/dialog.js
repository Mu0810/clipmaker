/* A modal dialog for the Autopilot screens and the composer (design §15;
   N5.1, N5.2), built on <dialog>, so the rest of the page is inert while it
   is open and Escape cancels it.

     const ok = await modal({
       title: "Delete this source?",
       content: [paragraph, list],             // nodes or strings
       confirm: "Delete source", danger: true,
       check: () => box.checked ? null : "Tick the box to confirm.",
     });

   It resolves true when confirmed and false when cancelled (Cancel, the
   close button or Escape). `check`, when given, runs on Confirm: a returned
   message is shown in the dialog (role=alert) and keeps it open. The
   element named by `focus` (a node in `content`) takes focus on opening;
   otherwise Cancel does, so Enter never confirms by accident. Focus returns
   to whatever had it before. The dialog is marked data-ap-escape so the
   router leaves Escape to it, and it is removed from the page on closing. */

let count = 0;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === true) node.setAttribute(name, "");
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(name, value);
  }
  node.append(...children);
  return node;
}

const CLOSE_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';

export function modal({ title, content = [], confirm = "OK", cancel = "Cancel",
                        danger = false, check = null, focus = null }) {
  return new Promise(resolve => {
    const opener = document.activeElement;
    const id = `apDialog${++count}`;
    const heading = el("h2", { id: `${id}-title` }, title);
    const close = el("button", { type: "button", class: "x", "aria-label": "Close" });
    close.insertAdjacentHTML("afterbegin", CLOSE_ICON);
    const error = el("p", { class: "ap-dialog-error", role: "alert", hidden: true });
    const body = el("div", { class: "ap-dialog-body" }, ...content, error);
    const no = el("button", { type: "button", class: "ap-btn ghost" }, cancel);
    const yes = el("button", { type: "button", class: danger ? "ap-btn ap-danger" : "ap-btn" }, confirm);
    const dialog = el("dialog", { class: "ap-dialog", id, "aria-labelledby": heading.id,
                                  "data-ap-escape": "" },
                      el("div", { class: "ap-dialog-head" }, heading, close),
                      body,
                      el("div", { class: "ap-dialog-foot" }, no, yes));

    let settled = false;
    function finish(value) {
      if (settled) return;
      settled = true;
      if (dialog.open) dialog.close();
      dialog.remove();
      if (opener && opener.isConnected && typeof opener.focus === "function") {
        opener.focus({ preventScroll: true });
      }
      resolve(value);
    }

    yes.addEventListener("click", () => {
      const problem = check ? check() : null;
      if (problem) {
        error.textContent = problem;
        error.hidden = false;
        return;
      }
      finish(true);
    });
    no.addEventListener("click", () => finish(false));
    close.addEventListener("click", () => finish(false));
    dialog.addEventListener("cancel", event => {    // Escape
      event.preventDefault();
      finish(false);
    });

    document.body.append(dialog);
    dialog.showModal();
    (focus && focus.isConnected ? focus : no).focus();
  });
}
