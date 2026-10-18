"""Posts and the review queue (design §6.1, §6.4, §7; Requirements 5.1, 5.5,
6.1-6.7, 13.4, 14.4, N3.2, N4.1).

A post is one clip going to one platform. `create_posts_for_job` turns a
processed source's clips into posts (skip, hold, approve-by-mode, block, then
plan). The rest are the review queue's actions: approve, reject, try a failed
post again, reset a field to the AI version, edit, move, add, bulk actions and
re-plan. Every function takes the owner from the caller (the session, never a
request field) and finds posts by id within that owner only, so another
owner's post is "not found".

Planning goes through autopilot/planner.py. A source is planned with the
profile settings stored when it was submitted (14.3); re-planning a profile
applies its current posting settings and records them on those sources (14.4).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from zoneinfo import ZoneInfo

from autopilot import activity, planner, readiness, store
from autopilot.owner import get_prefs
from platforms import limits

PLATFORMS = ("youtube", "instagram")
PLATFORM_NAMES = {"youtube": "YouTube", "instagram": "Instagram"}
FIELDS = {"youtube": ("title", "description", "tags", "category_id"),
          "instagram": ("caption",)}
FIELD_LABELS = {"title": "title", "description": "description", "tags": "tags",
                "category_id": "category", "caption": "caption",
                "target_visibility": "visibility", "disclosure_ai": "AI disclosure"}
POSTING_KEYS = ("windows", "min_gap_hours", "daily_max")

# 13.4: a clip already out on the account, uploaded to it, or kept private there.
PUBLISHED = ("live", "uploaded", "private", "kept_private")
EDITABLE = frozenset({"scheduled", "waiting_approval", "unscheduled", "blocked",
                      "needs_reconnect", "uploaded"})
APPROVABLE = frozenset({"scheduled", "waiting_approval", "unscheduled", "blocked",
                        "needs_reconnect"})
REJECTABLE = EDITABLE | {"failed"}
KNOWN_STATUSES = frozenset({"scheduled", "waiting_approval", "unscheduled", "blocked",
                            "publishing", "retrying", "uploaded", "live", "private",
                            "kept_private", "failed", "needs_reconnect", "rejected",
                            "cancelled"})
BULK_ACTIONS = ("approve", "reject")
MAX_BULK = 500
# Posts the user withdrew: rejected, or cancelled because the source was deleted.
WITHDRAWN = frozenset({"rejected", "cancelled"})
OUT = ("live", "private", "kept_private")       # published: kept as history (1.9)
CANCELLED_REASON = "The video it came from was deleted."


def is_out(post: dict) -> bool:
    """The post is published on the platform, so it's history to keep (1.9):
    live, private or kept private, or an Instagram Reel that went out and
    waits only for its link (a media id, or step ig_resolve)."""
    if post.get("status") in OUT:
        return True
    if post.get("platform") == "instagram" and post.get("method") == "official":
        data = post.get("step_data") or {}
        return bool(data.get("media_id") or post.get("platform_post_id")
                    or post.get("step") == "ig_resolve")
    return False


def cleanup_step(post: dict) -> str | None:
    """What the publisher must still do when a post is withdrawn (rejected,
    or its source deleted) so that nothing it started goes public by itself
    (1.9, 8.5). None when nothing was sent that could:
      - a YouTube video it uploaded: yt_delete, which leaves a public one;
      - an upload session whose video id isn't known: yt_abandon asks YouTube
        whether the upload finished, and deletes the video if it did;
      - an Instagram publish whose outcome isn't known: ig_abandon reads the
        container, and a Reel that went out is kept as published."""
    if post.get("method") != "official":
        return None
    data = post.get("step_data") or {}
    if post.get("platform") == "youtube":
        if post.get("status") == "uploaded" or post.get("platform_post_id") or data.get("video_id"):
            return "yt_delete"
        if data.get("session"):
            return "yt_abandon"
    elif post.get("platform") == "instagram" and post.get("step") == "ig_publish":
        return "ig_abandon"
    return None


def has_platform_state(post: dict) -> bool:
    """Something of the post is already on the platform or on its way there:
    a YouTube upload session or video, an Instagram container or Reel. Going
    on with the post must then build on it, never start a second one (10.1)."""
    data = post.get("step_data") or {}
    return bool(post.get("platform_post_id") or data.get("session") or data.get("container")
                or data.get("video_id") or data.get("media_id"))

HOLD_MESSAGES = {
    "unsupported_claims": "The AI couldn't back up a claim in the title or caption with "
                          "what's said in the clip.",
    "avoid_words": "A word from your avoid list is still in the text.",
    "heuristic_metadata": "No AI engine answered, so the title and caption were built "
                          "from keywords.",
    "sensitive": "The clip may be sensitive",
    "text_shaping": "The subtitles need text shaping (FriBiDi) that isn't installed, so "
                    "they may not render correctly.",
}


class PostError(Exception):
    """A refused action. `problems` lists [{field, message}] for a 422."""

    def __init__(self, status: int, message: str, problems: list[dict] | None = None):
        super().__init__(message)
        self.status, self.message, self.problems = status, message, problems or []


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def owner_timezone(owner: str) -> str:
    """The owner's time zone (5.6): their preference, else this machine's zone
    (TZ, then /etc/localtime), else UTC."""
    tz = get_prefs(owner).get("timezone")
    if tz:
        return tz
    names = [os.environ.get("TZ", "").strip().lstrip(":")]
    link = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in link:
        names.append(link.split("zoneinfo/", 1)[1])
    for name in names:
        if name:
            try:
                ZoneInfo(name)
                return name
            except Exception:
                continue
    return "UTC"


def make_holds(codes, *, now: float, sensitive_categories=()) -> list[dict]:
    """[{code, message, at}] for the §5.3 hold codes, without duplicates."""
    out, seen = [], set()
    for code in codes or ():
        if code in seen:
            continue
        seen.add(code)
        message = HOLD_MESSAGES.get(code, code)
        if code == "sensitive":
            cats = ", ".join(sensitive_categories or ())
            message += f": {cats}." if cats else "."
        out.append({"code": code, "message": message, "at": now})
    return out


def is_cleared(post: dict, *, paused: bool) -> bool:
    """§7: scheduled, approved (an override of any hold) or auto with no hold,
    and the owner not paused."""
    if paused or post.get("status") != "scheduled":
        return False
    if post.get("approval") == "approved":
        return True
    return post.get("approval") == "auto" and not post.get("holds")


def _label(post: dict, clip_idx=None) -> str:
    name = PLATFORM_NAMES.get(post.get("platform"), post.get("platform"))
    return f"{name} post for clip {clip_idx}" if clip_idx is not None else f"{name} post"


def _why_not(post: dict) -> str:
    status = post.get("status")
    if status in ("live", "private", "kept_private"):
        return "Already published, so it was skipped."
    if status in ("publishing", "retrying"):
        return "It's being published right now; try again in a moment."
    if status == "uploaded":
        return "It's already uploaded to YouTube."
    if status == "rejected":
        return "You already decided not to post it."
    if status == "cancelled":
        return CANCELLED_REASON
    if status == "failed":
        if post.get("method") == "official":
            return "Publishing failed; choose Try again, or Don't post."
        return "Publishing failed; choose Don't post, or add the clip again."
    return f"Not possible while the post is {status}."


def _get(conn, owner: str, post_id: str) -> dict:
    post = store.get_post(conn, owner, str(post_id or ""))
    if post is None:
        raise PostError(404, "Not found.")
    return post


def _clip_info(conn, owner: str, clip_ids) -> dict[str, dict]:
    ids = [c for c in dict.fromkeys(clip_ids) if c]
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT c.id, c.idx, c.score, c.score_reason, c.job_id, v.files_json "
        "FROM clips c LEFT JOIN clip_versions v ON v.id = c.current_version_id "
        f"WHERE c.owner_id=? AND c.id IN ({','.join('?' for _ in ids)})",
        (owner, *ids)).fetchall()
    return {r["id"]: store.decode_row(r) for r in rows}


def _snapshot(conn, owner: str, job_id) -> dict:
    """The profile settings a source was submitted with (14.3)."""
    row = conn.execute("SELECT profile_snapshot_json FROM jobs WHERE job_id=? "
                       "AND coalesce(user_id, 'local')=?", (job_id, owner)).fetchone()
    try:
        return json.loads(row["profile_snapshot_json"]) if row and row[0] else {}
    except (TypeError, ValueError):
        return {}


def view(post: dict, clip: dict | None, *, tz: str, paused: bool) -> dict:
    """A post as the API returns it: no owner id and no publisher step data
    (it can hold upload URLs), plus the clip's score, the slot shown in the
    owner's zone, which fields the user changed, and whether it's cleared."""
    out = {k: v for k, v in post.items() if k not in ("owner_id", "step_data")}
    clip = clip or {}
    out.update({"clip_idx": clip.get("idx"), "score": clip.get("score"),
                "score_reason": clip.get("score_reason"), "files": clip.get("files")})
    out["slot_label"] = planner.local_label(post["slot_at"], tz) if post.get("slot_at") else None
    meta, ai_meta = post.get("meta") or {}, post.get("ai_meta") or {}
    out["edited_fields"] = [f for f in FIELDS.get(post.get("platform"), ())
                            if meta.get(f) != ai_meta.get(f)]
    out["cleared"] = is_cleared(post, paused=paused)
    # 8.7: a video YouTube kept private links to the API audit form.
    out["audit_form"] = readiness.YT_AUDIT_FORM if post.get("status") == "kept_private" else None
    return out


# ── planning ─────────────────────────────────────────────────────────────

def _account_metrics(conn, owner: str, platform: str) -> list[dict]:
    """Live posts with their view snapshots, for planner.account_windows (5.4)."""
    rows = conn.execute(
        "SELECT p.id, p.published_at, m.age_hours, m.views FROM posts p "
        "LEFT JOIN post_metrics m ON m.post_id = p.id WHERE p.owner_id=? AND p.platform=? "
        "AND p.status='live' AND p.published_at IS NOT NULL", (owner, platform)).fetchall()
    posts: dict[str, dict] = {}
    for r in rows:
        entry = posts.setdefault(r["id"], {"published_at": r["published_at"], "snapshots": []})
        if r["age_hours"] is not None:
            entry["snapshots"].append({"age_hours": r["age_hours"], "views": r["views"]})
    return list(posts.values())


def _planner_profile(conn, owner: str, settings: dict, platforms, tz: str) -> dict:
    from autopilot import profiles

    defaults = settings.get("windows") or profiles.default_windows()
    windows, basis = {}, {}
    for platform in platforms:
        windows[platform], basis[platform] = planner.account_windows(
            _account_metrics(conn, owner, platform), defaults.get(platform) or {}, tz=tz)
    return {"windows": windows, "windows_basis": basis,
            "min_gap_hours": settings.get("min_gap_hours", 4),
            "daily_max": settings.get("daily_max", 2)}


def _apply_plan(conn, owner: str, post: dict, result: dict, clip_idx, *, now, tz) -> None:
    label = _label(post, clip_idx)
    if result["status"] == "scheduled":
        store.update_post(conn, post["id"], {
            "status": "scheduled", "slot_at": result["slot_at"],
            "slot_basis": result["slot_basis"], "last_error": None, "updated_at": now})
        activity.log(owner, "system", "post.planned",
                     f"{label} planned for {planner.local_label(result['slot_at'], tz)}",
                     job_id=post.get("job_id"), clip_id=post.get("clip_id"),
                     post_id=post["id"], detail={"basis": result["slot_basis"]}, conn=conn)
    else:
        store.update_post(conn, post["id"], {
            "status": "unscheduled", "slot_at": None, "slot_basis": None,
            "last_error": result["reason"], "updated_at": now})
        activity.log(owner, "system", "post.unscheduled", f"{label}: {result['reason']}",
                     job_id=post.get("job_id"), clip_id=post.get("clip_id"),
                     post_id=post["id"], conn=conn)


def _plan_posts(conn, owner: str, posts: list[dict], *, now: float, tz: str,
                settings_for=None, apply=None) -> dict[str, dict]:
    """Free these posts' slots and give them new ones, one source at a time
    (each source's posting settings may differ). Returns post id -> result.
    `apply(conn, owner, post, result, clip_idx, now=, tz=)` stores each
    result; the default sets the post scheduled or unscheduled."""
    apply = apply or _apply_plan
    clips = _clip_info(conn, owner, [p.get("clip_id") for p in posts])
    score = lambda p: (clips.get(p.get("clip_id")) or {}).get("score") or 0   # noqa: E731
    groups: dict = {}
    for post in posts:
        groups.setdefault(post.get("job_id"), []).append(post)
    results: dict[str, dict] = {}
    for job_id, group in sorted(groups.items(), key=lambda kv: -max(score(p) for p in kv[1])):
        settings = settings_for(job_id) if settings_for else _snapshot(conn, owner, job_id)
        inputs = [{"id": p["id"], "platform": p["platform"], "score": score(p),
                   "clip_idx": (clips.get(p.get("clip_id")) or {}).get("idx") or 0}
                  for p in group]
        occupied = planner.occupied_slots(store.owner_posts(conn, owner),
                                          replanning_ids={p["id"] for p in group})
        profile = _planner_profile(conn, owner, settings, {p["platform"] for p in group}, tz)
        out = planner.assign(inputs, occupied, profile, tz, now)
        for post in group:
            idx = (clips.get(post.get("clip_id")) or {}).get("idx")
            apply(conn, owner, post, out[post["id"]], idx, now=now, tz=tz)
            results[post["id"]] = out[post["id"]]
    return results


def free_slot_after(owner: str, post: dict, earliest: float, *,
                    now: float | None = None) -> dict:
    """The first free slot for `post` at or after `earliest`, under its
    source's posting windows, gap and daily maximum (9.5: once Instagram's
    publishing limit resets). The post's own current slot doesn't count as
    taken. Read-only: returns planner.assign's result, which the publisher
    stores with the step that asked."""
    now = _now() if now is None else now
    tz = owner_timezone(owner)
    with store.connect() as conn:
        clip = _clip_info(conn, owner, [post.get("clip_id")]).get(post.get("clip_id")) or {}
        settings = _snapshot(conn, owner, post.get("job_id"))
        others = [p for p in store.owner_posts(conn, owner) if p.get("id") != post["id"]]
        occupied = planner.occupied_slots(others)
        profile = _planner_profile(conn, owner, settings, {post["platform"]}, tz)
    inputs = [{"id": post["id"], "platform": post["platform"], "score": clip.get("score") or 0,
               "clip_idx": clip.get("idx") or 0}]
    return planner.assign(inputs, occupied, profile, tz, now, earliest=earliest)[post["id"]]


def _already_posted(conn, owner: str, platform: str, source_key, start: float,
                    end: float) -> bool:
    """13.4: a clip from the same source, overlapping this one by more than
    half of the shorter clip, is already published to this account. Each owner
    has one account per platform, so the account is the owner's platform."""
    if not source_key:
        return False
    rows = conn.execute(
        "SELECT c.src_start, c.src_end FROM posts p JOIN clips c ON c.id = p.clip_id "
        "JOIN jobs j ON j.job_id = p.job_id WHERE p.owner_id=? AND p.platform=? "
        f"AND p.status IN ({','.join('?' for _ in PUBLISHED)}) AND j.source_key=?",
        (owner, platform, *PUBLISHED, source_key)).fetchall()
    for r in rows:
        overlap = min(end, r["src_end"]) - max(start, r["src_start"])
        shorter = min(end - start, r["src_end"] - r["src_start"])
        if shorter > 0 and overlap > shorter / 2:
            return True
    return False


def platform_fields(meta: dict, platform: str) -> dict:
    """A clip's AI text for one platform, as the post's editable fields."""
    src = (meta or {}).get(platform) or {}
    if platform == "youtube":
        return {"title": str(src.get("title") or ""),
                "description": str(src.get("description") or ""),
                "tags": [str(t) for t in src.get("tags") or []],
                "category_id": str(src.get("category_id") or limits.DEFAULT_CATEGORY)}
    return {"caption": str(src.get("caption") or "")}


def _new_post(owner: str, clip: dict, platform: str, settings: dict, *, account_id,
              ready: tuple[bool, list[str]], now: float) -> dict:
    meta = clip.get("meta") or {}
    codes = list(meta.get("holds") or []) + list(
        (meta.get("platform_holds") or {}).get(platform) or [])
    holds = make_holds(codes, now=now, sensitive_categories=meta.get("sensitive_categories"))
    ok, reasons = ready
    fields = platform_fields(meta, platform)
    full_auto = settings.get("mode") == "full_auto"
    return {"id": _new_id(), "owner_id": owner, "job_id": clip["job_id"],
            "clip_id": clip["id"], "version_id": clip.get("current_version_id"),
            "platform": platform, "account_id": account_id, "method": "official",
            "status": "unscheduled" if ok else "blocked",
            "approval": "auto" if full_auto and not holds else "pending",
            "holds": holds, "slot_at": None, "slot_basis": None, "user_locked": 0,
            "target_visibility": (settings.get("youtube_visibility") or "public")
            if platform == "youtube" else "public",
            "meta": fields, "ai_meta": fields,
            "disclosure_ai": int(bool(settings.get("ai_disclosure"))),
            "last_error": None if ok else "; ".join(reasons),
            "last_error_class": None if ok else "setup",
            "created_at": now, "updated_at": now}


def _log_new(conn, owner: str, post: dict, clip: dict) -> None:
    label = _label(post, clip.get("idx"))
    if post["holds"]:
        activity.log(owner, "system", "post.held",
                     f"{label} is held: " + " ".join(h["message"] for h in post["holds"]),
                     job_id=post["job_id"], clip_id=clip["id"], post_id=post["id"],
                     detail={"holds": [h["code"] for h in post["holds"]]}, conn=conn)
    if post["status"] == "blocked":
        activity.log(owner, "system", "post.blocked", f"{label} is waiting: {post['last_error']}",
                     job_id=post["job_id"], clip_id=clip["id"], post_id=post["id"], conn=conn)


def create_posts_for_job(owner: str, job_id: str, *, now: float | None = None) -> dict:
    """§6.1 for a processed source: one post per profile platform for every
    clip reaching the minimum score (5.1), skipping clips already posted to the
    account (13.4), with holds, approval by mode (6.1, 6.4), `blocked` while a
    platform isn't ready, then slots from the planner. Returns
    {"created": [post ids], "skipped": [{clip_id, platform, reason}]}."""
    now = _now() if now is None else now
    tz = owner_timezone(owner)
    ready = {p: readiness.platform_ready(owner, p) for p in PLATFORMS}
    created, skipped, to_plan = [], [], []
    with store.connect() as conn:
        job = conn.execute("SELECT job_id, source_key, autopilot FROM jobs WHERE job_id=? "
                           "AND coalesce(user_id, 'local')=? AND deleted_at IS NULL",
                           (job_id, owner)).fetchone()
        if job is None:
            raise PostError(404, "Unknown video.")
        if not job["autopilot"]:
            return {"created": [], "skipped": []}      # Autopilot off: no posts (1.8)
        settings = _snapshot(conn, owner, job_id)
        platforms = [p for p in settings.get("platforms", PLATFORMS) if p in PLATFORMS]
        accounts = {r["platform"]: r["id"] for r in conn.execute(
            "SELECT id, platform FROM accounts WHERE owner_id=?", (owner,)).fetchall()}
        # A clip whose render failed or is still running gets no posts.
        clips = store.decode_rows(conn.execute(
            "SELECT c.* FROM clips c LEFT JOIN clip_versions v ON v.id = c.current_version_id "
            "WHERE c.job_id=? AND c.owner_id=? AND (c.current_version_id IS NULL "
            "OR v.status='ready') ORDER BY c.idx", (job_id, owner)).fetchall())
        for clip in clips:
            if (clip.get("score") or 0) < settings.get("min_score", 60):
                continue
            for platform in platforms:
                if _already_posted(conn, owner, platform, job["source_key"],
                                   clip["src_start"], clip["src_end"]):
                    reason = (f"Skipped: already posted to {PLATFORM_NAMES[platform]} "
                              f"(clip {clip['idx']} overlaps one that's out already)")
                    skipped.append({"clip_id": clip["id"], "platform": platform,
                                    "reason": reason})
                    activity.log(owner, "system", "post.skipped", reason, job_id=job_id,
                                 clip_id=clip["id"], conn=conn)
                    continue
                post = _new_post(owner, clip, platform, settings,
                                 account_id=accounts.get(platform), ready=ready[platform],
                                 now=now)
                store.insert_post(conn, post)
                _log_new(conn, owner, post, clip)
                created.append(post["id"])
                if post["status"] != "blocked":
                    to_plan.append(post)
        if to_plan:
            _plan_posts(conn, owner, to_plan, now=now, tz=tz)
    return {"created": created, "skipped": skipped}


def release_unapproved(now: float | None = None) -> list[str]:
    """§7, 6.3: a `scheduled` post still pending approval when its slot comes
    is not published; it moves to `waiting_approval` and its slot is released.
    Approving it later gives it the next free slot. Runs for every owner.
    Legacy schedules are left to their own rules (§8.7)."""
    now = _now() if now is None else now
    released = []
    with store.connect() as conn:
        rows = store.decode_rows(conn.execute(
            "SELECT * FROM posts WHERE status='scheduled' AND approval='pending' "
            "AND method='official' AND slot_at IS NOT NULL AND slot_at <= ?", (now,)).fetchall())
        for post in rows:
            store.update_post(conn, post["id"], {
                "status": "waiting_approval", "slot_at": None, "slot_basis": None,
                "user_locked": 0, "updated_at": now})
            activity.log(post["owner_id"], "system", "post.waiting_approval",
                         f"{_label(post)} reached its slot without approval, so it wasn't "
                         "published. Approve it to give it the next free slot.",
                         job_id=post.get("job_id"), clip_id=post.get("clip_id"),
                         post_id=post["id"], conn=conn)
            released.append(post["id"])
    return released


# ── the review queue ─────────────────────────────────────────────────────

def _sources_info(conn, owner: str, job_ids) -> dict[str, dict]:
    """Each source's title and the profile settings it was submitted with."""
    ids = [j for j in dict.fromkeys(job_ids) if j]
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT job_id, source_title, url, profile_snapshot_json FROM jobs "
        f"WHERE coalesce(user_id, 'local')=? AND job_id IN ({','.join('?' for _ in ids)})",
        (owner, *ids)).fetchall()
    out = {}
    for row in rows:
        try:
            settings = json.loads(row["profile_snapshot_json"]) if row["profile_snapshot_json"] else {}
        except (TypeError, ValueError):
            settings = {}
        out[row["job_id"]] = {"title": row["source_title"] or row["url"], "settings": settings}
    return out


def _views(conn, owner: str, posts: list[dict], tz: str, paused: bool) -> list[dict]:
    """Posts as the API returns them (`view`), plus what the review queue
    shows beside each (task 12.4): its source's title, the account's name,
    the slot's local day, time and zone abbreviation (5.6), the source's gap
    and daily maximum, and, for a slot the user chose, the gap and daily-limit
    warnings a move returns (§6.4), so they show again after a reload."""
    clips = _clip_info(conn, owner, [p.get("clip_id") for p in posts])
    sources = _sources_info(conn, owner, [p.get("job_id") for p in posts])
    accounts = conn.execute("SELECT id, platform, name FROM accounts WHERE owner_id=?",
                            (owner,)).fetchall()
    by_id = {a["id"]: a["name"] for a in accounts}
    by_platform = {a["platform"]: a["name"] for a in accounts}
    occupied = None
    out = []
    for post in posts:
        item = view(post, clips.get(post.get("clip_id")), tz=tz, paused=paused)
        source = sources.get(post.get("job_id")) or {}
        settings = source.get("settings") or {}
        item["source_title"] = source.get("title")
        item["account_name"] = by_id.get(post.get("account_id")) or by_platform.get(post["platform"])
        parts = planner.local_parts(post["slot_at"], tz) if post.get("slot_at") else {}
        item.update({"slot_day": parts.get("day"), "slot_time": parts.get("time"),
                     "slot_zone": parts.get("zone")})
        item["posting"] = {"min_gap_hours": float(settings.get("min_gap_hours", 4)),
                           "daily_max": int(settings.get("daily_max", 2))}
        item["warnings"] = []
        if (post.get("slot_at") and post.get("slot_basis") == planner.BASIS_USER
                and post.get("status") in planner.REPLANNABLE_STATUSES):
            if occupied is None:
                occupied = planner.occupied_slots(store.owner_posts(conn, owner))
            item["warnings"] = _slot_warnings(conn, owner, post, post["slot_at"], settings, tz,
                                              occupied=occupied)
        out.append(item)
    return out


def list_posts(owner: str, *, status: str | None = None, platform: str | None = None,
               source: str | None = None, start: float | None = None,
               end: float | None = None) -> dict:
    """GET /posts: the owner's posts by slot (unslotted last), with the platform
    limits for the editor's live counters and the pause state (6.8)."""
    where, params = [], []
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        unknown = [s for s in statuses if s not in KNOWN_STATUSES]
        if unknown:
            raise PostError(422, "Unknown status.", [{"field": "status",
                            "message": f"Unknown status: {', '.join(unknown)}"}])
        where.append(f"status IN ({','.join('?' for _ in statuses)})")
        params += statuses
    if platform:
        if platform not in PLATFORMS:
            raise PostError(422, "Unknown platform.", [{"field": "platform",
                            "message": "Use youtube or instagram."}])
        where.append("platform=?")
        params.append(platform)
    if source:
        where.append("job_id=?")
        params.append(source)
    if start is not None:
        where.append("slot_at >= ?")
        params.append(float(start))
    if end is not None:
        where.append("slot_at < ?")
        params.append(float(end))
    tz, paused = owner_timezone(owner), bool(get_prefs(owner).get("paused"))
    with store.connect() as conn:
        posts = store.owner_posts(conn, owner, " AND ".join(where), tuple(params))
        posts.sort(key=lambda p: (p.get("slot_at") is None, p.get("slot_at") or 0,
                                  p.get("created_at") or 0))
        items = _views(conn, owner, posts, tz, paused)
    return {"posts": items, "limits": limits.summary(), "paused": paused, "timezone": tz}


def get(owner: str, post_id: str) -> dict:
    tz, paused = owner_timezone(owner), bool(get_prefs(owner).get("paused"))
    with store.connect() as conn:
        return _views(conn, owner, [_get(conn, owner, post_id)], tz, paused)[0]


def _approve(conn, owner: str, post_id: str, *, now: float) -> tuple[dict, bool]:
    """Approve one post; returns (post, whether it now needs a slot)."""
    post = _get(conn, owner, post_id)
    if post["status"] not in APPROVABLE:
        raise PostError(409, _why_not(post))
    needs_slot = post["status"] in ("waiting_approval", "unscheduled") \
        and not post.get("user_locked")
    if post.get("approval") == "approved" and not needs_slot:
        raise PostError(409, "Already approved.")
    codes = [h["code"] for h in post.get("holds") or []]
    store.update_post(conn, post["id"], {"approval": "approved", "updated_at": now})
    summary = f"Approved {_label(post)}"
    if codes:
        summary += " (overriding its holds: " + ", ".join(codes) + ")"
    activity.log(owner, "user", "post.approved", summary, job_id=post.get("job_id"),
                 clip_id=post.get("clip_id"), post_id=post["id"],
                 detail={"override_holds": codes} if codes else None, conn=conn)
    return post, needs_slot


def approve(owner: str, post_id: str, *, now: float | None = None) -> dict:
    """Approve a post (6.2). Approving a held post is an explicit override and
    is logged with its hold codes (§7). A post whose slot passed unapproved, or
    that has no slot, gets the next free slot (6.3)."""
    now = _now() if now is None else now
    tz = owner_timezone(owner)
    with store.connect() as conn:
        post, needs_slot = _approve(conn, owner, post_id, now=now)
        if needs_slot:
            _plan_posts(conn, owner, [post], now=now, tz=tz)
    return get(owner, post_id)


def _reject(conn, owner: str, post_id: str, *, now: float) -> dict:
    post = _get(conn, owner, post_id)
    if post["status"] not in REJECTABLE:
        raise PostError(409, _why_not(post))
    fields = {"status": "rejected", "approval": "rejected", "slot_at": None,
              "slot_basis": None, "user_locked": 0, "updated_at": now}
    summary = f"Decided not to post the {_label(post)}"
    # Whatever the publish had started is cleaned up, never continued: an
    # uploaded video is deleted (8.5), and an upload or publish whose outcome
    # isn't known is checked first. A post waiting for a reconnect keeps no
    # publishing step, so reconnecting can't resume it.
    step = cleanup_step(post)
    fields.update({"step": step, "next_action_at": now if step else None, "retry_count": 0})
    if step in ("yt_delete", "yt_abandon"):
        summary += "; its private YouTube upload will be deleted"
    store.update_post(conn, post["id"], fields)
    activity.log(owner, "user", "post.rejected", summary, job_id=post.get("job_id"),
                 clip_id=post.get("clip_id"), post_id=post["id"], conn=conn)
    return post


def reject(owner: str, post_id: str, *, now: float | None = None) -> dict:
    """Reject a post (6.5): it never publishes and its slot is freed (§6.4)."""
    now = _now() if now is None else now
    with store.connect() as conn:
        _reject(conn, owner, post_id, now=now)
    return get(owner, post_id)


LEGACY_ONCE = ("A scheduled upload is tried only once, so it can't post twice. Schedule the clip "
               "again from the library.")


def _clip_version(conn, owner: str, post: dict) -> str | None:
    """The clip's current version when it's ready (7.7), else the post's own."""
    row = conn.execute(
        "SELECT v.id FROM clips c JOIN clip_versions v ON v.id = c.current_version_id "
        "WHERE c.id=? AND c.owner_id=? AND v.status='ready'",
        (post.get("clip_id"), owner)).fetchone()
    return row["id"] if row else post.get("version_id")


def retry(owner: str, post_id: str, *, now: float | None = None) -> dict:
    """Try a failed post again (10.10), with fresh retries, never publishing it twice:
      - nothing of it reached the platform: it goes back into the plan with the
        clip's current version (7.7), at its own time if that's still ahead,
        otherwise at the next free slot. The publisher then starts it afresh,
        its pre-publish checks included (10.8);
      - the platform holds part of it (has_platform_state): it carries on from
        the step that failed, which asks the platform first (10.1).
    A manually scheduled upload keeps today's rule and is tried only once."""
    from autopilot.publisher import FIRST_STEP           # the publisher imports this module

    now = _now() if now is None else now
    tz = owner_timezone(owner)
    with store.connect() as conn:
        post = _get(conn, owner, post_id)
        if post["status"] != "failed":
            raise PostError(409, "It isn't failed any more, so there's nothing to try again.")
        if post.get("method") != "official":
            raise PostError(409, LEGACY_ONCE)
        clip = _clip_info(conn, owner, [post.get("clip_id")]).get(post.get("clip_id")) or {}
        label = _label(post, clip.get("idx"))
        fields = {"retry_count": 0, "last_error": None, "last_error_class": None,
                  "updated_at": now}
        replan = False
        if has_platform_state(post):
            fields.update({"status": "retrying", "next_action_at": now,
                           "step": post.get("step") or FIRST_STEP[post["platform"]]})
            summary = f"Tried the {label} again; it carries on from where publishing stopped."
        else:
            fields.update({"step": None, "step_data": None, "next_action_at": None,
                           "scheduled_publish_at": None, "remote_dirty": 0, "paused_hold": 0,
                           "version_id": _clip_version(conn, owner, post)})
            if post.get("slot_at") and post["slot_at"] > now:
                fields["status"] = "scheduled"
                summary = (f"Tried the {label} again; it's planned for "
                           f"{planner.local_label(post['slot_at'], tz)}.")
            else:                                   # its time passed: the next free slot
                fields["user_locked"] = 0
                replan = True
                summary = f"Tried the {label} again."
        store.update_post(conn, post["id"], fields)
        activity.log(owner, "user", "post.retried", summary, job_id=post.get("job_id"),
                     clip_id=post.get("clip_id"), post_id=post["id"], conn=conn)
        if replan:
            _plan_posts(conn, owner, [{**post, **fields}], now=now, tz=tz)
    return get(owner, post_id)


def reset(owner: str, post_id: str, field: str, *, now: float | None = None) -> dict:
    """Put one field back to the AI's version (6.7)."""
    now = _now() if now is None else now
    with store.connect() as conn:
        post = _get(conn, owner, post_id)
        if field not in FIELDS[post["platform"]]:
            raise PostError(422, "Unknown field.", [{"field": "field", "message":
                            f"{PLATFORM_NAMES[post['platform']]} posts have no field "
                            f"'{field}'."}])
        if post["status"] not in EDITABLE:
            raise PostError(409, _why_not(post))
        meta = dict(post.get("meta") or {})
        meta[field] = (post.get("ai_meta") or {}).get(field)
        fields = {"meta": meta, "updated_at": now}
        if post["status"] == "uploaded":
            fields["remote_dirty"] = 1
        store.update_post(conn, post["id"], fields)
        activity.log(owner, "user", "post.reset",
                     f"Reset the {FIELD_LABELS[field]} of the {_label(post)} to the AI version",
                     job_id=post.get("job_id"), clip_id=post.get("clip_id"),
                     post_id=post["id"], conn=conn)
    return get(owner, post_id)


PATCHABLE = ("meta", "target_visibility", "disclosure_ai", "slot_at")


def _slot_warnings(conn, owner: str, post: dict, t: float, settings: dict, tz: str,
                   occupied: list[dict] | None = None) -> list[str]:
    """A user's move is allowed even when it breaks the gap or daily limit;
    the UI shows these warnings instead (§6.4). `occupied` (from
    planner.occupied_slots) saves reading the owner's posts once per post."""
    name = PLATFORM_NAMES[post["platform"]]
    if occupied is None:
        occupied = planner.occupied_slots(store.owner_posts(conn, owner))
    others = [o for o in occupied if o["id"] != post["id"] and o["platform"] == post["platform"]]
    gap_h = float(settings.get("min_gap_hours", 4))
    daily = int(settings.get("daily_max", 2))
    warnings = []
    close = sorted((o for o in others if abs(o["slot_at"] - t) < gap_h * 3600),
                   key=lambda o: abs(o["slot_at"] - t))
    if close:
        warnings.append(f"Less than {gap_h:g} h from another {name} post at "
                        f"{planner.local_label(close[0]['slot_at'], tz)}.")
    day = planner.local_date(t, tz)
    same_day = sum(1 for o in others if planner.local_date(o["slot_at"], tz) == day)
    if same_day >= daily:
        warnings.append(f"That day already has {same_day} {name} post"
                        f"{'s' if same_day != 1 else ''}; your daily maximum is {daily}.")
    return warnings


def _slot_value(value, now: float, problems: list[dict]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append({"field": "slot_at", "message": "slot_at must be a time (UTC seconds)."})
        return None
    if value <= now:
        problems.append({"field": "slot_at", "message": "Choose a time in the future."})
        return None
    return float(value)


def patch(owner: str, post_id: str, body: dict, *, now: float | None = None) -> dict:
    """Edit a post (6.5): its platform fields (checked against the §5.2 limits;
    the AI's version is kept for reset, 6.7), YouTube visibility, the AI
    disclosure (4.8), or its slot. A moved slot is locked so re-planning leaves
    it alone (5.5); `slot_at: null` removes the slot. Edits to a post already
    uploaded to YouTube set remote_dirty so the publisher updates it (8.4).
    Returns {"post", "warnings"}."""
    now = _now() if now is None else now
    if not isinstance(body, dict):
        raise PostError(422, "Send a JSON object.", [{"field": "body", "message": "Expected an object."}])
    problems = [{"field": k, "message": "This field can't be changed."}
                for k in body if k not in PATCHABLE]
    tz = owner_timezone(owner)
    warnings: list[str] = []
    with store.connect() as conn:
        post = _get(conn, owner, post_id)
        if post["status"] not in EDITABLE:
            raise PostError(409, _why_not(post))
        platform, fields, edited = post["platform"], {}, []
        if "meta" in body:
            changes = body["meta"]
            if not isinstance(changes, dict):
                problems.append({"field": "meta", "message": "meta must be an object."})
            else:
                allowed = FIELDS[platform]
                problems += [{"field": f"meta.{k}", "message":
                              f"{PLATFORM_NAMES[platform]} posts have no field '{k}'."}
                             for k in changes if k not in allowed]
                merged = {**(post.get("meta") or {}),
                          **{k: v for k, v in changes.items() if k in allowed}}
                check = limits.youtube_problems if platform == "youtube" else limits.instagram_problems
                problems += [{"field": f"meta.{p['field']}", "message": p["message"]}
                             for p in check(merged)]
                edited = [k for k in changes if k in allowed
                          and changes[k] != (post.get("meta") or {}).get(k)]
                fields["meta"] = merged
        if "target_visibility" in body:
            value = body["target_visibility"]
            if platform != "youtube":
                problems.append({"field": "target_visibility",
                                 "message": "Visibility applies to YouTube posts only."})
            elif value not in ("public", "private"):
                problems.append({"field": "target_visibility",
                                 "message": "Use public or private."})
            elif value != post.get("target_visibility"):
                fields["target_visibility"] = value
                edited.append("target_visibility")
        if "disclosure_ai" in body:
            if not isinstance(body["disclosure_ai"], bool):
                problems.append({"field": "disclosure_ai", "message": "Use true or false."})
            elif int(body["disclosure_ai"]) != int(post.get("disclosure_ai") or 0):
                fields["disclosure_ai"] = int(body["disclosure_ai"])
                edited.append("disclosure_ai")
        moved = False
        if "slot_at" in body:
            if post["status"] == "uploaded":
                problems.append({"field": "slot_at", "message":
                                 "This video is already uploaded to YouTube for its slot. "
                                 "Choose Don't post, then add the clip again to post it at another time."})
            elif body["slot_at"] is None:
                moved = True
                fields.update({"slot_at": None, "slot_basis": None, "user_locked": 1,
                               "last_error": "You removed the slot."})
                if post["status"] in ("scheduled", "waiting_approval"):
                    fields["status"] = "unscheduled"
            else:
                t = _slot_value(body["slot_at"], now, problems)
                if t is not None:
                    moved = True
                    fields.update({"slot_at": t, "slot_basis": planner.BASIS_USER,
                                   "user_locked": 1, "last_error": None})
                    if post["status"] in ("scheduled", "waiting_approval", "unscheduled"):
                        fields["status"] = "scheduled"
                    warnings = _slot_warnings(conn, owner, post, t,
                                              _snapshot(conn, owner, post.get("job_id")), tz)
        if problems:
            raise PostError(422, "Some changes aren't valid.", problems)
        if post["status"] == "uploaded" and edited:
            fields["remote_dirty"] = 1
        if not fields:
            return {"post": get(owner, post_id), "warnings": []}
        fields["updated_at"] = now
        store.update_post(conn, post["id"], fields)
        if edited:
            activity.log(owner, "user", "post.edited",
                         f"Edited the {', '.join(FIELD_LABELS.get(f, f) for f in edited)} "
                         f"of the {_label(post)}", job_id=post.get("job_id"),
                         clip_id=post.get("clip_id"), post_id=post["id"],
                         detail={"fields": edited}, conn=conn)
        if moved:
            when = planner.local_label(fields["slot_at"], tz) if fields.get("slot_at") else None
            activity.log(owner, "user", "post.rescheduled",
                         f"Moved the {_label(post)} to {when}" if when
                         else f"Removed the slot of the {_label(post)}",
                         job_id=post.get("job_id"), clip_id=post.get("clip_id"),
                         post_id=post["id"], detail={"warnings": warnings} if warnings else None,
                         conn=conn)
    return {"post": get(owner, post_id), "warnings": warnings}


def add(owner: str, clip_id: str, platform: str, slot_at=None, *,
        now: float | None = None) -> dict:
    """Add a post for one of the owner's clips (§6.4, 5.5). It starts from the
    clip's AI text for that platform and follows the source's mode and holds.
    With a slot it is locked there; without one it is planned. A clip already
    posted to the account (13.4) or already queued for it is refused."""
    now = _now() if now is None else now
    if platform not in PLATFORMS:
        raise PostError(422, "Unknown platform.", [{"field": "platform",
                        "message": "Use youtube or instagram."}])
    tz = owner_timezone(owner)
    ready = readiness.platform_ready(owner, platform)
    problems: list[dict] = []
    slot = None if slot_at is None else _slot_value(slot_at, now, problems)
    if problems:
        raise PostError(422, "Some values aren't valid.", problems)
    warnings: list[str] = []
    with store.connect() as conn:
        clip = store.get_clip(conn, owner, str(clip_id or ""))
        if clip is None or conn.execute("SELECT deleted_at FROM jobs WHERE job_id=?",
                                        (clip["job_id"],)).fetchone()["deleted_at"]:
            raise PostError(404, "Unknown clip.")
        if not (clip.get("meta") or {}).get(platform):
            raise PostError(409, f"This clip has no {PLATFORM_NAMES[platform]} text yet.")
        source_key = (conn.execute("SELECT source_key FROM jobs WHERE job_id=?",
                                   (clip["job_id"],)).fetchone() or [None])[0]
        if _already_posted(conn, owner, platform, source_key, clip["src_start"],
                           clip["src_end"]):
            raise PostError(409, f"This clip, or one overlapping it, is already posted to "
                                 f"your {PLATFORM_NAMES[platform]} account.")
        queued = conn.execute(
            "SELECT 1 FROM posts WHERE owner_id=? AND clip_id=? AND platform=? AND status "
            "NOT IN ('rejected','cancelled','failed')", (owner, clip["id"], platform)).fetchone()
        if queued:
            raise PostError(409, f"This clip already has a {PLATFORM_NAMES[platform]} post.")
        settings = _snapshot(conn, owner, clip["job_id"])
        account = conn.execute("SELECT id FROM accounts WHERE owner_id=? AND platform=?",
                               (owner, platform)).fetchone()
        post = _new_post(owner, clip, platform, settings,
                         account_id=account["id"] if account else None, ready=ready, now=now)
        if slot is not None:
            post.update({"slot_at": slot, "slot_basis": planner.BASIS_USER, "user_locked": 1})
            if post["status"] == "unscheduled":
                post["status"] = "scheduled"
            warnings = _slot_warnings(conn, owner, post, slot, settings, tz)
        store.insert_post(conn, post)
        activity.log(owner, "user", "post.added", f"Added a {_label(post, clip.get('idx'))}",
                     job_id=clip["job_id"], clip_id=clip["id"], post_id=post["id"], conn=conn)
        _log_new(conn, owner, post, clip)
        if slot is None and post["status"] != "blocked":
            _plan_posts(conn, owner, [post], now=now, tz=tz)
    return {"post": get(owner, post["id"]), "warnings": warnings}


def replace(owner: str, post_id: str, *, now: float | None = None) -> dict:
    """8.6: the user confirmed replacing an uploaded, not-yet-live YouTube video
    with the clip's newer version. The publisher's yt_replace deletes the
    upload and uploads the new version."""
    now = _now() if now is None else now
    with store.connect() as conn:
        post = _get(conn, owner, post_id)
        if post["platform"] != "youtube" or post["status"] != "uploaded":
            raise PostError(409, "Only a YouTube video that's uploaded and not yet public can be "
                                 "replaced.")
        if post.get("step"):
            raise PostError(409, "This post is busy on YouTube; try again in a moment.")
        clip = store.get_clip(conn, owner, post.get("clip_id") or "")
        current = (clip or {}).get("current_version_id")
        if not current or current == post.get("version_id"):
            raise PostError(409, "This clip has no newer version to upload.")
        version = conn.execute("SELECT version, status FROM clip_versions WHERE id=?",
                               (current,)).fetchone()
        if version is None or version["status"] != "ready":
            raise PostError(409, "The newer version is still rendering; try again when it's "
                                 "ready.")
        store.update_post(conn, post["id"], {
            "step": "yt_replace", "next_action_at": now, "updated_at": now,
            "step_data": {**(post.get("step_data") or {}), "replace_with": current}})
        activity.log(owner, "user", "post.version_changed",
                     f"Replacing the YouTube upload with version {version['version']} of the clip",
                     job_id=post.get("job_id"), clip_id=post.get("clip_id"), post_id=post["id"],
                     conn=conn)
    return get(owner, post_id)


def bulk(owner: str, ids, action: str, *, now: float | None = None) -> list[dict]:
    """Approve or reject several posts in one transaction (6.6). A post the
    action doesn't apply to is skipped with the reason, not an error; an
    unexpected failure rolls the whole action back. Returns one result per post."""
    now = _now() if now is None else now
    if action not in BULK_ACTIONS:
        raise PostError(422, "Unknown action.", [{"field": "action",
                        "message": "Use approve or reject."}])
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise PostError(422, "ids must be a list of post ids.",
                        [{"field": "ids", "message": "Send a list of post ids."}])
    if len(ids) > MAX_BULK:
        raise PostError(422, "Too many posts.", [{"field": "ids",
                        "message": f"At most {MAX_BULK} posts at a time."}])
    tz = owner_timezone(owner)
    results, to_plan = [], []
    with store.connect() as conn:
        for post_id in dict.fromkeys(ids):
            try:
                if action == "approve":
                    post, needs_slot = _approve(conn, owner, post_id, now=now)
                    if needs_slot:
                        to_plan.append(post)
                else:
                    _reject(conn, owner, post_id, now=now)
                results.append({"id": post_id, "ok": True})
            except PostError as exc:
                results.append({"id": post_id, "ok": False, "error": exc.message})
        if to_plan:
            _plan_posts(conn, owner, to_plan, now=now, tz=tz)
        for result in results:
            post = store.get_post(conn, owner, result["id"])
            if post:
                result["status"] = post["status"]
                result["slot_at"] = post.get("slot_at")
    return results


def replan_profile(owner: str, profile_id: str, *, now: float | None = None) -> dict:
    """"Re-plan pending posts" (14.4): apply the profile's current posting
    settings (windows, gap, daily maximum) to its sources' unpublished posts the
    user hasn't changed: scheduled or unscheduled, not user-locked. The new
    settings are recorded on those sources, so later approvals follow them.
    Posts waiting for approval keep waiting (they get a slot when approved)."""
    from autopilot import profiles

    now = _now() if now is None else now
    profile = profiles.get(owner, profile_id)
    if profile is None:
        raise PostError(404, "Preset not found.")
    settings = profile["settings"]
    tz = owner_timezone(owner)
    with store.connect() as conn:
        jobs = [r["job_id"] for r in conn.execute(
            "SELECT job_id FROM jobs WHERE profile_id=? AND coalesce(user_id, 'local')=?",
            (profile_id, owner)).fetchall()]
        for job_id in jobs:
            snapshot = _snapshot(conn, owner, job_id)
            snapshot.update({k: settings[k] for k in POSTING_KEYS if k in settings})
            conn.execute("UPDATE jobs SET profile_snapshot_json=? WHERE job_id=?",
                         (json.dumps(snapshot), job_id))
        posts = []
        if jobs:
            posts = store.owner_posts(
                conn, owner, f"job_id IN ({','.join('?' for _ in jobs)}) AND user_locked=0 "
                "AND status IN ('scheduled','unscheduled')", tuple(jobs))
        results = _plan_posts(conn, owner, posts, now=now, tz=tz) if posts else {}
        scheduled = sum(1 for r in results.values() if r["status"] == "scheduled")
        activity.log(owner, "user", "profile.replanned",
                     f"Re-planned {len(posts)} post{'s' if len(posts) != 1 else ''} with "
                     f"preset {profile['name']}", detail={"scheduled": scheduled,
                     "unscheduled": len(posts) - scheduled}, conn=conn)
    return {"replanned": len(posts), "scheduled": scheduled,
            "unscheduled": len(posts) - scheduled}


# ── disconnect and reconnect (11.7, §3.3) ────────────────────────────────

# An upload already scheduled on YouTube (`uploaded`) is left alone: it goes
# live on YouTube's own schedule whatever happens to our token.
WAIT_FOR_RECONNECT = ("scheduled", "unscheduled", "waiting_approval", "blocked",
                      "retrying", "publishing")


def hold_for_reconnect(owner: str, platform: str, reason: str, *,
                       now: float | None = None, conn=None) -> int:
    """Move the account's unpublished Autopilot posts to `needs_reconnect`
    (11.7, 10.7). A post interrupted mid-publish keeps its step and step data
    (its upload session or container), so the publish resumes, asking the
    platform first, once the account is back (10.1). Pass `conn` to run inside
    the caller's transaction. Returns how many posts moved."""
    now = _now() if now is None else now
    sql = (f"UPDATE posts SET status='needs_reconnect', last_error=?, updated_at=? "
           f"WHERE owner_id=? AND platform=? AND method='official' AND status IN "
           f"({','.join('?' for _ in WAIT_FOR_RECONNECT)})")
    args = (reason, now, owner, platform, *WAIT_FOR_RECONNECT)
    if conn is not None:
        return conn.execute(sql, args).rowcount
    with store.connect() as own:
        return own.execute(sql, args).rowcount


def restore_after_reconnect(owner: str, platform: str, *, now: float | None = None) -> dict:
    """§3.3 `needs_reconnect --> scheduled`: once the account is back, a post
    interrupted mid-publish resumes its step (`retrying`, due now); a post
    whose slot is still ahead keeps it; the others get the next free slots.
    If the platform still isn't ready (say the audience isn't confirmed),
    they wait as `blocked` with the reason instead."""
    now = _now() if now is None else now
    tz = owner_timezone(owner)
    ok, reasons = readiness.platform_ready(owner, platform)
    kept, replan, resumed = [], [], []
    with store.connect() as conn:
        waiting = store.owner_posts(conn, owner, "platform=? AND status='needs_reconnect' "
                                    "AND method='official'", (platform,))
        if not ok:
            for post in waiting:
                store.update_post(conn, post["id"], {"status": "blocked",
                                                     "last_error": "; ".join(reasons),
                                                     "last_error_class": "setup",
                                                     "updated_at": now})
            return {"kept": [], "replanned": [], "resumed": [],
                    "blocked": [p["id"] for p in waiting]}
        for post in waiting:
            if post.get("step"):
                store.update_post(conn, post["id"], {"status": "retrying", "next_action_at": now,
                                                     "last_error": None, "updated_at": now})
                resumed.append(post["id"])
            elif post.get("slot_at") and post["slot_at"] >= now + planner.LEAD_S:
                store.update_post(conn, post["id"], {"status": "scheduled", "last_error": None,
                                                     "updated_at": now})
                kept.append(post["id"])
            else:
                store.update_post(conn, post["id"], {"status": "unscheduled", "slot_at": None,
                                                     "slot_basis": None, "user_locked": 0,
                                                     "updated_at": now})
                replan.append({**post, "status": "unscheduled", "slot_at": None,
                               "user_locked": 0})
        if replan:
            _plan_posts(conn, owner, replan, now=now, tz=tz)
        # Steps on posts that kept their status (an uploaded video's check, a
        # rejected video's deletion) paused on the access error; run them now.
        conn.execute("UPDATE posts SET next_action_at=?, last_error=NULL, updated_at=? "
                     "WHERE owner_id=? AND platform=? AND method='official' "
                     "AND step IS NOT NULL AND next_action_at IS NULL "
                     "AND status IN ('uploaded','rejected','cancelled')",
                     (now, now, owner, platform))
    return {"kept": kept, "replanned": [p["id"] for p in replan], "resumed": resumed,
            "blocked": []}


def replan_blocked(owner: str, platform: str, *, now: float | None = None) -> dict:
    """11.2: posts blocked because the platform wasn't ready (`last_error_class`
    'setup') go ahead once it is. One whose slot is still ahead keeps it;
    the others get the next free slots, and a time the user had chosen that
    passed is dropped, with a note. Posts blocked for a missing rights record
    or a file the platform can't take stay blocked: the user has to act.
    Called by the hourly maintenance tick, and right after a connection or
    the audience declaration. Returns {"kept", "replanned"}."""
    now = _now() if now is None else now
    ok, _ = readiness.platform_ready(owner, platform)
    if not ok:
        return {"kept": [], "replanned": []}
    tz = owner_timezone(owner)
    kept, replan = [], []
    with store.connect() as conn:
        waiting = store.owner_posts(conn, owner, "platform=? AND status='blocked' "
                                    "AND method='official' AND last_error_class='setup'",
                                    (platform,))
        for post in waiting:
            if post.get("slot_at") and post["slot_at"] >= now + planner.LEAD_S:
                store.update_post(conn, post["id"], {"status": "scheduled", "last_error": None,
                                                     "last_error_class": None,
                                                     "updated_at": now})
                activity.log(owner, "system", "post.planned",
                             f"{PLATFORM_NAMES[platform]} is ready, so the {_label(post)} "
                             f"keeps its slot, {planner.local_label(post['slot_at'], tz)}",
                             job_id=post.get("job_id"), clip_id=post.get("clip_id"),
                             post_id=post["id"], conn=conn)
                kept.append(post["id"])
                continue
            dropped = bool(post.get("user_locked") and post.get("slot_at"))
            store.update_post(conn, post["id"], {"status": "unscheduled", "slot_at": None,
                                                 "slot_basis": None, "user_locked": 0,
                                                 "last_error": None, "last_error_class": None,
                                                 "updated_at": now})
            if dropped:
                activity.log(owner, "system", "post.replanned",
                             f"The time you chose for the {_label(post)} passed while "
                             f"{PLATFORM_NAMES[platform]} wasn't ready (or is now less than 30 "
                             "minutes away), so it was dropped and the post moved to the next "
                             "free slot.",
                             job_id=post.get("job_id"), clip_id=post.get("clip_id"),
                             post_id=post["id"], detail={"lock_dropped": True}, conn=conn)
            replan.append({**post, "status": "unscheduled", "slot_at": None, "user_locked": 0})
        if replan:
            _plan_posts(conn, owner, replan, now=now, tz=tz)
    return {"kept": kept, "replanned": [p["id"] for p in replan]}
