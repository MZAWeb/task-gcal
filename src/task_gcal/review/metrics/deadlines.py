"""Deadline integrity — do your due dates mean anything?

Three separate things live here, and conflating them is how this metric goes
wrong:

- **The promise ledger.** Original due date, current one, how many times it
  moved and by how much, and whether completion met the *original* promise or
  only the renegotiated one.
- **Reactive vs proactive.** A push made before the old deadline renegotiates
  a commitment; one made after it reports a miss. Neither is automatically
  bad — scope changes, dependencies and externally moved deadlines are all
  legitimate — so this describes behaviour and never scores it.
- **Missed unchanged deadlines.** The pushes are the visible failure mode,
  but plenty of tasks are simply finished late against a date nobody touched.
  Reporting only churn would miss all of them.

These come from Taskwarrior's own operation log, so they are exact: a due date
that moved twice is two pushes, not one. What is bounded is *reach* — nothing
is known before the earliest harvested change — and that is reported as
coverage rather than smoothed over.
"""

from __future__ import annotations

from ..model import Coverage, Section, Suggestion

KEY = "deadlines"

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "Counts due dates: which ones you moved, how far, and whether the work "
    "landed by the date in the end. Independent of the calendar — a task with "
    "no time set aside can still miss its date. Moving a date is not "
    "automatically bad; how often you do it is the interesting part."
)

# A task pushed this many times has stopped being a deadline and become a
# habit; it earns the review's closing line.
_REPEAT_OFFENDER = 3

# How many tasks to name before summarizing the rest.
_TOP_OFFENDERS = 5


def build(facts) -> Section:
    window = (facts.period.start, facts.period.end)
    timelines = facts.timelines
    # Measured as soon as we have any history at all: pushes inside the window
    # are real even if our reach starts mid-period. How far back the history
    # goes is a coverage question, not a measured/unmeasured one.
    has_history = facts.changes.earliest is not None
    covers_period = facts.change_history_reaches_period()

    pushed = {
        uuid: t.pushes(within=window)
        for uuid, t in timelines.items()
        if t.pushes(within=window)
    }
    push_count = sum(len(changes) for changes in pushed.values())
    days = sum(c.days for changes in pushed.values() for c in changes)
    reactive = sum(
        len(timelines[uuid].reactive_pushes(within=window)) for uuid in pushed
    )

    completed = facts.completed_in_period()
    ledger = [_ledger_entry(t, timelines.get(t.uuid)) for t in completed]
    with_due = [entry for entry in ledger if entry["final_due"] is not None]
    met_final = [e for e in with_due if e["met_final"]]
    met_original = [e for e in with_due if e["met_original"]]
    missed_unchanged = [
        e for e in with_due if not e["met_final"] and e["pushes"] == 0
    ]

    if not has_history and not with_due:
        return Section(
            key=KEY,
            label="Deadlines",
            summary="no deadline history for this period",
            measured=False,
            data={},
        )

    summary_parts = []
    if push_count:
        summary_parts.append(
            f"{push_count} push(es) across {len(pushed)} task(s)"
        )
        summary_parts.append(f"{days:.0f} days")
    if with_due:
        summary_parts.append(f"{len(met_final)}/{len(with_due)} met the final date")
    summary = " · ".join(summary_parts) or "no deadlines moved or met"

    detail = [
        f"Deadline pushes    {push_count} across {len(pushed)} task(s), "
        f"{days:.0f} day(s) total",
        f"  after the old date {reactive} (a miss being reported)",
        f"  before it          {push_count - reactive} (a commitment being "
        "renegotiated)",
        f"Met the final date {len(met_final)} of {len(with_due)} completed "
        "task(s) with a due date",
        f"Met the original   {len(met_original)} of {len(with_due)}",
        f"Late on a date we never saw move  {len(missed_unchanged)}",
    ]

    offenders = sorted(
        pushed.items(), key=lambda item: len(item[1]), reverse=True
    )
    if offenders:
        detail.append("Most-moved deadlines:")
        for uuid, changes in offenders[:_TOP_OFFENDERS]:
            moved = sum(c.days for c in changes)
            detail.append(
                f"  {len(changes)}x  +{moved:.0f}d  {facts.label_for(uuid)}"
            )
        if len(offenders) > _TOP_OFFENDERS:
            detail.append(f"  ... and {len(offenders) - _TOP_OFFENDERS} more")

    note = facts.change_coverage_note()
    if note:
        detail.append(note)

    suggestions: list[Suggestion] = []
    for uuid, changes in offenders:
        total_pushes = len(timelines[uuid].pushes())
        if total_pushes >= _REPEAT_OFFENDER:
            suggestions.append(
                Suggestion(
                    f'"{facts.label_for(uuid)}" was deferred for the '
                    f"{_ordinal(total_pushes)} time — decide whether it is "
                    "real.",
                    # The strongest routine finding: a deadline moved three
                    # times is a decision nobody has made yet.
                    weight=4.0,
                )
            )
            break
    if not suggestions and missed_unchanged:
        suggestions.append(
            Suggestion(
                f"{len(missed_unchanged)} task(s) finished late against a "
                "date nobody moved — the dates aren't being used.",
                weight=2.4,
            )
        )

    return Section(
        key=KEY,
        label="Deadlines",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="completed tasks had a due date to judge",
                observed=len(with_due),
                total=len(completed),
            ),
            Coverage(
                label="the period is covered by harvested change history",
                observed=1 if covers_period else 0,
                total=1,
            ),
        ),
        data={
            "observed_pushes": push_count,
            "tasks_pushed": len(pushed),
            "days_pushed": round(days, 1),
            "reactive_pushes": reactive,
            "proactive_pushes": push_count - reactive,
            "met_final": len(met_final),
            "met_original": len(met_original),
            "with_due": len(with_due),
            "late_on_unchanged_date": len(missed_unchanged),
            "ledger": [
                entry for entry in with_due if entry["pushes"] or not entry["met_final"]
            ],
        },
        suggestions=tuple(suggestions),
    )


def _ledger_entry(task, timeline) -> dict:
    """One task's promise, from first observed deadline to completion."""
    original = timeline.first_due if timeline is not None else None
    final = task.due
    pushes = len(timeline.pushes()) if timeline is not None else 0
    return {
        "uuid": task.uuid,
        "label": task.description,
        "original_due": _iso(original or final),
        "final_due": _iso(final),
        "pushes": pushes,
        "days_pushed": round(timeline.days_pushed, 1) if timeline else 0.0,
        "met_final": final is not None
        and task.end is not None
        and task.end <= final,
        # Falls back to the final date when there's no journal history: with
        # nothing observed, the only promise we know about is the current one.
        "met_original": (original or final) is not None
        and task.end is not None
        and task.end <= (original or final),
    }


def _iso(moment) -> str:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
