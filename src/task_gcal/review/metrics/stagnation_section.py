"""The Stagnation section — the triage list, as a report line.

The list itself lives in `review/stagnation.py`, because `review --triage`
consumes the same entries and turns them into commands.
"""

from __future__ import annotations

from ..model import Section, Suggestion
from ..words import plural
from ..stagnation import find

KEY = "stuck"

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "Open tasks with the most signs they aren't happening: due dates pushed "
    "back again and again, blocks that passed without them, an estimate that "
    "doubled. Each needs a decision: do it, shrink it, hand it off, or "
    "delete it."
)

_TOP = 5


def build(facts) -> Section:
    timelines = facts.timelines
    entries = find(
        list(facts.tasks),
        timelines=timelines,
        blocks_by_task=facts.blocks_by_task(),
        now=facts.now,
    )
    pending = sum(1 for t in facts.tasks if t.status == "pending")
    recurring = sum(
        1 for t in facts.tasks if t.status == "pending" and t.is_recurring
    )
    if not entries:
        return Section(
            key=KEY,
            label="Stuck",
            summary="no open task looks stuck",
            data={"stagnant": 0, "recurring_excluded": recurring},
        )

    # Not "N tasks need a decision": ten things is not a decision, and a tool
    # telling its owner what he "needs" is the one register that's out. The
    # count is context for the closing line, which is where this section
    # actually speaks — see the suggestion below.
    summary = f"{len(entries)} of {pending} open tasks look stuck"
    detail = ["Most stuck first:"]
    detail.extend(f"  {entry.summary}" for entry in entries[:_TOP])
    if len(entries) > _TOP:
        detail.append(f"  ... and {len(entries) - _TOP} more")
    if recurring:
        detail.append(
            f"{plural(recurring, 'recurring task')} left out: staying open "
            "is normal for them."
        )
    detail.append(
        "`task-gcal review --triage` lists commands to resolve each one."
    )

    worst = entries[0]
    others = len(entries) - 1
    also = (
        f" {plural(others, 'other task')} "
        f"{'looks' if others == 1 else 'look'} stuck too; "
        "`--section stuck` lists them."
        if others
        else ""
    )
    suggestions = (
        Suggestion(
            f'"{worst.task.description}": {", ".join(worst.reasons)}. '
            "Do it, shrink it, hand it off, or delete it." + also,
            # Just under a repeat-offender deadline: the same evidence, but
            # phrased as a list rather than as the one decision to make.
            weight=3.8,
        ),
    )

    return Section(
        key=KEY,
        label="Stuck",
        summary=summary,
        detail=tuple(detail),
        data={
            "stagnant": len(entries),
            "pending": pending,
            "recurring_excluded": recurring,
            "entries": [
                {
                    "uuid": e.task.uuid,
                    "ref": e.task.ref,
                    "label": e.task.description,
                    "reasons": list(e.reasons),
                    "pushes": e.pushes,
                    "days_pushed": e.days_pushed,
                    "passed_blocks": e.passed_blocks,
                    "age_days": e.age_days,
                }
                for e in entries
            ],
        },
        suggestions=suggestions,
    )
