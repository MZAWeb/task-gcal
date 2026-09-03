"""Estimate and scope churn — which tasks were really projects?

Upward estimate revisions and repeated title rewrites identify a task that was
never one task. Kept apart from deadline churn on purpose: `scheduled` and
`wait` moving is deliberate deferral, which is a different behaviour from a
deadline slipping, and an estimate doubling is a different one again.
"""

from __future__ import annotations

from ...intervals import humanize_minutes
from ..model import Coverage, Section, Suggestion
from ..observed import build_timelines

KEY = "scope"

# An estimate that has grown this much was not an estimate.
_BALLOONED = 2.0


def build(facts) -> Section:
    window = (facts.period.start, facts.period.end)
    timelines = build_timelines(facts.journal.records)
    in_period = facts.records_in_period()

    if not in_period:
        return Section(
            key=KEY,
            label="Scope",
            summary="no observations for this period",
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
        summary_parts.append(f"{len(grew)} estimate(s) revised up")
    if retitled:
        summary_parts.append(f"{len(retitled)} retitled")
    if deferred:
        summary_parts.append(f"{len(deferred)} deliberately deferred")
    summary = " · ".join(summary_parts) or "no scope changes observed"

    detail = [
        f"Estimates revised up   {len(grew)} task(s)",
        f"Titles rewritten       {len(retitled)} task(s)",
        f"Scheduled/wait moved   {len(deferred)} task(s) "
        "(deferral, not a deadline slipping)",
        f"Estimates that doubled {len(ballooned)}",
    ]
    for uuid, first, last in ballooned[:5]:
        detail.append(
            f"  {humanize_minutes(first)} → {humanize_minutes(last)}  "
            f"{timelines[uuid].label}"
        )

    suggestions: tuple[Suggestion, ...] = ()
    if ballooned:
        uuid, first, last = ballooned[0]
        suggestions = (
            Suggestion(
                f'"{timelines[uuid].label}" grew from '
                f"{humanize_minutes(first)} to {humanize_minutes(last)} — "
                "it's a project, so split it.",
                weight=3.2,
            ),
        )

    return Section(
        key=KEY,
        label="Scope",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="days of the period the journal observed",
                observed=len(facts.observed_days()),
                total=len(facts.period.days()),
            ),
        ),
        data={
            "estimates_up": len(grew),
            "retitled": len(retitled),
            "deferred": len(deferred),
            "doubled": [
                {
                    "uuid": uuid,
                    "label": timelines[uuid].label,
                    "from_minutes": first,
                    "to_minutes": last,
                }
                for uuid, first, last in ballooned
            ],
        },
        suggestions=suggestions,
    )
