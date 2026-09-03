"""`task-gcal backfill`: seed the journal from history that already exists.

Without this, the first useful review is several weeks away. Taskwarrior's
per-task modification log holds due-date, estimate and scheduled-date changes
for recent tasks, so a one-time importer can reconstruct daily observations
for a period the journal never saw.

Three limits, stated here because every number derived from a backfilled day
inherits them:

- **No block history.** Past calendar events reveal only their final stored
  times, not the moves made before they occurred, so backfilled records carry
  no placements at all. Placement churn over a backfilled period is therefore
  correctly zero-observed rather than falsely quiet, and follow-through is
  computed from the calendar at review time instead.
- **Fields the log never mentions are assumed to have always held their
  current value.** Inventing a change would be worse than assuming stability.
- **Timestamps come from a local-zone rendering with no offset**, so a
  machine that has moved timezones is off by the difference.

It is read-only with respect to Taskwarrior and the calendar, it never
overwrites an existing record, and it is safe to re-run: days that already
have any observation are skipped, so a live snapshot always wins over a
reconstruction.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Optional

from .config import Settings
from .history import (
    FIELD_DUE,
    FIELD_ESTIMATE,
    FIELD_PROJECT,
    FIELD_SCHEDULED,
    FIELD_WAIT,
    KIND_SET,
    HistoryUnavailable,
    TaskHistory,
    load_history,
    parse_local_value,
)
from .journal import (
    MODE_BACKFILL,
    JournalWriteError,
    TaskObservation,
    append,
    build_record,
    detail_fields,
    load,
)
from .journal.observe import DETAIL_OFF
from .progress import Progress
from .taskw import TaskInfo, load_all_tasks

# How far back to reconstruct when `--since` isn't given. Long enough for a
# monthly review and a 12-week trend to have something to stand on, short
# enough that the import is one subprocess per *recent* task rather than per
# task ever.
DEFAULT_BACKFILL_DAYS = 90


@dataclass
class BackfillSummary:
    """What the import did, for the report and for the tests."""

    days_written: int = 0
    days_skipped: int = 0
    tasks_considered: int = 0
    histories_read: int = 0
    histories_failed: int = 0
    unrecognized_lines: int = 0


class _Timeline:
    """One task's reconstructed state over time.

    Built from the change log plus the task's current values, which are the
    fallback for any field the log never mentions.
    """

    def __init__(self, task: TaskInfo, history: TaskHistory, tz: tzinfo) -> None:
        self._task = task
        self._tz = tz
        self._history = history

    def _raw_at(self, field: str, when: datetime) -> tuple[bool, Optional[str]]:
        """`(the log knows, raw value)` for `field` at `when`.

        Three cases, and the third is the one worth spelling out:

        - The log never mentions the field: it doesn't know, and the caller
          falls back to the value the task holds now.
        - Some change is at or before `when`: that change's new value.
        - Every change is *after* `when`: the earliest one still tells us
          what came before it — the old value it moved away from, or nothing
          at all if it was set from scratch.
        """
        changes = self._history.of(field)
        if not changes:
            return False, None
        latest = None
        for change in changes:
            if change.at <= when:
                latest = change
            else:
                break  # changes are sorted
        if latest is not None:
            return True, latest.new
        first = changes[0]
        return True, None if first.kind == KIND_SET else first.old

    def _timestamp_at(
        self, field: str, when: datetime, current: Optional[datetime]
    ) -> Optional[datetime]:
        known, raw = self._raw_at(field, when)
        if not known:
            return current
        return parse_local_value(raw, self._tz)

    def _estimate_at(self, when: datetime) -> Optional[int]:
        known, raw = self._raw_at(FIELD_ESTIMATE, when)
        if not known:
            return self._task.estimate_minutes
        if raw is None:
            return None
        try:
            minutes = int(round(float(raw)))
        except ValueError:
            return None
        return minutes if minutes > 0 else None

    def _project_at(self, when: datetime) -> Optional[str]:
        known, raw = self._raw_at(FIELD_PROJECT, when)
        return self._task.project if not known else raw

    def was_open(self, when: datetime) -> bool:
        """True if the task existed and had not yet closed at `when`."""
        entry = self._task.entry
        if entry is not None and entry > when:
            return False
        end = self._task.end
        return end is None or end > when

    def observe(self, when: datetime, *, detail: str) -> TaskObservation:
        """The task's state at `when`, as a journal observation.

        No block: see the module docstring. `urgency` is left unset rather
        than guessed — it's a function of the clock, and a reconstructed
        value would be a number nobody computed.
        """
        # The log records *that* a description changed, never the text, so
        # the current title is all we have for any past moment.
        description, description_hash = detail_fields(
            self._task.description, detail
        )
        return TaskObservation(
            uuid=self._task.uuid,
            id=self._task.id,
            description=description,
            description_hash=description_hash,
            project=self._project_at(when),
            tags=tuple(self._task.tags),
            estimate_minutes=self._estimate_at(when),
            due=self._timestamp_at(FIELD_DUE, when, self._task.due),
            scheduled=self._timestamp_at(
                FIELD_SCHEDULED, when, self._task.scheduled
            ),
            wait=self._timestamp_at(FIELD_WAIT, when, self._task.wait),
            urgency=None,
            status="pending",
            entry=self._task.entry,
            end=self._task.end,
            overrides=self._task.overrides_raw,
            block=None,
        )


def _observation_times(
    since: datetime, until: datetime, tz: tzinfo
) -> list[datetime]:
    """One reconstruction point per local day, at the end of that day.

    End of day rather than start: it captures every change made during the
    day, which is what a snapshot run late in the day would have seen.

    The last *instant* of the day rather than the midnight after it, so the
    record's own local date is the day it describes. At exact midnight it
    would belong to the following day, and a Sunday observation would land
    in the next week.
    """
    times: list[datetime] = []
    day = since.astimezone(tz).date()
    last = until.astimezone(tz).date()
    while day <= last:
        last_instant = (
            datetime.combine(day, time(0, 0), tzinfo=tz)
            + timedelta(days=1)
            - timedelta(seconds=1)
        )
        moment = min(last_instant.astimezone(timezone.utc), until)
        if moment > since:
            times.append(moment)
        day += timedelta(days=1)
    return times


def days_already_observed(since: datetime, tz: tzinfo) -> set[date]:
    """Local days the journal already has any record for.

    Any record, not just a backfilled one: a live snapshot is better data
    than a reconstruction, so backfill only ever fills gaps.

    Deliberately unbounded above. The window's last observation is timestamped
    at `now` exactly, and `load`'s `until` is exclusive — bounding it there
    would hide today's own record and re-import it on every run.
    """
    existing = load(since=since)
    return {r.at.astimezone(tz).date() for r in existing.records}


def relevant_tasks(tasks: list[TaskInfo], since: datetime) -> list[TaskInfo]:
    """Tasks that were open at some point in the window.

    A task closed before the window can't appear in any of its
    observations, and fetching its history would be one subprocess spent on
    nothing.
    """
    return [t for t in tasks if t.end is None or t.end >= since]


def backfill(
    settings: Settings,
    *,
    since: Optional[datetime] = None,
    now: Optional[datetime] = None,
    show_progress: bool = True,
) -> tuple[int, BackfillSummary]:
    """Reconstruct daily observations for a past window. Read-only upstream."""
    summary = BackfillSummary()
    if settings.journal_detail == DETAIL_OFF:
        print('Journal is off (journal_detail = "off"); nothing to backfill.')
        return 0, summary

    tz = settings.resolve_timezone()
    now = now or datetime.now(timezone.utc)
    since = since or (now - timedelta(days=DEFAULT_BACKFILL_DAYS))
    if since >= now:
        print("Nothing to backfill: --since is not in the past.")
        return 0, summary

    candidates = relevant_tasks(
        load_all_tasks(
            estimate_uda=settings.estimate_uda,
            override_uda=settings.override_uda,
        ),
        since,
    )
    summary.tasks_considered = len(candidates)

    # One subprocess per task, which is why this is a one-time import and
    # not the analytics API.
    progress = Progress(
        total=len(candidates),
        label="Reading Taskwarrior history…",
        enabled=show_progress,
    )
    timelines: list[_Timeline] = []
    for task in candidates:
        progress.advance(task.description[:48])
        try:
            history = load_history(
                task.uuid, tz=tz, estimate_uda=settings.estimate_uda
            )
        except HistoryUnavailable as e:
            summary.histories_failed += 1
            print(f"  ! skipping {task.ref}: {e}")
            continue
        summary.histories_read += 1
        summary.unrecognized_lines += history.unrecognized
        timelines.append(_Timeline(task, history, tz))
    progress.close()

    already = days_already_observed(since, tz)
    # Backfilled records describe a period that predates the settings in
    # force now, so they must not masquerade as observations of them. The
    # mode already says `backfill`; blanking the report name keeps a review
    # from attributing the reconstruction to today's population filter.
    record_settings = replace(settings, report="")

    for moment in _observation_times(since, now, tz):
        day = moment.astimezone(tz).date()
        if day in already:
            summary.days_skipped += 1
            continue
        observations = tuple(
            tl.observe(moment, detail=settings.journal_detail)
            for tl in timelines
            if tl.was_open(moment)
        )
        record = build_record(
            settings=record_settings,
            mode=MODE_BACKFILL,
            at=moment,
            observations=observations,
        )
        try:
            append(record)
        except JournalWriteError as e:
            # Unlike a scheduling run, writing is the whole job here.
            print(f"Backfill failed: {e}")
            return 1, summary
        summary.days_written += 1

    _print_summary(summary, since=since, now=now, tz=tz)
    return 0, summary


def _print_summary(
    summary: BackfillSummary, *, since: datetime, now: datetime, tz: tzinfo
) -> None:
    span = (
        f"{since.astimezone(tz):%Y-%m-%d} to {now.astimezone(tz):%Y-%m-%d}"
    )
    print(f"Backfilled {span}")
    print(
        f"  {summary.days_written} day(s) written, "
        f"{summary.days_skipped} already observed"
    )
    print(
        f"  {summary.histories_read} task histories read"
        + (
            f", {summary.histories_failed} unreadable"
            if summary.histories_failed
            else ""
        )
    )
    if summary.unrecognized_lines:
        print(
            f"  {summary.unrecognized_lines} modification line(s) not "
            "recognized (annotations, tags and priorities are expected here)"
        )
    if summary.days_written:
        print(
            "  Backfilled days carry no calendar blocks — past events show "
            "only their\n  final times, so placement churn starts from the "
            "journal, not from here."
        )
