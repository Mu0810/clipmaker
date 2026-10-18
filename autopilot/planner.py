"""Posting planner: candidate slots, slot assignment and account-data windows
(design §6.2, §6.3, Requirements 5.2, 5.3, 5.4, 5.6).

Everything here is pure: callers pass the posts, the slots already taken, the
profile settings, the owner's time zone and `now`, and get assignments back.
Times are UTC epoch seconds (5.6); `local_label` renders one for display in
the user's zone with the zone's name.

Slots come from the profile's posting windows, which map weekday ("0" =
Monday ... "6" = Sunday) to whole local hours. A window hour is turned into a
UTC instant with zoneinfo's PEP 495 rules at `fold=0`: on a spring-forward
day a nonexistent hour (02:00 in New York on 14 Mar 2027) moves forward one
hour to the same instant as 03:00, and on a fall-back day an ambiguous hour
(01:00 in New York on 1 Nov 2026) means its first occurrence, in daylight time.

Each owner has one account per platform (requirements, assumptions), so slots
are tracked per platform: posts and taken slots on YouTube never constrain
Instagram, and the reverse.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HORIZON_DAYS = 14                     # candidate slots: today plus the next 13 days
LEAD_S = 30 * 60                      # a slot must be at least 30 min away
UNSCHEDULED_REASON = "No free slot in the next 14 days"
BASIS_DEFAULT = "defaults"            # slot_basis values: defaults | your last N posts | user
BASIS_USER = "user"

# A post with one of these statuses keeps its slot while others are planned:
# waiting to publish, publishing, or already out on the platform.
OCCUPYING_STATUSES = frozenset({"scheduled", "publishing", "retrying", "uploaded",
                                "live", "private", "kept_private"})
# Statuses a re-plan may move (unpublished, not in flight); user-locked posts never move.
REPLANNABLE_STATUSES = frozenset({"scheduled", "unscheduled", "waiting_approval",
                                  "blocked", "needs_reconnect"})


def _zone(tz) -> ZoneInfo:
    if isinstance(tz, ZoneInfo):
        return tz
    return ZoneInfo(tz or "UTC")


def local_date(t: float, tz) -> date:
    """The calendar date of UTC instant `t` in time zone `tz`."""
    return datetime.fromtimestamp(t, _zone(tz)).date()


def local_label(t: float, tz) -> str:
    """`t` shown in the user's zone with the zone's name (5.6), e.g.
    "Sun 1 Nov 2026, 01:00 EDT (America/New_York)"."""
    zone = _zone(tz)
    dt = datetime.fromtimestamp(t, zone)
    return f"{dt:%a} {dt.day} {dt:%b %Y, %H:%M} {dt.tzname()} ({zone.key})"


def local_parts(t: float, tz) -> dict:
    """`t` in the user's zone as the review queue lays it out (5.6), with the
    same zone abbreviation as local_label: {"day": "2026-11-01", "time":
    "01:00", "zone": "EDT"}."""
    dt = datetime.fromtimestamp(t, _zone(tz))
    return {"day": dt.date().isoformat(), "time": f"{dt:%H:%M}", "zone": dt.tzname()}


def candidates(hours_by_weekday: dict, tz, now: float, *, days: int = HORIZON_DAYS,
               lead_s: float = LEAD_S) -> list[float]:
    """Every window hour over the next `days` local days, as sorted, distinct
    UTC instants at least `lead_s` after `now`. A nonexistent local hour lands
    one hour later, so it can coincide with the next window hour; duplicates
    are dropped."""
    zone = _zone(tz)
    today = datetime.fromtimestamp(now, zone).date()
    out: set[float] = set()
    for d in range(days):
        day = today + timedelta(days=d)
        for hour in set((hours_by_weekday or {}).get(str(day.weekday()), ())):
            hour = int(hour)
            if not 0 <= hour <= 23:
                continue
            t = datetime(day.year, day.month, day.day, hour, tzinfo=zone).timestamp()
            if t >= now + lead_s:
                out.add(t)
    return sorted(out)


def slot_time(post: dict) -> float | None:
    """When a post takes up its slot: the planned slot, else the time it was
    scheduled to go public, else when it went public."""
    for key in ("slot_at", "scheduled_publish_at", "published_at"):
        if post.get(key) is not None:
            return float(post[key])
    return None


def can_replan(post: dict) -> bool:
    return not post.get("user_locked") and post.get("status") in REPLANNABLE_STATUSES


def occupied_slots(existing: list[dict], replanning_ids=()) -> list[dict]:
    """The slots existing posts keep while other posts are planned (§6.2):
    user-locked posts, posts waiting to publish or in flight, and posts already
    out on the platform. Posts being re-planned free their slots, unless they
    are user-locked or past the point where a re-plan may move them.
    Returns [{"id", "platform", "slot_at"}]."""
    replanning = set(replanning_ids)
    out = []
    for post in existing:
        t = slot_time(post)
        if t is None:
            continue
        locked = bool(post.get("user_locked"))
        if not locked and post.get("status") not in OCCUPYING_STATUSES:
            continue
        if not locked and post.get("id") in replanning and can_replan(post):
            continue
        out.append({"id": post.get("id"), "platform": post.get("platform"), "slot_at": t})
    return out


def assign(posts: list[dict], occupied: list[dict], profile: dict, tz,
           now: float, *, earliest: float | None = None) -> dict[str, dict]:
    """Give each post the earliest free slot (§6.2, 5.2, 5.3), no earlier than
    `earliest` when given (9.5: after Instagram's publishing limit resets).

    `posts`: [{"id", "platform", "score", "clip_idx"}], placed by score
    (highest first), then clip number, then id. `occupied`: slots already
    taken, from `occupied_slots`. `profile`: settings with `windows`
    (platform -> weekday -> hours), `min_gap_hours`, `daily_max` and,
    optionally, `windows_basis` (platform -> label from `account_windows`).

    A slot is free when the account has fewer than `daily_max` posts on that
    local date and every taken slot is at least the minimum gap away. Returns
    {post id: {"status": "scheduled" | "unscheduled", "slot_at", "slot_basis",
    "reason"}}. The inputs are not modified."""
    zone = _zone(tz)
    windows = profile.get("windows") or {}
    bases = profile.get("windows_basis") or {}
    gap = float(profile.get("min_gap_hours", 4)) * 3600
    daily_max = int(profile.get("daily_max", 2))

    taken: dict[str, list[float]] = {}
    for slot in occupied:
        if slot.get("slot_at") is not None:
            taken.setdefault(slot.get("platform"), []).append(float(slot["slot_at"]))
    day_of: dict[float, date] = {}

    def day(t: float) -> date:
        if t not in day_of:
            day_of[t] = local_date(t, zone)
        return day_of[t]

    found: dict[str, list[float]] = {}
    order = sorted(posts, key=lambda p: (-(p.get("score") or 0), p.get("clip_idx") or 0,
                                         str(p.get("id"))))
    result: dict[str, dict] = {}
    for post in order:
        platform = post.get("platform")
        if platform not in found:
            found[platform] = [t for t in candidates(windows.get(platform) or {}, zone, now)
                               if earliest is None or t >= earliest]
        acc = taken.setdefault(platform, [])
        slot = None
        for t in found[platform]:
            if sum(1 for s in acc if day(s) == day(t)) >= daily_max:
                continue
            if any(abs(t - s) < gap for s in acc):
                continue
            slot = t
            break
        if slot is None:
            result[post["id"]] = {"status": "unscheduled", "slot_at": None,
                                  "slot_basis": None, "reason": UNSCHEDULED_REASON}
        else:
            acc.append(slot)
            result[post["id"]] = {"status": "scheduled", "slot_at": slot,
                                  "slot_basis": bases.get(platform, BASIS_DEFAULT),
                                  "reason": None}
    return result


# ── account-data windows (5.4) ───────────────────────────────────────────

SNAPSHOT_AGE_H = 72
MIN_ACCOUNT_POSTS = 10
MIN_BUCKET_POSTS = 2


def views_at_72h(snapshots: list[dict]) -> int | None:
    """A post's views at its first snapshot taken at age 72 h or later (the
    snapshot closest to 72 h among those old enough), or None when no snapshot
    is that old yet."""
    old = [s for s in snapshots or ()
           if s.get("age_hours") is not None and s["age_hours"] >= SNAPSHOT_AGE_H
           and s.get("views") is not None]
    if not old:
        return None
    return int(min(old, key=lambda s: s["age_hours"])["views"])


def _ranked(buckets: dict[int, list[int]]) -> list[int]:
    """Hours of the buckets with at least 2 posts, best average views first."""
    good = [(sum(v) / len(v), h) for h, v in buckets.items() if len(v) >= MIN_BUCKET_POSTS]
    return [h for _, h in sorted(good, key=lambda x: (-x[0], x[1]))]


def account_windows(account_metrics: list[dict], defaults: dict, *,
                    tz=None) -> tuple[dict[str, list[int]], str]:
    """Posting windows for one platform from the account's own results (§6.3).

    `account_metrics`: one entry per published post, {"published_at",
    "snapshots": [{"age_hours", "views"}]}. `defaults`: that platform's
    weekday -> hours from the profile. With fewer than 10 posts that have a
    72 h snapshot, the defaults are returned unchanged.

    Otherwise each weekday keeps as many hours as its defaults have (k),
    chosen in order: its own weekday-and-hour buckets with at least 2 posts,
    by average views; then hour-of-day buckets pooled across all weekdays,
    again with at least 2 posts; then its default hours. Hours are local to
    `tz`. Returns (windows, basis): basis is "your last N posts" when any
    window came from the data, else "defaults"."""
    zone = _zone(tz)
    normal = {str(d): sorted({int(h) for h in (defaults or {}).get(str(d), ())})
              for d in range(7)}
    samples = []
    for post in account_metrics or ():
        views = views_at_72h(post.get("snapshots") or [])
        if views is None or post.get("published_at") is None:
            continue
        local = datetime.fromtimestamp(float(post["published_at"]), zone)
        samples.append((local.weekday(), local.hour, views))
    if len(samples) < MIN_ACCOUNT_POSTS:
        return normal, BASIS_DEFAULT

    by_day_hour: dict[int, dict[int, list[int]]] = {}
    by_hour: dict[int, list[int]] = {}
    for weekday, hour, views in samples:
        by_day_hour.setdefault(weekday, {}).setdefault(hour, []).append(views)
        by_hour.setdefault(hour, []).append(views)
    pooled = _ranked(by_hour)

    windows, used = {}, False
    for d in range(7):
        k, chosen = len(normal[str(d)]), []
        for hour in _ranked(by_day_hour.get(d, {})) + pooled:
            if len(chosen) < k and hour not in chosen:
                chosen.append(hour)
                used = True
        for hour in normal[str(d)]:
            if len(chosen) < k and hour not in chosen:
                chosen.append(hour)
        windows[str(d)] = sorted(chosen)
    return windows, (f"your last {len(samples)} posts" if used else BASIS_DEFAULT)
