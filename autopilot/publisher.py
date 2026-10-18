"""The publisher: one durable state machine per post, advanced by a single loop
that holds a database lease (design §8.1, §8.5, §8.8, KD5; Requirements 6.3,
6.4, 10.1, 10.2, 10.3, 10.5, 10.7-10.9, N2.1).

Every 20 s, `Publisher.tick()`:

1. acquires or renews the `publisher_lease` (60 s). Without it, it does
   nothing, so two server processes on one database never both publish;
2. on the first tick that holds the lease (start-up or takeover), runs the
   missed-slot pass (§8.5), then writes the `publisher_heartbeat`;
3. releases unapproved posts whose slot came (posts.release_unapproved, 6.3);
4. collects due work, skipping paused owners except for the steps in
   PAUSE_STEPS (stopping a video going public, or finishing the record of an
   upload the platform already has):
   - new publishes of cleared posts: YouTube Public targets up to 24 h
     before the slot, YouTube Private targets and Instagram at the slot;
   - steps whose `next_action_at` has come (resumes, retries, cleanup);
   - uploaded YouTube posts: `yt_update` when edited, `yt_verify` from
     15 minutes after the publish time;
   - while paused, uploaded YouTube videos with a publish time ahead are held
     (`yt_unschedule`, 6.9); after the resume, held ones get their publish
     time back (`yt_reschedule`, 6.10). Task 9.4's autopilot/pause.py moves
     posts whose slots passed during the pause;
5. dispatches each item to its account's single-thread executor, so at most
   one step per account runs at a time (10.9).

Before a publish starts, the post's platform must be ready (otherwise it
waits as `needs_reconnect` or `blocked`), its source must have rights
recorded, and its file must pass the format check (§8.8, 10.8).

Steps are registered by name (`register`). A handler gets a StepContext,
makes its platform calls, and returns a StepResult, or raises
errors.PlatformError. The publisher then writes the `publish_attempts` row,
the post update and any activity entries in one transaction. It never holds
the database during a platform call. Errors follow §8.6: temporary errors
retry after 2, 10 and 30 minutes and then fail; deferrals wait without using
a retry; an access error moves every post of the account to
`needs_reconnect`; permanent errors fail at once.

Manually scheduled uploads (method = 'legacy', autopilot/legacy.py) run at
their time through today's upload functions, once: no format, rights or
readiness checks, no retries, and one left uploading by a restart is marked
failed on the next start (§8.7, task 9.5). The pause holds them too; after
the resume, one whose time passed goes out at once, as after downtime today.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import paths
from autopilot import accounts, activity, planner, posts, readiness, store
from autopilot.owner import get_prefs
from platforms import errors, limits
from platforms.http import redact

log = logging.getLogger(__name__)

TICK_S = 20
LEASE_TTL_S = 60
LEASE_KEY = "publisher_lease"
HEARTBEAT_KEY = "publisher_heartbeat"
MISSED_GRACE_S = 60                     # a slot this far in the past counts as missed (§8.5)
MISSED_PUBLISH_WITHIN_S = 2 * 3600      # ... and is still published if it passed less than this
YT_EARLY_UPLOAD_S = 24 * 3600           # Public YouTube posts upload up to 24 h ahead (8.2)
VERIFY_AFTER_S = 15 * 60                # first visibility check after the publish time (8.7)
PROCESSING_RECHECK_S = 60               # Instagram "media not ready" (9.3)
LIMIT_RECHECK_S = 3600                  # a deferral with no known end
CHAIN_LIMIT = 25                        # steps run back to back for one post per dispatch

FIRST_STEP = {"youtube": "yt_init", "instagram": "ig_limit"}
# Steps that run even while the owner is paused (§7, task 9.4): the ones that
# stop a video going public, and the ones that only finish recording an upload
# or a Reel the platform already has, so the pause can then hold the video.
# None of them sends a video or publishes anything.
PAUSE_STEPS = frozenset({"yt_unschedule", "yt_delete", "yt_finalize", "yt_lookup",
                         "ig_resolve", "yt_abandon", "ig_abandon"})
# Uploaded YouTube posts with one of these steps pending can still be held:
# yt_verify and yt_update come back by themselves after the resume, and a
# pending replacement is kept (yt_unschedule hands it on).
HOLDABLE_STEPS = (None, "yt_verify", "yt_update", "yt_replace")
# Statuses a post can carry a pending step in.
CONTINUE_STATUSES = ("publishing", "retrying", "rejected", "cancelled", "uploaded")
# A step on a post in one of these statuses doesn't change what the post is
# while it waits or retries: an uploaded video is still scheduled on YouTube,
# and a rejected post stays rejected while its upload is deleted.
KEEP_STATUS = frozenset({"uploaded", "rejected", "cancelled"})
PLATFORM_NAMES = {"youtube": "YouTube", "instagram": "Instagram"}


# ── steps ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Step:
    name: str
    platform: str
    handler: Callable


_REGISTRY: dict[str, Step] = {}


def register(name: str, platform: str, handler: Callable) -> None:
    """Register the handler for one publishing step (tasks 9.2 and 9.3)."""
    _REGISTRY[name] = Step(name, platform, handler)


@dataclass
class StepContext:
    post: dict                       # the post as stored, read just before the step
    now: float
    publisher: "Publisher"

    @property
    def owner(self) -> str:
        return self.post["owner_id"]

    @property
    def step_data(self) -> dict:
        return dict(self.post.get("step_data") or {})


@dataclass
class StepResult:
    """What a step did. `status` is the post's status afterwards; with a
    `next_step`, that step runs at `next_action_at` (default now). `fields`
    are other post columns to set (step_data, platform_post_id, url, ...)."""
    status: str
    next_step: str | None = None
    next_action_at: float | None = None
    fields: dict = field(default_factory=dict)
    outcome: str = "ok"
    message: str | None = None
    activity: list = field(default_factory=list)          # (kind, summary, detail)
    account_fields: dict = field(default_factory=dict)    # last_publish_at, thumbnail_capability


ACCOUNT_FIELDS = frozenset({"last_publish_at", "thumbnail_capability"})


# ── lease and heartbeat ──────────────────────────────────────────────────

def acquire_lease(holder: str, now: float, ttl: float = LEASE_TTL_S, *,
                  key: str = LEASE_KEY) -> bool:
    """Take or renew the publisher lease (KD5), or another lease named by
    `key`. One statement decides, so two processes can't both win: it updates
    the row only when this holder has it already or it has expired."""
    value = json.dumps({"holder": holder, "expires_at": now + ttl})
    with store.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO app_state(key, value_json) VALUES(?, ?)",
                     (key, value))
        cur = conn.execute(
            "UPDATE app_state SET value_json=? WHERE key=? AND "
            "(json_extract(value_json, '$.holder') = ? OR "
            " json_extract(value_json, '$.expires_at') < ?)",
            (value, key, holder, now))
        return cur.rowcount == 1


def release_lease(holder: str, *, key: str = LEASE_KEY) -> None:
    """Give the lease up at shutdown, so another process can take over at once."""
    with store.connect() as conn:
        conn.execute("UPDATE app_state SET value_json=json_set(value_json, '$.expires_at', 0) "
                     "WHERE key=? AND json_extract(value_json, '$.holder') = ?",
                     (key, holder))


def lease_holder() -> dict | None:
    return store.state_get(LEASE_KEY)


@dataclass
class Work:
    post_id: str
    owner: str
    platform: str
    kind: str                 # start | continue | maintain | hold | release | legacy
    step: str
    when: float               # for ordering within an account

    @property
    def account(self) -> str:
        return f"{self.owner}:{self.platform}"


def _label(post: dict) -> str:
    return f"{PLATFORM_NAMES.get(post.get('platform'), post.get('platform'))} post"


def _publish_time(post: dict) -> float | None:
    return post.get("scheduled_publish_at") or post.get("slot_at")


class Publisher:
    def __init__(self, *, clock: Callable[[], float] = time.time, holder: str | None = None):
        self.clock = clock
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._lock = threading.Lock()
        self._executors: dict[str, concurrent.futures.ThreadPoolExecutor] = {}
        self._inflight: set[str] = set()
        self._futures: list[concurrent.futures.Future] = []
        self._had_lease = False
        self._warned: set[str] = set()

    # ── the loop ────────────────────────────────────────────────────────

    def tick(self, now: float | None = None) -> dict:
        """One pass. Returns what it did, for tests and logs."""
        now = self.clock() if now is None else now
        if not acquire_lease(self.holder, now):
            self._had_lease = False
            return {"lease": False, "missed": None, "interrupted": [], "released": [],
                    "dispatched": []}
        missed, interrupted = None, []
        if not self._had_lease:
            missed = self._missed_pass(now, store.state_get(HEARTBEAT_KEY))
            interrupted = self._legacy_interrupted(now)
        self._had_lease = True
        store.state_set(HEARTBEAT_KEY, {"at": now, "holder": self.holder})
        released = posts.release_unapproved(now)
        dispatched = [self._dispatch(work) for work in self._collect(now)]
        return {"lease": True, "missed": missed, "interrupted": interrupted,
                "released": released,
                "dispatched": [(w.post_id, w.step) for w in dispatched if w]}

    def _paused_owners(self) -> Callable[[str], bool]:
        cache: dict[str, bool] = {}

        def paused(owner: str) -> bool:
            if owner not in cache:
                cache[owner] = bool(get_prefs(owner).get("paused"))
            return cache[owner]

        return paused

    # ── missed slots (§8.5, 10.5) ───────────────────────────────────────

    def _missed_pass(self, now: float, heartbeat: dict | None) -> dict:
        """Cleared posts whose slot passed more than a minute ago and never
        started. Per account, the most recent one still publishes if its slot
        passed less than 2 h ago (normal collection starts it this tick); the
        others get the next free slots, losing any time the user had set.
        Uploaded YouTube posts aren't missed: YouTube publishes them itself.
        Paused owners are left to the resume (6.10)."""
        paused = self._paused_owners()
        last = (heartbeat or {}).get("at")
        with store.connect() as conn:
            rows = store.decode_rows(conn.execute(
                "SELECT * FROM posts WHERE method='official' AND status='scheduled' "
                "AND step IS NULL AND slot_at IS NOT NULL AND slot_at < ? ORDER BY slot_at DESC",
                (now - MISSED_GRACE_S,)).fetchall())
        groups: dict[tuple, list[dict]] = {}
        for post in rows:
            if paused(post["owner_id"]) or not posts.is_cleared(post, paused=False):
                continue
            groups.setdefault((post["owner_id"], post["platform"]), []).append(post)
        publishing, replanned = [], []
        for (owner, platform), group in groups.items():
            tz = posts.owner_timezone(owner)
            latest, others = group[0], group[1:]
            note = (f" Clip Maker was last running at {planner.local_label(last, tz)}."
                    if last else "")
            if now - latest["slot_at"] < MISSED_PUBLISH_WITHIN_S:
                publishing.append(latest["id"])
                activity.log(owner, "system", "publish.missed",
                             f"The {_label(latest)}'s slot "
                             f"({planner.local_label(latest['slot_at'], tz)}) passed while "
                             f"Clip Maker wasn't running, so it's publishing now.{note}",
                             job_id=latest.get("job_id"), clip_id=latest.get("clip_id"),
                             post_id=latest["id"], detail={"last_running_at": last})
            else:
                others = group
            if others:
                replanned += self._replan_missed(owner, others, now=now, tz=tz, note=note,
                                                 last=last)
        return {"publishing": publishing, "replanned": replanned}

    def _replan_missed(self, owner: str, missed: list[dict], *, now: float, tz: str,
                       note: str, last) -> list[str]:
        with store.connect() as conn:
            for post in missed:
                locked = bool(post.get("user_locked"))
                store.update_post(conn, post["id"], {"status": "unscheduled", "slot_at": None,
                                                     "slot_basis": None, "user_locked": 0,
                                                     "updated_at": now})
                activity.log(owner, "system", "publish.missed",
                             f"The {_label(post)}'s slot "
                             f"({planner.local_label(post['slot_at'], tz)}) passed while Clip "
                             "Maker wasn't running, so it moved to the next free slot."
                             + (" The time you chose for it was dropped." if locked else "")
                             + note, job_id=post.get("job_id"), clip_id=post.get("clip_id"),
                             post_id=post["id"],
                             detail={"missed_slot": post["slot_at"], "lock_dropped": locked,
                                     "last_running_at": last}, conn=conn)
            posts._plan_posts(conn, owner, [{**p, "status": "unscheduled", "slot_at": None,
                                             "user_locked": 0} for p in missed],
                              now=now, tz=tz)
        return [p["id"] for p in missed]

    # ── due work (§8.1 step 3) ──────────────────────────────────────────

    def _collect(self, now: float) -> list[Work]:
        paused = self._paused_owners()
        with store.connect() as conn:
            starts = store.decode_rows(conn.execute(
                "SELECT * FROM posts WHERE method='official' AND status='scheduled' "
                "AND step IS NULL AND slot_at IS NOT NULL AND ("
                " (platform='youtube' AND target_visibility='public' AND slot_at <= ?) OR "
                " (platform='youtube' AND target_visibility!='public' AND slot_at <= ?) OR "
                " (platform='instagram' AND slot_at <= ?)) ORDER BY slot_at",
                (now + YT_EARLY_UPLOAD_S, now, now)).fetchall())
            continuing = store.decode_rows(conn.execute(
                "SELECT * FROM posts WHERE method='official' AND step IS NOT NULL "
                "AND next_action_at IS NOT NULL AND next_action_at <= ? AND status IN "
                f"({','.join('?' for _ in CONTINUE_STATUSES)}) ORDER BY next_action_at",
                (now, *CONTINUE_STATUSES)).fetchall())
            uploaded = store.decode_rows(conn.execute(
                "SELECT * FROM posts WHERE method='official' AND status='uploaded' "
                "AND step IS NULL AND paused_hold=0 AND (remote_dirty=1 OR "
                "coalesce(scheduled_publish_at, slot_at) + ? <= ?) ORDER BY slot_at",
                (VERIFY_AFTER_S, now)).fetchall())
            # 6.9: an uploaded video YouTube would publish while the owner is paused.
            to_hold = store.decode_rows(conn.execute(
                "SELECT * FROM posts WHERE method='official' AND platform='youtube' "
                "AND status='uploaded' AND paused_hold=0 AND target_visibility='public' "
                "AND scheduled_publish_at > ? AND (step IS NULL OR step IN "
                "('yt_verify', 'yt_update', 'yt_replace')) ORDER BY scheduled_publish_at",
                (now,)).fetchall())
            # 6.10: a held video whose owner resumed gets its publish time back.
            to_release = store.decode_rows(conn.execute(
                "SELECT * FROM posts WHERE method='official' AND platform='youtube' "
                "AND status='uploaded' AND paused_hold=1 AND step IS NULL "
                "ORDER BY slot_at").fetchall())
            # §8.7: manually scheduled uploads whose time has come.
            legacy_due = store.decode_rows(conn.execute(
                "SELECT * FROM posts WHERE method='legacy' AND status='scheduled' "
                "AND slot_at IS NOT NULL AND slot_at <= ? ORDER BY slot_at", (now,)).fetchall())
        work: list[Work] = []
        for p in starts:
            if posts.is_cleared(p, paused=paused(p["owner_id"])):
                work.append(Work(p["id"], p["owner_id"], p["platform"], "start",
                                 FIRST_STEP[p["platform"]], p["slot_at"]))
        for p in continuing:
            if p["step"] in PAUSE_STEPS or not paused(p["owner_id"]):
                work.append(Work(p["id"], p["owner_id"], p["platform"], "continue", p["step"],
                                 p["next_action_at"]))
        for p in uploaded:
            if p["platform"] == "youtube" and not paused(p["owner_id"]):
                step = "yt_update" if p.get("remote_dirty") else "yt_verify"
                work.append(Work(p["id"], p["owner_id"], p["platform"], "maintain", step,
                                 _publish_time(p) or now))
        for p in to_hold:
            if paused(p["owner_id"]):
                work.append(Work(p["id"], p["owner_id"], p["platform"], "hold",
                                 "yt_unschedule", p["scheduled_publish_at"]))
        for p in to_release:
            if not paused(p["owner_id"]):
                work.append(Work(p["id"], p["owner_id"], p["platform"], "release",
                                 "yt_reschedule", p.get("slot_at") or now))
        for p in legacy_due:
            if not paused(p["owner_id"]):
                work.append(Work(p["id"], p["owner_id"], p["platform"], "legacy",
                                 "legacy_upload", p["slot_at"]))
        with self._lock:
            return [w for w in work if w.post_id not in self._inflight]

    def _dispatch(self, work: Work) -> Work | None:
        with self._lock:
            if work.post_id in self._inflight:
                return None
            self._inflight.add(work.post_id)
            executor = self._executors.get(work.account)
            if executor is None:
                executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix=f"publish-{work.platform}")
                self._executors[work.account] = executor
            self._futures = [f for f in self._futures if not f.done()]
            self._futures.append(executor.submit(self._run, work))
        return work

    def wait(self, timeout: float = 30.0) -> None:
        """Wait for the dispatched steps to finish (tests, shutdown)."""
        with self._lock:
            pending = list(self._futures)
        done, not_done = concurrent.futures.wait(pending, timeout=timeout)
        if not_done:
            raise TimeoutError(f"{len(not_done)} publishing steps still running")
        for future in done:
            future.result()

    def close(self) -> None:
        with self._lock:
            executors, self._executors = list(self._executors.values()), {}
        for executor in executors:
            executor.shutdown(wait=True)
        release_lease(self.holder)


    # ── running steps ───────────────────────────────────────────────────

    def _load(self, post_id: str) -> dict | None:
        with store.connect() as conn:
            return store.decode_row(conn.execute("SELECT * FROM posts WHERE id=?",
                                                 (post_id,)).fetchone())

    def _account_ok(self, owner: str, platform: str) -> bool:
        account = accounts.get(owner, platform)
        return bool(account and account.get("status") == "ok")

    def _still_due(self, post: dict | None, kind: str, step: str, now: float) -> bool:
        """Re-check, just before running, that the post still wants this step:
        it may have been rejected, edited, paused or advanced since it was queued."""
        if post is None or post.get("method") != "official":
            return False
        paused = bool(get_prefs(post["owner_id"]).get("paused"))
        if kind == "start":
            slot = post.get("slot_at")
            if post["status"] != "scheduled" or post.get("step") or slot is None:
                return False
            if not posts.is_cleared(post, paused=paused):
                return False
            if post["platform"] == "youtube" and post.get("target_visibility") == "public":
                return slot <= now + YT_EARLY_UPLOAD_S
            return slot <= now
        if not self._account_ok(post["owner_id"], post["platform"]):
            return False                     # waits for a reconnect (10.7)
        if kind == "continue":
            due = post.get("next_action_at")
            return (post.get("step") == step and post["status"] in CONTINUE_STATUSES
                    and due is not None and due <= now
                    and (step in PAUSE_STEPS or not paused))
        if kind == "maintain":
            if post["status"] != "uploaded" or post.get("step") or paused:
                return False
            if post.get("paused_hold"):
                return False
            if step == "yt_update":
                return bool(post.get("remote_dirty"))
            when = _publish_time(post)
            return when is not None and when + VERIFY_AFTER_S <= now
        if kind == "hold":                   # 6.9: the pause holds an uploaded video
            return (paused and post["platform"] == "youtube" and post["status"] == "uploaded"
                    and not post.get("paused_hold")
                    and post.get("target_visibility") == "public"
                    and (post.get("scheduled_publish_at") or 0) > now
                    and post.get("step") in HOLDABLE_STEPS)
        if kind == "release":                # 6.10: and gives it back after the resume
            return (not paused and post["platform"] == "youtube"
                    and post["status"] == "uploaded" and bool(post.get("paused_hold"))
                    and not post.get("step"))
        return False

    def _run(self, work: Work) -> None:
        kind, step = work.kind, work.step
        try:
            if kind == "legacy":
                self._run_legacy(work.post_id)
                return
            for _ in range(CHAIN_LIMIT):
                now = self.clock()
                if not acquire_lease(self.holder, now):
                    return                   # another process took over
                post = self._load(work.post_id)
                if not self._still_due(post, kind, step, now):
                    return
                if kind == "start" and not self._prepublish(post, now):
                    return
                spec = _REGISTRY.get(step)
                if spec is None:
                    if step not in self._warned:
                        self._warned.add(step)
                        log.info("No handler for publishing step %s yet; waiting.", step)
                    return
                started = self.clock()
                try:
                    result = spec.handler(StepContext(post=post, now=started, publisher=self))
                except errors.PlatformError as exc:
                    self._failed(post, step, started, exc)
                    return
                except Exception as exc:
                    self._failed(post, step, started, self._as_platform_error(post, exc))
                    return
                self._succeeded(post, step, started, result, kind)
                if (result.next_step and result.status in CONTINUE_STATUSES
                        and (result.next_action_at is None
                             or result.next_action_at <= self.clock())):
                    kind, step = "continue", result.next_step
                    continue
                return
        finally:
            with self._lock:
                self._inflight.discard(work.post_id)

    @staticmethod
    def _as_platform_error(post: dict, exc: Exception) -> errors.PlatformError:
        """An exception that isn't a classified platform error."""
        try:
            return errors.PlatformError(errors.classify_exception(post["platform"], exc))
        except Exception:
            pass
        if isinstance(exc, FileNotFoundError):
            name = Path(getattr(exc, "filename", "") or "").name or "a file"
            return errors.PlatformError(errors.Classified(
                errors.PERMANENT, "fileMissing", f"A file this post needs is missing ({name})."))
        log.exception("Unexpected error in a publishing step for post %s", post["id"])
        return errors.PlatformError(errors.Classified(
            errors.TEMPORARY, exc.__class__.__name__, f"Unexpected error: {exc}"))

    # ── scheduled uploads (§8.7, 15.4) ──────────────────────────────────

    def _run_legacy(self, post_id: str) -> None:
        """Upload one manually scheduled post through today's functions, once.
        It's marked uploading (`publishing`) in its own commit before the upload
        starts, so a crash leaves it marked, and the next start fails it rather
        than uploading it again (§8.4). Any failure is final: today's rule."""
        from autopilot import legacy

        now = self.clock()
        if not acquire_lease(self.holder, now):
            return
        post = self._load(post_id)
        if (post is None or post.get("method") != "legacy" or post["status"] != "scheduled"
                or post.get("slot_at") is None or post["slot_at"] > now
                or get_prefs(post["owner_id"]).get("paused")):
            return
        label = f"scheduled {_label(post)}"
        path = legacy.clip_path(post.get("job_id"), (post.get("meta") or {}).get("file"))
        if path is None or not path.is_file():
            self._commit(post, "legacy_upload", now, outcome="failed", message=legacy.FILE_MISSING,
                         fields={"status": "failed", "last_error": legacy.FILE_MISSING},
                         entries=[("publish.failed", f"The {label} failed: {legacy.FILE_MISSING}",
                                   None)])
            return
        if self._commit(post, "legacy_start", now, outcome="ok", message=None,
                        fields={"status": "publishing", "step": "legacy_upload",
                                "last_error": ""},
                        entries=[("publish.started", f"Started the {label}", None)]) \
                != "publishing":
            return                                   # its source was deleted meanwhile
        started = self.clock()
        try:
            result = legacy.upload(post, path)
        except Exception as exc:
            message = redact(str(exc))[:500]
            log.warning("Scheduled upload %s failed: %s", post_id, message)
            self._commit(post, "legacy_upload", started, outcome="failed", message=message,
                         fields={"status": "failed", "step": None, "last_error": message},
                         entries=[("publish.failed", f"The {label} failed: {message}", None)])
            return
        result = result if isinstance(result, dict) else {}
        url = result.get("url") or ""
        self._commit(post, "legacy_upload", started, outcome="ok", message=url or None,
                     fields={"status": "live", "step": None, "last_error": "", "url": url,
                             "platform_post_id": result.get("video_id") or result.get("code"),
                             "published_at": self.clock()},
                     entries=[("publish.live", f"The {label} was uploaded"
                               + (f": {url}" if url else "."), {"url": url} if url else None)])

    def _legacy_interrupted(self, now: float) -> list[str]:
        """Scheduled uploads a crash or restart left uploading are marked
        failed, as today, rather than uploaded again (which could post twice).
        One still running in this process isn't touched."""
        from autopilot import legacy

        with store.connect() as conn:
            rows = store.decode_rows(conn.execute(
                "SELECT * FROM posts WHERE method='legacy' AND status='publishing'").fetchall())
        with self._lock:
            running = set(self._inflight)
        failed = []
        for post in rows:
            if post["id"] in running:
                continue
            self._commit(post, "legacy_upload", now, outcome="failed", message=legacy.INTERRUPTED,
                         fields={"status": "failed", "step": None,
                                 "last_error": legacy.INTERRUPTED},
                         entries=[("publish.failed", f"The scheduled {_label(post)} was "
                                   f"interrupted by a restart, so it wasn't uploaded again.",
                                   None)])
            failed.append(post["id"])
        return failed

    # ── pre-publish checks (§8.1 step 4) ────────────────────────────────

    def _file_probe(self, post: dict) -> tuple[dict | None, str | None]:
        with store.connect() as conn:
            version = store.decode_row(conn.execute(
                "SELECT files_json, probe_json FROM clip_versions WHERE id=?",
                (post.get("version_id"),)).fetchone()) if post.get("version_id") else None
        name = ((version or {}).get("files") or {}).get("vertical")
        if not name:
            return None, "This post has no rendered video yet."
        path = paths.output_dir() / str(post.get("job_id") or "") / Path(name).name
        if not path.is_file():
            return None, "The rendered video is missing; render the clip again."
        probe = (version or {}).get("probe")
        if not probe:
            from media.probe import ProbeError, probe as run_probe

            try:
                probe = run_probe(path)
            except ProbeError as exc:
                return None, f"The rendered video couldn't be read: {exc}"
        return probe, None

    def _prepublish(self, post: dict, now: float) -> bool:
        """True when the post may start publishing. Otherwise the post waits
        (`needs_reconnect` or `blocked`) with the reason, recorded as a "check"
        attempt in the same transaction."""
        owner, platform = post["owner_id"], post["platform"]
        name = PLATFORM_NAMES[platform]
        ok, reasons = readiness.platform_ready(owner, platform)
        if not ok:
            account = accounts.get(owner, platform)
            if account is None or account.get("status") != "ok":
                reason = ((account or {}).get("status_reason")
                          or f"{name} isn't connected for Autopilot.")
                self._commit(post, "check", now, outcome="access", message=reason,
                             fields={"status": "needs_reconnect", "last_error": reason},
                             entries=[("post.blocked", f"The {_label(post)} is waiting: {reason}",
                                       None)])
            else:
                reason = " ".join(reasons) or f"{name} isn't ready for Autopilot."
                self._block(post, now, reason, "setup")
            return False
        with store.connect() as conn:
            job = conn.execute("SELECT rights_basis FROM jobs WHERE job_id=?",
                               (post.get("job_id"),)).fetchone()
        if job is None or not job["rights_basis"]:
            self._block(post, now, "No rights confirmation is recorded for this post's source "
                                   "video, so it can't be published.", "rights")
            return False
        probe, problem = self._file_probe(post)
        problems = [problem] if problem else limits.check_file(probe, platform)
        if problems:
            self._block(post, now, " ".join(problems), "format")
            return False
        return True

    def _block(self, post: dict, now: float, reason: str, kind: str) -> None:
        """`kind` is `setup` (the platform isn't ready: maintenance re-plans the
        post once it is, 11.2), `rights` or `format` (the user has to act)."""
        self._commit(post, "check", now, outcome="blocked", message=reason,
                     fields={"status": "blocked", "last_error": reason, "last_error_class": kind},
                     entries=[("post.blocked", f"The {_label(post)} is blocked: {reason}", None)])

    # ── outcomes ────────────────────────────────────────────────────────

    def _succeeded(self, post: dict, step: str, started: float, result: StepResult,
                   kind: str) -> None:
        now = self.clock()
        fields = dict(result.fields)
        fields["status"] = result.status
        fields["step"] = result.next_step
        fields["next_action_at"] = (now if result.next_action_at is None
                                    else result.next_action_at) if result.next_step else None
        fields.setdefault("retry_count", 0)
        fields.setdefault("last_error", None)
        fields.setdefault("last_error_class", None)
        entries = []
        if kind == "start":
            entries.append(("publish.started", f"Started publishing the {_label(post)}", None))
        entries += list(result.activity)
        self._commit(post, step, started, outcome=result.outcome, message=result.message,
                     fields=fields, entries=entries, account_fields=result.account_fields)

    def _failed(self, post: dict, step: str, started: float, exc: errors.PlatformError) -> None:
        now = self.clock()
        r, label = exc.result, _label(post)
        keep = post["status"] in KEEP_STATUS
        retry_step = exc.retry_step or step
        # A step can hand its retry new step data (an unusable Instagram container).
        carried = {} if exc.step_data is None else {"step_data": exc.step_data}
        info = {"http_status": exc.status, "platform_code": r.code, "request_id": exc.request_id}
        tz = posts.owner_timezone(post["owner_id"])
        if r.kind == errors.TEMPORARY:
            retries = int(post.get("retry_count") or 0)
            delay = errors.retry_delay(retries)
            if delay is not None:
                when = now + delay
                fields = {"step": retry_step, "next_action_at": when, "retry_count": retries + 1,
                          "last_error": r.message, "last_error_class": r.kind, **carried}
                if not keep:
                    fields["status"] = "retrying"
                self._commit(post, step, started, outcome="retry", message=r.message,
                             fields=fields, **info,
                             entries=[("publish.retry", f"The {label} hit a temporary problem "
                                       f"({r.message}); trying again at "
                                       f"{planner.local_label(when, tz)}.", info)])
                return
            message = f"Gave up after {retries} retries: {r.message}"
            self._final_failure(post, step, started, message, r.kind, info, keep)
            return
        if r.kind in (errors.DEFERRED, errors.PROCESSING):
            if r.kind == errors.PROCESSING:
                when, entry = now + PROCESSING_RECHECK_S, None
            else:
                when = errors.resume_at(r, now) or now + LIMIT_RECHECK_S
                entry = ("publish.deferred", f"The {label} waits until "
                         f"{planner.local_label(when, tz)}: {r.message}", info)
            fields = {"step": retry_step, "next_action_at": when, "last_error": r.message,
                      "last_error_class": r.kind, **carried}
            if not keep:
                fields["status"] = "retrying" if r.kind == errors.DEFERRED else "publishing"
            self._commit(post, step, started, outcome=r.kind, message=r.message, fields=fields,
                         entries=[entry] if entry else [], **info)
            return
        if r.kind == errors.ACCESS:
            name = PLATFORM_NAMES[post["platform"]]
            reason = {
                "reconnect": f"{name} access was lost: {r.message} Reconnect {name} to publish.",
                "permission": f"{name} permission is missing: {r.message}",
            }.get(r.reason, r.message)
            fields = {"step": retry_step, "next_action_at": None, "last_error": reason,
                      "last_error_class": r.kind, **carried}
            if not keep:
                fields["status"] = "needs_reconnect"
            self._commit(post, step, started, outcome="access", message=r.message, fields=fields,
                         fanout=reason, **info,
                         entries=[("account.needs_reconnect", reason, info)])
            return
        self._final_failure(post, step, started, r.message, r.kind, info, keep)

    def _final_failure(self, post, step, started, message, kind, info, keep) -> None:
        """Permanent error, or retries spent (10.2, 10.3): the post fails with
        the platform's reason. A cleanup step on a rejected or cancelled post
        gives up instead, and the post stays as it is."""
        if post["status"] in ("rejected", "cancelled"):
            fields = {"step": None, "next_action_at": None, "last_error": message,
                      "last_error_class": kind}
            summary = f"Couldn't finish cleaning up the {_label(post)}: {message}"
        else:
            fields = {"status": "failed", "next_action_at": None, "last_error": message,
                      "last_error_class": kind}
            summary = f"The {_label(post)} failed: {message}"
        self._commit(post, step, started, outcome="failed", message=message, fields=fields,
                     entries=[("publish.failed", summary, info)], **info)

    def _commit(self, post: dict, step: str, started: float, *, outcome: str,
                message: str | None, fields: dict, entries=(), fanout: str | None = None,
                account_fields: dict | None = None, http_status: int | None = None,
                platform_code: str | None = None, request_id: str | None = None) -> str:
        """The attempt row, the post update, the activity entries and, for an
        access error, the account's other posts: one transaction (§8.1 step 5).
        Returns the status stored, which isn't the step's when the user
        withdrew the post while the step ran (see _withdrawn_meanwhile)."""
        now = self.clock()
        owner, platform = post["owner_id"], post["platform"]
        clean = redact(" ".join(str(message).split()))[:1000] if message else None
        with store.connect() as conn:
            current = conn.execute("SELECT status, last_error, last_error_class, version_id "
                                   "FROM posts WHERE id=?", (post["id"],)).fetchone()
            if (current is not None and current["status"] in posts.WITHDRAWN
                    and post["status"] not in posts.WITHDRAWN):
                fields, entries = self._withdrawn_meanwhile(post, dict(current), fields,
                                                            entries, now)
            # The editor switched the post to a new version while this step started
            # sending the one it had (task 11.1): the post keeps the version it's
            # uploading, so the record matches the platform; it can be replaced later (8.6).
            started = ((fields.get("step_data") or {}).get("version_id")
                       if step in ("yt_init", "ig_container") else None)
            if current is not None and started and current["version_id"] not in (None, started):
                fields = {**fields, "version_id": started}
                entries = [*entries, ("post.version_changed",
                                      f"The {_label(post)} had already started uploading the "
                                      "previous version, so it keeps that one.", None)]
            conn.execute(
                "INSERT INTO publish_attempts(id, post_id, step, started_at, finished_at, "
                "outcome, http_status, platform_code, request_id, message) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:16], post["id"], step, started, now, outcome, http_status,
                 platform_code, request_id, clean))
            store.update_post(conn, post["id"], {**fields, "updated_at": now})
            for kind, summary, detail in entries:
                activity.log(owner, "system", kind, redact(summary), job_id=post.get("job_id"),
                             clip_id=post.get("clip_id"), post_id=post["id"], detail=detail,
                             conn=conn)
            if fanout:
                posts.hold_for_reconnect(owner, platform, fanout, now=now, conn=conn)
                accounts.mark_needs_reconnect(owner, platform, fanout, conn=conn)
            wanted = {k: v for k, v in (account_fields or {}).items() if k in ACCOUNT_FIELDS}
            if wanted:
                conn.execute(f"UPDATE accounts SET {', '.join(f'{k}=?' for k in wanted)} "
                             "WHERE owner_id=? AND platform=?", (*wanted.values(), owner, platform))
        return fields.get("status", current["status"] if current is not None else post["status"])

    @staticmethod
    def _withdrawn_meanwhile(before: dict, current: dict, fields: dict, entries,
                             now: float) -> tuple[dict, list]:
        """The user rejected the post, or deleted its source, while this step
        ran. What the step did on the platform decides the outcome (1.9, 8.5):
          - content that went out stays published, and is kept as history;
          - otherwise the post stays withdrawn. The identifiers the step
            produced are kept, and the cleanup step it now needs is queued
            (posts.cleanup_step), so an upload it finished can't go public."""
        after = {**before, **fields}
        label = _label(before)
        why = "you deleted the video it came from" if current["status"] == "cancelled" else "you decided not to post it"
        if posts.is_out(after):
            return fields, [*entries, ("publish.step", f"The {label} had already been published "
                                       f"when {why}, so it's kept in your history.", None)]
        step = posts.cleanup_step(after)
        merged = {k: fields[k] for k in ("step_data", "platform_post_id", "url") if k in fields}
        merged.update({"status": current["status"], "step": step,
                       "next_action_at": now if step else None, "retry_count": 0,
                       "scheduled_publish_at": None, "last_error": current["last_error"],
                       "last_error_class": current["last_error_class"]})
        notes = []
        if step in ("yt_delete", "yt_abandon"):
            notes.append(("publish.step", f"The {label} was uploading when {why}; the upload "
                          "is deleted from YouTube unless it's already public.", None))
        elif step == "ig_abandon":
            notes.append(("publish.step", f"The {label} was publishing when {why}; checking "
                          "whether Instagram published it.", None))
        return merged, notes


# ── the background thread (bootstrap) ────────────────────────────────────

_background: Publisher | None = None
_stop = threading.Event()


def start_background(interval: float = TICK_S) -> Publisher:
    """Start ticking every `interval` seconds in a daemon thread."""
    global _background
    if _background is not None:
        return _background
    _background = Publisher()
    _stop.clear()

    def loop() -> None:
        while not _stop.is_set():
            try:
                _background.tick()
            except Exception:
                log.exception("A publisher tick failed")
            _stop.wait(interval)

    threading.Thread(target=loop, daemon=True, name="publisher").start()
    return _background


def stop_background() -> None:
    global _background
    _stop.set()
    if _background is not None:
        _background.close()
        _background = None
