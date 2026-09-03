"""Interval arithmetic on `(start, end)` pairs of aware datetimes.

Capacity, meeting load and boundary erosion are all the same three
operations — merge, clip, total — and each of them has a way of being
subtly wrong. Two overlapping meetings must not count twice; a meeting that
starts before the working day must only count for the part inside it; a
zero-length interval must not become a one-minute one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

Interval = tuple[datetime, datetime]


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Collapse overlapping and touching intervals into disjoint ones.

    Anything that sums durations has to merge first, or two people
    double-booking you costs two hours of capacity for one hour of your
    time.
    """
    ordered = sorted((s, e) for s, e in intervals if e > s)
    merged: list[Interval] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            last_start, last_end = merged[-1]
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def clip(intervals: Iterable[Interval], window: Interval) -> list[Interval]:
    """The parts of `intervals` that fall inside `window`."""
    ws, we = window
    out: list[Interval] = []
    for start, end in intervals:
        lo, hi = max(start, ws), min(end, we)
        if hi > lo:
            out.append((lo, hi))
    return out


def clip_to_windows(
    intervals: Iterable[Interval], windows: Iterable[Interval]
) -> list[Interval]:
    """The parts of `intervals` inside any of `windows`.

    `windows` are assumed disjoint — they're working-hours windows, one per
    day — so a single interval can contribute to more than one of them
    without overlapping itself.
    """
    materialized = list(intervals)
    out: list[Interval] = []
    for window in windows:
        out.extend(clip(materialized, window))
    return merge(out)


def total_minutes(intervals: Iterable[Interval]) -> int:
    """Whole minutes covered. Merge first if the input can overlap."""
    seconds = sum((e - s).total_seconds() for s, e in intervals if e > s)
    return int(seconds // 60)


def subtract(base: Interval, cuts: Iterable[Interval]) -> list[Interval]:
    """What's left of `base` after removing `cuts`."""
    remaining = [base]
    for cut_start, cut_end in merge(cuts):
        next_remaining: list[Interval] = []
        for start, end in remaining:
            if cut_end <= start or cut_start >= end:
                next_remaining.append((start, end))
                continue
            if cut_start > start:
                next_remaining.append((start, cut_start))
            if cut_end < end:
                next_remaining.append((cut_end, end))
        remaining = next_remaining
    return remaining


def overlap_minutes(a: Interval, b: Interval) -> int:
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return int((hi - lo).total_seconds() // 60) if hi > lo else 0


def duration_minutes(interval: Interval) -> int:
    return int((interval[1] - interval[0]).total_seconds() // 60)


def humanize_minutes(minutes: Optional[int]) -> str:
    """`90` -> `1h30`, `45` -> `45m`, `120` -> `2h`, None -> `—`.

    Hours because a review is read at a glance and "2700 minutes" isn't a
    quantity anyone feels.
    """
    if minutes is None:
        return "—"
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h" if rest == 0 else f"{hours}h{rest:02d}"


def humanize_duration(delta: Optional[timedelta]) -> str:
    """A span in the coarsest unit that still says something: `6d`, `4h`."""
    if delta is None:
        return "—"
    days = delta.days
    if days >= 1:
        return f"{days}d"
    return humanize_minutes(int(delta.total_seconds() // 60))
