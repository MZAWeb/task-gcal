"""Top-level orchestration + CLI."""

from __future__ import annotations

import argparse
import bisect
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

from googleapiclient.errors import HttpError

from .config import (
    VISIBILITY_CHOICES,
    Settings,
    apply_overrides,
    coerce_work_days,
    load_settings,
    parse_task_overrides,
)
from .gcal import CalEvent, GCal
from .progress import Progress
from .scheduler import find_earliest_slot
from .taskw import TaskInfo, load_next_tasks


# ---------------------------------------------------------------------------
# Event content helpers
# ---------------------------------------------------------------------------

def _event_description(t: TaskInfo, tz: tzinfo) -> str:
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


def _effective_due(due: datetime, tz, settings: Settings) -> datetime:
    """Adjust the due date for scheduling.

    A due date with no time-of-day (local midnight, e.g. `due:monday`)
    is treated as the end of that day's working window, so the task can
    be scheduled during that day. Due dates with an explicit time are
    used as-is.
    """
    local = due.astimezone(tz)
    if local.hour == 0 and local.minute == 0 and local.second == 0:
        end_of_day = local.replace(
            hour=settings.work_end_hour, minute=0, second=0, microsecond=0
        )
        return end_of_day.astimezone(timezone.utc)
    return due


# ---------------------------------------------------------------------------
# Main reconcile
# ---------------------------------------------------------------------------

def reconcile(settings: Settings, *, dry_run: bool = False) -> int:
    tz = settings.resolve_timezone()
    now = datetime.now(timezone.utc)

    tasks = load_next_tasks(
        settings.report,
        estimate_uda=settings.estimate_uda,
        override_uda=settings.override_uda,
    )
    next_uuids = {t.uuid for t in tasks}

    # Resolve each task's effective Settings from its override UDA up front,
    # so the horizon below can account for per-task overdue windows. A bad
    # override warns and falls back to the global settings.
    task_settings: dict[str, Settings] = {}
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
        task_settings[t.uuid] = s

    gcal = GCal(settings)

    # Horizon for slot search and event listing.
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
    horizon_end = horizon_end + timedelta(days=1)

    # List our managed events out to at least the horizon (so far-future
    # events stay visible to reconciliation).
    list_horizon = max(horizon_end, now + timedelta(days=400))
    existing = gcal.list_scheduler_events(
        time_min=now - timedelta(days=settings.lookback_days),
        time_max=list_horizon,
    )

    # Map task -> chosen "keeper" event. Prefer the earliest event with
    # start > now; fall back to a past event only when no future one
    # exists for that task.
    keepers: dict[str, CalEvent] = {}
    for ev in existing:
        if not ev.task_uuid:
            continue
        cur = keepers.get(ev.task_uuid)
        if cur is None:
            keepers[ev.task_uuid] = ev
            continue
        cur_future = cur.start > now
        ev_future = ev.start > now
        if ev_future and not cur_future:
            keepers[ev.task_uuid] = ev
        elif ev_future and cur_future and ev.start < cur.start:
            keepers[ev.task_uuid] = ev
        elif not ev_future and not cur_future and ev.start > cur.start:
            # Among past events, prefer the most recent for stability.
            keepers[ev.task_uuid] = ev

    # Progress is only useful for the slow, network-bound real run on a TTY.
    show_progress = not dry_run
    active_progress: Optional[Progress] = None

    # ---------------- Step 1: orphan + duplicate cleanup ----------------
    removed_orphans: list[str] = []
    removed_duplicates: list[str] = []

    def _delete(ev: CalEvent, bucket: list[str], tag: str = "") -> None:
        ref = f"{ev.task_uuid[:8]} " if ev.task_uuid else ""
        label = f"{tag}{ref}{ev.summary or ev.id}"
        if not dry_run:
            try:
                gcal.delete_event(ev.id)
            except HttpError as e:
                bucket.append(f"{label} (delete failed: {e})")
                return
            if active_progress is not None:
                active_progress.advance((ev.summary or ev.id)[:48])
        bucket.append(label)

    cleanup_prog = Progress(label="Cleaning up calendar…", enabled=show_progress)
    active_progress = cleanup_prog
    cleanup_prog.render()
    for ev in existing:
        # Future events whose task is no longer in `next` -> delete.
        if ev.task_uuid not in next_uuids:
            if ev.start > now:
                _delete(ev, removed_orphans)
            continue
        # Future duplicates beyond the keeper -> delete.
        if ev.id != keepers[ev.task_uuid].id and ev.start > now:
            _delete(ev, removed_duplicates, tag="(duplicate) ")
    cleanup_prog.close()
    active_progress = None

    # ---------------- Step 2: pull busy intervals ----------------------
    our_event_ids = {ev.id for ev in existing}
    busy = gcal.list_busy_events(
        time_min=now - timedelta(hours=1),
        time_max=horizon_end,
        exclude_event_ids=our_event_ids,
    )

    # ---------------- Step 3: schedule ---------------------------------
    no_estimate: list[TaskInfo] = []
    no_due: list[TaskInfo] = []
    unschedulable: list[TaskInfo] = []
    placed: list[tuple[TaskInfo, datetime, datetime, str, bool]] = []
    # (task, start, end, action, was_overdue)
    removed_stale: list[str] = []

    def _drop_existing_if_future(uuid: str, why: str) -> None:
        ev = keepers.get(uuid)
        if ev and ev.start > now:
            _delete(ev, removed_stale, tag=f"({why}) ")

    tasks_sorted = sorted(tasks, key=lambda t: t.urgency, reverse=True)

    sched_prog = Progress(
        total=len(tasks_sorted),
        label="Scheduling tasks…",
        enabled=show_progress,
    )

    for t in tasks_sorted:
        sched_prog.advance(t.description[:48])
        if t.estimate_minutes is None:
            no_estimate.append(t)
            _drop_existing_if_future(t.uuid, "no estimate")
            continue

        if t.due is None:
            no_due.append(t)
            _drop_existing_if_future(t.uuid, "no due date")
            continue

        # Per-task effective settings (global + any `gcal` UDA override).
        ts = task_settings[t.uuid]

        # Adjust a midnight due date to end of that working day.
        due = _effective_due(t.due, tz, ts)

        # Honor `scheduled`/`wait`: never place the task before that date.
        # The floor is inclusive, so a slot may start on the date itself.
        earliest = now
        floor = t.earliest_start
        if floor is not None and floor > earliest:
            earliest = floor

        # Overdue policy: if the task is past due, we still schedule it
        # ASAP. The effective deadline is pushed out so the slot search
        # has room. The task remains "overdue" in the user's eyes; we
        # flag it in the report.
        was_overdue = due <= now
        effective_deadline = (
            now + timedelta(days=ts.overdue_horizon_days)
            if was_overdue
            else due
        )

        slot = find_earliest_slot(
            duration_minutes=t.estimate_minutes,
            earliest_start=earliest,
            deadline=effective_deadline,
            busy=busy,
            tz=tz,
            settings=ts,
        )
        if slot is None:
            unschedulable.append(t)
            _drop_existing_if_future(t.uuid, "no slot fits")
            continue

        start, end = slot
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        summary = t.description
        description = _event_description(t, tz)
        existing_ev = keepers.get(t.uuid)

        if existing_ev is None or existing_ev.start <= now:
            # No keeper, or the keeper is in the past (history): create
            # a fresh event at the new time. We don't move past events;
            # they stay as a record.
            action = "create"
            if not dry_run:
                gcal.create_event(
                    task_uuid=t.uuid,
                    summary=summary,
                    description=description,
                    start=start_utc,
                    end=end_utc,
                    color_id=ts.event_color_id,
                    visibility=ts.event_visibility,
                    attendees=ts.attendees,
                )
        else:
            need_summary = existing_ev.summary != summary
            need_time = not (
                _almost_equal(existing_ev.start, start_utc)
                and _almost_equal(existing_ev.end, end_utc)
            )
            existing_desc = existing_ev.raw.get("description") or ""
            need_desc = existing_desc != description
            need_color = existing_ev.raw.get("colorId") != ts.event_color_id
            need_visibility = (
                existing_ev.raw.get("visibility") != ts.event_visibility
            )
            # Additive: invite UDA addresses that aren't already on the
            # event; never drop anyone (preserves manual attendees).
            existing_atts = existing_ev.raw.get("attendees") or []
            existing_emails = {
                (a.get("email") or "").lower() for a in existing_atts
            }
            new_atts = [
                e for e in ts.attendees if e.lower() not in existing_emails
            ]
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
                        existing_ev.id,
                        summary=summary if need_summary else None,
                        description=description if need_desc else None,
                        start=start_utc if need_time else None,
                        end=end_utc if need_time else None,
                        color_id=ts.event_color_id if need_color else None,
                        visibility=(
                            ts.event_visibility if need_visibility else None
                        ),
                        attendees=(
                            existing_atts + [{"email": e} for e in new_atts]
                            if need_attendees
                            else None
                        ),
                    )
                    if not ok:
                        # Event vanished between list and patch; recreate.
                        gcal.create_event(
                            task_uuid=t.uuid,
                            summary=summary,
                            description=description,
                            start=start_utc,
                            end=end_utc,
                            color_id=ts.event_color_id,
                            visibility=ts.event_visibility,
                            attendees=ts.attendees,
                        )
                        action = "create"
            else:
                action = "unchanged"

        placed.append((t, start, end, action, was_overdue))
        bisect.insort(busy, (start_utc, end_utc))

    sched_prog.close()

    _print_report(
        placed=placed,
        no_estimate=no_estimate,
        no_due=no_due,
        unschedulable=unschedulable,
        removed_orphans=removed_orphans,
        removed_duplicates=removed_duplicates,
        removed_stale=removed_stale,
        dry_run=dry_run,
        tz=tz,
    )
    return 0


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _est(t: TaskInfo) -> str:
    """Estimate column, right-aligned to 3 digits so later columns line up.

    `—` when the task has no estimate; estimates over 999m just widen.
    """
    val = f"{t.estimate_minutes}m" if t.estimate_minutes is not None else "—"
    return f"est={val:>4}"


def _override_flag(t: TaskInfo) -> str:
    """A trailing marker showing the per-task overrides that were applied."""
    return f"  [override: {t.overrides_raw}]" if t.overrides_raw else ""


def _print_report(
    *,
    placed,
    no_estimate,
    no_due,
    unschedulable,
    removed_orphans,
    removed_duplicates,
    removed_stale,
    dry_run: bool,
    tz: tzinfo,
) -> None:
    fmt = "%a %Y-%m-%d %H:%M"

    if dry_run:
        print("# DRY RUN -- no changes will be made\n")

    if placed:
        print(f"Scheduled ({len(placed)}):")
        for t, start, end, action, was_overdue in placed:
            s = start.astimezone(tz).strftime(fmt)
            e = end.astimezone(tz).strftime("%H:%M")
            tag = " [OVERDUE]" if was_overdue else ""
            print(
                f"  [{action:<9}] {s}-{e}  u={t.urgency:5.2f}  {_est(t)}{tag}  "
                f"{t.ref} {t.description}{_override_flag(t)}"
            )
        print()

    if removed_orphans:
        print(
            f"Removed completed/dropped tasks from future calendar "
            f"({len(removed_orphans)}):"
        )
        for s in removed_orphans:
            print(f"  - {s}")
        print()

    if removed_duplicates:
        print(f"Removed duplicate events ({len(removed_duplicates)}):")
        for s in removed_duplicates:
            print(f"  - {s}")
        print()

    if removed_stale:
        print(
            f"Removed stale future events for tasks no longer schedulable "
            f"({len(removed_stale)}):"
        )
        for s in removed_stale:
            print(f"  - {s}")
        print()

    if no_estimate:
        print(f"Skipped: no `estimate` UDA ({len(no_estimate)}):")
        for t in no_estimate:
            print(f"  - u={t.urgency:5.2f}  {_est(t)}  {t.ref} {t.description}")
        print()

    if no_due:
        print(f"Skipped: no due date ({len(no_due)}):")
        for t in no_due:
            print(f"  - u={t.urgency:5.2f}  {_est(t)}  {t.ref} {t.description}")
        print()

    if unschedulable:
        print(f"Could not fit before due date ({len(unschedulable)}):")
        for t in unschedulable:
            due = t.due.astimezone(tz).strftime(fmt) if t.due else "(no due)"
            print(
                f"  - u={t.urgency:5.2f}  {_est(t)}  due={due}  "
                f"{t.ref} {t.description}{_override_flag(t)}"
            )
        print()

    if not (
        placed
        or removed_orphans
        or removed_duplicates
        or removed_stale
        or no_estimate
        or no_due
        or unschedulable
    ):
        print("Nothing to do.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_work_days(raw: str) -> frozenset[int]:
    """argparse adapter around `coerce_work_days` (0=Mon .. 6=Sun)."""
    try:
        return coerce_work_days(raw)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


# CLI flags that override config.toml / defaults. `dest` matches the
# Settings field name so overrides can be applied generically. All default
# to None so an unset flag leaves the config value untouched.
_OVERRIDE_DESTS = (
    "work_start_hour",
    "work_end_hour",
    "work_days",
    "slot_align_minutes",
    "buffer_minutes",
    "estimate_uda",
    "calendar_id",
    "event_color_id",
    "event_visibility",
    "report",
    "timezone",
    "overdue_horizon_days",
    "lookback_days",
    "override_uda",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="task-gcal",
        description=(
            "Schedule Taskwarrior 'next' tasks into free Google Calendar slots."
        ),
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run the OAuth flow and exit (use after placing credentials.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without modifying the calendar.",
    )

    g = parser.add_argument_group(
        "overrides",
        "Override config.toml / built-in defaults for this run.",
    )
    g.add_argument(
        "--calendar-id", dest="calendar_id", metavar="ID",
        help="Calendar to write to (default: primary).",
    )
    g.add_argument(
        "--report", dest="report", metavar="NAME",
        help="Taskwarrior report to pull tasks from (default: next).",
    )
    g.add_argument(
        "--estimate-uda", dest="estimate_uda", metavar="NAME",
        help="Taskwarrior UDA holding the time estimate in minutes "
             "(default: estimate).",
    )
    g.add_argument(
        "--override-uda", dest="override_uda", metavar="NAME",
        help="Taskwarrior UDA holding inline per-task overrides, e.g. "
             "gcal:'work_end_hour=20 buffer_minutes=0' (default: gcal).",
    )
    g.add_argument(
        "--timezone", dest="timezone", metavar="ZONE",
        help="IANA timezone to schedule in, e.g. Europe/London "
             "(default: system local zone).",
    )
    g.add_argument(
        "--work-start", dest="work_start_hour", type=int, metavar="HOUR",
        help="Working-hours start hour, 0-23 (default: 9).",
    )
    g.add_argument(
        "--work-end", dest="work_end_hour", type=int, metavar="HOUR",
        help="Working-hours end hour, exclusive, 0-24 (default: 18).",
    )
    g.add_argument(
        "--work-days", dest="work_days", type=_parse_work_days, metavar="D,D,..",
        help="Comma-separated working weekdays, 0=Mon..6=Sun "
             "(default: 0,1,2,3,4).",
    )
    g.add_argument(
        "--slot-align", dest="slot_align_minutes", type=int, metavar="MIN",
        help="Align slot start times to this minute boundary (default: 15).",
    )
    g.add_argument(
        "--buffer-minutes", dest="buffer_minutes", type=int, metavar="MIN",
        help="Free time to keep around every event; 0 packs tasks "
             "back-to-back (default: 0).",
    )
    g.add_argument(
        "--event-color", dest="event_color_id", metavar="ID",
        help="Google Calendar colorId for created events (default: 9).",
    )
    g.add_argument(
        "--event-visibility", dest="event_visibility", choices=VISIBILITY_CHOICES,
        help="Visibility of created events (default: private).",
    )
    g.add_argument(
        "--overdue-horizon-days", dest="overdue_horizon_days", type=int,
        metavar="DAYS",
        help="How many days ahead an overdue task may be scheduled "
             "(default: 30).",
    )
    g.add_argument(
        "--lookback-days", dest="lookback_days", type=int, metavar="DAYS",
        help="How far back to scan for our own past events (default: 7).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    overrides = {dest: getattr(args, dest) for dest in _OVERRIDE_DESTS}
    settings = apply_overrides(load_settings(), overrides)

    if args.setup:
        GCal(settings)
        print("Auth OK.")
        return 0

    try:
        return reconcile(settings, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except HttpError as e:
        print(f"Google Calendar API error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
