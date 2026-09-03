"""Per-task change series, derived from consecutive journal observations.

Every churn metric is a *diff between two observations*, and this is the one
place that diffing happens — so "observed" means the same thing everywhere and
the sampling caveat only has to be written once.

What that word buys, and costs:

- If a due date moves twice between two runs, we see one move.
- If it moves out and back, we see none.
- A field's value before its first observation is unknown, not absent.

So every number built on this says **observed**, and reports the period it
sampled. It is a lower bound whose tightness depends on how often `snapshot`
ran, which is why cadence is a feature rather than a caveat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Generic, Iterable, Optional, TypeVar

from ..journal import MODE_BACKFILL, RunRecord, TaskObservation

T = TypeVar("T")


@dataclass(frozen=True)
class Change(Generic[T]):
    """One observed transition of one field.

    `at` is when we *noticed*, not when it happened — the change occurred
    somewhere in the gap since the previous observation.
    """

    at: datetime
    previous: Optional[T]
    current: Optional[T]
    # The observation that preceded this one, so a metric can ask "had the
    # old deadline already passed when this was noticed?"
    observed_after: Optional[datetime] = None

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
    label: str = ""
    project: Optional[str] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    due_changes: list[Change[datetime]] = field(default_factory=list)
    scheduled_changes: list[Change[datetime]] = field(default_factory=list)
    wait_changes: list[Change[datetime]] = field(default_factory=list)
    estimate_changes: list[Change[int]] = field(default_factory=list)
    title_changes: list[Change[str]] = field(default_factory=list)
    # Where the block sat at each observation, so a move is a difference
    # between consecutive entries rather than something we have to be told.
    placements: list[tuple[datetime, datetime, datetime]] = field(
        default_factory=list
    )
    first_due: Optional[datetime] = None
    last_due: Optional[datetime] = None
    first_estimate: Optional[int] = None
    last_estimate: Optional[int] = None
    # True when some of this task's history came from `backfill`, which has
    # no block observations at all.
    reconstructed: bool = False

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

    def placement_moves(self, *, within=None) -> int:
        """How many times the block was observed at a different start.

        Zero for a task whose history is reconstructed: backfill carries no
        placements, so "no moves observed" there means unobserved, not still.
        """
        moves = 0
        previous: Optional[datetime] = None
        for at, start, _end in self.placements:
            if within is not None and not (within[0] <= at < within[1]):
                previous = start
                continue
            if previous is not None and start != previous:
                moves += 1
            previous = start
        return moves

    @property
    def days_pushed(self) -> float:
        return sum(c.days for c in self.pushes())

    @staticmethod
    def _in(changes: Iterable[Change], within) -> list[Change]:
        if within is None:
            return list(changes)
        low, high = within
        return [c for c in changes if low <= c.at < high]


def _title_key(obs: TaskObservation) -> Optional[str]:
    """What to compare titles by, whatever the journal detail level was."""
    return obs.description if obs.description is not None else obs.description_hash


def build_timelines(records: Iterable[RunRecord]) -> dict[str, Timeline]:
    """Walk observations in order, recording every value that changed.

    Only consecutive observations *of the same task* are compared. A task
    that leaves the report and comes back is compared against the last time
    we saw it, which is the honest reading of "observed": we don't know when
    in the gap it changed, only that it did.
    """
    timelines: dict[str, Timeline] = {}
    previous: dict[str, tuple[datetime, TaskObservation]] = {}

    for record in sorted(records, key=lambda r: r.at):
        for obs in record.tasks:
            timeline = timelines.setdefault(obs.uuid, Timeline(uuid=obs.uuid))
            timeline.label = obs.label
            if obs.project is not None:
                timeline.project = obs.project
            if timeline.first_seen is None:
                timeline.first_seen = record.at
                timeline.first_due = obs.due
                timeline.first_estimate = obs.estimate_minutes
            timeline.last_seen = record.at
            timeline.last_due = obs.due
            timeline.last_estimate = obs.estimate_minutes
            if record.mode == MODE_BACKFILL:
                timeline.reconstructed = True

            if obs.block is not None:
                timeline.placements.append(
                    (record.at, obs.block.start, obs.block.end)
                )

            seen = previous.get(obs.uuid)
            if seen is not None:
                _diff(timeline, at=record.at, before=seen, after=obs)
            previous[obs.uuid] = (record.at, obs)

    return timelines


def _diff(
    timeline: Timeline,
    *,
    at: datetime,
    before: tuple[datetime, TaskObservation],
    after: TaskObservation,
) -> None:
    previous_at, old = before
    for attribute, bucket in (
        ("due", timeline.due_changes),
        ("scheduled", timeline.scheduled_changes),
        ("wait", timeline.wait_changes),
        ("estimate_minutes", timeline.estimate_changes),
    ):
        old_value = getattr(old, attribute)
        new_value = getattr(after, attribute)
        if old_value != new_value:
            bucket.append(
                Change(
                    at=at,
                    previous=old_value,
                    current=new_value,
                    observed_after=previous_at,
                )
            )

    old_title, new_title = _title_key(old), _title_key(after)
    if old_title != new_title and old_title is not None and new_title is not None:
        timeline.title_changes.append(
            Change(
                at=at,
                previous=old_title,
                current=new_title,
                observed_after=previous_at,
            )
        )


def sampling_note(records: list[RunRecord]) -> str:
    """One sentence describing how densely the period was sampled."""
    if not records:
        return "no observations"
    if len(records) == 1:
        return "sampled once"
    span = records[-1].at - records[0].at
    gap = span / max(len(records) - 1, 1)
    return f"sampled every {_round_gap(gap)} on average"


def _round_gap(gap: timedelta) -> str:
    hours = gap.total_seconds() / 3600
    if hours < 1:
        return f"{int(gap.total_seconds() // 60)}m"
    if hours < 36:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


def total_observed(timelines: dict[str, Timeline], attribute: str, **kwargs) -> Any:
    """Sum a per-timeline query across every task. Convenience for metrics."""
    return sum(len(getattr(t, attribute)(**kwargs)) for t in timelines.values())
