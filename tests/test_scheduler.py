"""Tests for `find_earliest_slot`, the one genuinely pure piece of the tool.

No fakes needed here, so this is where the awkward cases belong: alignment,
buffers at window edges, and days that aren't 24 hours long.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from task_gcal.config import Settings
from task_gcal.scheduler import find_earliest_slot

UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")

# Monday .. Friday 2026-09-07 .. 2026-09-11.
MON = datetime(2026, 9, 7, tzinfo=UTC)


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    return MON + timedelta(days=day_offset, hours=hour, minutes=minute)


def find(
    *,
    minutes: int,
    earliest: datetime,
    deadline: datetime,
    busy=(),
    tz=UTC,
    **settings_kwargs,
):
    settings = Settings(timezone="UTC", **settings_kwargs)
    return find_earliest_slot(
        duration_minutes=minutes,
        earliest_start=earliest,
        deadline=deadline,
        busy=sorted(busy),
        tz=tz,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------

def test_takes_the_start_of_the_working_day():
    slot = find(minutes=60, earliest=at(0, 0), deadline=at(0, 23))
    assert slot == (at(0, 9), at(0, 10))


def test_does_not_start_before_earliest_start():
    slot = find(minutes=30, earliest=at(0, 14), deadline=at(0, 23))
    assert slot == (at(0, 14), at(0, 14, 30))


def test_start_is_rounded_up_to_the_alignment():
    slot = find(minutes=30, earliest=at(0, 9, 7), deadline=at(0, 23))
    assert slot == (at(0, 9, 15), at(0, 9, 45))


def test_alignment_of_one_minute_allows_any_start():
    slot = find(
        minutes=30, earliest=at(0, 9, 7), deadline=at(0, 23), slot_align_minutes=1
    )
    assert slot == (at(0, 9, 7), at(0, 9, 37))


def test_sub_minute_earliest_start_still_advances():
    earliest = at(0, 9) + timedelta(seconds=30)
    slot = find(minutes=30, earliest=earliest, deadline=at(0, 23))
    assert slot == (at(0, 9, 15), at(0, 9, 45))


# ---------------------------------------------------------------------------
# Busy time
# ---------------------------------------------------------------------------

def test_skips_a_busy_interval():
    slot = find(
        minutes=60,
        earliest=at(0, 9),
        deadline=at(0, 23),
        busy=[(at(0, 9), at(0, 10, 30))],
    )
    assert slot == (at(0, 10, 30), at(0, 11, 30))


def test_fits_into_a_gap_between_two_meetings():
    slot = find(
        minutes=30,
        earliest=at(0, 9),
        deadline=at(0, 23),
        busy=[(at(0, 9), at(0, 10)), (at(0, 10, 30), at(0, 17))],
    )
    assert slot == (at(0, 10), at(0, 10, 30))


def test_gap_too_small_falls_through_to_the_next_one():
    slot = find(
        minutes=60,
        earliest=at(0, 9),
        deadline=at(0, 23),
        busy=[(at(0, 9), at(0, 10)), (at(0, 10, 30), at(0, 12))],
    )
    assert slot == (at(0, 12), at(0, 13))


def test_overlapping_busy_intervals_are_both_avoided():
    slot = find(
        minutes=60,
        earliest=at(0, 9),
        deadline=at(0, 23),
        busy=[(at(0, 9), at(0, 11)), (at(0, 10), at(0, 12))],
    )
    assert slot == (at(0, 12), at(0, 13))


def test_busy_time_wholly_before_the_window_is_ignored():
    slot = find(
        minutes=60,
        earliest=at(0, 9),
        deadline=at(0, 23),
        busy=[(at(0, 2), at(0, 4))],
    )
    assert slot == (at(0, 9), at(0, 10))


def test_busy_time_after_the_window_is_ignored():
    slot = find(
        minutes=60,
        earliest=at(0, 9),
        deadline=at(0, 18),
        busy=[(at(0, 20), at(0, 22))],
    )
    assert slot == (at(0, 9), at(0, 10))


# ---------------------------------------------------------------------------
# Buffers
# ---------------------------------------------------------------------------

def test_buffer_keeps_a_gap_after_a_meeting():
    slot = find(
        minutes=60,
        earliest=at(0, 9),
        deadline=at(0, 23),
        busy=[(at(0, 9), at(0, 10))],
        buffer_minutes=15,
    )
    assert slot == (at(0, 10, 15), at(0, 11, 15))


def test_buffer_keeps_a_gap_before_a_meeting():
    # 09:00-10:00 free, meeting at 10:00: a 45m task would touch the meeting,
    # so with a 15m buffer it must end by 09:45 -- which it can.
    slot = find(
        minutes=45,
        earliest=at(0, 9),
        deadline=at(0, 23),
        busy=[(at(0, 10), at(0, 12))],
        buffer_minutes=15,
    )
    assert slot == (at(0, 9), at(0, 9, 45))


def test_buffer_pushes_past_a_gap_that_no_longer_fits():
    slot = find(
        minutes=60,
        earliest=at(0, 9),
        deadline=at(0, 23),
        busy=[(at(0, 10), at(0, 11))],
        buffer_minutes=15,
    )
    assert slot == (at(0, 11, 15), at(0, 12, 15))


def test_buffer_does_not_pad_the_start_of_the_working_day():
    # The buffer is for gaps *between* events; the window edge is not an event.
    slot = find(
        minutes=60, earliest=at(0, 0), deadline=at(0, 23), buffer_minutes=30
    )
    assert slot == (at(0, 9), at(0, 10))


def test_buffer_does_not_pad_the_end_of_the_working_day():
    slot = find(
        minutes=60,
        earliest=at(0, 17),
        deadline=at(0, 23),
        buffer_minutes=30,
    )
    assert slot == (at(0, 17), at(0, 18))


# ---------------------------------------------------------------------------
# Windows and deadlines
# ---------------------------------------------------------------------------

def test_rolls_over_to_the_next_day_when_today_is_full():
    slot = find(
        minutes=60,
        earliest=at(0, 9),
        deadline=at(1, 23),
        busy=[(at(0, 9), at(0, 18))],
    )
    assert slot == (at(1, 9), at(1, 10))


def test_slot_must_end_by_the_deadline():
    assert find(minutes=60, earliest=at(0, 9), deadline=at(0, 9, 30)) is None


def test_slot_ending_exactly_on_the_deadline_is_allowed():
    slot = find(minutes=60, earliest=at(0, 9), deadline=at(0, 10))
    assert slot == (at(0, 9), at(0, 10))


def test_slot_must_fit_inside_the_working_window():
    # 17:30 start, 60m task, day ends 18:00 -> tomorrow.
    slot = find(minutes=60, earliest=at(0, 17, 30), deadline=at(1, 23))
    assert slot == (at(1, 9), at(1, 10))


def test_non_working_days_are_skipped():
    # Saturday is day 5 from Monday; Mon-Fri only, so Sat/Sun are skipped.
    slot = find(minutes=60, earliest=at(5, 0), deadline=at(7, 23))
    assert slot == (at(7, 9), at(7, 10))


def test_custom_work_days_are_honored():
    slot = find(
        minutes=60,
        earliest=at(5, 0),
        deadline=at(7, 23),
        work_days=frozenset({5}),  # Saturday
    )
    assert slot == (at(5, 9), at(5, 10))


def test_earliest_start_at_or_after_the_deadline_finds_nothing():
    assert find(minutes=30, earliest=at(0, 12), deadline=at(0, 12)) is None
    assert find(minutes=30, earliest=at(0, 13), deadline=at(0, 12)) is None


def test_task_longer_than_a_working_day_never_fits():
    assert find(minutes=10 * 60, earliest=at(0, 0), deadline=at(4, 23)) is None


def test_task_exactly_as_long_as_the_working_day_fits():
    slot = find(minutes=9 * 60, earliest=at(0, 0), deadline=at(0, 23))
    assert slot == (at(0, 9), at(0, 18))


# ---------------------------------------------------------------------------
# Days that aren't 24 hours long
# ---------------------------------------------------------------------------

_OVERNIGHT = dict(work_start_hour=0, work_end_hour=6, work_days=frozenset(range(7)))

# Europe/London transitions: 2026-03-29 loses an hour at 01:00, 2026-10-25
# gains one at 02:00. A 00:00-06:00 window is therefore 5 real hours in March
# and 7 in October.
SPRING_EARLIEST = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
SPRING_DEADLINE = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
FALL_EARLIEST = datetime(2026, 10, 24, 21, 0, tzinfo=UTC)
FALL_DEADLINE = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def elapsed(slot) -> timedelta:
    """Real time between the two ends of a slot, not wall-clock difference."""
    return slot[1].astimezone(UTC) - slot[0].astimezone(UTC)


def test_a_spring_forward_window_holds_one_hour_less_than_it_looks():
    """An estimate is a claim about real time, not about the wall clock.

    A 00:00-06:00 window on the night the clocks go forward is five hours long,
    however it reads. So a 6h task doesn't fit, and a 5h one does — the numbers
    a person would give if you asked them.
    """
    assert (
        find(
            minutes=6 * 60,
            earliest=SPRING_EARLIEST,
            deadline=SPRING_DEADLINE,
            tz=LONDON,
            **_OVERNIGHT,
        )
        is None
    )

    fits = find(
        minutes=5 * 60,
        earliest=SPRING_EARLIEST,
        deadline=SPRING_DEADLINE,
        tz=LONDON,
        **_OVERNIGHT,
    )
    assert elapsed(fits) == timedelta(hours=5)
    assert fits[0].astimezone(LONDON).hour == 0
    assert fits[1].astimezone(LONDON).hour == 6


def test_a_fall_back_slot_stops_after_the_hours_it_asked_for():
    # The window is seven real hours on this night, so six fits with room to
    # spare — and takes six, not the seven the wall clock would give it.
    slot = find(
        minutes=6 * 60,
        earliest=FALL_EARLIEST,
        deadline=FALL_DEADLINE,
        tz=LONDON,
        **_OVERNIGHT,
    )
    assert elapsed(slot) == timedelta(hours=6)
    # Six real hours read as five on the clock face, because 01:00-02:00
    # happens twice inside them. The block is right; the clock is the odd one.
    assert slot[0].astimezone(LONDON).hour == 0
    assert slot[1].astimezone(LONDON).hour == 5


def test_a_block_that_spans_a_transition_is_not_seen_as_a_changed_estimate():
    # `invalid_reason` measures a block in real time. While the scheduler
    # measured it in wall-clock, a settled block over a transition disagreed
    # with its own estimate and was moved on every run, for ever.
    from task_gcal.stability import invalid_reason

    slot = find(
        minutes=5 * 60,
        earliest=SPRING_EARLIEST,
        deadline=SPRING_DEADLINE,
        tz=LONDON,
        **_OVERNIGHT,
    )
    assert (
        invalid_reason(
            start=slot[0],
            end=slot[1],
            duration_minutes=5 * 60,
            earliest_start=SPRING_EARLIEST,
            deadline=SPRING_DEADLINE,
            busy=[],
            tz=LONDON,
            settings=Settings(timezone="Europe/London", **_OVERNIGHT),
        )
        is None
    )


def test_a_slot_fully_inside_one_offset_has_the_exact_duration():
    # The same window a day later, no transition in it: 6h means 6h.
    slot = find(
        minutes=6 * 60,
        earliest=datetime(2026, 3, 29, 23, 0, tzinfo=UTC),
        deadline=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
        tz=LONDON,
        **_OVERNIGHT,
    )
    assert elapsed(slot) == timedelta(hours=6)


def test_windows_follow_local_wall_clock_across_a_transition():
    # A 09:00 London start is 08:00 UTC in summer and 09:00 UTC in winter.
    summer = find(
        minutes=60,
        earliest=datetime(2026, 10, 23, 0, 0, tzinfo=UTC),  # Fri, BST
        deadline=datetime(2026, 10, 23, 23, 0, tzinfo=UTC),
        tz=LONDON,
    )
    winter = find(
        minutes=60,
        earliest=datetime(2026, 10, 26, 0, 0, tzinfo=UTC),  # Mon, GMT
        deadline=datetime(2026, 10, 26, 23, 0, tzinfo=UTC),
        tz=LONDON,
    )
    assert summer[0].astimezone(UTC).hour == 8
    assert winter[0].astimezone(UTC).hour == 9
    assert summer[0].astimezone(LONDON).hour == winter[0].astimezone(LONDON).hour == 9


def test_deadline_in_another_timezone_is_still_respected():
    # The deadline arrives as UTC while we schedule in London; the comparison
    # must be absolute, not wall-clock.
    slot = find(
        minutes=60,
        earliest=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        deadline=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),  # 10:00 London
        tz=LONDON,
    )
    assert slot is not None
    assert slot[1].astimezone(UTC) <= datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# work_end_hour = 24
# ---------------------------------------------------------------------------

def test_work_end_hour_24_means_midnight():
    slot = find(minutes=60, earliest=at(0, 22), deadline=at(1, 12), work_end_hour=24)
    assert slot == (at(0, 22), at(0, 23))


def test_a_slot_can_run_up_to_midnight():
    slot = find(minutes=60, earliest=at(0, 23), deadline=at(1, 12), work_end_hour=24)
    assert slot == (at(0, 23), at(1, 0))


def test_work_end_hour_24_does_not_bleed_into_the_next_day():
    # The window ends *at* midnight, so a 90m task starting 23:00 doesn't run
    # to 00:30 -- it waits for the next working window (09:00, not 00:00,
    # because windows are built per calendar day).
    slot = find(minutes=90, earliest=at(0, 23), deadline=at(2, 12), work_end_hour=24)
    assert slot == (at(1, 9), at(1, 10, 30))


def test_a_full_day_window_is_twenty_four_hours():
    slot = find(
        minutes=24 * 60,
        earliest=at(0, 0),
        deadline=at(1, 12),
        work_start_hour=0,
        work_end_hour=24,
    )
    assert slot == (at(0, 0), at(1, 0))
