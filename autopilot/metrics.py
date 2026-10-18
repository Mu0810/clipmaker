"""Views, likes and comments for published posts (design §11; Requirements
12.5, 5.4).

`fetch_due(now)` runs on every maintenance tick. A post is due when it's an
Autopilot post out on the platform (live, private or kept private, with its
platform id), published between 1 hour and 30 days ago, and its last
snapshot is at least 23 hours old. So each post gets one snapshot a day, at
about the same age every day (1, 24, 48, 72 h, ...): the planner reads each
post's views at 72 h for the account's best hours (5.4), and 23 hours rather
than 24 means an hourly tick never skips a day (12.5).

- YouTube: videos.list part=statistics, 50 ids per call (1 unit each). The
  day's units stop at 9,000, so publishing keeps headroom (§8.2). Each
  batch's rows are stored before the next is asked for, so a stop keeps what
  was fetched; the rest wait for YouTube's quota day to reset (midnight
  Pacific). A video that's gone from YouTube gets no row.
- Instagram: /<media id>/insights, one call per post, only when the account
  granted instagram_manage_insights (the connect screen warns when it
  didn't). A Reel Instagram no longer has is skipped. Meta notes insights
  can lag by up to 48 h.

Each fetch adds a post_metrics row with `age_hours`. Manual scheduled
uploads (method 'legacy') aren't fetched: many went out through the manual
connections, possibly to another channel, and counting them would skew the
account's own data (5.4).
"""
from __future__ import annotations

import logging
import time

from autopilot import accounts, activity, store
from platforms import errors, youtube_api
from platforms.instagram_api import InstagramAPI

log = logging.getLogger(__name__)

WINDOW_S = 30 * 86400            # 12.5: for 30 days after publishing
MIN_AGE_S = 3600                 # the platforms need a moment to count anything
EVERY_S = 23 * 3600              # once a day, never skipping one with an hourly tick
BATCH = 50
INSIGHTS_SCOPE = "instagram_manage_insights"
NAMES = {"youtube": "YouTube", "instagram": "Instagram"}


def due(now: float) -> list[dict]:
    """Posts due for a snapshot, most overdue first."""
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT p.id, p.owner_id, p.platform, p.platform_post_id, p.published_at, "
            "max(m.fetched_at) AS last FROM posts p "
            "LEFT JOIN post_metrics m ON m.post_id = p.id "
            "WHERE p.method='official' AND p.status IN ('live', 'private', 'kept_private') "
            "AND p.platform_post_id IS NOT NULL AND p.published_at IS NOT NULL "
            "AND p.published_at <= ? AND p.published_at >= ? "
            "GROUP BY p.id HAVING last IS NULL OR last <= ? "
            "ORDER BY coalesce(last, 0), p.published_at, p.id",
            (now - MIN_AGE_S, now - WINDOW_S, now - EVERY_S)).fetchall()
    return [dict(r) for r in rows]


def _store(post: dict, stats: dict, now: float) -> None:
    with store.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO post_metrics(post_id, fetched_at, age_hours, views, likes, "
            "comments) VALUES(?,?,?,?,?,?)",
            (post["id"], now, round((now - post["published_at"]) / 3600, 3),
             stats.get("views"), stats.get("likes"), stats.get("comments")))


def _youtube(owner: str, group: list[dict], now: float) -> tuple[list[str], list[str]]:
    """Returns (post ids fetched, post ids the day's unit cap left unasked)."""
    done: list[str] = []
    with youtube_api.YouTubeAPI(owner, clock=lambda: now) as api:
        for start in range(0, len(group), BATCH):
            batch = group[start:start + BATCH]
            try:
                stats = api.statistics([p["platform_post_id"] for p in batch])
            except errors.PlatformError as exc:
                if exc.result.code == "metricsCap":       # checked before any call is made
                    return done, [p["id"] for p in group[start:]]
                raise
            for post in batch:
                if post["platform_post_id"] in stats:
                    _store(post, stats[post["platform_post_id"]], now)
                    done.append(post["id"])
    return done, []


def _instagram(owner: str, group: list[dict], now: float) -> list[str]:
    done: list[str] = []
    with InstagramAPI(owner, clock=lambda: now) as api:
        for post in group:
            try:
                stats = api.insights(post["platform_post_id"])
            except errors.PlatformError as exc:
                if exc.kind != errors.PERMANENT:
                    raise
                log.info("Skipped Instagram insights for post %s: %s", post["id"], exc)
                continue                                   # this Reel only (deleted, say)
            _store(post, stats, now)
            done.append(post["id"])
    return done


def fetch_due(now: float | None = None) -> dict:
    """One pass. Returns {"fetched": [post ids], "waiting": [post ids the
    YouTube cap stopped], "skipped": [post ids whose account can't be read]}."""
    now = time.time() if now is None else now
    groups: dict[tuple[str, str], list[dict]] = {}
    for post in due(now):
        groups.setdefault((post["owner_id"], post["platform"]), []).append(post)
    report: dict = {"fetched": [], "waiting": [], "skipped": []}
    per_owner: dict[str, dict] = {}
    for (owner, platform), group in groups.items():
        account = accounts.get(owner, platform)
        if not account or account.get("status") != "ok" or (
                platform == "instagram" and INSIGHTS_SCOPE not in (account.get("scopes") or [])):
            report["skipped"] += [p["id"] for p in group]
            continue
        waiting: list[str] = []
        try:
            if platform == "youtube":
                done, waiting = _youtube(owner, group, now)
            else:
                done = _instagram(owner, group, now)
        except errors.PlatformError as exc:
            # The day's account check (maintenance) deals with lost access;
            # anything else is tried again on the next tick.
            log.warning("Couldn't fetch %s metrics for %s: %s", platform, owner, exc)
            report["skipped"] += [p["id"] for p in group]
            continue
        report["fetched"] += done
        report["waiting"] += waiting
        note = per_owner.setdefault(owner, {"counts": {}, "waiting": 0})
        if done:
            note["counts"][platform] = len(done)
        note["waiting"] += len(waiting)
    for owner, note in per_owner.items():
        if not note["counts"]:
            continue                 # a cap reached before anything was fetched isn't news
        counts = note["counts"]
        parts = [f"{counts[p]} {NAMES[p]} post{'s' if counts[p] != 1 else ''}"
                 for p in ("youtube", "instagram") if p in counts]
        summary = f"Fetched views, likes and comments for {' and '.join(parts)}."
        if note["waiting"]:
            summary += (f" YouTube's daily quota for view counts is used up, so "
                        f"{note['waiting']} more wait until midnight Pacific time.")
        activity.log(owner, "system", "metrics.fetched", summary, at=now,
                     detail={"counts": note["counts"], "waiting": note["waiting"]})
    return report
