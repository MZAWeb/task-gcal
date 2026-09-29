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
from ..words import plural, times
from ..placements import BY_HUMAN, build_blocks

KEY = "rescheduling"


def cause_words(cause: str) -> str:
    """A recorded move reason, in plain words."""
    return _CAUSES.get(cause, cause)

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "How often blocks were moved before their time came, and why: a meeting "
    "landed on one, task-gcal found a better slot, or you moved it yourself. "
    "Moves are only seen on days task-gcal runs."
)

# The recorded reasons, in the words you'd use. Anything not listed is shown
# as recorded, so a new reason is never hidden.
_CAUSES = {
    "overlaps a calendar event": "a meeting landed on it",
    "replanned": "task-gcal found a better slot",
    "you moved it": "you moved it",
    "outside working hours": "it fell outside working hours",
    "estimate changed": "its estimate changed",
    "ends after its due date": "its due date moved earlier",
    "starts before its scheduled/wait date": "its start date moved later",
}

# How many times one block has to move before it's worth naming.
_RESTLESS = 3

# How many blocks to name before summarising the rest.
_TOP_BLOCKS = 5


def build(facts) -> Section:
    records = facts.journal.records
    if not records:
        return Section(
            key=KEY,
            label="Rescheduling",
            summary=f"task-gcal didn't run this {facts.period.kind}",
            measured=False,
            detail=(
                "Blocks are only moved, and moves only seen, when task-gcal "
                "runs, so nothing is known about this "
                f"{facts.period.kind}. That's not the same as nothing moving.",
            ),
            data={"moves": 0},
        )

    window = (facts.period.start, facts.period.end)
    blocks = build_blocks(records)
    if not blocks and _predates_placements(records):
        # Runs recorded before the journal held placements. Reporting "nothing
        # moved" here would turn missing data into a calm week.
        return Section(
            key=KEY,
            label="Rescheduling",
            summary="not recorded yet for this period",
            measured=False,
            detail=(
                "These runs happened before task-gcal started recording where "
                "each block was, so how often blocks moved is unknown here. "
                "Runs from now on record it.",
            ),
            data={"moves": 0},
        )
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
        summary = f"nothing moved ({plural(len(live), 'block')} booked)"
    else:
        summary = (
            f"{len(moved_blocks)} of {plural(len(live), 'block')} moved, "
            f"{plural(len(moves), 'move')} in all"
        )

    detail = [
        f"Blocks            {len(live)}",
        f"Blocks moved      {len(moved_blocks)}",
        f"Moves             {len(moves)}",
    ]
    if causes:
        detail.append("Why they moved:")
        for cause, count in causes.most_common():
            detail.append(f"  {count:>3}  {cause_words(cause)}")
    if by_human:
        detail.append(
            f"Moved by you      at least {len(by_human)}; a move you make is "
            "only noticed at the next run"
        )

    detail.extend(_restless(blocks, window, facts))

    return Section(
        key=KEY,
        label="Rescheduling",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="days task-gcal ran, so moves on the others are missed",
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


def _predates_placements(records) -> bool:
    """True if these records come from before the placement log existed.

    Recognised by what they *did* carry: per-task observations, which a current
    reader keeps in `unknown` rather than discarding.
    """
    return any("tasks" in r.unknown for r in records)


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
    out = ["Moved most:"]
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
            f'"{facts.label_for(worst.task_uuid)}" was moved '
            f"{times(worst_count)} this {facts.period.kind}. Book it at a "
            "time you'll protect, or take it off the plan.",
            weight=3.6,
        ),
    )
