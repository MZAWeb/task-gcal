"""Tests for the observed-change layer.

Every churn metric is a diff between two journal observations, so this is the
one place that has to be exactly right about what "observed" means: a value
that moved twice between two runs is one change, and one that moved out and
back is none.
"""

from __future__ import annotations

from datetime import timedelta

from task_gcal import journal
from task_gcal.config import Settings
from task_gcal.review.observed import build_timelines, sampling_note

from conftest import NOW, a_task, at

SETTINGS = Settings(timezone="UTC")


def record(when, *tasks, mode=journal.MODE_SNAPSHOT, blocks=None):
    return journal.build_record(
        settings=SETTINGS,
        mode=mode,
        at=when,
        observations=journal.observe_tasks(
            list(tasks), blocks=blocks or {}, detail="full"
        ),
    )


def timeline(*records, uuid="u1"):
    return build_timelines(records)[uuid]


# ---------------------------------------------------------------------------
# Due dates
# ---------------------------------------------------------------------------

def test_a_due_date_moving_later_is_a_push():
    t = timeline(
        record(at(0, 9), a_task(due=at(1, 17))),
        record(at(1, 9), a_task(due=at(3, 17))),
    )
    (push,) = t.pushes()
    assert push.previous == at(1, 17)
    assert push.current == at(3, 17)
    assert push.days == 2.0


def test_a_due_date_moving_earlier_is_not_a_push():
    t = timeline(
        record(at(0, 9), a_task(due=at(3, 17))),
        record(at(1, 9), a_task(due=at(1, 17))),
    )
    assert t.pushes() == []
    assert len(t.pulls()) == 1


def test_an_unchanged_due_date_records_nothing():
    t = timeline(
        record(at(0, 9), a_task(due=at(3, 17))),
        record(at(1, 9), a_task(due=at(3, 17))),
        record(at(2, 9), a_task(due=at(3, 17))),
    )
    assert t.due_changes == []


def test_a_date_that_moved_out_and_back_between_runs_is_unobserved():
    # The documented cost of sampling. It's why every number says "observed".
    t = timeline(
        record(at(0, 9), a_task(due=at(3, 17))),
        record(at(2, 9), a_task(due=at(3, 17))),
    )
    assert t.pushes() == []


def test_two_moves_between_runs_look_like_one():
    t = timeline(
        record(at(0, 9), a_task(due=at(1, 17))),
        record(at(2, 9), a_task(due=at(5, 17))),
    )
    (push,) = t.pushes()
    assert push.days == 4.0  # not 2 + 2 as two separate observations


def test_the_first_observation_is_not_a_change():
    # A value's history before we started looking is unknown, not absent.
    t = timeline(record(at(0, 9), a_task(due=at(3, 17))))
    assert t.due_changes == []
    assert t.first_due == at(3, 17)


def test_days_pushed_accumulates():
    t = timeline(
        record(at(0, 9), a_task(due=at(1, 17))),
        record(at(1, 9), a_task(due=at(3, 17))),
        record(at(3, 9), a_task(due=at(6, 17))),
    )
    assert t.days_pushed == 5.0


# ---------------------------------------------------------------------------
# Reactive vs proactive
# ---------------------------------------------------------------------------

def test_a_push_noticed_after_the_old_date_is_reactive():
    # Moved on Wednesday a deadline that was Monday: reporting a miss.
    t = timeline(
        record(at(0, 9), a_task(due=at(0, 17))),
        record(at(2, 9), a_task(due=at(4, 17))),
    )
    assert len(t.reactive_pushes()) == 1
    assert t.proactive_pushes() == []


def test_a_push_noticed_before_the_old_date_is_proactive():
    # Moved on Monday a deadline that was Friday: renegotiating.
    t = timeline(
        record(at(0, 9), a_task(due=at(4, 17))),
        record(at(0, 18), a_task(due=at(6, 17))),
    )
    assert len(t.proactive_pushes()) == 1
    assert t.reactive_pushes() == []


def test_the_two_kinds_are_counted_separately_not_ranked():
    t = timeline(
        record(at(0, 9), a_task(due=at(4, 17))),
        record(at(1, 9), a_task(due=at(6, 17))),   # proactive
        record(at(7, 9), a_task(due=at(9, 17))),   # reactive
    )
    assert len(t.pushes()) == 2
    assert len(t.reactive_pushes()) == 1
    assert len(t.proactive_pushes()) == 1


# ---------------------------------------------------------------------------
# Estimates, titles, deferrals
# ---------------------------------------------------------------------------

def test_an_estimate_growing_is_an_upward_change():
    t = timeline(
        record(at(0, 9), a_task(estimate=60)),
        record(at(1, 9), a_task(estimate=180)),
    )
    (change,) = t.upward_estimate_changes()
    assert (change.previous, change.current) == (60, 180)
    assert t.first_estimate == 60
    assert t.last_estimate == 180


def test_an_estimate_shrinking_is_not():
    t = timeline(
        record(at(0, 9), a_task(estimate=180)),
        record(at(1, 9), a_task(estimate=60)),
    )
    assert t.upward_estimate_changes() == []
    assert len(t.estimate_changes) == 1


def test_a_retitle_is_observed():
    t = timeline(
        record(at(0, 9), a_task(description="do the thing")),
        record(at(1, 9), a_task(description="do the other thing")),
    )
    assert len(t.retitles()) == 1


def test_a_retitle_is_observed_even_when_titles_are_not_stored():
    # `journal_detail = "minimal"` keeps a digest, which is enough to notice.
    minimal = journal.observe_tasks(
        [a_task(description="before")], blocks={}, detail="minimal"
    )
    after = journal.observe_tasks(
        [a_task(description="after")], blocks={}, detail="minimal"
    )
    records = [
        journal.build_record(
            settings=SETTINGS, mode=journal.MODE_SNAPSHOT, at=at(0, 9),
            observations=minimal,
        ),
        journal.build_record(
            settings=SETTINGS, mode=journal.MODE_SNAPSHOT, at=at(1, 9),
            observations=after,
        ),
    ]
    assert len(build_timelines(records)["u1"].retitles()) == 1


def test_scheduled_and_wait_moves_are_deferrals_not_deadline_churn():
    t = timeline(
        record(at(0, 9), a_task(scheduled=at(1, 9))),
        record(at(1, 9), a_task(scheduled=at(3, 9), wait=at(3, 9))),
    )
    assert len(t.deferrals()) == 2
    assert t.pushes() == []


# ---------------------------------------------------------------------------
# Placements
# ---------------------------------------------------------------------------

def test_a_block_moving_is_a_placement_move():
    block_a = {"u1": journal.ObservedBlock(start=at(1, 9), end=at(1, 10))}
    block_b = {"u1": journal.ObservedBlock(start=at(2, 9), end=at(2, 10))}
    t = timeline(
        record(at(0, 9), a_task(), blocks=block_a),
        record(at(0, 18), a_task(), blocks=block_a),
        record(at(1, 9), a_task(), blocks=block_b),
    )
    assert t.placement_moves() == 1


def test_a_reconstructed_history_reports_no_placement_moves(isolated_journal):
    # Backfill carries no blocks at all, so zero here means unobserved
    # rather than still — which is why the caveat says so.
    t = timeline(
        record(at(0, 9), a_task(), mode=journal.MODE_BACKFILL),
        record(at(1, 9), a_task(), mode=journal.MODE_BACKFILL),
    )
    assert t.placement_moves() == 0
    assert t.reconstructed is True


# ---------------------------------------------------------------------------
# Windows and gaps
# ---------------------------------------------------------------------------

def test_changes_can_be_restricted_to_a_window():
    t = timeline(
        record(at(-7, 9), a_task(due=at(1, 17))),
        record(at(-6, 9), a_task(due=at(3, 17))),   # last week
        record(at(1, 9), a_task(due=at(6, 17))),    # this week
    )
    this_week = (NOW, NOW + timedelta(days=7))
    assert len(t.pushes()) == 2
    assert len(t.pushes(within=this_week)) == 1


def test_a_task_that_leaves_and_returns_is_compared_with_when_we_last_saw_it():
    # The honest reading of "observed": we don't know when in the gap it
    # changed, only that it did.
    t = timeline(
        record(at(0, 9), a_task(due=at(1, 17))),
        record(at(1, 9)),                            # absent from the report
        record(at(2, 9), a_task(due=at(5, 17))),
    )
    (push,) = t.pushes()
    assert push.at == at(2, 9)
    assert push.observed_after == at(0, 9)


def test_records_are_diffed_in_time_order_whatever_order_they_arrive():
    t = timeline(
        record(at(2, 9), a_task(due=at(6, 17))),
        record(at(0, 9), a_task(due=at(1, 17))),
        record(at(1, 9), a_task(due=at(3, 17))),
    )
    assert [c.days for c in t.pushes()] == [2.0, 3.0]


def test_several_tasks_are_tracked_independently():
    timelines = build_timelines([
        record(at(0, 9), a_task(uuid="a", due=at(1, 17)),
               a_task(uuid="b", due=at(1, 17))),
        record(at(1, 9), a_task(uuid="a", due=at(4, 17)),
               a_task(uuid="b", due=at(1, 17))),
    ])
    assert len(timelines["a"].pushes()) == 1
    assert timelines["b"].pushes() == []


# ---------------------------------------------------------------------------
# Sampling note
# ---------------------------------------------------------------------------

def test_the_sampling_note_describes_the_cadence():
    records = [record(at(0, 9 + n * 6), a_task()) for n in range(3)]
    assert sampling_note(records) == "sampled every 6h on average"


def test_a_single_observation_says_so():
    assert sampling_note([record(at(0, 9), a_task())]) == "sampled once"


def test_no_observations_says_so():
    assert sampling_note([]) == "no observations"
