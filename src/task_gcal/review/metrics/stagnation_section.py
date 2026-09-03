"""The Stagnation section — the triage list, as a report line.

The list itself lives in `review/stagnation.py`, because `review --triage`
consumes the same entries and turns them into commands.
"""

from __future__ import annotations

from ..model import Coverage, Section, Suggestion
from ..observed import build_timelines
from ..stagnation import find

KEY = "stagnation"

_TOP = 5


def build(facts) -> Section:
    timelines = build_timelines(facts.journal.records)
    entries = find(
        list(facts.tasks),
        timelines=timelines,
        blocks_by_task=facts.blocks_by_task(),
        now=facts.now,
    )
    pending = sum(1 for t in facts.tasks if t.status == "pending")

    if not entries:
        return Section(
            key=KEY,
            label="Stagnation",
            summary="nothing has accumulated enough evidence against it",
            coverage=(
                Coverage(
                    label="pending tasks examined", observed=pending, total=pending
                ),
            ),
            data={"stagnant": 0},
        )

    summary = f"{len(entries)} of {pending} open task(s) need a decision"
    detail = [f"  {entry.summary}" for entry in entries[:_TOP]]
    if len(entries) > _TOP:
        detail.append(f"  ... and {len(entries) - _TOP} more")
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
        coverage=(
            Coverage(
                label="pending tasks examined", observed=pending, total=pending
            ),
        ),
        data={
            "stagnant": len(entries),
            "pending": pending,
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
