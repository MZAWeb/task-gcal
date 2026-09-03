"""Slot-finding logic.

Given busy intervals and a working-hours window, find the earliest
contiguous free slot of a requested length, with start times aligned to
a fixed minute boundary, that ends no later than a caller-supplied
deadline.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, tzinfo
from typing import Optional

from .config import Settings


def _ceil_to_alignment(dt: datetime, minutes: int) -> datetime:
    """Round `dt` up to the next `minutes`-aligned wall-clock boundary."""
    base = dt.replace(second=0, microsecond=0)
    rem = base.minute % minutes
    if rem == 0 and dt.second == 0 and dt.microsecond == 0:
        return base
    if rem == 0:
        # Sub-minute remainder pushed us past the boundary.
        return base + timedelta(minutes=minutes)
    return base + timedelta(minutes=(minutes - rem))


def _work_windows(
    start: datetime, end: datetime, tz: tzinfo, settings: Settings
) -> list[tuple[datetime, datetime]]:
    """Working-hours windows in [start, end), expressed in `tz`."""
    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)

    windows: list[tuple[datetime, datetime]] = []
    day = start_local.date()
    last_day = end_local.date()
    while day <= last_day:
        if day.weekday() in settings.work_days:
            # Offsets from midnight rather than `time(hour)`: `work_end_hour`
            # is exclusive and may be 24 (midnight ending the day), which
            # `time()` can't represent.
            midnight = datetime.combine(day, time(0, 0), tzinfo=tz)
            day_start = midnight + timedelta(hours=settings.work_start_hour)
            day_end = midnight + timedelta(hours=settings.work_end_hour)
            ws = max(day_start, start_local)
            we = min(day_end, end_local)
            if ws < we:
                windows.append((ws, we))
        day = day + timedelta(days=1)
    return windows


def _subtract_busy(
    window: tuple[datetime, datetime],
    busy: list[tuple[datetime, datetime]],
    buffer: timedelta = timedelta(0),
) -> list[tuple[datetime, datetime]]:
    """Return free sub-intervals of `window` after removing `busy`.

    `busy` must be sorted by start. Each busy interval is widened by
    `buffer` on both sides so a returned free interval keeps that much
    breathing room from any event. The widening never extends past the
    window edges (`buffer` is for gaps *between* events, not for padding
    the start/end of the working day). We exit early once we pass the
    window's end.
    """
    ws, we = window
    free: list[tuple[datetime, datetime]] = [(ws, we)]
    for bs, be in busy:
        if bs - buffer >= we:
            break  # sorted: nothing after this can overlap
        if be + buffer <= ws:
            continue
        new_free: list[tuple[datetime, datetime]] = []
        for fs, fe in free:
            bs_l = (bs - buffer).astimezone(fs.tzinfo)
            be_l = (be + buffer).astimezone(fs.tzinfo)
            if be_l <= fs or bs_l >= fe:
                new_free.append((fs, fe))
                continue
            if bs_l > fs:
                new_free.append((fs, bs_l))
            if be_l < fe:
                new_free.append((be_l, fe))
        free = new_free
    return free


def find_earliest_slot(
    *,
    duration_minutes: int,
    earliest_start: datetime,
    deadline: datetime,
    busy: list[tuple[datetime, datetime]],
    tz: tzinfo,
    settings: Settings,
) -> Optional[tuple[datetime, datetime]]:
    """Find the earliest aligned slot of `duration_minutes` that fits.

    - Slot must be fully inside a working-hours window.
    - Slot must not overlap any interval in `busy` (must be sorted), and
      must keep `settings.buffer_minutes` of free time on either side of
      every busy interval.
    - Slot start >= `earliest_start`, slot end <= `deadline`.
    - Start aligned to `settings.slot_align_minutes`.
    """
    duration = timedelta(minutes=duration_minutes)
    if earliest_start >= deadline:
        return None

    buffer = timedelta(minutes=settings.buffer_minutes)
    windows = _work_windows(earliest_start, deadline, tz, settings)
    for win in windows:
        for fs, fe in _subtract_busy(win, busy, buffer):
            slot_start = _ceil_to_alignment(fs, settings.slot_align_minutes)
            slot_end = slot_start + duration
            if slot_end <= fe and slot_end.astimezone(deadline.tzinfo) <= deadline:
                return slot_start, slot_end
    return None
