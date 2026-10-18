/* Whether Autopilot can publish anywhere yet (11.1), for the reminders
   outside the Setup screen: the notice on the other Autopilot screens and
   the line under the composer's profile summary. */
import { api } from "./api.js";

/** The required Setup items still open while no platform can publish; 0
    once one can. A platform already publishing is reminder enough: its
    own missing items show on its posts in the queue. */
export async function setupLeft() {
  const { items, platforms } = await api.get("/api/autopilot/readiness");
  if (Object.values(platforms || {}).some(p => p.ready)) return 0;
  return (items || []).filter(i => i.kind === "required" && !i.ok).length;
}

export const setupText = left =>
  `Autopilot can't publish yet: Setup has ${left} required ${left === 1 ? "item" : "items"} left.`;
