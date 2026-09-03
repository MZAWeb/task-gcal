"""Backlog flow and lead time — is the funnel filling faster than it empties?

Two numbers that need no journal and no estimates, because `entry` and `end`
are Taskwarrior facts. Lead time reports its tail as well as its median: the
median says what a normal task costs in waiting, and the tail is where the
things you keep not doing live.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional, Sequence

from ...intervals import humanize_duration
from ..model import Coverage, Section, Suggestion

KEY_FLOW = "flow"
KEY_LEAD_TIME = "lead_time"

# Net growth beyond this many tasks in one period is worth a closing note
# rather than a shrug.
_GROWTH_THRESHOLD = 5


def _median(values: Sequence[timedelta]) -> Optional[timedelta]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _percentile(values: Sequence[timedelta], fraction: float) -> Optional[timedelta]:
    if not values:
        return None
    ordered = sorted(values)
    # Nearest-rank, which is the honest choice for the small samples a week
    # produces: interpolating between two tasks invents a task.
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def build_flow(facts) -> Section:
    created = facts.created_in_period()
    completed = facts.completed_in_period()
    deleted = facts.deleted_in_period()
    net = len(created) - len(completed) - len(deleted)

    # Plain counts: the verb already carries the direction, and a signed
    # zero ("+0 deleted") reads as a typo.
    summary = (
        f"{len(created)} created · {len(completed)} completed · "
        f"{len(deleted)} deleted · net {net:+d}"
    )
    detail = (
        f"Created           {len(created)}",
        f"Completed         {len(completed)}",
        f"Deleted           {len(deleted)}",
        f"Net change        {net:+d}",
        f"Still open        "
        f"{sum(1 for t in facts.tasks if t.status == 'pending')}",
    )

    suggestions: tuple[Suggestion, ...] = ()
    if net >= _GROWTH_THRESHOLD:
        suggestions = (
            Suggestion(
                f"The backlog grew by {net} tasks this "
                f"{facts.period.kind} — the constraint is intake, not speed.",
                weight=1.8,
            ),
        )

    return Section(
        key=KEY_FLOW,
        label="Backlog",
        summary=summary,
        detail=detail,
        data={
            "created": len(created),
            "completed": len(completed),
            "deleted": len(deleted),
            "net": net,
            "open": sum(1 for t in facts.tasks if t.status == "pending"),
        },
        suggestions=suggestions,
    )


def build_lead_time(facts) -> Section:
    completed = facts.completed_in_period()
    spans = [
        t.end - t.entry
        for t in completed
        if t.end is not None and t.entry is not None and t.end >= t.entry
    ]

    if not spans:
        return Section(
            key=KEY_LEAD_TIME,
            label="Lead time",
            summary="nothing completed in this period",
            measured=False,
            coverage=(
                Coverage(
                    label="completed tasks had usable dates",
                    observed=0,
                    total=len(completed),
                ),
            ),
            data={},
        )

    median = _median(spans)
    p90 = _percentile(spans, 0.9)
    longest = max(spans)
    summary = (
        f"median {humanize_duration(median)} · "
        f"90th {humanize_duration(p90)} · longest {humanize_duration(longest)}"
    )
    detail = (
        f"Median            {humanize_duration(median)} from created to done",
        f"90th percentile   {humanize_duration(p90)}",
        f"Longest           {humanize_duration(longest)}",
        f"Shortest          {humanize_duration(min(spans))}",
    )

    return Section(
        key=KEY_LEAD_TIME,
        label="Lead time",
        summary=summary,
        detail=detail,
        coverage=(
            Coverage(
                label="completed tasks had usable dates",
                observed=len(spans),
                total=len(completed),
            ),
        ),
        data={
            "median_days": round(median.total_seconds() / 86400, 2),
            "p90_days": round(p90.total_seconds() / 86400, 2),
            "longest_days": round(longest.total_seconds() / 86400, 2),
            "sample": len(spans),
        },
    )
