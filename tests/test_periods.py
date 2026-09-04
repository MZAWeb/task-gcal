"""What "this week" means, pinned.

A week is an ISO week, Monday start, in the local timezone. If two metrics
ever disagree about which day a Sunday-evening block belongs to, it will be
because something here changed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from task_gcal.config import Settings
from task_gcal.intervals import total_minutes
from task_gcal.review.periods import (
    KIND_MONTH,
    KIND_WEEK,
    month_of,
    resolve,
    week_of,
    work_windows,
)

UTC = timezone.utc
MADRID = ZoneInfo("Europe/Madrid")
TOKYO = ZoneInfo("Asia/Tokyo")


# ---------------------------------------------------------------------------
# Weeks
# ---------------------------------------------------------------------------

def test_a_week_starts_on_monday():
    # 2026-09-10 is a Thursday.
    period = week_of(date(2026, 9, 10), UTC)
    assert period.start == datetime(2026, 9, 7, tzinfo=UTC)
    assert period.end == datetime(2026, 9, 14, tzinfo=UTC)


def test_a_sunday_belongs_to_the_week_that_began_that_monday():
    sunday = week_of(date(2026, 9, 13), UTC)
    thursday = week_of(date(2026, 9, 10), UTC)
    assert sunday.start == thursday.start


def test_the_label_is_the_iso_week_number():
    assert week_of(date(2026, 9, 10), UTC).label == "Week 37"


def test_a_week_spanning_new_year_names_its_iso_year():
    # 2027-01-01 is a Friday, so its ISO week began 2026-12-28.
    period = week_of(date(2027, 1, 1), UTC)
    assert period.start == datetime(2026, 12, 28, tzinfo=UTC)
    assert "2026" not in period.label  # the Monday's own year — no need to say


def test_a_week_whose_monday_is_in_the_previous_year_says_so():
    # 2026-01-01 is a Thursday; its ISO week 1 began Mon 2025-12-29.
    period = week_of(date(2025, 12, 30), UTC)
    assert "(2026)" in period.label


def test_the_local_zone_decides_the_boundary():
    # Monday 00:00 in Madrid is Sunday 22:00 UTC in September.
    period = week_of(date(2026, 9, 10), MADRID)
    assert period.start == datetime(2026, 9, 6, 22, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Months
# ---------------------------------------------------------------------------

def test_a_month_runs_from_the_first_to_the_first():
    period = month_of(date(2026, 9, 20), UTC)
    assert period.start == datetime(2026, 9, 1, tzinfo=UTC)
    assert period.end == datetime(2026, 10, 1, tzinfo=UTC)
    assert period.label == "September 2026"


def test_december_rolls_into_january():
    period = month_of(date(2026, 12, 5), UTC)
    assert period.end == datetime(2027, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Clamping to now
# ---------------------------------------------------------------------------

def test_the_current_period_stops_at_now():
    # Otherwise a Tuesday review reports three days of unused capacity as a
    # shortfall.
    now = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)  # Tuesday
    period = week_of(now.date(), UTC, now=now)
    assert period.end == now
    assert period.in_progress is True
    assert period.nominal_end == datetime(2026, 9, 14, tzinfo=UTC)


def test_a_finished_period_is_not_clamped():
    now = datetime(2026, 9, 20, tzinfo=UTC)
    period = week_of(date(2026, 9, 7), UTC, now=now)
    assert period.end == datetime(2026, 9, 14, tzinfo=UTC)
    assert period.in_progress is False


def test_a_week_review_means_the_week_you_are_in():
    # You read a weekly review on Friday, about the week you're still in.
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    assert resolve(KIND_WEEK, UTC, now=now).start == datetime(2026, 9, 7, tzinfo=UTC)


def test_a_month_review_means_the_month_that_finished():
    # You read a monthly review once the month is over. Nobody sits down on
    # the 10th to reflect on ten days.
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    period = resolve(KIND_MONTH, UTC, now=now)
    assert period.start == datetime(2026, 8, 1, tzinfo=UTC)
    assert period.in_progress is False


def test_a_period_can_be_named_by_any_day_inside_it():
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    named = resolve(KIND_MONTH, UTC, now=now, anchor=date(2026, 9, 1))
    assert named.label == "September 2026"
    # And naming the month you're in still clamps to now rather than pretending
    # the rest of it has happened.
    assert named.in_progress is True


def test_an_offset_walks_back_whole_periods():
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    assert resolve(KIND_WEEK, UTC, now=now, offset=1).start == (
        datetime(2026, 8, 31, tzinfo=UTC)
    )
    # Months count back from the month that finished, so two further back
    # from August is June.
    assert resolve(KIND_MONTH, UTC, now=now, offset=2).start == (
        datetime(2026, 6, 1, tzinfo=UTC)
    )


def test_a_past_period_resolved_by_offset_is_complete():
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    previous = resolve(KIND_WEEK, UTC, now=now, offset=1)
    assert previous.in_progress is False
    assert previous.end == datetime(2026, 9, 7, tzinfo=UTC)


def test_shifted_months_do_not_drift_into_short_months():
    # Naive day arithmetic on 31 January lands in March.
    january = month_of(date(2026, 1, 31), UTC)
    assert january.shifted(1).label == "February 2026"


# ---------------------------------------------------------------------------
# Days and membership
# ---------------------------------------------------------------------------

def test_days_lists_every_local_date_touched():
    period = week_of(date(2026, 9, 10), UTC)
    assert period.days() == [date(2026, 9, 7 + i) for i in range(7)]


def test_days_does_not_include_the_exclusive_end():
    period = week_of(date(2026, 9, 10), UTC)
    assert date(2026, 9, 14) not in period.days()


def test_membership_is_half_open():
    period = week_of(date(2026, 9, 10), UTC)
    assert period.contains(period.start) is True
    assert period.contains(period.end) is False
    assert period.contains(None) is False


# ---------------------------------------------------------------------------
# Working-hours windows
# ---------------------------------------------------------------------------

def test_work_windows_skip_non_working_days():
    period = week_of(date(2026, 9, 10), UTC)
    windows = list(work_windows(period, Settings(timezone="UTC")))
    assert len(windows) == 5  # Mon-Fri
    assert total_minutes(windows) == 5 * 9 * 60


def test_work_windows_are_clipped_to_the_period():
    # Reviewed on Tuesday at 15:00: Monday in full, Tuesday to 15:00.
    now = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
    period = week_of(now.date(), UTC, now=now)
    windows = list(work_windows(period, Settings(timezone="UTC")))
    assert total_minutes(windows) == (9 * 60) + (6 * 60)


def test_work_windows_follow_the_configured_hours():
    period = week_of(date(2026, 9, 10), UTC)
    settings = Settings(timezone="UTC", work_start_hour=8, work_end_hour=20)
    assert total_minutes(work_windows(period, settings)) == 5 * 12 * 60


def test_work_windows_follow_the_configured_days():
    period = week_of(date(2026, 9, 10), UTC)
    settings = Settings(timezone="UTC", work_days=frozenset({0, 2, 4}))
    assert len(list(work_windows(period, settings))) == 3


@pytest.mark.parametrize("tz", [UTC, MADRID, TOKYO])
def test_a_full_week_is_the_same_number_of_working_minutes_anywhere(tz):
    # The window is defined in local wall-clock terms, so the zone shifts
    # when it happens but not how much of it there is.
    period = week_of(date(2026, 9, 10), tz)
    settings = Settings(timezone=str(tz))
    assert total_minutes(work_windows(period, settings)) == 5 * 9 * 60


def test_a_dst_transition_does_not_change_the_working_day():
    # Europe/Madrid falls back on 2026-10-25 (a Sunday), so the following
    # working week must still be 5 x 9 hours of wall clock.
    period = week_of(date(2026, 10, 26), MADRID)
    settings = Settings(timezone="Europe/Madrid")
    assert total_minutes(work_windows(period, settings)) == 5 * 9 * 60


def test_a_week_containing_a_dst_transition_is_still_seven_days_long():
    period = week_of(date(2026, 10, 20), MADRID)
    assert len(period.days()) == 7
    # And one hour longer in absolute terms than a normal week.
    assert period.end - period.start == timedelta(days=7, hours=1)
