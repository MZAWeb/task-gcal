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

from ..config import Settings
from ..gcal import CalEvent, GCal
from ..journal import JournalRead, load
from ..journal import MODE_BACKFILL, MODE_SCHEDULE, MODE_SNAPSHOT
from ..taskw import TaskInfo, load_all_tasks
from .periods import Period

# How far before the period to pull our own blocks. Blocks-to-completion and
# stagnation both ask "how many blocks came before this?", which needs blocks
# that predate the window being reported on.
BLOCK_LOOKBACK_DAYS = 180


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
    # Set when a source failed. A metric that depends on it must report
    # itself unmeasured rather than treat the gap as zero.
    calendar_ok: bool = True

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

    def backfilled_days(self) -> set:
        return {
            r.at.astimezone(self.period.tz).date()
            for r in self.records_in_period()
            if r.mode == MODE_BACKFILL
        }


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
    client = gcal if gcal is not None else GCal(settings)
    try:
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
    except Exception:  # noqa: BLE001 - any calendar failure is the same to us
        # A review is a document, not a transaction: report what we have and
        # mark the rest unmeasured rather than failing the whole run.
        calendar_ok = False

    journal = load(
        since=period.start,
        until=period.end,
        modes=(MODE_SCHEDULE, MODE_SNAPSHOT, MODE_BACKFILL),
    )

    return Facts(
        period=period,
        settings=settings,
        now=now,
        tasks=tasks,
        blocks=blocks,
        meetings=meetings,
        journal=journal,
        calendar_ok=calendar_ok,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
