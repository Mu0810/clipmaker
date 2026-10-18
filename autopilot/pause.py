"""Pause and resume all publishing (design §7; Requirements 6.8-6.10).

`set_paused(owner, paused)` is what POST /api/autopilot/pause calls.

Pausing stores the flag first, so the publisher starts nothing more for the
owner from its next tick (6.9). The publisher itself then holds every
uploaded YouTube video with a publish time ahead (`yt_unschedule`): YouTube's
publish time is removed, the video stays private and the post gets
`paused_hold = 1`. Holding in the publisher rather than here also catches an
upload that finishes during the pause. A reschedule still pending from an
earlier resume becomes an unschedule, since it may already have restored the
publish time.

Resuming does its work while the owner is still paused, so the publisher
can't start a half-planned post, and clears the flag last (6.10):
  - a pending unschedule is dropped (a pending replacement comes back);
  - cleared `scheduled` posts whose slot passed more than a minute ago get
    the next free slots, losing any time the user had set (a slot under a
    minute old still publishes now, as in the missed-slot pass);
  - held videos whose slot passed are planned with them, by score, so several
    held videos never go public at once; the publisher then gives each held
    video its publish time back (`yt_reschedule`) at the later of its slot
    and 15 minutes from now;
  - `paused_hold` is cleared on posts no longer uploaded (rejected meanwhile).
"""
from __future__ import annotations

import threading

from autopilot import activity, planner, posts, publisher, store
from autopilot.owner import get_prefs, set_prefs
from platforms import youtube_api

_LOCK = threading.Lock()          # one pause or resume at a time


def _videos(n: int) -> str:
    return f"{n} uploaded YouTube video" + ("" if n == 1 else "s")


def _holding(conn, owner: str, now: float) -> list[str]:
    """Uploaded YouTube videos the pause will hold: a publish time ahead."""
    rows = conn.execute(
        "SELECT id FROM posts WHERE owner_id=? AND method='official' AND platform='youtube' "
        "AND status='uploaded' AND paused_hold=0 AND target_visibility='public' "
        "AND scheduled_publish_at > ? ORDER BY scheduled_publish_at", (owner, now)).fetchall()
    return [r["id"] for r in rows]


def set_paused(owner: str, paused: bool, *, now: float | None = None) -> dict:
    """Turn the pause on or off (6.8). Setting the state it already has
    changes nothing. Returns {"paused", "changed", ...} with the posts the
    change affects."""
    if not isinstance(paused, bool):
        raise TypeError("paused must be true or false")
    with _LOCK:
        now = posts._now() if now is None else now
        if bool(get_prefs(owner).get("paused")) == paused:
            return {"paused": paused, "changed": False}
        return _pause(owner, now) if paused else _resume(owner, now)


def _pause(owner: str, now: float) -> dict:
    set_prefs(owner, paused=True)
    with store.connect() as conn:
        pending = store.owner_posts(conn, owner, "method='official' AND step='yt_reschedule'")
        for post in pending:
            store.update_post(conn, post["id"], {"step": "yt_unschedule", "next_action_at": now,
                                                 "retry_count": 0, "updated_at": now})
        holding = _holding(conn, owner, now)
        summary = "Paused all publishing: nothing new is published until you resume."
        if holding:
            summary += (f" {_videos(len(holding))} with a publish time ahead "
                        f"{'is' if len(holding) == 1 else 'are'} kept private on YouTube until "
                        "then.")
        activity.log(owner, "user", "pause.on", summary,
                     detail={"holding": holding, "rescheduling": [p["id"] for p in pending]},
                     conn=conn)
    return {"paused": True, "changed": True, "holding": holding + [p["id"] for p in pending]}


def _resume(owner: str, now: float) -> dict:
    tz = posts.owner_timezone(owner)
    cutoff = now - publisher.MISSED_GRACE_S
    with store.connect() as conn:
        for post in store.owner_posts(conn, owner, "method='official' AND step='yt_unschedule'"):
            replace = "yt_replace" if (post.get("step_data") or {}).get("replace_with") else None
            store.update_post(conn, post["id"], {"step": replace,
                                                 "next_action_at": now if replace else None,
                                                 "retry_count": 0, "updated_at": now})
        conn.execute("UPDATE posts SET paused_hold=0 WHERE owner_id=? AND paused_hold=1 "
                     "AND status != 'uploaded'", (owner,))
        passed = [p for p in store.owner_posts(
            conn, owner, "method='official' AND status='scheduled' AND step IS NULL "
            "AND slot_at IS NOT NULL AND slot_at < ?", (cutoff,))
            if posts.is_cleared(p, paused=False)]
        held = store.owner_posts(conn, owner, "method='official' AND platform='youtube' "
                                 "AND status='uploaded' AND paused_hold=1")
        held_passed = [p for p in held if (p.get("slot_at") or 0) < cutoff]
        plan = []
        for post in passed + held_passed:
            locked = bool(post.get("user_locked"))
            fields = {"slot_at": None, "user_locked": 0, "updated_at": now}
            if post["status"] == "scheduled":
                fields.update({"status": "unscheduled", "slot_basis": None})
                what = f"The {posts._label(post)}'s slot"
            else:
                fields["scheduled_publish_at"] = None
                what = "The held YouTube video's slot"
            when = (f" at {planner.local_label(post['slot_at'], tz)}" if post.get("slot_at")
                    else "")
            store.update_post(conn, post["id"], fields)
            activity.log(owner, "system", "post.replanned",
                         f"{what}{when} passed while publishing was paused, so it moved to the "
                         "next free slot." + (" The time you chose for it was dropped."
                                              if locked else ""),
                         job_id=post.get("job_id"), clip_id=post.get("clip_id"),
                         post_id=post["id"], detail={"passed_slot": post.get("slot_at"),
                                                     "lock_dropped": locked}, conn=conn)
            plan.append({**post, **fields})
        results = posts._plan_posts(conn, owner, plan, now=now, tz=tz,
                                    apply=_apply) if plan else {}
    set_prefs(owner, paused=False)
    moved = [p["id"] for p in passed]
    summary = "Resumed publishing."
    if moved:
        summary += (f" {len(moved)} post{'' if len(moved) == 1 else 's'} whose slot passed "
                    "during the pause moved to the next free slots.")
    if held:
        summary += (f" {_videos(len(held))} held during the pause "
                    f"{'gets its' if len(held) == 1 else 'get their'} publish time back.")
    activity.log(owner, "user", "pause.off", summary,
                 detail={"replanned": moved, "releasing": [p["id"] for p in held]})
    return {"paused": False, "changed": True, "replanned": moved,
            "releasing": [p["id"] for p in held],
            "slots": {pid: r.get("slot_at") for pid, r in results.items()}}


def _apply(conn, owner: str, post: dict, result: dict, clip_idx, *, now: float, tz) -> None:
    """Store a re-planned slot: scheduled posts as usual; a held video keeps
    `uploaded` and gets its slot, and the publisher restores YouTube's publish
    time from it (yt_reschedule)."""
    if post["status"] != "uploaded":
        posts._apply_plan(conn, owner, post, result, clip_idx, now=now, tz=tz)
        return
    if result["status"] == "scheduled":
        slot, basis, note = result["slot_at"], result["slot_basis"], ""
    else:
        slot, basis = now + youtube_api.PUBLISH_LEAD_S, post.get("slot_basis")
        note = (" There's no free slot in the next 14 days, so it goes public 15 minutes after "
                "publishing resumed, even if that breaks the minimum gap or the daily maximum.")
    store.update_post(conn, post["id"], {"slot_at": slot, "slot_basis": basis, "last_error": None,
                                         "updated_at": now})
    activity.log(owner, "system", "post.rescheduled",
                 f"{posts._label(post, clip_idx)}: the YouTube video goes public at "
                 f"{planner.local_label(slot, tz)}.{note}", job_id=post.get("job_id"),
                 clip_id=post.get("clip_id"), post_id=post["id"], detail={"basis": basis},
                 conn=conn)
