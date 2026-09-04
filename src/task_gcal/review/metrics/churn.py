"""Plan churn — how often blocks moved, and what moved them.

The count on its own is close to meaningless, which is why this section is
mostly a breakdown. A block that moved because a meeting landed on it says
something about the week; one that moved because *you* dragged it says
something about the plan; one that moved because it wasn't settled yet says
only that the scheduler is doing its job. Reporting "37 moves" without that
split would invite exactly the wrong conclusion.

Two asymmetries are stated rather than smoothed over. Moves the scheduler made
are exact — they only happen during a run, and every run records what it did.
Moves you made are seen once per run, so that half is a lower bound. And a move
is only visible at all if a run happened between the two positions, which is
what the coverage line is for.
"""

from __future__ import annotations

from collections import Counter

from ..model import Coverage, Section, Suggestion
from ..placements import BY_HUMAN, build_blocks

KEY = "churn"

# How many times one block has to move before it's worth naming.
_RESTLESS = 3

# How many blocks to name before summarising the rest.
_TOP_BLOCKS = 5


def build(facts) -> Section:
    records = facts.journal.records
    if not records:
        return Section(
            key=KEY,
            label="Plan churn",
            summary="no scheduling runs recorded for this period",
            measured=False,
            detail=(
                "Blocks only move during a run, so a period with no runs has "
                "nothing to say about churn — which is not the same as a calm "
                "one.",
            ),
            data={"moves": 0},
        )

    window = (facts.period.start, facts.period.end)
    blocks = build_blocks(records)
    moves = [m for b in blocks.values() for m in b.moves_in(window)]
    # Blocks that existed during the period, whether or not they moved: the
    # denominator, so "6 moves" can be read against how many blocks there were.
    live = [
        b
        for b in blocks.values()
        if b.end > facts.period.start and b.start < facts.period.end
    ]
    moved_blocks = {m.event_id for m in moves}

    by_human = [m for m in moves if m.by_human]
    causes = Counter(m.cause for m in moves)

    if not moves:
        summary = f"nothing moved · {len(live)} block(s)"
    else:
        summary = (
            f"{len(moves)} move(s) across {len(moved_blocks)} of "
            f"{len(live)} block(s)"
        )

    detail = [
        f"Blocks in the period   {len(live)}",
        f"Blocks that moved      {len(moved_blocks)}",
        f"Moves                  {len(moves)}",
    ]
    if causes:
        detail.append("Why:")
        for cause, count in causes.most_common():
            detail.append(f"  {count:>3}  {cause}")
    if by_human:
        detail.append(
            f"Your own moves         {len(by_human)} — counted once per run, "
            "so this is a floor"
        )

    detail.extend(_restless(blocks, window, facts))

    return Section(
        key=KEY,
        label="Plan churn",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="days in the period a run observed",
                observed=len(facts.observed_days()),
                total=len(facts.period.days()),
            ),
        ),
        data={
            "moves": len(moves),
            "blocks": len(live),
            "blocks_moved": len(moved_blocks),
            "by_human": len(by_human),
            "causes": dict(causes.most_common()),
        },
        suggestions=_suggest(blocks, window, facts),
    )


def _restless(blocks, window, facts) -> list[str]:
    """The blocks that moved most, named so they can be dealt with."""
    counted = sorted(
        ((b, len(b.moves_in(window))) for b in blocks.values()),
        key=lambda pair: pair[1],
        reverse=True,
    )
    counted = [(b, n) for b, n in counted if n > 1]
    if not counted:
        return []
    out = ["Most-moved blocks:"]
    for block, count in counted[:_TOP_BLOCKS]:
        mine = sum(1 for m in block.moves_in(window) if m.by == BY_HUMAN)
        share = f" ({mine} by you)" if mine else ""
        out.append(f"  {count}x{share}  {facts.label_for(block.task_uuid)}")
    if len(counted) > _TOP_BLOCKS:
        out.append(f"  ... and {len(counted) - _TOP_BLOCKS} more")
    return out


def _suggest(blocks, window, facts) -> tuple[Suggestion, ...]:
    """One suggestion, and only for a block that keeps moving.

    A block that has been moved this many times isn't scheduled — it's being
    carried. Naming it is more useful than any average.
    """
    worst = None
    worst_count = 0
    for block in blocks.values():
        count = len(block.moves_in(window))
        if count > worst_count:
            worst, worst_count = block, count
    if worst is None or worst_count < _RESTLESS:
        return ()
    return (
        Suggestion(
            f'"{facts.label_for(worst.task_uuid)}" has been moved '
            f"{worst_count} times this {facts.period.kind} — it isn't "
            "scheduled, it's being carried. Give it a slot you'll defend or "
            "take it out of the plan.",
            weight=3.6,
        ),
    )
