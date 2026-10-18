/* Counting post text the way the server checks it (platforms/limits.py),
   for the review queue's live counters (Requirements 6.5, 6.7; design §15).
   The limits themselves come from GET /api/autopilot/posts (`limits`); this
   module only counts, the same way the server does:
   - characters are code points, as Python's len() counts them, so an emoji
     is one character, not two;
   - the description is counted in UTF-8 bytes;
   - tags cost what YouTube counts: each tag's length, two quotes around a
     tag with a space in it, and a comma between tags;
   - hashtags and @-mentions match the server's patterns. */

export const chars = text => [...(text || "")].length;

const encoder = new TextEncoder();
export const utf8Bytes = text => encoder.encode(text || "").length;

/** One tag's cost toward YouTube's 500 (limits.tag_cost). */
export function tagCost(tag) {
  return chars(tag) + (tag.includes(" ") ? 2 : 0) + 1;
}

/** The tags' total as YouTube counts it (limits.youtube_problems). */
export function tagsTotal(tags) {
  const list = tags || [];
  const total = list.map(t => t.trim()).filter(Boolean).reduce((sum, t) => sum + tagCost(t), 0);
  return total - (list.length ? 1 : 0);
}

/** Tags as typed: "creator tips, growth" → ["creator tips", "growth"]. */
export function parseTags(text) {
  return (text || "").split(",").map(t => t.trim()).filter(Boolean);
}

const HASHTAG = /#[^\s#]+/gu;
const MENTION = /@[A-Za-z0-9._]+/g;

export const hashtags = text => ((text || "").match(HASHTAG) || []).length;
export const mentions = text => ((text || "").match(MENTION) || []).length;
