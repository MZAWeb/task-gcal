"""Estimate and scope churn — which tasks were really projects?

Upward estimate revisions and repeated title rewrites identify a task that was
never one task. Kept apart from deadline churn on purpose: `scheduled` and
`wait` moving is deliberate deferral, which is a different behaviour from a
deadline slipping, and an estimate doubling is a different one again.
"""

from __future__ import annotations

from ...intervals import humanize_minutes
from ..model import Section, Suggestion
from ..words import plural

KEY = "growth"

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "Tasks that turned out bigger than planned: the estimate raised or the "
    "title rewritten, which usually means it was several tasks all along. "
    "Also counts tasks you postponed on purpose with a wait or scheduled date."
)

# An estimate that has grown this much was not an estimate.
_BALLOONED = 2.0


def build(facts) -> Section:
    window = (facts.period.start, facts.period.end)
    timelines = facts.timelines

    if facts.changes.earliest is None:
        return Section(
            key=KEY,
            label="Tasks that grew",
            summary="no task history yet",
            measured=False,
            data={},
        )

    grew = {
        uuid: t.upward_estimate_changes(within=window)
        for uuid, t in timelines.items()
        if t.upward_estimate_changes(within=window)
    }
    retitled = {
        uuid: t.retitles(within=window)
        for uuid, t in timelines.items()
        if t.retitles(within=window)
    }
    deferred = {
        uuid: t.deferrals(within=window)
        for uuid, t in timelines.items()
        if t.deferrals(within=window)
    }

    ballooned = []
    for uuid in grew:
        timeline = timelines[uuid]
        first, last = timeline.first_estimate, timeline.last_estimate
        if first and last and last / first >= _BALLOONED:
            ballooned.append((uuid, first, last))

    summary_parts = []
    if grew:
        summary_parts.append(f"{plural(len(grew), 'estimate')} raised")
    if retitled:
        summary_parts.append(f"{plural(len(retitled), 'task')} renamed")
    if deferred:
        summary_parts.append(f"{plural(len(deferred), 'task')} postponed")
    summary = ", ".join(summary_parts) or "no task changed size"

    detail = [
        f"Estimate raised    {plural(len(grew), 'task')}",
        f"Estimate doubled   {plural(len(ballooned), 'task')}",
        f"Renamed            {plural(len(retitled), 'task')}",
        f"Postponed          {plural(len(deferred), 'task')}, by moving a "
        "wait or scheduled date (not the due date)",
    ]
    if ballooned:
        detail.append("Estimates that doubled:")
    for uuid, first, last in ballooned[:5]:
        detail.append(
            f"  {humanize_minutes(first)} → {humanize_minutes(last)}  "
            f"{facts.label_for(uuid)}"
        )

    suggestions: tuple[Suggestion, ...] = ()
    if ballooned:
        uuid, first, last = ballooned[0]
        suggestions = (
            Suggestion(
                f'"{facts.label_for(uuid)}" grew from '
                f"{humanize_minutes(first)} to {humanize_minutes(last)}. "
                "That's a project, not a task: split it into pieces you can "
                "finish in one sitting.",
                weight=3.2,
            ),
        )

    return Section(
        key=KEY,
        label="Tasks that grew",
        summary=summary,
        detail=tuple(detail),
        data={
            "estimates_up": len(grew),
            "retitled": len(retitled),
            "deferred": len(deferred),
            "doubled": [
                {
                    "uuid": uuid,
                    "label": facts.label_for(uuid),
                    "from_minutes": first,
                    "to_minutes": last,
                }
                for uuid, first, last in ballooned
            ],
        },
        suggestions=suggestions,
    )
