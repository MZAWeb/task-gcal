"""Throughput — how much moved, and how much of it we can say anything about.

Two rules this section exists to obey:

- **Planned minutes are not hours worked.** The estimates attached to
  completed tasks say what you thought they'd take, and nothing about what
  they did take. Until a check-in supplies actuals, this line is never
  labelled as time spent.
- **Missing estimates are not zero minutes.** They're missing, and the
  coverage line says how many.
"""

from __future__ import annotations

from collections import Counter

from ...intervals import humanize_minutes
from ..model import Coverage, Section, Suggestion

KEY = "throughput"

# How many projects to name before collapsing the rest into "other".
_TOP_PROJECTS = 5


def _previous(facts) -> tuple[int, str]:
    """Completions in the comparable stretch of the previous period.

    A period in progress is only part-way through, so comparing it against a
    whole previous one prints a deficit that is just the calendar: running
    the default review on a Tuesday would report `-9 vs previous week`. The
    previous period is truncated to the same elapsed span instead, which
    `Period` already goes to some trouble to avoid elsewhere.
    """
    previous = facts.period.shifted(-1)
    cutoff = previous.end
    label = f"previous {facts.period.kind}"
    if facts.period.in_progress:
        elapsed = facts.period.end - facts.period.start
        cutoff = min(previous.start + elapsed, previous.end)
        label = f"same point last {facts.period.kind}"
    count = sum(
        1
        for t in facts.tasks
        if t.status == "completed"
        and t.end is not None
        and previous.start <= t.end < cutoff
    )
    return count, label


def build(facts) -> Section:
    completed = facts.completed_in_period()
    with_estimate = [t for t in completed if t.estimate_minutes is not None]
    planned = sum(t.estimate_minutes for t in with_estimate)

    previous_count, comparison = _previous(facts)

    summary = f"{len(completed)} tasks · {humanize_minutes(planned)} planned"
    if previous_count:
        change = len(completed) - previous_count
        summary += f" · {change:+d} vs {comparison}"

    by_project = Counter(t.project or "(no project)" for t in completed)
    recurring = sum(1 for t in completed if t.is_recurring)
    detail = [
        f"Completed         {len(completed)} task(s)",
        f"Planned minutes   {humanize_minutes(planned)} "
        "(estimates, not time spent)",
        f"Versus {comparison[:11]:<11} {previous_count} task(s)",
    ]
    if recurring:
        # A recurring chore ticked off is real work, but a count made mostly
        # of them says something different from one made of new work.
        detail.append(
            f"Of those, {recurring} were recurring task(s)"
        )
    if by_project:
        detail.append("By project:")
        for name, count in by_project.most_common(_TOP_PROJECTS):
            detail.append(f"  {count:>3}  {name}")
        remaining = len(by_project) - _TOP_PROJECTS
        if remaining > 0:
            detail.append(f"  ... and {remaining} more project(s)")

    suggestions: tuple[Suggestion, ...] = ()
    # Only worth saying when there's enough to be worth explaining: below
    # that, coverage is the finding, not the mix.
    if completed and len(with_estimate) * 2 < len(completed):
        suggestions = (
            Suggestion(
                f"Only {len(with_estimate)} of {len(completed)} completed "
                "tasks had an estimate — most of this report can't see them.",
                weight=1.5,
            ),
        )

    return Section(
        key=KEY,
        label="Completed",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="completed tasks had estimates",
                observed=len(with_estimate),
                total=len(completed),
            ),
        ),
        data={
            "completed": len(completed),
            "planned_minutes": planned,
            "previous_completed": previous_count,
            "recurring": recurring,
            "with_estimate": len(with_estimate),
            "by_project": dict(by_project.most_common()),
        },
        suggestions=suggestions,
    )
