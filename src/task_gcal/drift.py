"""Noticing when something outside task-gcal changed one of our blocks.

Every event we write carries a stamp of where we left it (`gcal.Expectation`).
Comparing that stamp against what the event says now tells us whether we're
looking at our own last decision or at somebody's edit — locally, off the
event itself, without reading a line of history. That matters twice over: it
keeps the scheduling path's "never consult your own past" rule intact, and a
hand-move stays detectable after the journal is deleted, or from a second
machine that has never seen it.

Only blocks that haven't ended yet count. A block in the past is history: we
don't move it and we don't report it. Dragging yesterday's block somewhere
else is a note about what happened, not a decision to react to — and if the
task is still open, the scheduler just finds it a fresh slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .gcal import BY_HUMAN, CalEvent, Expectation
from .intervals import same_instant

# What changed under us. Both can be true of one block at once, so a drift
# carries a set of these rather than a single verdict.
MOVED = "moved"
RETITLED = "retitled"


@dataclass(frozen=True)
class Drift:
    """One of our blocks, as we found it against how we left it."""

    event: CalEvent
    kinds: tuple[str, ...]
    # How we left it. The event itself says how it is now, so the pair
    # describes the whole change without consulting the last run.
    expectation: Expectation

    @property
    def moved(self) -> bool:
        return MOVED in self.kinds

    @property
    def expected_start(self) -> datetime:
        return self.expectation.start

    def adopted(self) -> Expectation:
        """The stamp that accepts what we found: this is the block now.

        A block someone moved becomes theirs, so later runs keep it there
        instead of reconsidering it every time — dragging something and having
        it spring back tomorrow would make the calendar unusable. A rename
        doesn't change whose position it is.
        """
        return Expectation(
            start=self.event.start,
            end=self.event.end,
            summary=self.event.summary,
            placed_by=BY_HUMAN if self.moved else self.expectation.placed_by,
        )

    def describe(self, tz) -> str:
        """A one-line account for the run report."""
        parts = []
        if self.moved:
            was = self.expected_start.astimezone(tz).strftime("%a %d %b %H:%M")
            now = self.event.start.astimezone(tz).strftime("%a %d %b %H:%M")
            parts.append(f"moved from {was} to {now} — kept")
        if RETITLED in self.kinds:
            parts.append("renamed — the task's title wins, so it goes back")
        return f"{self.event.summary or self.event.id} ({'; '.join(parts)})"


def is_pinned(event: CalEvent) -> bool:
    """True if this block's position is somebody's choice rather than ours.

    Placement asks this: a block a human put somewhere is one to keep, not one
    to reconsider. Two ways to be pinned, and both are needed — the run that
    first notices the move sees a stamp that still says we chose the old
    position, and every run after that sees the stamp we adopted.
    """
    return was_moved(event) or event.placed_by == BY_HUMAN


def was_moved(event: CalEvent) -> bool:
    """True if this block isn't where we last put it."""
    expectation = event.expectation
    if expectation is None:
        return False
    return not (
        same_instant(event.start, expectation.start)
        and same_instant(event.end, expectation.end)
    )


def detect(events: Iterable[CalEvent], *, now: datetime) -> list[Drift]:
    """Which of our unfinished blocks changed under us since we wrote them.

    An event with no stamp — written before we started stamping them — reads
    as unknown rather than as drift, so upgrading doesn't report a wave of
    hand-moves that never happened.
    """
    found: list[Drift] = []
    for event in events:
        if event.end <= now:
            continue
        expectation = event.expectation
        if expectation is None:
            continue
        kinds: list[str] = []
        if was_moved(event):
            kinds.append(MOVED)
        # A stamp from before summaries were recorded says nothing about the
        # title, and a task that was renamed in Taskwarrior isn't drift: the
        # stamp still holds the title *we* wrote, so only an edit to the event
        # itself can disagree with it.
        if expectation.summary is not None and event.summary != expectation.summary:
            kinds.append(RETITLED)
        if kinds:
            found.append(
                Drift(event=event, kinds=tuple(kinds), expectation=expectation)
            )
    return found
