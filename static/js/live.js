/* What a screen announces to screen readers (N5.2): a source finishing, a
   deletion done. It goes to #apStatus, a polite live region of its own. The
   header chip announces count changes in #apLive, and with one region a
   count change caused by the same action would overwrite the screen's news
   a moment later.
   What is said during one task is announced together, in order, once the
   task ends: an edit and the problem it causes ("Cut 2 words. The clip is
   2.1 s; …") would otherwise replace each other before a screen reader had
   read the first. */

let pending = [];

export function say(text) {
  if (!text) return;
  if (!pending.length) setTimeout(flush, 0);
  pending.push(text);
}

function flush() {
  const region = document.getElementById("apStatus");
  const text = pending.join(" ");
  pending = [];
  if (!region) return;
  // The same words again are announced again only if the text changes, so a
  // repeat gets a trailing no-break space (whitespace to everything else).
  region.textContent = region.textContent === text ? `${text}\u00a0` : text;
}
