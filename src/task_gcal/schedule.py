"""The scheduling run: read tasks, reconcile the calendar, report.

This is the fast action path. It reads Taskwarrior and the calendar, decides
placements, and mutates events. It never reads history to make a placement
decision — see `journal/` for why that boundary matters.
"""

from __future__ import annotations

import bisect
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

from googleapiclient.errors import HttpError

from .config import Settings, apply_overrides, parse_task_overrides
from .gcal import CalEvent, GCal
from .progress import Progress
from .report import print_report
from .scheduler import find_earliest_slot
from .stability import invalid_reason, is_settled
from .taskw import TaskInfo, load_next_tasks


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


@dataclass
class _Decision:
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


# ---------------------------------------------------------------------------
# Event content helpers
# ---------------------------------------------------------------------------

def event_description(t: TaskInfo, tz: tzinfo) -> str:
    """Stable, idempotent description.

    Crucially: no urgency value (changes constantly via Taskwarrior's age
    and due-proximity coefficients). No dynamic timestamps either.
    """
    parts = [f"Task UUID: {t.uuid}"]
    if t.project:
        parts.append(f"Project: {t.project}")
    if t.tags:
        parts.append(f"Tags: {', '.join(t.tags)}")
    if t.due:
        parts.append(
            f"Due: {t.due.astimezone(tz).strftime('%Y-%m-%d %H:%M %Z')}"
        )
    if t.annotations:
        parts.append("")
        parts.append("Annotations:")
        parts.extend(f"  {a}" for a in t.annotations)
    parts.append("")
    parts.append(
        "Managed by task-gcal. Edits to time/title may be overwritten on next run."
    )
    return "\n".join(parts)


def _almost_equal(a: datetime, b: datetime) -> bool:
    return a.replace(microsecond=0) == b.replace(microsecond=0)


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


# Below this many removals a run is never blocked: clearing a couple of
# finished or duplicated events is routine, and a fresh calendar shouldn't
# need `--force` on its first cleanup.
_REMOVAL_GUARD_FLOOR = 3


def removal_guard_error(
    *, removals: int, owned_unfinished: int, ratio: float
) -> Optional[str]:
    """Explain why a run should refuse to remove this many events, or None.

    One bad input can make every task look unschedulable and turn a normal
    run into a mass deletion: a task source that returns nothing (wrong
    report name, an active Taskwarrior context, `TASKDATA` pointing at
    another replica), or a mistyped `--estimate-uda` so no task has an
    estimate. Removing a few of our events is routine; removing most of what
    we own means the *input* is wrong, not the calendar.

    `ratio` is the share of our unfinished events a run may remove; 1.0
    disables the guard, since a run can never remove more than all of them.
    """
    if owned_unfinished <= 0 or removals < _REMOVAL_GUARD_FLOOR:
        return None
    if removals <= ratio * owned_unfinished:
        return None
    return (
        f"refusing to remove {removals} of {owned_unfinished} unfinished "
        f"events we own ({removals / owned_unfinished:.0%}; the guard trips "
        f"above {ratio:.0%}).\n"
        "  An unusually large cleanup usually means the input is wrong, not "
        "the calendar.\n"
        "  Check the report/UDA names above, then re-run with --force to "
        "remove them anyway\n"
        "  (or raise removal_guard_ratio in config.toml)."
    )


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


# ---------------------------------------------------------------------------
# Placement planning
# ---------------------------------------------------------------------------

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
    progress: Optional[Progress] = None,
) -> tuple[list[_Decision], list[TaskInfo]]:
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
    decisions: list[_Decision] = []
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
            decision = _pin_in_progress(t, keeper, now, ts)
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
            decision = _Decision(
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
        if progress is not None:
            progress.advance(t.description[:48])
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
        decision = _Decision(
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
    return is_settled(keeper.start, now, settings.settle_days)


def _pin_in_progress(
    t: TaskInfo, keeper: CalEvent, now: datetime, ts: Settings
) -> _Decision:
    """Keep an in-progress block's start, let its end follow the estimate.

    So extending an estimate mid-block extends the block, but we never move
    the start or duplicate it.
    """
    end_utc = keeper.start + timedelta(minutes=t.estimate_minutes)
    if end_utc <= now:
        # A shortened estimate would end the block in the past; leave the
        # end where it is rather than rewind it.
        end_utc = keeper.end
    return _Decision(
        task=t,
        start_utc=keeper.start,
        end_utc=end_utc,
        past_due=False,
        keeper=keeper,
    )


# ---------------------------------------------------------------------------
# Writing placements to the calendar
# ---------------------------------------------------------------------------

def _apply_decision(
    d: _Decision,
    *,
    gcal: GCal,
    tz: tzinfo,
    ts: Settings,
    now: datetime,
    dry_run: bool,
) -> Placement:
    """Create, patch, or leave alone the event for one decision."""
    t = d.task
    summary = t.description
    description = event_description(t, tz)
    keeper = d.keeper

    def _create() -> None:
        if dry_run:
            return
        gcal.create_event(
            task_uuid=t.uuid,
            summary=summary,
            description=description,
            start=d.start_utc,
            end=d.end_utc,
            color_id=ts.event_color_id,
            visibility=ts.event_visibility,
            attendees=ts.attendees,
        )

    if keeper is None or keeper.end <= now:
        # No keeper, or the keeper has already finished: create a fresh
        # event. In-progress and upcoming keepers are patched in place;
        # finished ones stay put as a record.
        _create()
        action = "create"
    else:
        need_summary = keeper.summary != summary
        need_time = not (
            _almost_equal(keeper.start, d.start_utc)
            and _almost_equal(keeper.end, d.end_utc)
        )
        need_desc = (keeper.raw.get("description") or "") != description
        need_color = keeper.raw.get("colorId") != ts.event_color_id
        need_visibility = keeper.raw.get("visibility") != ts.event_visibility
        # Additive: invite UDA addresses that aren't already on the event;
        # never drop anyone (preserves manual attendees).
        existing_atts = keeper.raw.get("attendees") or []
        existing_emails = {(a.get("email") or "").lower() for a in existing_atts}
        new_atts = [e for e in ts.attendees if e.lower() not in existing_emails]
        need_attendees = bool(new_atts)
        if (
            need_summary
            or need_time
            or need_desc
            or need_color
            or need_visibility
            or need_attendees
        ):
            action = "update"
            if not dry_run:
                ok = gcal.patch_event(
                    keeper.id,
                    summary=summary if need_summary else None,
                    description=description if need_desc else None,
                    start=d.start_utc if need_time else None,
                    end=d.end_utc if need_time else None,
                    color_id=ts.event_color_id if need_color else None,
                    visibility=ts.event_visibility if need_visibility else None,
                    attendees=(
                        existing_atts + [{"email": e} for e in new_atts]
                        if need_attendees
                        else None
                    ),
                )
                if not ok:
                    # Event vanished between list and patch; recreate.
                    _create()
                    action = "create"
        else:
            action = "unchanged"

    return Placement(
        task=t,
        start=d.start_utc.astimezone(tz),
        end=d.end_utc.astimezone(tz),
        action=action,
        past_due=d.past_due,
        moved_reason=d.moved_reason,
    )


# ---------------------------------------------------------------------------
# Main reconcile
# ---------------------------------------------------------------------------

def reconcile(
    settings: Settings, *, dry_run: bool = False, force: bool = False
) -> int:
    tz = settings.resolve_timezone()
    now = datetime.now(timezone.utc)

    tasks = load_next_tasks(
        settings.report,
        estimate_uda=settings.estimate_uda,
        override_uda=settings.override_uda,
    )
    next_uuids = {t.uuid for t in tasks}

    # Resolve each task's effective Settings from its override UDA up front,
    # so the horizon below can account for per-task overdue windows.
    task_settings = resolve_task_settings(tasks, settings)

    gcal = GCal(settings)

    horizon_end = horizon_for(tasks, task_settings, settings, now)

    # List our managed events out to at least the horizon (so far-future
    # events stay visible to reconciliation).
    list_horizon = max(horizon_end, now + timedelta(days=400))
    existing = gcal.list_scheduler_events(
        time_min=now - timedelta(days=settings.lookback_days),
        time_max=list_horizon,
    )

    # Map task -> chosen "keeper" event. An in-progress event wins (never
    # move/duplicate a task you're doing now), then the earliest upcoming
    # event, then the most recent past one (see `_keeper_rank`).
    keepers: dict[str, CalEvent] = {}
    for ev in existing:
        if not ev.task_uuid:
            continue
        cur = keepers.get(ev.task_uuid)
        if cur is None or _keeper_rank(ev, now) < _keeper_rank(cur, now):
            keepers[ev.task_uuid] = ev

    # Everything we could still remove. Also the denominator for the
    # bulk-removal guard: finished events are history and never touched.
    owned_unfinished = sum(1 for ev in existing if ev.end > now)

    # A source that returns nothing is indistinguishable from "you finished
    # everything" — except that the second case is rare and the first has
    # several silent causes. Bail before mutating anything.
    if not tasks and owned_unfinished and not force:
        print(
            f"`task export {settings.report}` returned no tasks, but "
            f"{owned_unfinished} unfinished event(s) on "
            f"{settings.calendar_id} are ours.\n"
            "Refusing to clear them. An empty task list is usually a wrong "
            "report name, an active\nTaskwarrior context, or TASKDATA "
            "pointing at another replica — not an empty backlog.\n"
            "Re-run with --force if the list really is empty.",
            file=sys.stderr,
        )
        return 1

    # Progress is only useful for the slow, network-bound real run on a TTY.
    show_progress = not dry_run
    active_progress: Optional[Progress] = None

    # ---------------- Step 1: plan orphan + duplicate cleanup ------------
    removed_orphans: list[str] = []
    removed_duplicates: list[str] = []

    # Removals are planned here and executed at the very end, so the guard
    # sees the whole plan before anything is deleted (a broken input shows up
    # as a large cleanup, and later steps add to it). Deferring is safe:
    # `list_busy_events` excludes every event we own regardless of whether
    # it has been deleted yet, so placement is unaffected.
    planned_removals: list[tuple[CalEvent, list[str], str]] = []

    def _plan_delete(ev: CalEvent, bucket: list[str], tag: str = "") -> None:
        planned_removals.append((ev, bucket, tag))

    def _label(ev: CalEvent, tag: str) -> str:
        ref = f"{ev.task_uuid[:8]} " if ev.task_uuid else ""
        return f"{tag}{ref}{ev.summary or ev.id}"

    def _delete(ev: CalEvent, bucket: list[str], tag: str = "") -> None:
        label = _label(ev, tag)
        if not dry_run:
            try:
                gcal.delete_event(ev.id)
            except HttpError as e:
                bucket.append(f"{label} (delete failed: {e})")
                return
            if active_progress is not None:
                active_progress.advance((ev.summary or ev.id)[:48])
        bucket.append(label)

    for ev in existing:
        # Future events whose task is no longer in `next` -> delete.
        if ev.task_uuid not in next_uuids:
            if ev.start > now:
                _plan_delete(ev, removed_orphans)
            continue
        # Duplicates beyond the keeper that haven't finished yet -> delete.
        # This includes ones overlapping now: only the keeper is protected
        # from removal mid-event, so spurious in-progress copies still go.
        if ev.id != keepers[ev.task_uuid].id and ev.end > now:
            _plan_delete(ev, removed_duplicates, tag="(duplicate) ")

    # ---------------- Step 2: pull busy intervals ----------------------
    our_event_ids = {ev.id for ev in existing}
    busy = gcal.list_busy_events(
        time_min=now - timedelta(hours=1),
        time_max=horizon_end,
        exclude_event_ids=our_event_ids,
    )

    # ---------------- Step 3: decide placements -------------------------
    no_estimate: list[TaskInfo] = []
    no_due: list[TaskInfo] = []
    removed_stale: list[str] = []

    def _drop_existing_if_future(uuid: str, why: str) -> None:
        ev = keepers.get(uuid)
        if ev and ev.start > now:
            _plan_delete(ev, removed_stale, tag=f"({why}) ")

    schedulable: list[TaskInfo] = []
    for t in sorted(tasks, key=lambda t: t.urgency, reverse=True):
        if t.estimate_minutes is None:
            no_estimate.append(t)
            _drop_existing_if_future(t.uuid, "no estimate")
        elif t.due is None:
            no_due.append(t)
            _drop_existing_if_future(t.uuid, "no due date")
        else:
            schedulable.append(t)

    sched_prog = Progress(
        total=len(schedulable),
        label="Scheduling tasks…",
        enabled=show_progress,
    )
    decisions, unschedulable = plan_placements(
        schedulable,
        task_settings=task_settings,
        keepers=keepers,
        busy=busy,
        now=now,
        tz=tz,
        settings=settings,
        progress=sched_prog,
    )
    for t in unschedulable:
        _drop_existing_if_future(t.uuid, "no slot fits")

    # ---------------- Step 4: write the placements ---------------------
    placed = [
        _apply_decision(
            d,
            gcal=gcal,
            tz=tz,
            ts=task_settings[d.task.uuid],
            now=now,
            dry_run=dry_run,
        )
        for d in decisions
    ]
    sched_prog.close()

    # ---------------- Step 5: execute the removal plan -------------------
    guard_error = (
        None
        if force
        else removal_guard_error(
            removals=len(planned_removals),
            owned_unfinished=owned_unfinished,
            ratio=settings.removal_guard_ratio,
        )
    )
    withheld: list[str] = []
    if guard_error:
        # Leave the removal buckets empty so the report can't claim we
        # deleted anything; list what was held back instead.
        withheld = [_label(ev, tag) for ev, _bucket, tag in planned_removals]
    elif planned_removals:
        cleanup_prog = Progress(
            total=len(planned_removals),
            label="Cleaning up calendar…",
            enabled=show_progress,
        )
        active_progress = cleanup_prog
        cleanup_prog.render()
        for ev, bucket, tag in planned_removals:
            _delete(ev, bucket, tag)
        cleanup_prog.close()
        active_progress = None

    print_report(
        placed=placed,
        no_estimate=no_estimate,
        no_due=no_due,
        unschedulable=unschedulable,
        removed_orphans=removed_orphans,
        removed_duplicates=removed_duplicates,
        removed_stale=removed_stale,
        withheld=withheld,
        guard_error=guard_error,
        dry_run=dry_run,
        tz=tz,
    )
    return 1 if guard_error else 0
