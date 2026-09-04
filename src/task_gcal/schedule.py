"""The scheduling run: read tasks, reconcile the calendar, report.

This is the fast action path. It reads Taskwarrior and the calendar, decides
placements via `placement.py`, and writes events. It appends an observation
to the journal on the way out and never reads one — see `journal/` for why
that direction matters.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

from googleapiclient.errors import HttpError

from .config import Settings
from .gcal import CalEvent, GCal
from .changes import harvest as harvest_changes
from .guard import removal_guard_error
from .journal import MODE_SCHEDULE, PlacementObservation, record_run
from .placement import (
    Decision,
    Placement,
    horizon_for,
    pick_keepers,
    plan_placements,
    resolve_task_settings,
)
from .progress import Progress
from .report import print_report
from .taskw import TaskInfo, load_next_tasks


# ---------------------------------------------------------------------------
# Event content
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


# ---------------------------------------------------------------------------
# Writing placements to the calendar
# ---------------------------------------------------------------------------

def _apply_decision(
    d: Decision,
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

    def _create() -> Optional[str]:
        if dry_run:
            return None
        return gcal.create_event(
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
        event_id = _create()
        action = "create"
    else:
        event_id = keeper.id
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
                    event_id = _create()
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
        event_id=event_id,
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

    # Copy any task edits you've made since the last run into our own history.
    # Read-only against Taskwarrior, and it can only fail quietly — placement
    # never consults history, so a failed harvest cannot affect the calendar.
    if not dry_run:
        harvest_changes(settings)

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

    keepers = pick_keepers(existing, now)

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

    decisions, unschedulable = plan_placements(
        schedulable,
        task_settings=task_settings,
        keepers=keepers,
        busy=busy,
        now=now,
        tz=tz,
        settings=settings,
    )
    for t in unschedulable:
        _drop_existing_if_future(t.uuid, "no slot fits")

    # ---------------- Step 4: write the placements ---------------------
    # The bar belongs here, not around the planning: planning is pure and
    # instant, and a bar that fills before the first API call then sits at
    # N/N for the whole network phase is worse than none.
    sched_prog = Progress(
        total=len(decisions),
        label="Scheduling tasks…",
        enabled=show_progress,
    )
    placed = []
    for d in decisions:
        sched_prog.advance(d.task.description[:48])
        placed.append(
            _apply_decision(
                d,
                gcal=gcal,
                tz=tz,
                ts=task_settings[d.task.uuid],
                now=now,
                dry_run=dry_run,
            )
        )
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

    # A dry run deliberately writes no journal record. Its placements were
    # never made, so keeping them would put moves that never happened into
    # placement churn.
    if not dry_run:
        record_run(
            settings=settings,
            mode=MODE_SCHEDULE,
            at=now,
            placements=tuple(
                PlacementObservation(
                    task_uuid=d.task.uuid,
                    event_id=p.event_id,
                    start=d.start_utc,
                    end=d.end_utc,
                    action=p.action,
                    moved_reason=d.moved_reason,
                )
                for d, p in zip(decisions, placed)
                # No id means the write failed; there is no block to record.
                if p.event_id
            ),
        )

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
