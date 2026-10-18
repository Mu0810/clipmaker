"""Hourly maintenance (design §8.9, §11; Requirements 5.4, 10.6, 10.7, 11.2,
12.1, 12.5).

`Maintenance.tick()` runs every hour in a background thread started from
bootstrap(). It holds its own lease, so two server processes on one
database never both run it, and then:

1. checks each connected account's sign-in, at most once a day per account:
   - Google: the token is refreshed and saved. Refused (`invalid_grant`,
     say) means the account and its posts need reconnecting (10.7). When
     that happens 6 to 8 days after the user connected, the message says the
     Google Cloud app is probably still in "Testing" (11.1);
   - Meta: debug_token on the saved sign-in. Invalid, a removed publishing
     permission, or access that already ended means reconnecting. Access
     that ends within 7 days gets a reminder to reconnect before then (10.6).
     The account's expiry dates and permissions are updated from the reply.
   A check that got no answer (a network problem, Google or Meta down) isn't
   counted and is tried again on the next tick;
2. re-plans posts blocked while their platform wasn't ready, once it is
   (11.2, posts.replan_blocked);
3. fetches views, likes and comments (autopilot/metrics.py, 12.5);
4. once a day, deletes activity older than 400 days (12.1 asks for 90).

Each step runs on its own: one failing doesn't stop the others.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid

from autopilot import accounts, activity, metrics, planner, posts, publisher, store
from platforms import errors, google_creds, http, meta_auth

log = logging.getLogger(__name__)

TICK_S = 3600
LEASE_KEY = "maintenance_lease"
LEASE_TTL_S = 2 * TICK_S            # a holder that stops is replaced within 2 hours
CHECK_EVERY_S = 24 * 3600           # each account at most once a day (§8.9)
PURGE_EVERY_S = 24 * 3600
REMIND_WITHIN_S = 7 * 86400         # 10.6: ask at least 7 days before access lapses
TESTING_DAYS = (6.0, 8.0)           # Google ends a Testing app's sign-ins after 7 days
NAMES = {"youtube": "YouTube", "instagram": "Instagram"}
NO_ANSWER = (errors.TEMPORARY, errors.DEFERRED, errors.PROCESSING)


# ── once a day ───────────────────────────────────────────────────────────

def _claim(key: str, now: float, every: float) -> dict | None:
    """Take the job `key` if it last ran `every` seconds ago or more, so a
    restart doesn't run it again. Returns the previous record, to give back
    with _give_back when the job got no answer; None when it isn't due."""
    previous = store.state_get(key) or {}
    if now - float(previous.get("at") or 0) < every:
        return None
    store.state_set(key, {**previous, "at": now})
    return previous


def _give_back(key: str, previous: dict) -> None:
    store.state_set(key, previous)


def _check_key(owner: str, platform: str) -> str:
    return f"account_check:{owner}:{platform}"


# ── lost access (10.7) ───────────────────────────────────────────────────

def lose_access(owner: str, platform: str, why: str, now: float) -> str:
    """Stop publishing to the account: it and its unpublished posts need
    reconnecting, with the reason (10.7). Reconnecting restores them
    (posts.restore_after_reconnect). Returns the reason."""
    name = NAMES[platform]
    reason = f"{name} access was lost: {why}"
    with store.connect() as conn:
        accounts.mark_needs_reconnect(owner, platform, reason, conn=conn)
        moved = posts.hold_for_reconnect(owner, platform, reason, now=now, conn=conn)
        waiting = (f" {moved} unpublished post{'s' if moved != 1 else ''} "
                   f"{'waits' if moved == 1 else 'wait'} until you reconnect." if moved else "")
        activity.log(owner, "system", "account.needs_reconnect", reason + waiting, at=now,
                     detail={"posts": moved}, conn=conn)
    return reason


def _check_google(owner: str, account: dict, now: float) -> str:
    try:
        google_creds.access_token(owner, now=now, force=True)      # refreshed and saved
    except errors.PlatformError as exc:
        if exc.kind in NO_ANSWER:
            raise
        connected = account.get("connected_at")
        days = (now - connected) / 86400 if connected else None
        if (exc.result.code == "invalid_grant" and days is not None
                and TESTING_DAYS[0] <= days <= TESTING_DAYS[1]):
            why = (f"Google ended the sign-in {days:.0f} days after you connected, which is what "
                   "happens while the Google Cloud app's publishing status is \"Testing\". "
                   "Publish the app on the OAuth consent screen in Google Cloud, then "
                   "reconnect YouTube.")
        else:
            why = (f"Google refused the saved sign-in ({exc}). Reconnect YouTube to keep "
                   "publishing.")
        lose_access(owner, "youtube", why, now)
        return "needs_reconnect"
    return "ok"


def _check_meta(owner: str, now: float) -> str:
    app = meta_auth.app_settings(owner)
    token = (meta_auth.load_token(owner) or {}).get("user_token")
    if not app or not token:
        lose_access(owner, "instagram", "the saved Meta sign-in is missing. Connect Instagram "
                                        "again to keep publishing.", now)
        return "needs_reconnect"
    try:
        with http.client() as client:
            info = meta_auth.debug_token(client, app, token)
    except errors.PlatformError as exc:
        if exc.kind in NO_ANSWER:
            raise
        lose_access(owner, "instagram", f"Meta refused to check the saved sign-in ({exc}). "
                                        "Reconnect Instagram to keep publishing.", now)
        return "needs_reconnect"
    with store.connect() as conn:
        conn.execute("UPDATE accounts SET token_expires_at=?, data_access_expires_at=?, "
                     "scopes_json=? WHERE owner_id=? AND platform='instagram'",
                     (info["expires_at"], info["data_access_expires_at"],
                      json.dumps(sorted(set(info["scopes"]))), owner))
    if not info["is_valid"]:
        lose_access(owner, "instagram", "Meta says the saved sign-in isn't valid any more: it "
                                        "expired, or Clip Maker's access was removed in "
                                        "Facebook's settings. Reconnect Instagram to keep "
                                        "publishing.", now)
        return "needs_reconnect"
    missing = meta_auth.missing_scopes(info["scopes"])
    if missing:
        lose_access(owner, "instagram", f"the {', '.join(missing)} permission was removed, so "
                                        "Clip Maker can't publish Reels. Reconnect Instagram "
                                        "and allow it.", now)
        return "needs_reconnect"
    lapses = [t for t in (info["expires_at"], info["data_access_expires_at"]) if t]
    if not lapses:
        return "ok"
    soonest = min(lapses)
    label = planner.local_label(soonest, posts.owner_timezone(owner))
    if soonest <= now:
        lose_access(owner, "instagram", f"Meta's access to your account ended on {label}. "
                                        "Reconnect Instagram to keep publishing.", now)
        return "needs_reconnect"
    if soonest - now <= REMIND_WITHIN_S:
        activity.log(owner, "system", "account.reconnect_reminder",
                     f"Reconnect Instagram before {label}: Meta ends Clip Maker's access to "
                     "your account then, and Instagram posts would stop until you reconnect.",
                     at=now, detail={"lapses_at": soonest})
        return "reminded"
    return "ok"


def check_accounts(now: float) -> list[dict]:
    """Step 1: each connected account, at most once a day."""
    with store.connect() as conn:
        rows = store.decode_rows(conn.execute(
            "SELECT * FROM accounts WHERE status='ok' ORDER BY owner_id, platform").fetchall())
    results = []
    for account in rows:
        owner, platform = account["owner_id"], account["platform"]
        key = _check_key(owner, platform)
        previous = _claim(key, now, CHECK_EVERY_S)
        if previous is None:
            continue
        try:
            if platform == "youtube":
                outcome = _check_google(owner, account, now)
            else:
                outcome = _check_meta(owner, now)
        except errors.PlatformError as exc:            # no answer: try again next tick
            _give_back(key, previous)
            log.warning("Couldn't check %s for %s, trying again next hour: %s",
                        NAMES[platform], owner, exc)
            outcome = "retry"
        else:
            store.state_set(key, {"at": now, "outcome": outcome})
        results.append({"owner": owner, "platform": platform, "outcome": outcome})
    return results


# ── blocked posts, purge ─────────────────────────────────────────────────

def replan_blocked(now: float) -> dict:
    """Step 2: posts blocked while their platform wasn't ready (11.2)."""
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT owner_id, platform FROM posts WHERE status='blocked' "
            "AND method='official' AND last_error_class='setup'").fetchall()
    out = {}
    for row in rows:
        result = posts.replan_blocked(row["owner_id"], row["platform"], now=now)
        if result["kept"] or result["replanned"]:
            out[f"{row['owner_id']}:{row['platform']}"] = result
    return out


def purge(now: float) -> int | None:
    """Step 4, once a day: activity older than 400 days. Each owner whose
    entries went gets a note. None when it isn't due yet."""
    if _claim("activity_purge", now, PURGE_EVERY_S) is None:
        return None
    cutoff = now - activity.RETENTION_DAYS * 86400
    with store.connect() as conn:
        counts = conn.execute("SELECT owner_id, count(*) AS n FROM activity WHERE at < ? "
                              "GROUP BY owner_id", (cutoff,)).fetchall()
    removed = activity.purge(activity.RETENTION_DAYS, now=now)
    for row in counts:
        activity.log(row["owner_id"], "system", "maintenance.purged",
                     f"Deleted {row['n']} activity entr{'y' if row['n'] == 1 else 'ies'} older "
                     f"than {activity.RETENTION_DAYS} days.", at=now, detail={"count": row["n"]})
    return removed


# ── the tick ─────────────────────────────────────────────────────────────

class Maintenance:
    def __init__(self, *, clock=time.time, holder: str | None = None):
        self.clock = clock
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def tick(self, now: float | None = None) -> dict:
        """One pass. Returns what each step did, for tests and logs."""
        now = self.clock() if now is None else now
        if not publisher.acquire_lease(self.holder, now, LEASE_TTL_S, key=LEASE_KEY):
            return {"lease": False}
        report: dict = {"lease": True}
        for name, step in (("accounts", check_accounts), ("replanned", replan_blocked),
                           ("metrics", metrics.fetch_due), ("purged", purge)):
            try:
                report[name] = step(now)
            except Exception:                       # one step failing doesn't stop the rest
                log.exception("Maintenance step %s failed", name)
                report[name] = "error"
        return report

    def close(self) -> None:
        publisher.release_lease(self.holder, key=LEASE_KEY)


_background: Maintenance | None = None
_stop = threading.Event()


def start_background(interval: float = TICK_S) -> Maintenance:
    """Tick now, then every `interval` seconds, in a daemon thread."""
    global _background
    if _background is not None:
        return _background
    _background = Maintenance()
    _stop.clear()

    def loop() -> None:
        while not _stop.is_set():
            try:
                _background.tick()
            except Exception:
                log.exception("A maintenance tick failed")
            _stop.wait(interval)

    threading.Thread(target=loop, daemon=True, name="maintenance").start()
    return _background


def stop_background() -> None:
    global _background
    _stop.set()
    if _background is not None:
        _background.close()
        _background = None
