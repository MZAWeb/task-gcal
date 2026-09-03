"""Characterization tests for `reconcile`.

These pin the behaviors recent commits paid for — in-progress pinning, the
overdue horizon, duplicate cleanup, `scheduled`/`wait` floors, the midnight-due
bump — so a refactor can't quietly undo one. They assert semantic outcomes
(what was created, moved, or deleted), not the exact prose of the report.

`NOW` is Monday 2026-09-07 09:00 UTC, the first minute of a default working
day, and tests schedule in UTC so nothing depends on the machine's zone.

Three pieces of the implementation are deliberately not covered, because no
test can distinguish them — they're redundant safety nets or alternative
spellings rather than behavior:

- `_keeper_rank`'s in-progress tier. An in-progress event always has a smaller
  `start` than an upcoming one, so the plain earliest-first ordering already
  puts it first; the explicit tier only documents the intent.
- the `slot_end <= deadline` term in `find_earliest_slot`. `_work_windows`
  already clips every window to the deadline, so the term can never fire.
- `day_start` built as `midnight + timedelta(hours=...)` rather than
  `time(hour)`. The two are identical for 0-23, which is all a *start* hour may
  be; only `day_end` needs the offset form, to express hour 24.

All three are worth keeping and worth knowing about: if a mutation run reports
them as surviving, that's expected, not a gap.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from task_gcal import schedule as schedule_mod
from task_gcal.config import Settings

from conftest import FRI_5PM, MIDNIGHT, NOW, WED_5PM, at, managed_event, task_row


# ---------------------------------------------------------------------------
# Placement basics
# ---------------------------------------------------------------------------

def test_schedules_a_new_task_at_the_next_free_slot(harness):
    harness.tasks(task_row(uuid="u1", description="do it", due=WED_5PM, estimate=60))
    res = harness.run()

    assert res.code == 0
    (created,) = harness.gcal.created
    assert created["task_uuid"] == "u1"
    assert created["summary"] == "do it"
    assert (created["start"], created["end"]) == (at(0, 9), at(0, 10))
    assert res.section("Scheduled")[0].startswith("[create")


def test_running_twice_changes_nothing_the_second_time(harness):
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    harness.run()
    before = harness.gcal.mutations

    res = harness.run()
    assert harness.gcal.mutations == before
    assert res.section("Scheduled")[0].startswith("[unchanged")


def test_higher_urgency_takes_the_earlier_slot(harness):
    # Report order is deliberately wrong; reconcile re-sorts by urgency.
    harness.tasks(
        task_row(uuid="low", id=1, urgency=1.0, due=WED_5PM, estimate=60),
        task_row(uuid="high", id=2, urgency=50.0, due=WED_5PM, estimate=60),
    )
    harness.run()

    placed = {c["task_uuid"]: (c["start"], c["end"]) for c in harness.gcal.created}
    assert placed["high"] == (at(0, 9), at(0, 10))
    assert placed["low"] == (at(0, 10), at(0, 11))


def test_a_placed_task_blocks_the_slot_for_the_next_one(harness):
    harness.tasks(
        task_row(uuid="a", id=1, urgency=9.0, due=WED_5PM, estimate=90),
        task_row(uuid="b", id=2, urgency=8.0, due=WED_5PM, estimate=30),
    )
    harness.run()
    placed = {c["task_uuid"]: c["start"] for c in harness.gcal.created}
    assert placed["b"] == at(0, 10, 30)


def test_existing_meetings_are_avoided(harness):
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    harness.busy((at(0, 9), at(0, 11)))
    harness.run()
    assert harness.gcal.created[0]["start"] == at(0, 11)


def test_our_own_events_are_excluded_from_busy_time(harness):
    ours = managed_event(id="ev-old", task_uuid="u1", start=at(0, 14), end=at(0, 15))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60)).events(ours)
    harness.run()
    (_, _, excluded) = harness.gcal.busy_list_calls[0]
    assert "ev-old" in excluded


def test_lookback_days_bounds_the_search_for_our_events(harness):
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    harness.configure(lookback_days=30).run()
    time_min, _ = harness.gcal.scheduler_list_calls[0]
    assert time_min == NOW - timedelta(days=30)


# ---------------------------------------------------------------------------
# In-progress pinning (never move a block you're inside)
# ---------------------------------------------------------------------------

def test_an_in_progress_event_keeps_its_start(harness):
    # 09:00 is free, so earliest-fit would pull this back — it must not.
    ongoing = managed_event(
        id="ev1", task_uuid="u1", start=at(0, 8, 30), end=at(0, 9, 30)
    )
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60)).events(ongoing)
    harness.run()

    assert harness.gcal.created == []
    assert harness.gcal.deleted == []
    assert harness.gcal.event_for("u1").start == at(0, 8, 30)


def test_an_in_progress_events_end_follows_a_grown_estimate(harness):
    ongoing = managed_event(
        id="ev1", task_uuid="u1", start=at(0, 8, 30), end=at(0, 9, 30)
    )
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=120)).events(ongoing)
    harness.run()

    ev = harness.gcal.event_for("u1")
    assert ev.start == at(0, 8, 30)
    assert ev.end == at(0, 10, 30)


def test_a_shrunk_estimate_never_rewinds_the_end_into_the_past(harness):
    ongoing = managed_event(id="ev1", task_uuid="u1", start=at(0, 8), end=at(0, 10))
    # 08:00 + 30m = 08:30, which is behind NOW; the end must stay put.
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=30)).events(ongoing)
    harness.run()

    assert harness.gcal.event_for("u1").end == at(0, 10)


def test_an_in_progress_task_is_never_reported_as_past_due(harness):
    ongoing = managed_event(
        id="ev1", task_uuid="u1", start=at(0, 8, 30), end=at(0, 9, 30)
    )
    # Due last Friday: overdue, but we're working on it right now.
    harness.tasks(
        task_row(uuid="u1", due=at(-3, 17), estimate=60)
    ).events(ongoing)
    res = harness.run()
    assert res.section("Overdue") == []


# ---------------------------------------------------------------------------
# Schedule stability (a near-term block is a commitment, not a suggestion)
# ---------------------------------------------------------------------------

def test_a_settled_block_is_not_pulled_earlier(harness):
    # Tomorrow 14:00 while today 09:00 is wide open. Earliest-fit would move
    # it; a block you've planned around must not move for a marginal gain.
    settled = managed_event(id="ev1", task_uuid="u1", start=at(1, 14), end=at(1, 15))
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60)).events(settled)
    res = harness.run()

    assert harness.gcal.event_for("u1").start == at(1, 14)
    # The fixture's blank description still needs a refresh, but the time
    # must not be part of that patch.
    (_, body) = harness.gcal.patched[0]
    assert "start" not in body and "end" not in body
    assert "moved" not in res.out


def test_a_block_beyond_the_settle_window_is_re_optimized(harness):
    # Day 3 is outside the default 2-day window: you haven't planned that
    # day yet, so taking the earlier slot is free.
    loose = managed_event(id="ev1", task_uuid="u1", start=at(3, 14), end=at(3, 15))
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60)).events(loose)
    harness.run()

    assert harness.gcal.event_for("u1").start == at(0, 9)


def test_settle_days_zero_restores_earliest_fit(harness):
    settled = managed_event(id="ev1", task_uuid="u1", start=at(1, 14), end=at(1, 15))
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60)).events(settled)
    harness.configure(settle_days=0).run()

    assert harness.gcal.event_for("u1").start == at(0, 9)


def test_a_settled_block_yields_when_a_meeting_lands_on_it(harness):
    settled = managed_event(id="ev1", task_uuid="u1", start=at(1, 14), end=at(1, 15))
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60)).events(settled)
    harness.busy((at(1, 14), at(1, 15)))
    res = harness.run()

    assert harness.gcal.event_for("u1").start == at(0, 9)
    assert "overlaps a calendar event" in res.section("Scheduled")[0]


def test_a_settled_block_yields_when_its_estimate_grows(harness):
    settled = managed_event(id="ev1", task_uuid="u1", start=at(1, 14), end=at(1, 15))
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=120)).events(settled)
    res = harness.run()

    assert harness.gcal.event_for("u1").end - harness.gcal.event_for("u1").start == (
        timedelta(minutes=120)
    )
    assert "estimate changed" in res.section("Scheduled")[0]


def test_a_settled_block_yields_when_the_due_date_moves_in_front_of_it(harness):
    settled = managed_event(id="ev1", task_uuid="u1", start=at(1, 14), end=at(1, 15))
    harness.tasks(task_row(uuid="u1", due=at(1, 12), estimate=60)).events(settled)
    res = harness.run()

    assert harness.gcal.event_for("u1").start == at(0, 9)
    assert "ends after its due date" in res.section("Scheduled")[0]


def test_a_settled_block_survives_a_newly_urgent_task(harness):
    # The whole point of reserving settled blocks first: the urgent task
    # takes the earliest *free* slot, not the one already promised.
    settled = managed_event(id="ev1", task_uuid="calm", start=at(0, 9), end=at(0, 10))
    harness.tasks(
        task_row(uuid="calm", id=1, urgency=1.0, due=FRI_5PM, estimate=60),
        task_row(uuid="urgent", id=2, urgency=99.0, due=FRI_5PM, estimate=60),
    ).events(settled)
    harness.run()

    assert harness.gcal.event_for("calm").start == at(0, 9)
    assert harness.gcal.event_for("urgent").start == at(0, 10)


def test_a_settled_block_is_not_patched_at_all(harness):
    # Cost three of always re-deriving placements: pointless API calls, and
    # a notification for every block that has attendees.
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60))
    harness.run()
    harness.gcal.patched.clear()

    harness.run()
    assert harness.gcal.patched == []


def test_two_settled_blocks_that_overlap_resolve_in_favour_of_the_earlier(harness):
    # Only reachable by editing the calendar by hand, but it must not leave
    # both in place on top of each other.
    first = managed_event(id="ev1", task_uuid="a", start=at(0, 10), end=at(0, 11))
    second = managed_event(id="ev2", task_uuid="b", start=at(0, 10, 30), end=at(0, 11, 30))
    harness.tasks(
        task_row(uuid="a", id=1, due=FRI_5PM, estimate=60),
        task_row(uuid="b", id=2, due=FRI_5PM, estimate=60),
    ).events(first, second)
    harness.run()

    assert harness.gcal.event_for("a").start == at(0, 10)
    assert harness.gcal.event_for("b").start == at(0, 9)


# ---------------------------------------------------------------------------
# Duplicate and orphan cleanup
# ---------------------------------------------------------------------------

def test_the_earliest_upcoming_event_is_the_keeper(harness):
    first = managed_event(id="ev1", task_uuid="u1", start=at(1, 9), end=at(1, 10))
    second = managed_event(id="ev2", task_uuid="u1", start=at(2, 9), end=at(2, 10))
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60)).events(first, second)
    harness.run()

    assert harness.gcal.deleted == ["ev2"]
    assert harness.gcal.created == []


def test_an_in_progress_keeper_beats_an_earlier_upcoming_duplicate(harness):
    ongoing = managed_event(
        id="ev-now", task_uuid="u1", start=at(0, 8, 30), end=at(0, 9, 30)
    )
    later = managed_event(id="ev-later", task_uuid="u1", start=at(0, 14), end=at(0, 15))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60)).events(ongoing, later)
    harness.run()

    assert harness.gcal.deleted == ["ev-later"]
    assert harness.gcal.event_for("u1").start == at(0, 8, 30)


def test_a_second_in_progress_copy_is_removed(harness):
    # Only the keeper is protected from removal mid-event. Both of these are
    # happening right now, so the later one is a spurious copy and goes --
    # which is why duplicate cleanup keys on `end > now`, not `start > now`.
    keeper = managed_event(id="ev-keeper", task_uuid="u1", start=at(0, 8), end=at(0, 10))
    copy = managed_event(id="ev-copy", task_uuid="u1", start=at(0, 8, 45), end=at(0, 9, 45))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=120)).events(keeper, copy)
    harness.run()

    assert harness.gcal.deleted == ["ev-copy"]
    assert harness.gcal.event_for("u1").id == "ev-keeper"


def test_a_finished_duplicate_is_kept_as_history(harness):
    past = managed_event(id="ev-past", task_uuid="u1", start=at(-3, 9), end=at(-3, 10))
    future = managed_event(id="ev-next", task_uuid="u1", start=at(1, 9), end=at(1, 10))
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60)).events(past, future)
    harness.run()

    assert harness.gcal.deleted == []


def test_a_future_event_for_a_vanished_task_is_removed(harness):
    orphan = managed_event(id="ev-orphan", task_uuid="gone", start=at(1, 9), end=at(1, 10))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60)).events(orphan)
    res = harness.run()

    assert harness.gcal.deleted == ["ev-orphan"]
    assert len(res.section("Removed completed/dropped")) == 1


def test_a_past_event_for_a_vanished_task_is_kept(harness):
    done = managed_event(id="ev-done", task_uuid="gone", start=at(-3, 9), end=at(-3, 10))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60)).events(done)
    harness.run()

    assert harness.gcal.deleted == []


def test_a_tagged_event_with_no_task_uuid_is_collected(harness):
    # It carries our scheduler tag but no task, so it can only be a leftover
    # from a bug or a hand-edited copy: never a keeper, removed if it's still
    # in the future, kept if it's already history.
    stray = managed_event(id="ev-stray", task_uuid=None, start=at(1, 9), end=at(1, 10))
    old = managed_event(id="ev-old", task_uuid=None, start=at(-3, 9), end=at(-3, 10))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60)).events(stray, old)
    harness.run()

    assert harness.gcal.deleted == ["ev-stray"]


# ---------------------------------------------------------------------------
# Tasks we can't place
# ---------------------------------------------------------------------------

def test_a_task_without_an_estimate_is_skipped_and_its_event_dropped(harness):
    ev = managed_event(id="ev1", task_uuid="u1", start=at(1, 9), end=at(1, 10))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=None)).events(ev)
    res = harness.run()

    assert harness.gcal.deleted == ["ev1"]
    assert len(res.section("Skipped: no `estimate`")) == 1


def test_a_task_without_a_due_date_is_skipped(harness):
    harness.tasks(task_row(uuid="u1", due=None, estimate=60))
    res = harness.run()

    assert harness.gcal.created == []
    assert len(res.section("Skipped: no due date")) == 1


def test_a_task_that_cannot_fit_is_reported_not_placed(harness):
    # Ten hours of work, nine hours of working day.
    harness.tasks(task_row(uuid="u1", due=at(1, 17), estimate=600))
    res = harness.run()

    assert harness.gcal.created == []
    assert len(res.section("Could not fit")) == 1


def test_an_unplaceable_tasks_future_event_is_dropped(harness):
    ev = managed_event(id="ev1", task_uuid="u1", start=at(1, 9), end=at(1, 10))
    harness.tasks(task_row(uuid="u1", due=at(1, 17), estimate=600)).events(ev)
    harness.run()
    assert harness.gcal.deleted == ["ev1"]


def test_nothing_to_do_when_there_is_nothing(harness):
    res = harness.run()
    assert res.code == 0
    assert "Nothing to do." in res.out


# ---------------------------------------------------------------------------
# scheduled / wait floors
# ---------------------------------------------------------------------------

def test_scheduled_date_is_an_earliest_start(harness):
    harness.tasks(
        task_row(uuid="u1", due=FRI_5PM, estimate=60, scheduled=at(2, 0))
    )
    harness.run()
    assert harness.gcal.created[0]["start"] == at(2, 9)


def test_wait_date_is_an_earliest_start(harness):
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60, wait=at(2, 0)))
    harness.run()
    assert harness.gcal.created[0]["start"] == at(2, 9)


def test_the_later_of_scheduled_and_wait_wins(harness):
    harness.tasks(
        task_row(
            uuid="u1", due=FRI_5PM, estimate=60, scheduled=at(1, 0), wait=at(3, 0)
        )
    )
    harness.run()
    assert harness.gcal.created[0]["start"] == at(3, 9)


def test_a_floor_inside_the_working_day_is_inclusive(harness):
    harness.tasks(
        task_row(uuid="u1", due=FRI_5PM, estimate=60, scheduled=at(1, 14))
    )
    harness.run()
    assert harness.gcal.created[0]["start"] == at(1, 14)


def test_a_floor_in_the_past_is_ignored(harness):
    harness.tasks(
        task_row(uuid="u1", due=WED_5PM, estimate=60, scheduled=at(-5, 9))
    )
    harness.run()
    assert harness.gcal.created[0]["start"] == at(0, 9)


# ---------------------------------------------------------------------------
# The midnight-due bump
# ---------------------------------------------------------------------------

def test_effective_due_bumps_a_midnight_due_to_the_end_of_that_day():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    got = schedule_mod.effective_due(at(1, 0), tz, Settings(timezone="UTC"))
    assert got == at(1, 18)


def test_effective_due_leaves_an_explicit_time_alone():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    got = schedule_mod.effective_due(at(1, 14, 30), tz, Settings(timezone="UTC"))
    assert got == at(1, 14, 30)


def test_a_date_only_due_can_still_be_scheduled_on_that_day(harness):
    # Monday is full, so this can only be placed on Tuesday — which is only
    # legal because `due:tuesday` (midnight) is read as end of Tuesday.
    harness.tasks(task_row(uuid="u1", due=at(1, 0), estimate=60))
    harness.busy((at(0, 9), at(0, 18)))
    harness.run()

    assert harness.gcal.created[0]["start"] == at(1, 9)


def test_a_midnight_due_survives_a_work_end_hour_of_24(harness):
    # `_effective_due` used to build the end of day with replace(hour=...),
    # which raises for hour=24.
    harness.tasks(task_row(uuid="u1", due=at(1, 0), estimate=60))
    res = harness.configure(work_end_hour=24).run()

    assert res.code == 0
    assert harness.gcal.created[0]["start"] == at(0, 9)


def test_a_midnight_due_with_work_end_hour_24_means_the_whole_day(harness):
    # Monday is booked solid to midnight, so this can only land on Tuesday --
    # which is legal because `due:tuesday` now means "by the end of Tuesday".
    harness.tasks(task_row(uuid="u1", due=at(1, 0), estimate=60))
    harness.busy((at(0, 9), at(1, 0)))
    harness.configure(work_end_hour=24).run()

    assert harness.gcal.created[0]["start"] == at(1, 9)


def test_a_per_task_work_end_hour_of_24_is_accepted(harness):
    harness.tasks(
        task_row(uuid="u1", due=at(1, 0), estimate=60, gcal="work_end_hour=24")
    )
    harness.busy((at(0, 9), at(0, 23)))
    res = harness.run()

    assert res.err == ""
    assert harness.gcal.created[0]["start"] == at(0, 23)


def test_a_per_task_hour_out_of_range_warns_instead_of_crashing(harness):
    harness.tasks(
        task_row(uuid="u1", id=42, due=WED_5PM, estimate=60, gcal="work_end_hour=25")
    )
    res = harness.run()

    assert "#42" in res.err
    assert "between 0 and 24" in res.err
    assert harness.gcal.created[0]["start"] == at(0, 9)


def test_an_estimate_under_a_minute_counts_as_missing(harness):
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=0.4))
    res = harness.run()

    assert harness.gcal.created == []
    assert len(res.section("Skipped: no `estimate`")) == 1


def test_the_bump_respects_a_per_task_work_end_hour(harness):
    harness.tasks(
        task_row(uuid="u1", due=at(1, 0), estimate=60, gcal="work_end_hour=20")
    )
    harness.busy((at(0, 9), at(0, 20)), (at(1, 9), at(1, 18, 30)))
    harness.run()
    # Tuesday's window now runs to 20:00, so 18:30 is still schedulable.
    assert harness.gcal.created[0]["start"] == at(1, 18, 30)


# ---------------------------------------------------------------------------
# Overdue policy
# ---------------------------------------------------------------------------

def test_an_overdue_task_is_scheduled_as_soon_as_possible(harness):
    harness.tasks(task_row(uuid="u1", due=at(-3, 17), estimate=60))
    harness.run()
    assert harness.gcal.created[0]["start"] == at(0, 9)


def test_a_task_scheduled_past_its_due_date_is_flagged(harness):
    harness.tasks(task_row(uuid="u1", due=at(-3, 17), estimate=60))
    res = harness.run()

    assert len(res.section("Overdue")) >= 1
    assert res.section("Scheduled") == []


def test_the_overdue_warning_is_printed_last(harness):
    harness.tasks(
        task_row(uuid="late", id=1, urgency=9.0, due=at(-3, 17), estimate=60),
        task_row(uuid="fine", id=2, urgency=8.0, due=WED_5PM, estimate=60),
    )
    res = harness.run()
    assert res.out.index("Scheduled (") < res.out.index("Overdue —")


def test_due_today_at_midnight_is_not_reported_as_past_due(harness):
    # Overdue by the raw due date, but placing it later today still counts as
    # in time, so it stays in the normal list.
    harness.tasks(task_row(uuid="u1", due=at(0, 0), estimate=60))
    res = harness.run()

    assert harness.gcal.created[0]["start"] == at(0, 9)
    assert res.section("Overdue") == []
    assert len(res.section("Scheduled")) == 1


def test_the_overdue_horizon_bounds_how_far_ahead_we_look(harness):
    harness.tasks(task_row(uuid="u1", due=at(-3, 17), estimate=60))
    res = harness.configure(overdue_horizon_days=0).run()

    assert harness.gcal.created == []
    assert len(res.section("Could not fit")) == 1


def test_a_per_task_overdue_horizon_is_honored(harness):
    harness.tasks(
        task_row(
            uuid="u1", due=at(-3, 17), estimate=60, gcal="overdue_horizon_days=0"
        )
    )
    res = harness.run()
    assert harness.gcal.created == []
    assert len(res.section("Could not fit")) == 1


# ---------------------------------------------------------------------------
# Per-task overrides
# ---------------------------------------------------------------------------

def test_an_override_can_extend_the_working_day(harness):
    harness.tasks(
        task_row(uuid="u1", due=at(0, 21), estimate=60, gcal="work_end_hour=20")
    )
    harness.busy((at(0, 9), at(0, 17, 30)))
    res = harness.run()

    assert harness.gcal.created[0]["start"] == at(0, 17, 30)
    assert "[override: work_end_hour=20]" in "\n".join(res.section("Scheduled"))


def test_without_the_override_the_same_task_does_not_fit(harness):
    harness.tasks(task_row(uuid="u1", due=at(0, 21), estimate=60))
    harness.busy((at(0, 9), at(0, 17, 30)))
    res = harness.run()

    assert harness.gcal.created == []
    assert len(res.section("Could not fit")) == 1


def test_an_override_can_open_the_weekend(harness):
    harness.tasks(
        task_row(uuid="u1", due=at(6, 17), estimate=60, gcal="work_days=5,6")
    )
    harness.run()
    # Saturday is five days after Monday.
    assert harness.gcal.created[0]["start"] == at(5, 9)


def test_a_per_task_color_is_used(harness):
    harness.tasks(
        task_row(uuid="u1", due=WED_5PM, estimate=60, gcal="event_color_id=11")
    )
    harness.run()
    assert harness.gcal.created[0]["color_id"] == "11"


def test_a_bad_override_warns_and_falls_back_to_global_settings(harness):
    harness.tasks(
        task_row(uuid="u1", id=42, due=WED_5PM, estimate=60, gcal="wrok_end_hour=20")
    )
    res = harness.run()

    assert "#42" in res.err
    assert "unknown or non-per-task" in res.err
    assert harness.gcal.created[0]["start"] == at(0, 9)


# ---------------------------------------------------------------------------
# Event content and attendees
# ---------------------------------------------------------------------------

def test_the_description_carries_the_task_uuid_and_metadata(harness):
    harness.tasks(
        task_row(
            uuid="u1",
            due=WED_5PM,
            estimate=60,
            project="proj",
            tags=["work"],
            annotations=["remember this"],
        )
    )
    harness.run()
    desc = harness.gcal.created[0]["description"]
    assert "Task UUID: u1" in desc
    assert "Project: proj" in desc
    assert "Tags: work" in desc
    assert "remember this" in desc


def test_the_description_never_contains_urgency(harness):
    # Urgency changes every hour via Taskwarrior's age coefficient; putting it
    # in the body would make every run a patch.
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60, urgency=27.66))
    harness.run()
    assert "27.66" not in harness.gcal.created[0]["description"]


def test_attendees_are_invited_from_the_override(harness):
    harness.tasks(
        task_row(uuid="u1", due=WED_5PM, estimate=60, gcal="attendees=a@b.com")
    )
    harness.run()
    assert harness.gcal.created[0]["attendees"] == ("a@b.com",)


def test_attendees_are_added_without_dropping_existing_ones(harness):
    ev = managed_event(
        id="ev1",
        task_uuid="u1",
        start=at(1, 9),
        end=at(1, 10),
        attendees=[{"email": "manual@x.com"}],
    )
    harness.tasks(
        task_row(uuid="u1", due=FRI_5PM, estimate=60, gcal="attendees=new@y.com")
    ).events(ev)
    harness.run()

    (_, body) = harness.gcal.patched[0]
    assert body["attendees"] == [{"email": "manual@x.com"}, {"email": "new@y.com"}]


def test_an_already_invited_attendee_is_not_re_added(harness):
    ev = managed_event(
        id="ev1",
        task_uuid="u1",
        start=at(1, 9),
        end=at(1, 10),
        attendees=[{"email": "Same@X.com"}],
    )
    harness.tasks(
        task_row(uuid="u1", due=FRI_5PM, estimate=60, gcal="attendees=same@x.com")
    ).events(ev)
    harness.run()

    for _id, body in harness.gcal.patched:
        assert "attendees" not in body


# ---------------------------------------------------------------------------
# Patch / create fallbacks
# ---------------------------------------------------------------------------

def test_an_event_that_vanished_before_the_patch_is_recreated(harness):
    ev = managed_event(id="ev1", task_uuid="u1", start=at(1, 9), end=at(1, 10))
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60)).events(ev)
    harness.gcal.vanished.add("ev1")
    res = harness.run()

    assert harness.gcal.patched
    assert len(harness.gcal.created) == 1
    assert res.section("Scheduled")[0].startswith("[create")


def test_a_finished_event_for_a_still_pending_task_gets_a_fresh_one(harness):
    past = managed_event(id="ev-old", task_uuid="u1", start=at(-3, 9), end=at(-3, 10))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60)).events(past)
    res = harness.run()

    assert len(harness.gcal.created) == 1
    assert harness.gcal.created[0]["start"] == at(0, 9)
    assert "ev-old" not in harness.gcal.deleted
    assert res.section("Scheduled")[0].startswith("[create")


def test_only_the_fields_that_changed_are_patched(harness):
    harness.tasks(task_row(uuid="u1", description="original", due=FRI_5PM, estimate=60))
    harness.run()
    harness.gcal.patched.clear()

    harness.tasks(task_row(uuid="u1", description="renamed", due=FRI_5PM, estimate=60))
    harness.run()

    (_, body) = harness.gcal.patched[0]
    assert body["summary"] == "renamed"
    assert "start" not in body and "end" not in body


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_dry_run_makes_no_mutating_calls(harness):
    orphan = managed_event(id="ev-orphan", task_uuid="gone", start=at(1, 9), end=at(1, 10))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60)).events(orphan)
    res = harness.run(dry_run=True)

    assert harness.gcal.mutations == 0
    assert res.out.startswith("# DRY RUN")


def test_dry_run_still_reports_what_it_would_do(harness):
    orphan = managed_event(id="ev-orphan", task_uuid="gone", start=at(1, 9), end=at(1, 10))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60)).events(orphan)
    res = harness.run(dry_run=True)

    assert res.section("Scheduled")[0].startswith("[create")
    assert len(res.section("Removed completed/dropped")) == 1


# ---------------------------------------------------------------------------
# The bulk-removal guard, end to end
# ---------------------------------------------------------------------------

def test_an_empty_task_list_never_clears_the_calendar(harness):
    ev = managed_event(id="ev1", task_uuid="u1", start=at(1, 9), end=at(1, 10))
    harness.events(ev)
    res = harness.run()

    assert res.code == 1
    assert harness.gcal.mutations == 0
    assert "returned no tasks" in res.err


def test_the_empty_task_list_message_names_the_likely_causes(harness):
    harness.events(managed_event(id="ev1", task_uuid="u1", start=at(1, 9), end=at(1, 10)))
    res = harness.run()
    assert "context" in res.err and "TASKDATA" in res.err


def test_an_empty_task_list_is_fine_when_we_own_nothing(harness):
    res = harness.run()
    assert res.code == 0
    assert "Nothing to do." in res.out


def test_a_finished_event_does_not_block_an_empty_task_list(harness):
    # Past events are history, not something the guard should protect.
    harness.events(
        managed_event(id="ev-old", task_uuid="u1", start=at(-3, 9), end=at(-3, 10))
    )
    res = harness.run()
    assert res.code == 0


def test_a_wholesale_removal_is_refused(harness):
    # Every task loses its estimate at once: the mistyped-UDA failure mode.
    events = [
        managed_event(id=f"ev{i}", task_uuid=f"u{i}", start=at(1, 9 + i), end=at(1, 10 + i))
        for i in range(4)
    ]
    harness.tasks(
        *[task_row(uuid=f"u{i}", id=i + 1, estimate=None, due=WED_5PM) for i in range(4)]
    ).events(*events)
    res = harness.run()

    assert res.code == 1
    assert harness.gcal.deleted == []
    assert len(res.section("Held back")) == 4
    assert res.section("Removed stale") == []


def test_force_bypasses_the_guard(harness):
    events = [
        managed_event(id=f"ev{i}", task_uuid=f"u{i}", start=at(1, 9 + i), end=at(1, 10 + i))
        for i in range(4)
    ]
    harness.tasks(
        *[task_row(uuid=f"u{i}", id=i + 1, estimate=None, due=WED_5PM) for i in range(4)]
    ).events(*events)
    res = harness.run(force=True)

    assert res.code == 0
    assert sorted(harness.gcal.deleted) == ["ev0", "ev1", "ev2", "ev3"]


def test_a_ratio_of_one_disables_the_guard(harness):
    events = [
        managed_event(id=f"ev{i}", task_uuid=f"u{i}", start=at(1, 9 + i), end=at(1, 10 + i))
        for i in range(4)
    ]
    harness.tasks(
        *[task_row(uuid=f"u{i}", id=i + 1, estimate=None, due=WED_5PM) for i in range(4)]
    ).events(*events)
    res = harness.configure(removal_guard_ratio=1.0).run()

    assert res.code == 0
    assert len(harness.gcal.deleted) == 4


def test_a_small_cleanup_is_not_refused(harness):
    events = [
        managed_event(id=f"ev{i}", task_uuid=f"u{i}", start=at(1, 9 + i), end=at(1, 10 + i))
        for i in range(4)
    ]
    # One task loses its estimate; the other three are placed normally.
    rows = [task_row(uuid=f"u{i}", id=i + 1, due=WED_5PM, estimate=60) for i in range(4)]
    rows[0] = task_row(uuid="u0", id=1, due=WED_5PM, estimate=None)
    harness.tasks(*rows).events(*events)
    res = harness.run()

    assert res.code == 0
    assert harness.gcal.deleted == ["ev0"]


def test_the_guard_still_lets_placements_happen(harness):
    # A blocked cleanup must not block the useful half of the run.
    events = [
        managed_event(id=f"ev{i}", task_uuid=f"u{i}", start=at(1, 9 + i), end=at(1, 10 + i))
        for i in range(4)
    ]
    rows = [task_row(uuid=f"u{i}", id=i + 1, estimate=None, due=WED_5PM) for i in range(4)]
    rows.append(task_row(uuid="fresh", id=99, due=WED_5PM, estimate=60))
    harness.tasks(*rows).events(*events)
    res = harness.run()

    assert res.code == 1
    assert [c["task_uuid"] for c in harness.gcal.created] == ["fresh"]


def test_the_guard_reports_in_a_dry_run_too(harness):
    events = [
        managed_event(id=f"ev{i}", task_uuid=f"u{i}", start=at(1, 9 + i), end=at(1, 10 + i))
        for i in range(4)
    ]
    harness.tasks(
        *[task_row(uuid=f"u{i}", id=i + 1, estimate=None, due=WED_5PM) for i in range(4)]
    ).events(*events)
    res = harness.run(dry_run=True)

    assert res.code == 1
    assert len(res.section("Held back")) == 4
