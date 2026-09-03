"""Interval arithmetic: the three ways a capacity number goes wrong.

Two overlapping meetings costing two hours of one hour's time; a meeting that
starts before work counting in full; a zero-length event becoming a minute.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from task_gcal.intervals import (
    clip,
    clip_to_windows,
    duration_minutes,
    humanize_duration,
    humanize_minutes,
    merge,
    overlap_minutes,
    subtract,
    total_minutes,
)

from conftest import at


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def test_merge_collapses_an_overlap():
    got = merge([(at(0, 9), at(0, 11)), (at(0, 10), at(0, 12))])
    assert got == [(at(0, 9), at(0, 12))]


def test_merge_joins_intervals_that_touch():
    got = merge([(at(0, 9), at(0, 10)), (at(0, 10), at(0, 11))])
    assert got == [(at(0, 9), at(0, 11))]


def test_merge_keeps_a_real_gap():
    intervals = [(at(0, 9), at(0, 10)), (at(0, 11), at(0, 12))]
    assert merge(intervals) == intervals


def test_merge_sorts_its_input():
    got = merge([(at(0, 14), at(0, 15)), (at(0, 9), at(0, 10))])
    assert got == [(at(0, 9), at(0, 10)), (at(0, 14), at(0, 15))]


def test_merge_swallows_a_contained_interval():
    got = merge([(at(0, 9), at(0, 17)), (at(0, 11), at(0, 12))])
    assert got == [(at(0, 9), at(0, 17))]


def test_merge_drops_zero_length_intervals():
    # A zero-minute calendar event isn't busy time.
    assert merge([(at(0, 9), at(0, 9))]) == []


def test_double_booking_costs_one_hour_not_two():
    got = total_minutes(merge([(at(0, 9), at(0, 10)), (at(0, 9), at(0, 10))]))
    assert got == 60


# ---------------------------------------------------------------------------
# clip
# ---------------------------------------------------------------------------

def test_clip_keeps_only_the_part_inside_the_window():
    got = clip([(at(0, 8), at(0, 10))], (at(0, 9), at(0, 18)))
    assert got == [(at(0, 9), at(0, 10))]


def test_clip_drops_an_interval_entirely_outside():
    assert clip([(at(0, 6), at(0, 7))], (at(0, 9), at(0, 18))) == []


def test_clip_drops_one_that_only_touches_the_edge():
    assert clip([(at(0, 8), at(0, 9))], (at(0, 9), at(0, 18))) == []


def test_clip_to_windows_spans_several_days():
    windows = [(at(0, 9), at(0, 18)), (at(1, 9), at(1, 18))]
    # An overnight event contributes to both working days, once each.
    got = clip_to_windows([(at(0, 17), at(1, 10))], windows)
    assert total_minutes(got) == 60 + 60


def test_clip_to_windows_merges_the_result():
    windows = [(at(0, 9), at(0, 18))]
    got = clip_to_windows(
        [(at(0, 10), at(0, 12)), (at(0, 11), at(0, 13))], windows
    )
    assert got == [(at(0, 10), at(0, 13))]


# ---------------------------------------------------------------------------
# subtract
# ---------------------------------------------------------------------------

def test_subtract_punches_a_hole():
    got = subtract((at(0, 9), at(0, 18)), [(at(0, 12), at(0, 13))])
    assert got == [(at(0, 9), at(0, 12)), (at(0, 13), at(0, 18))]


def test_subtract_can_remove_everything():
    assert subtract((at(0, 9), at(0, 10)), [(at(0, 8), at(0, 11))]) == []


def test_subtract_trims_an_edge():
    got = subtract((at(0, 9), at(0, 18)), [(at(0, 8), at(0, 10))])
    assert got == [(at(0, 10), at(0, 18))]


def test_subtract_handles_overlapping_cuts():
    got = subtract(
        (at(0, 9), at(0, 18)), [(at(0, 10), at(0, 12)), (at(0, 11), at(0, 13))]
    )
    assert got == [(at(0, 9), at(0, 10)), (at(0, 13), at(0, 18))]


# ---------------------------------------------------------------------------
# totals and overlap
# ---------------------------------------------------------------------------

def test_total_minutes_ignores_seconds_beyond_the_minute():
    from datetime import timedelta as td

    assert total_minutes([(at(0, 9), at(0, 9) + td(seconds=90))]) == 1


def test_overlap_minutes_is_zero_when_they_only_touch():
    assert overlap_minutes((at(0, 9), at(0, 10)), (at(0, 10), at(0, 11))) == 0


def test_overlap_minutes_measures_the_intersection():
    assert overlap_minutes((at(0, 9), at(0, 11)), (at(0, 10), at(0, 12))) == 60


def test_duration_minutes():
    assert duration_minutes((at(0, 9), at(0, 10, 30))) == 90


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "minutes, expected",
    [
        (0, "0m"),
        (45, "45m"),
        (60, "1h"),
        (90, "1h30"),
        (125, "2h05"),
        (2700, "45h"),
        (None, "—"),
    ],
)
def test_humanize_minutes(minutes, expected):
    assert humanize_minutes(minutes) == expected


@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(days=6), "6d"),
        (timedelta(days=1, hours=12), "1d"),
        (timedelta(hours=4), "4h"),
        (timedelta(minutes=30), "30m"),
        (None, "—"),
    ],
)
def test_humanize_duration(delta, expected):
    assert humanize_duration(delta) == expected
