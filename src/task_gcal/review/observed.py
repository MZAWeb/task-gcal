"""Per-task change series, from Taskwarrior's own record of what changed.

Every churn metric is a series of field transitions, and this is the one place
those get assembled — so "when did this move" means the same thing everywhere.

These are **exact**, not sampled. They come from Taskwarrior's operation log by
way of `changes/`, so a due date that moved twice is two changes and one that
moved out and back is two more. That wasn't true of the sampled snapshots this
replaced, where both cases collapsed into one observation or none — which is why
the metrics no longer have to describe themselves as a lower bound.

What is still bounded is *reach*: nothing is known before the earliest harvested
change, and if operations were pruned before we saw them there is a hole. Both
are reported as coverage rather than smoothed over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, Iterable, Optional, TypeVar

from ..changes import (
    FIELD_DESCRIPTION,
    FIELD_DUE,
    FIELD_ESTIMATE,
    FIELD_SCHEDULED,
    FIELD_WAIT,
    TaskChange,
)

T = TypeVar("T")


@dataclass(frozen=True)
class Change(Generic[T]):
    """One transition of one field, at the instant it happened."""

    at: datetime
    previous: Optional[T]
    current: Optional[T]

    @property
    def is_later(self) -> bool:
        """For dates: did the value move into the future?"""
        return (
            isinstance(self.previous, datetime)
            and isinstance(self.current, datetime)
            and self.current > self.previous
        )

    @property
    def is_earlier(self) -> bool:
        return (
            isinstance(self.previous, datetime)
            and isinstance(self.current, datetime)
            and self.current < self.previous
        )

    @property
    def days(self) -> float:
        """Signed days moved, for date changes; 0 for anything else."""
        if not isinstance(self.previous, datetime) or not isinstance(
            self.current, datetime
        ):
            return 0.0
        return (self.current - self.previous).total_seconds() / 86400


@dataclass
class Timeline:
    """Everything the journal saw happen to one task."""

    uuid: str
    # The last title we have a record of. May be a digest under
    # `journal_detail = "minimal"`, so ask `Facts.label_for` for anything a
    # person is going to read.
    label: str = ""
    redacted: bool = False
    project: Optional[str] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    due_changes: list[Change[datetime]] = field(default_factory=list)
    scheduled_changes: list[Change[datetime]] = field(default_factory=list)
    wait_changes: list[Change[datetime]] = field(default_factory=list)
    estimate_changes: list[Change[int]] = field(default_factory=list)
    title_changes: list[Change[str]] = field(default_factory=list)
    # The value each field held before its earliest recorded change — the
    # original promise, for the ledger. None when nothing was ever recorded,
    # which is not the same as "it had no due date".
    first_due: Optional[datetime] = None
    last_due: Optional[datetime] = None
    first_estimate: Optional[int] = None
    last_estimate: Optional[int] = None

    # ------------------------------ queries -------------------------------

    def pushes(self, *, within: Optional[tuple[datetime, datetime]] = None):
        """Observed moves of the due date into the future."""
        return [c for c in self._in(self.due_changes, within) if c.is_later]

    def pulls(self, *, within=None):
        return [c for c in self._in(self.due_changes, within) if c.is_earlier]

    def reactive_pushes(self, *, within=None):
        """Pushes noticed only after the old deadline had already passed.

        Not automatically worse than a push made in advance, but a different
        behaviour: one renegotiates a commitment, the other reports a miss.
        """
        return [
            c
            for c in self.pushes(within=within)
            if c.previous is not None and c.previous < c.at
        ]

    def proactive_pushes(self, *, within=None):
        return [
            c
            for c in self.pushes(within=within)
            if c.previous is not None and c.previous >= c.at
        ]

    def retitles(self, *, within=None):
        """Observed title rewrites — a task being redefined in place."""
        return self._in(self.title_changes, within)

    def deferrals(self, *, within=None):
        """Observed `scheduled`/`wait` moves.

        Deliberate deferral, kept apart from a deadline slipping: choosing
        not to start something yet is a different act from missing a date.
        """
        return self._in(self.scheduled_changes, within) + self._in(
            self.wait_changes, within
        )

    def upward_estimate_changes(self, *, within=None):
        return [
            c
            for c in self._in(self.estimate_changes, within)
            if c.previous is not None
            and c.current is not None
            and c.current > c.previous
        ]

    @property
    def days_pushed(self) -> float:
        return sum(c.days for c in self.pushes())

    @staticmethod
    def _in(changes: Iterable[Change], within) -> list[Change]:
        if within is None:
            return list(changes)
        low, high = within
        return [c for c in changes if low <= c.at < high]


# Which harvested field lands in which bucket. Anything absent is kept in the
# store but not yet asked about by a metric.
_BUCKETS = {
    FIELD_DUE: "due_changes",
    FIELD_SCHEDULED: "scheduled_changes",
    FIELD_WAIT: "wait_changes",
    FIELD_ESTIMATE: "estimate_changes",
    FIELD_DESCRIPTION: "title_changes",
}


def build_timelines(changes: Iterable[TaskChange]) -> dict[str, Timeline]:
    """Assemble per-task series from harvested field changes.

    Each change already carries both sides of the transition, so nothing is
    inferred by comparing states — which is what makes these exact rather than
    a lower bound.
    """
    timelines: dict[str, Timeline] = {}
    for change in sorted(changes, key=lambda c: (c.at, c.uuid, c.field)):
        bucket_name = _BUCKETS.get(change.field)
        timeline = timelines.setdefault(change.uuid, Timeline(uuid=change.uuid))
        _widen_span(timeline, change.at)

        if change.field == FIELD_DESCRIPTION:
            # The current title, for display. A task always gets a description
            # set when it is created, so this is populated for every task we
            # have any history for at all.
            current = change.value_at()
            if isinstance(current, str):
                timeline.label = current
                timeline.redacted = change.redacted
        elif change.field == "project":
            project = change.value_at()
            if isinstance(project, str):
                timeline.project = project

        if bucket_name is None:
            continue

        previous, current = change.value_at(before=True), change.value_at()
        if change.field == FIELD_DUE:
            _remember_first(timeline, "first_due", previous, current)
            timeline.last_due = current
        elif change.field == FIELD_ESTIMATE:
            _remember_first(timeline, "first_estimate", previous, current)
            timeline.last_estimate = current

        if change.field == FIELD_DESCRIPTION and (
            previous is None or current is None
        ):
            # Setting a title on creation is not a rewrite.
            continue

        getattr(timeline, bucket_name).append(
            Change(at=change.at, previous=previous, current=current)
        )
    return timelines


def _widen_span(timeline: Timeline, at: datetime) -> None:
    if timeline.first_seen is None or at < timeline.first_seen:
        timeline.first_seen = at
    if timeline.last_seen is None or at > timeline.last_seen:
        timeline.last_seen = at


def _remember_first(timeline: Timeline, attribute: str, previous, current) -> None:
    """Record the value a field held before we ever saw it change.

    Only on the earliest change: `previous` is what it was before, and when
    that's None the field was being set for the first time, so `current` is the
    original value.
    """
    if getattr(timeline, attribute) is not None:
        return
    setattr(timeline, attribute, previous if previous is not None else current)


def total_observed(timelines: dict[str, Timeline], attribute: str, **kwargs) -> Any:
    """Sum a per-timeline query across every task. Convenience for metrics."""
    return sum(len(getattr(t, attribute)(**kwargs)) for t in timelines.values())
