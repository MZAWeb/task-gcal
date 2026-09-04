"""Per-block histories, from the run journal's placement log.

The one place that turns a sequence of runs into "this block moved, then moved
again". Metrics ask questions of it; nothing here decides what a move *means*.

That split is deliberate and it runs the other way too. The writer records raw
transitions — where the block was, where it went, what reason the scheduler
gave, whether it had drifted first — and never a classification. Classifying at
write time would bake today's taxonomy into a file we can't re-derive; doing it
here means a better classification tomorrow reclassifies the whole history.

Identity is the Google event id, never the task. A block deleted and recreated
somewhere else is a new block, and merging the two would report one move that
never happened while hiding the replacement that did.

Exactness: moves the scheduler made are exact, because they only happen during
a run and every run records the result. Moves *you* made are seen once per run —
drag a block three times between two runs and the log has one move, from where
we left it to where it ended up. So the count of your own moves is a lower
bound, and it's reported as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from ..drift import MOVED
from ..journal import PlacementObservation, RunRecord

# Who moved a block. Not stored anywhere — derived from whether the run found
# the block already moved before it did anything.
BY_SCHEDULER = "scheduler"
BY_HUMAN = "human"

# What the scheduler was doing when no reason was recorded. A settled block that
# has to yield carries `moved_reason`; an unsettled one is simply replanned,
# which is the scheduler working as intended rather than a disruption.
REPLANNED = "replanned"


@dataclass(frozen=True)
class Move:
    """One block changing position, as one run saw it."""

    at: datetime
    event_id: str
    task_uuid: str
    frm: datetime
    to: datetime
    by: str
    # Why, when the scheduler moved it. None for a move somebody else made —
    # we don't know why you moved it and won't guess.
    reason: Optional[str] = None

    @property
    def by_human(self) -> bool:
        return self.by == BY_HUMAN

    @property
    def hours(self) -> float:
        """Signed hours moved. Negative means it came earlier."""
        return (self.to - self.frm).total_seconds() / 3600

    @property
    def cause(self) -> str:
        """A label for grouping: the recorded reason, or who moved it."""
        if self.by_human:
            return "you moved it"
        return self.reason or REPLANNED


@dataclass
class BlockHistory:
    """Everything the journal saw happen to one calendar block."""

    event_id: str
    task_uuid: str
    first_seen: datetime
    last_seen: datetime
    start: datetime
    end: datetime
    moves: list[Move] = field(default_factory=list)

    def moves_in(self, window: Optional[tuple[datetime, datetime]] = None):
        if window is None:
            return list(self.moves)
        low, high = window
        return [m for m in self.moves if low <= m.at < high]


def build_blocks(records: Iterable[RunRecord]) -> dict[str, BlockHistory]:
    """Assemble one history per event from the runs that mention it."""
    blocks: dict[str, BlockHistory] = {}
    for record in sorted(records, key=lambda r: r.at):
        for placement in record.placements:
            block = blocks.get(placement.event_id)
            if block is None:
                blocks[placement.event_id] = _first_sighting(record, placement)
                continue
            block.moves.extend(_moves_between(block, record.at, placement))
            block.last_seen = record.at
            block.start, block.end = placement.start, placement.end
    return blocks


def _first_sighting(
    record: RunRecord, placement: PlacementObservation
) -> BlockHistory:
    return BlockHistory(
        event_id=placement.event_id,
        task_uuid=placement.task_uuid,
        first_seen=record.at,
        last_seen=record.at,
        start=placement.start,
        end=placement.end,
    )


def _moves_between(
    block: BlockHistory, at: datetime, placement: PlacementObservation
) -> list[Move]:
    """The moves one record implies for a block we've already seen.

    Up to two, and both can happen in one run: somebody moved the block, and
    then the run moved it again because where they'd put it no longer worked.
    Keeping them apart is the point — "it moved twice" with one move yours and
    one ours is a different week from either alone.
    """
    moves: list[Move] = []
    position = block.start

    if MOVED in placement.drift:
        # Where we found it. `drifted_to` is only written when the run went on
        # to move the block again, so its absence means "where it ended up".
        found = placement.drifted_to or placement.start
        if found != position:
            moves.append(
                _move(block, at=at, frm=position, to=found, by=BY_HUMAN)
            )
            position = found

    if placement.start != position:
        moves.append(
            _move(
                block,
                at=at,
                frm=position,
                to=placement.start,
                by=BY_SCHEDULER,
                reason=placement.moved_reason,
            )
        )
    return moves


def _move(
    block: BlockHistory,
    *,
    at: datetime,
    frm: datetime,
    to: datetime,
    by: str,
    reason: Optional[str] = None,
) -> Move:
    return Move(
        at=at,
        event_id=block.event_id,
        task_uuid=block.task_uuid,
        frm=frm,
        to=to,
        by=by,
        reason=reason,
    )
