"""Is an existing placement still good enough to leave alone?

The scheduler used to re-derive every placement from scratch and take the
earliest slot that fits, so a block sitting at Thursday 14:00 moved to
Tuesday 09:00 the moment a meeting was cancelled. Three costs: you stop
trusting a block that might move, follow-through becomes unmeasurable
(you can't miss a plan you never had), and every run patches events that
didn't need patching — API calls and notification noise.

So a near-term placement is *sticky*: kept unless it is invalid. This
module answers only "invalid, and why?" — pure, no calendar, no clock
beyond what it is handed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

from .config import Settings
from .scheduler import within_work_window


def overlaps_busy(
    start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]
) -> bool:
    """True if `[start, end)` intersects any interval in sorted `busy`.

    Deliberately ignores `buffer_minutes`. The buffer is breathing room we
    ask for when *choosing* a slot; a meeting that lands next to an
    existing block hasn't taken its time, and moving a commitment for
    politeness is the churn stickiness exists to prevent.
    """
    for bs, be in busy:
        if bs >= end:
            return False  # sorted: nothing later can overlap
        if be > start:
            return True
    return False


def invalid_reason(
    *,
    start: datetime,
    end: datetime,
    duration_minutes: int,
    earliest_start: Optional[datetime],
    deadline: datetime,
    busy: list[tuple[datetime, datetime]],
    tz: tzinfo,
    settings: Settings,
) -> Optional[str]:
    """Why this existing placement can no longer stand, or None if it can.

    The reason is user-facing: the run report prints it next to the block
    it had to move, so "why did this shift?" never needs guessing.
    """
    # In real time, not wall-clock: subtracting two datetimes that share a
    # zone subtracts their *clock faces*, so a block spanning a transition
    # would disagree with its own estimate and be moved on every run for ever.
    if end.astimezone(timezone.utc) - start.astimezone(timezone.utc) != timedelta(
        minutes=duration_minutes
    ):
        return "estimate changed"
    if earliest_start is not None and start < earliest_start:
        return "starts before its scheduled/wait date"
    if end > deadline:
        return "ends after its due date"
    if not within_work_window(start, end, tz, settings):
        return "outside working hours"
    if overlaps_busy(start, end, busy):
        return "overlaps a calendar event"
    return None


def is_settled(start: datetime, now: datetime, settle_days: int) -> bool:
    """True if a placement is near enough that moving it breaks a promise.

    Tomorrow shouldn't move. Next Thursday can be re-optimized freely,
    because you haven't planned your Thursday yet. `settle_days = 0`
    restores the old always-take-the-earliest-slot behaviour.
    """
    if settle_days <= 0:
        return False
    return start < now + timedelta(days=settle_days)
