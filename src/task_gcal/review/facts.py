"""Everything a review reads, gathered once.

Reviews are read-only by construction: this module makes the queries, the
metrics get a frozen `Facts`, and nothing downstream can reach the calendar
or Taskwarrior again. That's what keeps "reviews never write to Taskwarrior"
and "review code never participates in calendar reconciliation" true by
structure rather than by care.

Where each fact comes from matters, because it decides what a metric can
honestly claim:

- **Taskwarrior** gives completion facts — `entry`, `end`, `status`, and the
  current `due`/`estimate`. Exact, and available for the whole history.
- **The calendar** gives past blocks. Past events are never moved or deleted,
  so the calendar is already a complete record of blocks that happened;
  `lookback_days` only bounds the *scheduler's* query, and a review is free
  to look back further.
- **The journal** gives what neither of the above can reconstruct: what the
  due date and estimate *were at the time*, and how often a block moved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..changes import ChangeHistory, harvest
from ..changes import load as load_changes
from ..config import Settings
from ..gcal import CalEvent, GCal
from ..intervals import clip_to_windows, total_minutes
from ..journal import (
    MODE_BACKFILL,
    MODE_SCHEDULE,
    MODE_SNAPSHOT,
    JournalRead,
    load,
)
from ..taskw import TaskInfo, load_all_tasks
from .periods import Period, work_windows

# How far before the period to pull our own blocks. Blocks-to-completion and
# stagnation both ask "how many blocks came before this?", which needs blocks
# that predate the window being reported on.
BLOCK_LOOKBACK_DAYS = 180

# How far before the period to read the journal. A change is only observable
# as a *difference* between two observations, so the first day of the period
# needs a predecessor to be compared against — and the promise ledger wants
# the earliest due date it can find, not the earliest one inside the window.
JOURNAL_LOOKBACK_DAYS = 400


@dataclass(frozen=True)
class Facts:
    """Immutable inputs for every metric."""

    period: Period
    settings: Settings
    now: datetime
    # Every task Taskwarrior knows about, closed ones included.
    tasks: tuple[TaskInfo, ...] = ()
    # Blocks we own, from `BLOCK_LOOKBACK_DAYS` before the period onward.
    blocks: tuple[CalEvent, ...] = ()
    # Other people's time in the period. Excludes our own blocks, so it is
    # meeting load rather than total busy-ness.
    meetings: tuple[tuple[datetime, datetime], ...] = ()
    journal: JournalRead = field(default_factory=JournalRead)
    # Taskwarrior's own record of what changed, harvested into our store. The
    # journal no longer carries task fields at all.
    changes: ChangeHistory = field(default_factory=ChangeHistory)
    # Set when a source failed. A metric that depends on it must report
    # itself unmeasured rather than treat the gap as zero.
    calendar_ok: bool = True
    # Built once here rather than five times across the metrics that want it.
    timelines: dict = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.timelines and self.changes.changes:
            from .observed import build_timelines

            object.__setattr__(
                self, "timelines", build_timelines(self.changes.changes)
            )

    # ---------------------------- derived views ---------------------------

    def by_uuid(self) -> dict[str, TaskInfo]:
        return {t.uuid: t for t in self.tasks}

    def completed_in_period(self) -> list[TaskInfo]:
        return [
            t
            for t in self.tasks
            if t.status == "completed" and self.period.contains(t.end)
        ]

    def deleted_in_period(self) -> list[TaskInfo]:
        return [
            t
            for t in self.tasks
            if t.status == "deleted" and self.period.contains(t.end)
        ]

    def created_in_period(self) -> list[TaskInfo]:
        return [t for t in self.tasks if self.period.contains(t.entry)]

    def blocks_in_period(self) -> list[CalEvent]:
        """Blocks whose time falls inside the period at all."""
        return [
            b
            for b in self.blocks
            if b.end > self.period.start and b.start < self.period.end
        ]

    def blocks_ended_in_period(self) -> list[CalEvent]:
        """Blocks that finished inside the period.

        Follow-through asks about blocks that have *had their chance*, so it
        keys on the end rather than the start: a block still running tonight
        hasn't been missed.
        """
        return [b for b in self.blocks if self.period.contains(b.end)]

    def blocks_by_task(self) -> dict[str, list[CalEvent]]:
        out: dict[str, list[CalEvent]] = {}
        for block in self.blocks:
            if block.task_uuid:
                out.setdefault(block.task_uuid, []).append(block)
        for blocks in out.values():
            blocks.sort(key=lambda b: b.start)
        return out

    def records_in_period(self) -> list:
        """Journal records observed during the period, oldest first."""
        return [r for r in self.journal.records if self.period.contains(r.at)]

    def observed_days(self) -> set:
        """Local dates the journal has a healthy observation for."""
        return {
            r.at.astimezone(self.period.tz).date()
            for r in self.records_in_period()
            if r.source_ok
        }

    def changes_in_period(self) -> list:
        """Harvested field changes that happened inside the period."""
        return [c for c in self.changes.changes if self.period.contains(c.at)]

    def change_history_reaches_period(self) -> bool:
        """True when harvested history covers the period being reported on.

        False means the answer to "did anything move?" is *unknown*, not "no" —
        either nothing has been harvested yet, or our earliest change is after
        the period started.
        """
        earliest = self.changes.earliest
        return earliest is not None and earliest <= self.period.start

    def change_coverage_note(self) -> Optional[str]:
        """Why the change history might be incomplete, if it might be."""
        earliest = self.changes.earliest
        if earliest is None:
            return (
                "No task-change history yet. `task-gcal backfill` imports what "
                "Taskwarrior still remembers."
            )
        if earliest > self.period.start:
            return (
                "Task-change history starts "
                f"{earliest.astimezone(self.period.tz):%Y-%m-%d}, after this "
                f"{self.period.kind} began, so earlier moves are unknown "
                "rather than absent."
            )
        if self.changes.gaps:
            return (
                f"{len(self.changes.gaps)} Taskwarrior operation(s) could not "
                "be read, so a change may be missing."
            )
        return None

    def backfilled_days(self) -> set:
        return {
            r.at.astimezone(self.period.tz).date()
            for r in self.records_in_period()
            if r.mode == MODE_BACKFILL
        }

    # ---------------------------- shared arithmetic -----------------------
    # Capacity owns the *reporting* of these, but more than one metric needs
    # the numbers, and two implementations of "how much of the week was
    # meetings" would eventually disagree.

    def work_windows(self) -> list[tuple[datetime, datetime]]:
        return list(work_windows(self.period, self.settings))

    def working_minutes(self) -> int:
        return total_minutes(self.work_windows())

    def meetings_in_working_hours(self) -> list[tuple[datetime, datetime]]:
        """Meeting time inside working hours, merged so it can be summed."""
        return clip_to_windows(self.meetings, self.work_windows())

    def meeting_share(self) -> Optional[float]:
        """Fraction of working hours spent in meetings, or None if unknown."""
        if not self.calendar_ok:
            return None
        available = self.working_minutes()
        if not available:
            return None
        return total_minutes(self.meetings_in_working_hours()) / available


def collect(
    settings: Settings,
    period: Period,
    *,
    now: datetime,
    gcal: Optional[GCal] = None,
) -> Facts:
    """Run every query a review needs. The only I/O in the review path."""
    tasks = tuple(
        load_all_tasks(
            estimate_uda=settings.estimate_uda,
            override_uda=settings.override_uda,
        )
    )

    blocks: tuple[CalEvent, ...] = ()
    meetings: tuple[tuple[datetime, datetime], ...] = ()
    calendar_ok = True
    try:
        # Building the client is inside the `try` on purpose: on a machine
        # that has never run `--setup` this is where it fails, and that is
        # the most common calendar failure of all. `allow_interactive=False`
        # keeps a read-only path from opening a browser — a review run from
        # cron must degrade, not block on an OAuth flow.
        client = (
            gcal
            if gcal is not None
            else GCal(settings, allow_interactive=False)
        )
        blocks = tuple(
            client.list_scheduler_events(
                time_min=period.start - timedelta(days=BLOCK_LOOKBACK_DAYS),
                time_max=period.end,
            )
        )
        meetings = tuple(
            client.list_busy_events(
                time_min=period.start,
                time_max=period.end,
                exclude_event_ids={b.id for b in blocks},
            )
        )
    except (Exception, SystemExit):  # noqa: BLE001
        # A review is a document, not a transaction: report what we have and
        # mark the rest unmeasured rather than failing the whole run. Any
        # calendar failure is the same to us, and `SystemExit` is named
        # explicitly because that's what missing credentials raise.
        calendar_ok = False

    journal = load(
        since=period.start - timedelta(days=JOURNAL_LOOKBACK_DAYS),
        until=period.end,
        modes=(MODE_SCHEDULE, MODE_SNAPSHOT, MODE_BACKFILL),
    )
    # Harvest before reading: a change you made since the last run should
    # appear in the review you're about to read. Never raises — a failed
    # harvest degrades to the history we already had.
    harvest(settings)
    changes = load_changes()

    return Facts(
        period=period,
        settings=settings,
        now=now,
        tasks=tasks,
        blocks=blocks,
        meetings=meetings,
        journal=journal,
        changes=changes,
        calendar_ok=calendar_ok,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
