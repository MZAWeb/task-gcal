"""The Stagnation section — the triage list, as a report line.

The list itself lives in `review/stagnation.py`, because `review --triage`
consumes the same entries and turns them into commands.
"""

from __future__ import annotations

from ..model import Coverage, Section, Suggestion
from ..stagnation import find

KEY = "stagnation"

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
    examined = Coverage(
        label="pending tasks examined (recurring ones excluded)",
        observed=pending - recurring,
        total=pending,
    )

    if not entries:
        return Section(
            key=KEY,
            label="Stagnation",
            summary="nothing has accumulated enough evidence against it",
            coverage=(examined,),
            data={"stagnant": 0, "recurring_excluded": recurring},
        )

    summary = f"{len(entries)} of {pending} open task(s) need a decision"
    detail = [f"  {entry.summary}" for entry in entries[:_TOP]]
    if len(entries) > _TOP:
        detail.append(f"  ... and {len(entries) - _TOP} more")
    if recurring:
        detail.append(
            f"{recurring} recurring task(s) excluded: their dates are "
            "generated, so staying open is what they're for."
        )
    detail.append("`task-gcal review --triage` prints commands for each.")

    worst = entries[0]
    suggestions = (
        Suggestion(
            f'"{worst.task.description}" — {", ".join(worst.reasons)}. '
            "Do it, shrink it, hand it off, or kill it.",
            # Just under a repeat-offender deadline: the same evidence, but
            # phrased as a list rather than as the one decision to make.
            weight=3.8,
        ),
    )

    return Section(
        key=KEY,
        label="Stagnation",
        summary=summary,
        detail=tuple(detail),
        coverage=(examined,),
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
