"""The schedule run report.

Rendering only: `reconcile` hands over what it did and this module decides
how it reads. Kept separate from the review reports under `review/`, which
answer a different question (what happened over a period, not what this run
just changed).
"""

from __future__ import annotations

from datetime import tzinfo

from .taskw import TaskInfo

_WHEN_FMT = "%a %Y-%m-%d %H:%M"


def _est(t: TaskInfo) -> str:
    """Estimate column, right-aligned to 3 digits so later columns line up.

    `—` when the task has no estimate; estimates over 999m just widen.
    """
    val = f"{t.estimate_minutes}m" if t.estimate_minutes is not None else "—"
    return f"est={val:>4}"


def _override_flag(t: TaskInfo) -> str:
    """A trailing marker showing the per-task overrides that were applied."""
    return f"  [override: {t.overrides_raw}]" if t.overrides_raw else ""


def print_report(
    *,
    placed,
    no_estimate,
    no_due,
    unschedulable,
    removed_orphans,
    removed_duplicates,
    removed_stale,
    withheld,
    guard_error,
    dry_run: bool,
    tz: tzinfo,
) -> None:
    fmt = _WHEN_FMT

    if dry_run:
        print("# DRY RUN -- no changes will be made\n")

    # Tasks that spilled past their due date get their own section,
    # printed last (see below) so it lands at the bottom of the output
    # where it won't scroll out of view behind a long schedule.
    overdue = [p for p in placed if p.past_due]
    on_time = [p for p in placed if not p.past_due]

    if on_time:
        print(f"Scheduled ({len(on_time)}):")
        for p in on_time:
            s = p.start.astimezone(tz).strftime(fmt)
            e = p.end.astimezone(tz).strftime("%H:%M")
            print(
                f"  [{p.action:<9}] {s}-{e}  u={p.task.urgency:5.2f}  "
                f"{_est(p.task)}  {p.task.ref} {p.task.description}"
                f"{_override_flag(p.task)}"
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
        or withheld
    ):
        print("Nothing to do.")

    # Second-to-last: the guard tripping means this run did *not* finish the
    # job, which matters more than any individual line above it.
    if guard_error:
        print(f"Bulk-removal guard: {guard_error}")
        print(f"\nHeld back ({len(withheld)}):")
        for s in withheld:
            print(f"  - {s}")

    # Printed last, on purpose: a missed deadline is the one thing you
    # most want to notice, and the bottom of the output is what stays on
    # screen after a long run.
    if overdue:
        print(f"Overdue — scheduled past due date ({len(overdue)}):")
        print(
            "  (couldn't fit in time — reschedule, change the due date, "
            "or make room)"
        )
        for p in overdue:
            s = p.start.astimezone(tz).strftime(fmt)
            e = p.end.astimezone(tz).strftime("%H:%M")
            due_s = (
                p.task.due.astimezone(tz).strftime(_WHEN_FMT)
                if p.task.due else "(no due)"
            )
            print(
                f"  ! [{p.action:<9}] {s}-{e}  u={p.task.urgency:5.2f}  "
                f"{_est(p.task)}  {p.task.ref} {p.task.description}  "
                f"(due {due_s}){_override_flag(p.task)}"
            )
