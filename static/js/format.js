/* Words, times and sizes for the Autopilot screens (design §15; 5.6).

     plural(3, "clip")                    "3 clips"
     formatTime(t, "Asia/Kolkata")        e.g. "17 Oct, 15:56 GMT+5:30" in an en-GB browser
     formatDuration(3725)                 "1:02:05"
     formatBytes(1_250_000_000)           "1.3 GB"

   Times are shown in the user's preferred time zone with its name, so a
   time never reads as the browser's own zone by mistake. The wall-time
   helpers at the end convert between UTC and a clock in that zone, for the
   review queue's slot field. */

export const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** "A, B and C". */
export function listOf(words) {
  const items = words.filter(Boolean);
  if (items.length <= 1) return items[0] || "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

const formats = new Map();

function formatter(zone, year) {
  const key = `${zone || ""}|${year ? "y" : ""}`;
  if (!formats.has(key)) {
    const options = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
                      timeZoneName: "short", ...(year ? { year: "numeric" } : {}) };
    try {
      formats.set(key, new Intl.DateTimeFormat(undefined, zone ? { ...options, timeZone: zone } : options));
    } catch {                                     // a zone this browser doesn't know
      formats.set(key, new Intl.DateTimeFormat(undefined, options));
    }
  }
  return formats.get(key);
}

/** A Unix time in seconds, in `zone` (an IANA name) with the zone's name;
    `year` adds the year, for dates that may be months away. */
export function formatTime(seconds, zone, { year = false } = {}) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "";
  return formatter(zone, year).format(new Date(seconds * 1000));
}

/** A length in seconds as m:ss, or h:mm:ss from an hour up. */
export function formatDuration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

/** A file size in decimal units, as the Finder shows it. */
export function formatBytes(bytes) {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "";
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  if (unit === 0) return plural(bytes, "byte");
  return `${value >= 100 ? Math.round(value) : Math.round(value * 10) / 10} ${units[unit]}`;
}

/* ── wall times in the user's zone (5.6) ────────────────────────────────
   A slot is stored in UTC and shown, and edited, in the user's preferred
   zone, which needn't be the browser's. */

const wallFormats = new Map();

function wallFormat(zone) {
  if (!wallFormats.has(zone)) {
    wallFormats.set(zone, new Intl.DateTimeFormat("en-US", {
      timeZone: zone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit" }));
  }
  return wallFormats.get(zone);
}

/** What a clock in `zone` shows at Unix time `seconds`:
    {year, month, day, hour, minute}, month 1-12. */
export function wallTime(seconds, zone) {
  const parts = wallFormat(zone).formatToParts(new Date(seconds * 1000));
  const get = type => Number(parts.find(p => p.type === type).value);
  return { year: get("year"), month: get("month"), day: get("day"), hour: get("hour"),
           minute: get("minute") };
}

/** The Unix time at which a clock in `zone` shows this wall time. A time a
    clock change skips lands after the change. */
export function fromWallTime({ year, month, day, hour, minute }, zone) {
  const wanted = Date.UTC(year, month - 1, day, hour, minute) / 1000;
  let guess = wanted;
  for (let i = 0; i < 3; i += 1) {
    const seen = wallTime(guess, zone);
    const diff = wanted - Date.UTC(seen.year, seen.month - 1, seen.day, seen.hour, seen.minute) / 1000;
    if (diff === 0) break;
    guess += diff;
  }
  return guess;
}

const pad = n => String(n).padStart(2, "0");

/** "2026-10-17T19:00", the value a datetime-local field takes. */
export function wallInputValue(seconds, zone) {
  const w = wallTime(seconds, zone);
  return `${w.year}-${pad(w.month)}-${pad(w.day)}T${pad(w.hour)}:${pad(w.minute)}`;
}

/** The Unix time a datetime-local value means in `zone`, or null. */
export function fromInputValue(value, zone) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value || "");
  if (!m) return null;
  const [year, month, day, hour, minute] = m.slice(1).map(Number);
  return fromWallTime({ year, month, day, hour, minute }, zone);
}

/** Today's date in `zone`, as "2026-10-17". */
export function todayIn(zone, now = Date.now() / 1000) {
  return wallInputValue(now, zone).slice(0, 10);
}

/** An ISO date `days` later. */
export function addDays(iso, days) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}

/** An ISO date as the browser's locale writes it: short ("Sat 17 Oct") or
    long ("Saturday 17 October"). The date has no zone of its own. */
export function dateLabel(iso, { long = false } = {}) {
  const [y, m, d] = iso.split("-").map(Number);
  const options = long ? { weekday: "long", day: "numeric", month: "long" }
                       : { weekday: "short", day: "numeric", month: "short" };
  return new Intl.DateTimeFormat(undefined, { ...options, timeZone: "UTC" })
    .format(new Date(Date.UTC(y, m - 1, d))).replace(",", "");
}

/** The zone's long name, "India Standard Time", or the zone id. */
export function zoneLongName(zone) {
  try {
    const parts = new Intl.DateTimeFormat(undefined, { timeZone: zone, timeZoneName: "long" })
      .formatToParts(new Date());
    return (parts.find(p => p.type === "timeZoneName") || {}).value || zone;
  } catch {
    return zone;
  }
}
