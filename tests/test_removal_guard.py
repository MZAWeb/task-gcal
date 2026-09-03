"""Tests for the bulk-removal guard.

`removal_guard_error` is pure, so it needs no calendar or Taskwarrior — the
point of keeping it a free function.
"""

from __future__ import annotations

from task_gcal.cli import removal_guard_error


def test_small_cleanups_are_never_blocked():
    # Two of two events is 100%, but below the floor: a fresh calendar
    # shouldn't need --force to tidy up.
    assert removal_guard_error(removals=2, owned_unfinished=2, ratio=0.5) is None


def test_minority_cleanup_allowed():
    assert removal_guard_error(removals=4, owned_unfinished=20, ratio=0.5) is None


def test_exactly_at_the_ratio_allowed():
    assert removal_guard_error(removals=10, owned_unfinished=20, ratio=0.5) is None


def test_majority_cleanup_blocked():
    msg = removal_guard_error(removals=11, owned_unfinished=20, ratio=0.5)
    assert msg is not None
    assert "11 of 20" in msg
    assert "--force" in msg


def test_wiping_everything_is_blocked():
    # The failure mode this exists for: an empty task source or a mistyped
    # estimate UDA makes every task look unschedulable.
    assert removal_guard_error(removals=16, owned_unfinished=16, ratio=0.5)


def test_ratio_of_one_disables_the_guard():
    assert removal_guard_error(removals=16, owned_unfinished=16, ratio=1.0) is None


def test_nothing_owned_is_not_an_error():
    # Can't happen (removals only come from owned events), but must not
    # divide by zero if it ever does.
    assert removal_guard_error(removals=0, owned_unfinished=0, ratio=0.5) is None
