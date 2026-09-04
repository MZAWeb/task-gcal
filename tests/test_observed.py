"""Tests for the per-task change series.

These used to test snapshot diffing, with its sampling blindness — a value that
moved twice between runs looked like one move, and one that moved out and back
looked like none. The series now comes from Taskwarrior's own operation log, so
both cases are recorded exactly, and these tests pin that.
"""

from __future__ import annotations

from datetime import timedelta

from task_gcal.review.observed import build_timelines

from conftest import NOW, a_field_change, at, due_moved, estimate_changed


def timeline(*changes, uuid="u1"):
    return build_timelines(changes)[uuid]


# ---------------------------------------------------------------------------
# Due dates
# ---------------------------------------------------------------------------

def test_a_due_date_moving_later_is_a_push():
    t = timeline(due_moved("u1", at(1, 9), at(1, 17), at(3, 17)))
    (push,) = t.pushes()

    assert push.previous == at(1, 17)
    assert push.current == at(3, 17)
    assert push.days == 2.0


def test_a_due_date_moving_earlier_is_not_a_push():
    t = timeline(due_moved("u1", at(1, 9), at(3, 17), at(1, 17)))
    assert t.pushes() == []
    assert len(t.pulls()) == 1


def test_two_moves_are_two_changes():
    # The whole point of the exact source: sampled snapshots collapsed these
    # into one, understating churn.
    t = timeline(
        due_moved("u1", at(1, 9), at(1, 17), at(3, 17)),
        due_moved("u1", at(1, 10), at(3, 17), at(5, 17)),
    )
    assert [c.days for c in t.pushes()] == [2.0, 2.0]
    assert t.days_pushed == 4.0


def test_a_date_moved_out_and_back_records_both_moves():
    # Sampling saw neither of these. One is a push, one is a pull.
    t = timeline(
        due_moved("u1", at(1, 9), at(1, 17), at(5, 17)),
        due_moved("u1", at(2, 9), at(5, 17), at(1, 17)),
    )
    assert len(t.pushes()) == 1
    assert len(t.pulls()) == 1


def test_the_original_due_date_is_the_value_before_the_first_change():
    t = timeline(
        due_moved("u1", at(1, 9), at(1, 17), at(3, 17)),
        due_moved("u1", at(2, 9), at(3, 17), at(6, 17)),
    )
    assert t.first_due == at(1, 17)
    assert t.last_due == at(6, 17)


def test_a_due_date_set_from_nothing_is_the_original():
    t = timeline(a_field_change(at=at(0, 9), field="due", old=None, new=at(2, 17)))
    assert t.first_due == at(2, 17)


def test_a_due_date_being_cleared_is_a_change_with_no_current_value():
    t = timeline(a_field_change(at=at(1, 9), field="due", old=at(2, 17), new=None))
    (change,) = t.due_changes

    assert change.current is None
    assert t.pushes() == []  # not a push; there's no later date


# ---------------------------------------------------------------------------
# Reactive vs proactive
# ---------------------------------------------------------------------------

def test_a_push_made_after_the_old_date_is_reactive():
    # Moved on Wednesday a deadline that was Monday: reporting a miss.
    t = timeline(due_moved("u1", at(2, 9), at(0, 17), at(4, 17)))
    assert len(t.reactive_pushes()) == 1
    assert t.proactive_pushes() == []


def test_a_push_made_before_the_old_date_is_proactive():
    # Moved on Monday a deadline that was Friday: renegotiating.
    t = timeline(due_moved("u1", at(0, 9), at(4, 17), at(6, 17)))
    assert len(t.proactive_pushes()) == 1
    assert t.reactive_pushes() == []


def test_the_two_kinds_are_counted_separately_not_ranked():
    t = timeline(
        due_moved("u1", at(0, 9), at(4, 17), at(6, 17)),   # proactive
        due_moved("u1", at(7, 9), at(6, 17), at(9, 17)),   # reactive
    )
    assert len(t.pushes()) == 2
    assert len(t.reactive_pushes()) == 1
    assert len(t.proactive_pushes()) == 1


# ---------------------------------------------------------------------------
# Estimates, titles, deferrals
# ---------------------------------------------------------------------------

def test_an_estimate_growing_is_an_upward_change():
    t = timeline(estimate_changed("u1", at(1, 9), 60, 180))
    (change,) = t.upward_estimate_changes()

    assert (change.previous, change.current) == (60, 180)
    assert t.first_estimate == 60
    assert t.last_estimate == 180


def test_an_estimate_shrinking_is_not():
    t = timeline(estimate_changed("u1", at(1, 9), 180, 60))
    assert t.upward_estimate_changes() == []
    assert len(t.estimate_changes) == 1


def test_a_retitle_is_recorded():
    t = timeline(
        a_field_change(at=at(1, 9), field="description", old="before", new="after")
    )
    assert len(t.retitles()) == 1
    assert t.label == "after"


def test_naming_a_task_when_it_is_created_is_not_a_retitle():
    # Every task gets a description set at creation; counting that as a rewrite
    # would make every task look redefined.
    t = timeline(
        a_field_change(at=at(0, 9), field="description", old=None, new="a task")
    )
    assert t.retitles() == []
    assert t.label == "a task"


def test_scheduled_and_wait_moves_are_deferrals_not_deadline_churn():
    t = timeline(
        a_field_change(at=at(1, 9), field="scheduled", old=at(1, 9), new=at(3, 9)),
        a_field_change(at=at(1, 9), field="wait", old=None, new=at(3, 9)),
    )
    assert len(t.deferrals()) == 2
    assert t.pushes() == []


def test_the_project_is_tracked_without_being_a_change_bucket():
    t = timeline(
        a_field_change(at=at(0, 9), field="project", old=None, new="fraud")
    )
    assert t.project == "fraud"


# ---------------------------------------------------------------------------
# Windows and ordering
# ---------------------------------------------------------------------------

def test_changes_can_be_restricted_to_a_window():
    t = timeline(
        due_moved("u1", at(-6, 9), at(1, 17), at(3, 17)),   # last week
        due_moved("u1", at(1, 9), at(3, 17), at(6, 17)),     # this week
    )
    this_week = (NOW, NOW + timedelta(days=7))

    assert len(t.pushes()) == 2
    assert len(t.pushes(within=this_week)) == 1


def test_changes_are_ordered_however_they_arrive():
    t = timeline(
        due_moved("u1", at(2, 9), at(3, 17), at(6, 17)),
        due_moved("u1", at(1, 9), at(1, 17), at(3, 17)),
    )
    assert [c.at for c in t.due_changes] == [at(1, 9), at(2, 9)]
    assert t.first_due == at(1, 17)


def test_the_span_covers_the_first_and_last_change():
    t = timeline(
        due_moved("u1", at(1, 9), at(1, 17), at(3, 17)),
        due_moved("u1", at(4, 9), at(3, 17), at(6, 17)),
    )
    assert (t.first_seen, t.last_seen) == (at(1, 9), at(4, 9))


def test_several_tasks_are_tracked_independently():
    timelines = build_timelines(
        [
            due_moved("a", at(1, 9), at(1, 17), at(4, 17)),
            estimate_changed("b", at(1, 9), 30, 60),
        ]
    )
    assert len(timelines["a"].pushes()) == 1
    assert timelines["b"].pushes() == []
    assert len(timelines["b"].upward_estimate_changes()) == 1


def test_no_changes_means_no_timelines():
    assert build_timelines([]) == {}
