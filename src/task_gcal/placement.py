"""Deciding where each task's calendar block goes.

Separate from `schedule.py` on purpose: everything here answers "where should
this be?" and nothing here talks to Google. The only I/O is a warning on
stderr for a malformed per-task override.
"""

from __future__ import annotations

import bisect
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

from .config import Settings, apply_overrides, parse_task_overrides
from .drift import is_pinned
from .gcal import CalEvent
from .scheduler import find_earliest_slot
from .stability import invalid_reason, is_settled
from .taskw import TaskInfo


@dataclass
class Placement:
    """One task's resulting calendar block, as the report wants to see it."""

    task: TaskInfo
    start: datetime  # local
    end: datetime  # local
    action: str  # create | update | unchanged
    past_due: bool
    # Set when a settled block had to be given up: why it was no longer
    # valid. None for a block that was new, or that never moved.
    moved_reason: Optional[str] = None
    # The Google event this block lives in. The journal keys placements on it
    # rather than on the task, so a block deleted and recreated elsewhere
    # reads as a new block instead of the old one having moved. None on a dry
    # run, which creates nothing.
    event_id: Optional[str] = None
    # Whether this run wrote a fresh expectation stamp on the event. Not the
    # same as "we patched it": a patch that only fixes the description leaves
    # the stamp alone, and a drifted block like that still has to be adopted
    # or we'd report the same hand-move on every run from now on.
    restamped: bool = False


@dataclass
class Decision:
    """Where a task's block will be, decided before the calendar is touched."""

    task: TaskInfo
    start_utc: datetime
    end_utc: datetime
    past_due: bool
    keeper: Optional[CalEvent]
    moved_reason: Optional[str] = None

    @property
    def interval(self) -> tuple[datetime, datetime]:
        return (self.start_utc, self.end_utc)


def effective_due(due: datetime, tz, settings: Settings) -> datetime:
    """Adjust the due date for scheduling.

    A due date with no time-of-day (local midnight, e.g. `due:monday`)
    is treated as the end of that day's working window, so the task can
    be scheduled during that day. Due dates with an explicit time are
    used as-is.
    """
    local = due.astimezone(tz)
    if local.hour == 0 and local.minute == 0 and local.second == 0:
        # Offset from the midnight we're already standing on: `work_end_hour`
        # is exclusive and may be 24, which `replace(hour=...)` can't express.
        end_of_day = local.replace(microsecond=0) + timedelta(
            hours=settings.work_end_hour
        )
        return end_of_day.astimezone(timezone.utc)
    return due


def _keeper_rank(ev: CalEvent, now: datetime) -> tuple[int, float]:
    """Sort key for picking a task's keeper event (lower is better).

    Prefer an event happening *right now* (so an in-progress task is never
    moved or duplicated), then the earliest upcoming event, then the most
    recently finished one (kept only as a record).
    """
    start = ev.start.timestamp()
    if ev.start <= now < ev.end:
        return (0, start)  # in progress
    if ev.start > now:
        return (1, start)  # upcoming: earliest first
    return (2, -start)     # finished: most recent first


def pick_keepers(events: list[CalEvent], now: datetime) -> dict[str, CalEvent]:
    """Each task's one canonical block among the events we own.

    Anything else we own for that task is a duplicate, which reconcile
    removes.
    """
    keepers: dict[str, CalEvent] = {}
    for ev in events:
        if not ev.task_uuid:
            continue
        cur = keepers.get(ev.task_uuid)
        if cur is None or _keeper_rank(ev, now) < _keeper_rank(cur, now):
            keepers[ev.task_uuid] = ev
    return keepers


def resolve_task_settings(
    tasks: list[TaskInfo], settings: Settings
) -> dict[str, Settings]:
    """Effective Settings per task uuid, from the per-task override UDA.

    A bad override warns and falls back to the global settings, so one typo
    can't take a whole run down.
    """
    resolved: dict[str, Settings] = {}
    for t in tasks:
        s = settings
        if t.overrides_raw:
            try:
                s = apply_overrides(settings, parse_task_overrides(t.overrides_raw))
            except ValueError as e:
                print(
                    f"  ! ignoring {settings.override_uda} override on "
                    f"{t.ref}: {e}",
                    file=sys.stderr,
                )
        resolved[t.uuid] = s
    return resolved


def horizon_for(
    tasks: list[TaskInfo],
    task_settings: dict[str, Settings],
    settings: Settings,
    now: datetime,
) -> datetime:
    """How far ahead slot search and event listing need to look."""
    horizon_end = now + timedelta(days=1)
    for t in tasks:
        if t.due and t.due > horizon_end:
            horizon_end = t.due
    # Account for overdue tasks scheduled into the future, honoring the
    # largest overdue window in play (global or any per-task override).
    max_overdue_days = max(
        [settings.overdue_horizon_days]
        + [task_settings[t.uuid].overdue_horizon_days for t in tasks]
    )
    overdue_horizon = now + timedelta(days=max_overdue_days)
    if overdue_horizon > horizon_end:
        horizon_end = overdue_horizon
    return horizon_end + timedelta(days=1)


def _task_window(
    t: TaskInfo, ts: Settings, now: datetime, tz: tzinfo
) -> tuple[datetime, datetime, datetime]:
    """The bounds a placement for `t` must respect.

    Returns `(earliest allowed start, effective deadline, adjusted due)`.

    Overdue policy: a past-due task is still scheduled ASAP, so its
    effective deadline is pushed out to give the slot search room. The task
    stays "overdue" in the user's eyes and the report flags it.

    Overdue-ness is judged from the *raw* due date, not the end-of-day
    adjusted one: a date-only `due:today` is midnight today, which has
    already passed (Taskwarrior sets +OVERDUE for it too). Using the bumped
    value here would mask that and drop the task ("could not fit before due
    date") instead of letting it spill past today. The earliest-slot search
    still prefers today when a slot is free; the horizon only matters once
    today fills.
    """
    due = effective_due(t.due, tz, ts)

    # Honor `scheduled`/`wait`: never place the task before that date. The
    # floor is inclusive, so a slot may start on the date.
    earliest = now
    floor = t.earliest_start
    if floor is not None and floor > earliest:
        earliest = floor

    deadline = (
        now + timedelta(days=ts.overdue_horizon_days) if t.due <= now else due
    )
    return earliest, deadline, due


def plan_placements(
    tasks: list[TaskInfo],
    *,
    task_settings: dict[str, Settings],
    keepers: dict[str, CalEvent],
    busy: list[tuple[datetime, datetime]],
    now: datetime,
    tz: tzinfo,
    settings: Settings,
) -> tuple[list[Decision], list[TaskInfo]]:
    """Decide where every schedulable task's block goes.

    Two passes, and the order is the point. Pass one reserves the blocks we
    already committed to — in-progress and settled near-term placements that
    are still valid. Only then does pass two place the rest, so a newly
    urgent task can claim time that isn't already promised. One
    urgency-ordered pass would let that task take a slot a settled block was
    sitting in, and the settled block would have to move after all.

    `busy` is extended in place with every block reserved or placed. Returns
    the decisions in urgency order, and the tasks that didn't fit.
    """
    decisions: list[Decision] = []
    unschedulable: list[TaskInfo] = []
    # Why each settled candidate had to be given up, so the report can say.
    moved: dict[str, str] = {}

    # ---- Pass 1: reserve blocks we already committed to -------------------
    # Earliest first, so an in-progress block is reserved before anything
    # else and two settled blocks that somehow overlap resolve in favour of
    # the one starting sooner.
    settled_first = sorted(
        (t for t in tasks if _is_sticky_candidate(t, keepers, now, settings)),
        key=lambda t: keepers[t.uuid].start,
    )
    for t in settled_first:
        ts = task_settings[t.uuid]
        keeper = keepers[t.uuid]
        if keeper.start <= now < keeper.end:
            decision = _pin_in_progress(t, keeper, now)
        else:
            earliest, deadline, due = _task_window(t, ts, now, tz)
            why = invalid_reason(
                start=keeper.start,
                end=keeper.end,
                duration_minutes=t.estimate_minutes,
                earliest_start=earliest,
                deadline=deadline,
                busy=busy,
                tz=tz,
                settings=ts,
            )
            if why is not None:
                moved[t.uuid] = why
                continue
            decision = Decision(
                task=t,
                start_utc=keeper.start,
                end_utc=keeper.end,
                past_due=keeper.start > due,
                keeper=keeper,
            )
        decisions.append(decision)
        bisect.insort(busy, decision.interval)

    # ---- Pass 2: place everything else, most urgent first ----------------
    reserved = {d.task.uuid for d in decisions}
    for t in sorted(tasks, key=lambda t: t.urgency, reverse=True):
        if t.uuid in reserved:
            continue
        ts = task_settings[t.uuid]
        earliest, deadline, due = _task_window(t, ts, now, tz)
        slot = find_earliest_slot(
            duration_minutes=t.estimate_minutes,
            earliest_start=earliest,
            deadline=deadline,
            busy=busy,
            tz=tz,
            settings=ts,
        )
        if slot is None:
            unschedulable.append(t)
            continue
        start_utc = slot[0].astimezone(timezone.utc)
        end_utc = slot[1].astimezone(timezone.utc)
        decision = Decision(
            task=t,
            start_utc=start_utc,
            end_utc=end_utc,
            # "Past due" means the chosen slot actually starts after the
            # (end-of-day-adjusted) due date — the task could not be done in
            # time and spilled. A `due:today` task scheduled later today is
            # overdue but not past due, so it stays in the normal list.
            past_due=start_utc > due,
            keeper=keepers.get(t.uuid),
            moved_reason=moved.get(t.uuid),
        )
        decisions.append(decision)
        bisect.insort(busy, decision.interval)

    decisions.sort(key=lambda d: d.task.urgency, reverse=True)
    return decisions, unschedulable


def _is_sticky_candidate(
    t: TaskInfo,
    keepers: dict[str, CalEvent],
    now: datetime,
    settings: Settings,
) -> bool:
    """True if `t`'s existing block is one pass one should try to keep.

    An in-progress block always qualifies — never yank a block you're in the
    middle of. A finished one never does: it stays put as history and the
    task gets a fresh event.
    """
    keeper = keepers.get(t.uuid)
    if keeper is None or keeper.end <= now:
        return False
    if keeper.start <= now < keeper.end:
        return True
    if is_pinned(keeper):
        # Somebody dragged this block somewhere. Where they put it beats where
        # we would have put it, settled or not — being able to move your own
        # calendar is the point, and a block that springs back tomorrow is
        # worse than one that never moved. It still goes through
        # `invalid_reason` below, so a position that can't work (a meeting on
        # top of it, past the deadline) yields, with a reason — and when we
        # move it, it becomes ours again.
        return True
    return is_settled(keeper.start, now, settings.settle_days)


def _pin_in_progress(t: TaskInfo, keeper: CalEvent, now: datetime) -> Decision:
    """Keep an in-progress block's start, let its end follow the estimate.

    So extending an estimate mid-block extends the block, but we never move
    the start or duplicate it.
    """
    end_utc = keeper.start + timedelta(minutes=t.estimate_minutes)
    if end_utc <= now:
        # A shortened estimate would end the block in the past; leave the
        # end where it is rather than rewind it.
        end_utc = keeper.end
    return Decision(
        task=t,
        start_utc=keeper.start,
        end_utc=end_utc,
        past_due=False,
        keeper=keeper,
    )
