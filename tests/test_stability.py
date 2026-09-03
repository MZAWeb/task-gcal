"""Tests for the pure "is this placement still good?" layer.

No calendar, no clock: `invalid_reason` is handed everything it needs, which
is the point of keeping it out of `reconcile`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from task_gcal.config import Settings
from task_gcal.scheduler import within_work_window
from task_gcal.stability import invalid_reason, is_settled, overlaps_busy

from conftest import NOW, at

UTC_SETTINGS = Settings(timezone="UTC")
TZ = UTC_SETTINGS.resolve_timezone()

# Monday 10:00-11:00, comfortably inside a default 09:00-18:00 Monday.
START = at(0, 10)
END = at(0, 11)


def reason(**kwargs):
    """`invalid_reason` for the reference placement, with overrides."""
    args = dict(
        start=START,
        end=END,
        duration_minutes=60,
        earliest_start=None,
        deadline=at(4, 17),
        busy=[],
        tz=TZ,
        settings=UTC_SETTINGS,
    )
    args.update(kwargs)
    return invalid_reason(**args)


# ---------------------------------------------------------------------------
# overlaps_busy
# ---------------------------------------------------------------------------

def test_no_busy_never_overlaps():
    assert overlaps_busy(START, END, []) is False


@pytest.mark.parametrize(
    "busy_start, busy_end",
    [
        (at(0, 10, 30), at(0, 12)),   # starts inside
        (at(0, 9), at(0, 10, 30)),    # ends inside
        (at(0, 9), at(0, 12)),        # swallows it
        (at(0, 10, 15), at(0, 10, 45)),  # inside it
    ],
)
def test_any_real_intersection_overlaps(busy_start, busy_end):
    assert overlaps_busy(START, END, [(busy_start, busy_end)]) is True


@pytest.mark.parametrize(
    "busy_start, busy_end",
    [
        (at(0, 9), at(0, 10)),   # ends exactly where we start
        (at(0, 11), at(0, 12)),  # starts exactly where we end
    ],
)
def test_touching_at_an_edge_is_not_an_overlap(busy_start, busy_end):
    assert overlaps_busy(START, END, [(busy_start, busy_end)]) is False


def test_the_buffer_is_not_applied():
    # A meeting butting up against a block hasn't taken its time. Demanding
    # breathing room here would move a commitment for politeness, which is
    # the churn stickiness exists to prevent.
    tight = Settings(timezone="UTC", buffer_minutes=30)
    assert reason(busy=[(at(0, 11), at(0, 12))], settings=tight) is None


def test_a_later_busy_interval_short_circuits():
    # `busy` is sorted, so scanning stops at the first interval past the end.
    assert overlaps_busy(
        START, END, [(at(0, 12), at(0, 13)), (at(0, 10), at(0, 11))]
    ) is False


# ---------------------------------------------------------------------------
# within_work_window
# ---------------------------------------------------------------------------

def test_a_block_inside_the_working_day_is_within_the_window():
    assert within_work_window(START, END, TZ, UTC_SETTINGS) is True


def test_a_block_starting_before_work_is_not():
    assert within_work_window(at(0, 7), at(0, 8), TZ, UTC_SETTINGS) is False


def test_a_block_spilling_past_work_end_is_not():
    assert within_work_window(at(0, 17), at(0, 19), TZ, UTC_SETTINGS) is False


def test_a_block_on_a_non_working_day_is_not():
    # Saturday: day 5 after Monday.
    assert within_work_window(at(5, 10), at(5, 11), TZ, UTC_SETTINGS) is False


def test_a_block_straddling_two_days_is_not():
    assert within_work_window(at(0, 17), at(1, 10), TZ, UTC_SETTINGS) is False


def test_the_window_edges_are_inclusive():
    assert within_work_window(at(0, 9), at(0, 18), TZ, UTC_SETTINGS) is True


# ---------------------------------------------------------------------------
# invalid_reason
# ---------------------------------------------------------------------------

def test_a_good_placement_has_no_reason_to_move():
    assert reason() is None


def test_a_changed_estimate_invalidates_the_placement():
    assert reason(duration_minutes=90) == "estimate changed"


def test_a_floor_moved_past_the_start_invalidates_it():
    assert reason(earliest_start=at(0, 14)) == (
        "starts before its scheduled/wait date"
    )


def test_a_floor_before_the_start_is_fine():
    assert reason(earliest_start=at(0, 9)) is None


def test_a_deadline_pulled_earlier_invalidates_it():
    assert reason(deadline=at(0, 10, 30)) == "ends after its due date"


def test_ending_exactly_on_the_deadline_is_fine():
    assert reason(deadline=END) is None


def test_a_narrowed_working_day_invalidates_it():
    assert reason(
        settings=Settings(timezone="UTC", work_end_hour=10)
    ) == "outside working hours"


def test_a_new_meeting_on_top_invalidates_it():
    assert reason(busy=[(at(0, 10, 30), at(0, 12))]) == (
        "overlaps a calendar event"
    )


def test_the_estimate_is_checked_before_anything_else():
    # A block whose estimate grew is going to be re-placed regardless, and
    # "estimate changed" is the honest explanation — reporting the overlap
    # its old length happens to have would send you looking at the calendar.
    assert reason(
        duration_minutes=90, busy=[(at(0, 10, 30), at(0, 12))]
    ) == "estimate changed"


# ---------------------------------------------------------------------------
# is_settled
# ---------------------------------------------------------------------------

def test_a_placement_inside_the_settle_window_is_settled():
    assert is_settled(NOW + timedelta(hours=5), NOW, 2) is True


def test_a_placement_beyond_the_settle_window_is_not():
    assert is_settled(NOW + timedelta(days=3), NOW, 2) is False


def test_the_settle_window_boundary_is_exclusive():
    assert is_settled(NOW + timedelta(days=2), NOW, 2) is False


def test_zero_settle_days_settles_nothing():
    # The old always-take-the-earliest-slot behaviour, still available.
    assert is_settled(NOW + timedelta(minutes=1), NOW, 0) is False
