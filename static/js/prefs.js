/* The user's Autopilot preferences, read once per page and shared by the
   composer and the screens (design §15; Requirements 1.1, 5.6).

     const p = await prefs();               // {timezone, paused, last_profile_id, autopilot_default}
     await savePrefs({ autopilot_default: true });
     await ensureTimezone();

   On the first Autopilot visit an unset time zone is taken from the
   browser (design §15), so posting windows aren't read in UTC by default.
   The Setup screen can change it. */
import { api } from "./api.js";

let current = null;        // a promise of the preferences

/** The preferences; `fresh` reads them again. */
export function prefs({ fresh = false } = {}) {
  if (!current || fresh) {
    current = api.get("/api/autopilot/prefs").catch(err => {
      current = null;      // the next call tries again
      throw err;
    });
  }
  return current;
}

/** Store changes; returns the full preferences. */
export async function savePrefs(changes) {
  const saved = await api.patch("/api/autopilot/prefs", changes);
  current = Promise.resolve(saved);
  return saved;
}

/** The browser's IANA time zone, or null. */
export function browserZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

let ensuring = null;

/** The preferred time zone, taking the browser's when none is set yet.
    Returns null when neither is known; nothing here throws. */
export function ensureTimezone() {
  if (!ensuring) {
    ensuring = (async () => {
      try {
        const known = (await prefs()).timezone;
        if (known) return known;
        const zone = browserZone();
        if (!zone) return null;
        return (await savePrefs({ timezone: zone })).timezone;
      } catch {
        return null;       // offline, signed out, or a zone the server doesn't know
      } finally {
        ensuring = null;
      }
    })();
  }
  return ensuring;
}
