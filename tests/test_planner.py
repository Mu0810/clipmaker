"""Posting planner (task 6.3, §6.2, §6.3)."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from autopilot import planner as P

NY, KOL = ZoneInfo("America/New_York"), ZoneInfo("Asia/Kolkata")


def utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def local(zone, *args) -> float:
    return datetime(*args, tzinfo=zone).timestamp()


def every_day(hours) -> dict:
    return {str(d): list(hours) for d in range(7)}


# ── candidate slots ──────────────────────────────────────────────────────

@pytest.mark.req("5.2", "5.6")
def test_candidates_in_kolkata_use_its_half_hour_offset():
    now = utc(2026, 11, 2, 0, 0)                         # 05:30 IST, Monday
    c = P.candidates(every_day([18]), "Asia/Kolkata", now)
    assert c[0] == utc(2026, 11, 2, 12, 30)              # 18:00 IST
    assert len(c) == 14                                  # today plus 13 days
    assert {b - a for a, b in zip(c, c[1:])} == {86400.0}   # no DST in Kolkata


@pytest.mark.req("5.2", "5.6")
def test_an_ambiguous_fall_back_hour_means_its_first_occurrence():
    now = utc(2026, 10, 28, 12)                          # Wed 28 Oct, 08:00 EDT
    c = P.candidates({"6": [1]}, "America/New_York", now)    # Sundays at 01:00
    assert c == [utc(2026, 11, 1, 5), utc(2026, 11, 8, 6)]
    first = datetime.fromtimestamp(c[0], NY)
    assert first.hour == 1 and first.utcoffset() == timedelta(hours=-4)   # EDT, fold=0


@pytest.mark.req("5.2", "5.6")
def test_a_nonexistent_spring_forward_hour_moves_forward_one_hour():
    now = utc(2027, 3, 10, 12)                           # Wed 10 Mar 2027
    c = P.candidates({"6": [2, 3]}, "America/New_York", now)
    # 14 Mar: 02:00 doesn't exist, lands on 03:00 EDT, the same instant as the
    # real 03:00, so it appears once. 21 Mar: both hours exist.
    assert c == [utc(2027, 3, 14, 7), utc(2027, 3, 21, 6), utc(2027, 3, 21, 7)]
    assert datetime.fromtimestamp(c[0], NY).hour == 3


@pytest.mark.req("5.2")
def test_a_slot_must_be_at_least_30_minutes_away():
    hours = every_day([18])
    at_boundary = local(KOL, 2026, 11, 2, 17, 30)
    assert P.candidates(hours, KOL, at_boundary)[0] == local(KOL, 2026, 11, 2, 18)
    assert P.candidates(hours, KOL, at_boundary + 1)[0] == local(KOL, 2026, 11, 3, 18)


def test_out_of_range_hours_and_missing_weekdays_are_ignored():
    now = local(KOL, 2026, 11, 2, 9)                     # Monday
    assert P.candidates({"0": [24, -1, 10]}, KOL, now) == [
        local(KOL, 2026, 11, 2, 10), local(KOL, 2026, 11, 9, 10)]
    assert P.candidates({}, KOL, now) == []


@pytest.mark.req("5.6")
def test_times_are_shown_in_the_users_zone_with_its_name():
    assert P.local_label(utc(2026, 11, 1, 5), NY) == \
        "Sun 1 Nov 2026, 01:00 EDT (America/New_York)"
    assert P.local_label(utc(2026, 11, 1, 6), "America/New_York") == \
        "Sun 1 Nov 2026, 01:00 EST (America/New_York)"   # same wall time, told apart
    assert P.local_label(utc(2026, 11, 2, 12, 30), "Asia/Kolkata") == \
        "Mon 2 Nov 2026, 18:00 IST (Asia/Kolkata)"


# ── slot assignment ──────────────────────────────────────────────────────

NOW = local(KOL, 2026, 11, 2, 9)                         # Mon 2 Nov 2026, 09:00 IST
WIN = every_day([11, 18, 20])


def profile(**over) -> dict:
    base = {"windows": {"youtube": WIN, "instagram": WIN}, "min_gap_hours": 4, "daily_max": 2}
    base.update(over)
    return base


def at(day: int, hour: int) -> float:
    return local(KOL, 2026, 11, 2 + day, hour)


def post(pid, score=60, clip_idx=0, platform="youtube") -> dict:
    return {"id": pid, "platform": platform, "score": score, "clip_idx": clip_idx}


def slots(result) -> dict:
    return {pid: r["slot_at"] for pid, r in result.items()}


@pytest.mark.req("5.3")
def test_the_minimum_gap_is_kept_from_taken_and_new_slots():
    taken = [{"platform": "youtube", "slot_at": at(0, 18)}]
    r = P.assign([post("p1", 90), post("p2", 80)], taken, profile(daily_max=3), KOL, NOW)
    # p2 can't take 11:00 (p1), 18:00 (taken) or 20:00 (2 h after 18:00).
    assert slots(r) == {"p1": at(0, 11), "p2": at(1, 11)}


@pytest.mark.req("5.3")
def test_the_daily_maximum_is_counted_per_local_date():
    three = [post("a", 90), post("b", 80), post("c", 70)]
    one_a_day = P.assign(three, [], profile(daily_max=1, min_gap_hours=0), KOL, NOW)
    assert slots(one_a_day) == {"a": at(0, 11), "b": at(1, 11), "c": at(2, 11)}
    two_a_day = P.assign(three, [], profile(), KOL, NOW)
    assert slots(two_a_day) == {"a": at(0, 11), "b": at(0, 18), "c": at(1, 11)}


@pytest.mark.req("5.2")
def test_the_highest_scores_get_the_earliest_slots():
    posts = [post("low", 50, 0), post("top", 90, 1), post("mid_a", 70, 2), post("mid_b", 70, 3)]
    r = P.assign(posts, [], profile(), KOL, NOW)
    assert slots(r) == {"top": at(0, 11), "mid_a": at(0, 18), "mid_b": at(1, 11),
                        "low": at(1, 18)}          # a tie goes to the lower clip number
    assert all(v["status"] == "scheduled" and v["slot_basis"] == "defaults" for v in r.values())


@pytest.mark.req("5.3", "5.5")
def test_locked_uploaded_and_live_posts_keep_their_slots():
    existing = [
        {"id": "e1", "platform": "youtube", "status": "scheduled", "user_locked": 1,
         "slot_at": at(0, 11)},
        {"id": "e2", "platform": "youtube", "status": "uploaded", "slot_at": at(0, 18)},
        {"id": "e3", "platform": "youtube", "status": "live", "slot_at": None,
         "published_at": at(1, 11)},
        {"id": "e4", "platform": "youtube", "status": "rejected", "slot_at": at(1, 18)},
        {"id": "e5", "platform": "youtube", "status": "failed", "slot_at": at(1, 20)},
        {"id": "e6", "platform": "youtube", "status": "scheduled", "slot_at": at(2, 11)},
        {"id": "e7", "platform": "youtube", "status": "waiting_approval", "slot_at": at(2, 18)},
        {"id": "e8", "platform": "youtube", "status": "publishing", "slot_at": at(3, 11)},
    ]
    occupied = P.occupied_slots(existing, replanning_ids={"e6", "e8"})
    assert sorted(o["id"] for o in occupied) == ["e1", "e2", "e3", "e8"]   # e8 is in flight
    assert {o["id"]: o["slot_at"] for o in occupied}["e3"] == at(1, 11)    # went live then
    # A user-locked post keeps its slot even if a caller lists it for re-planning,
    # and a scheduled post not being re-planned keeps its slot too.
    assert "e1" in {o["id"] for o in P.occupied_slots(existing, replanning_ids={"e1"})}
    assert "e6" in {o["id"] for o in P.occupied_slots(existing)}

    r = P.assign([post("new")], occupied, profile(), KOL, NOW)
    # Day 0 is full (e1, e2) and 11:00 on day 1 is e3's; the rejected post's
    # old slot at 18:00 on day 1 is free again.
    assert slots(r) == {"new": at(1, 18)}


@pytest.mark.req("5.2")
def test_a_post_with_no_free_slot_is_unscheduled_with_the_reason():
    mondays = profile(windows={"youtube": {"0": [11]}, "instagram": {}}, daily_max=1)
    r = P.assign([post("a", 90), post("b", 80), post("c", 70)], [], mondays, KOL, NOW)
    assert slots(r) == {"a": at(0, 11), "b": at(7, 11), "c": None}   # two Mondays in 14 days
    assert r["c"] == {"status": "unscheduled", "slot_at": None, "slot_basis": None,
                      "reason": "No free slot in the next 14 days"}


@pytest.mark.req("5.3", "5.4")
def test_platforms_are_planned_independently_with_their_own_basis():
    p = profile(windows_basis={"youtube": "your last 12 posts"})
    taken = [{"platform": "youtube", "slot_at": at(0, 11)}]
    r = P.assign([post("yt"), post("ig", platform="instagram")], taken, p, KOL, NOW)
    assert slots(r) == {"yt": at(0, 18), "ig": at(0, 11)}    # YouTube's slot doesn't block Instagram
    assert (r["yt"]["slot_basis"], r["ig"]["slot_basis"]) == ("your last 12 posts", "defaults")


def test_assign_is_pure_and_deterministic():
    posts = [post("a"), post("b"), post("c", platform="instagram")]
    taken = [{"platform": "youtube", "slot_at": at(0, 11)}]
    before = copy.deepcopy((posts, taken))
    first = P.assign(posts, taken, profile(), KOL, NOW)
    assert (posts, taken) == before
    assert P.assign(list(reversed(posts)), taken, profile(), KOL, NOW) == first


# ── account-data windows ─────────────────────────────────────────────────

def posted(y, m, d, h, views, ages=(80,)) -> dict:
    return {"published_at": local(KOL, y, m, d, h),
            "snapshots": [{"age_hours": a, "views": views} for a in ages]}


DATA = [                                                    # Nov 2026 in Kolkata
    posted(2026, 11, 2, 9, 1000), posted(2026, 11, 9, 9, 1200),     # Mon 09 x2, avg 1,100
    posted(2026, 11, 2, 15, 5000), posted(2026, 11, 9, 15, 7000),   # Mon 15 x2, avg 6,000
    posted(2026, 11, 16, 21, 99999),                                 # Mon 21, alone
    posted(2026, 11, 4, 21, 3000), posted(2026, 11, 5, 21, 3000),   # Wed 21, Thu 21
    posted(2026, 11, 3, 7, 10),                                      # Tue 07
    posted(2026, 11, 7, 13, 50), posted(2026, 11, 14, 13, 70),      # Sat 13 x2, avg 60
    posted(2026, 11, 8, 4, 5), posted(2026, 11, 8, 23, 8),          # Sun 04, Sun 23
]


@pytest.mark.req("5.4")
def test_views_are_read_at_the_first_snapshot_72_hours_or_older():
    snaps = [{"age_hours": 24, "views": 10}, {"age_hours": 200, "views": 9000},
             {"age_hours": 80, "views": 500}]
    assert P.views_at_72h(snaps) == 500
    assert P.views_at_72h([{"age_hours": 24, "views": 10}]) is None
    assert P.views_at_72h([]) is None


@pytest.mark.req("5.4")
def test_account_data_picks_weekday_hours_then_pooled_hours():
    defaults = {**every_day([11, 18]), "6": []}               # no Sunday windows by default
    windows, basis = P.account_windows(DATA, defaults, tz="Asia/Kolkata")
    assert windows == {
        "0": [9, 15],      # Monday's own buckets (15 then 9; 21 has only one post)
        "1": [15, 21],     # no own bucket: hour-of-day pooled (21 avg 35,333, then 15)
        "2": [15, 21], "3": [15, 21], "4": [15, 21],
        "5": [13, 21],     # Saturday's own 13:00, then the best pooled hour
        "6": [],           # k comes from the defaults: Sunday keeps none
    }
    assert basis == "your last 12 posts"


@pytest.mark.req("5.4")
def test_fewer_than_10_posts_with_a_72_hour_snapshot_keep_the_defaults():
    young = [dict(p, snapshots=[{"age_hours": 48, "views": 10**6}]) for p in DATA[:3]]
    windows, basis = P.account_windows(DATA[3:] + young, every_day([18, 11, 18]), tz=KOL)
    assert windows == every_day([11, 18]) and basis == "defaults"   # 9 old enough; sorted, unique


@pytest.mark.req("5.4")
def test_buckets_with_one_post_each_fall_back_to_the_defaults():
    singles = [posted(2026, 11, 2 + d, 8 + d, 100 * (d + 1)) for d in range(10)]
    windows, basis = P.account_windows(singles, every_day([11, 18]), tz=KOL)
    assert windows == every_day([11, 18]) and basis == "defaults"


@pytest.mark.req("5.2", "5.4")
def test_account_windows_drive_the_slot_and_its_basis():
    windows, basis = P.account_windows(DATA, every_day([11, 18]), tz=KOL)
    p = {"windows": {"youtube": windows}, "windows_basis": {"youtube": basis},
         "min_gap_hours": 4, "daily_max": 2}
    r = P.assign([post("p")], [], p, KOL, NOW)
    # Monday's windows are 09:00 and 15:00; 09:00 is now, inside the 30-minute lead.
    assert r["p"]["slot_at"] == at(0, 15) and r["p"]["slot_basis"] == "your last 12 posts"
