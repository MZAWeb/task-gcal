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
from ..words import plural

KEY = "finished"

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "Tasks you finished, and how long you had estimated they would take, "
    "by project."
)

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
    label = f"the {facts.period.kind} before"
    if facts.period.in_progress:
        elapsed = facts.period.end - facts.period.start
        cutoff = min(previous.start + elapsed, previous.end)
        label = f"this point last {facts.period.kind}"
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

    # The comparison as the count it was, not as a delta: "+2" is a score,
    # "5 by this point last week" is a fact you can disagree with.
    summary = plural(len(completed), "task")
    if planned:
        summary += f", estimated at {humanize_minutes(planned)} in total"
    if previous_count:
        summary += f" ({previous_count} by {comparison})"

    by_project = Counter(t.project or "(no project)" for t in completed)
    recurring = sum(1 for t in completed if t.is_recurring)
    detail = [
        f"Finished          {plural(len(completed), 'task')}",
        f"Estimated time    {humanize_minutes(planned)} (what you expected, "
        "not time tracked)",
        f"{comparison.capitalize():<16}  {plural(previous_count, 'task')}",
    ]
    if recurring:
        # A recurring chore ticked off is real work, but a count made mostly
        # of them says something different from one made of new work.
        detail.append(f"Recurring         {recurring} of them")
    if by_project:
        detail.append("By project:")
        for name, count in by_project.most_common(_TOP_PROJECTS):
            detail.append(f"  {count:>3}  {name}")
        remaining = len(by_project) - _TOP_PROJECTS
        if remaining > 0:
            detail.append(f"  ... and {plural(remaining, 'more project')}")

    suggestions: tuple[Suggestion, ...] = ()
    # Only worth saying when there's enough to be worth explaining: below
    # that, coverage is the finding, not the mix.
    if completed and len(with_estimate) * 2 < len(completed):
        suggestions = (
            Suggestion(
                f"Only {len(with_estimate)} of {len(completed)} finished tasks "
                "had an estimate, so the time figures in this review miss "
                "most of your work. Add an estimate when you create a task.",
                weight=1.5,
            ),
        )

    return Section(
        key=KEY,
        label="Finished",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="finished tasks had an estimate",
                qualifies="Estimated time",
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
