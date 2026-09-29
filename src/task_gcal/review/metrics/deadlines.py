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
from ..words import plural, times

KEY = "dates"

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "Due dates you moved, how far, and whether finished tasks landed on "
    "their date. Moving a date once is normal; moving the same one again and "
    "again usually means the task isn't really going to happen."
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
            label="Dates",
            summary="no task history yet",
            measured=False,
            data={},
        )

    # The largest single push, not the total. A sum of pushes across unrelated
    # tasks is a number nobody has experienced; "you moved something by six
    # weeks" is a sentence about a real event.
    longest = max(
        (c.days for changes in pushed.values() for c in changes), default=0.0
    )
    summary_parts = []
    if push_count:
        moved = (
            f"{plural(len(pushed), 'task')} had a due date pushed back"
        )
        if push_count > len(pushed):
            moved += f", {plural(push_count, 'time')} in all"
        summary_parts.append(
            f"{moved}, the furthest by {plural(round(longest), 'day')}."
        )
    if with_due:
        summary_parts.append(
            f"{len(met_final)} of the {plural(len(with_due), 'task')} "
            "finished with a due date made it on time."
        )
    summary = " ".join(summary_parts) or "no due dates moved"

    detail = [
        f"Dates pushed back  {plural(push_count, 'time')} on "
        f"{plural(len(pushed), 'task')}, the furthest by "
        f"{plural(round(longest), 'day')}",
        f"  after missing    {reactive}, moved once the date had passed",
        f"  in advance       {push_count - reactive}, moved before the date "
        "came",
        f"On time            {len(met_final)} of {len(with_due)} finished "
        "tasks with a due date",
        f"On the first date  {len(met_original)} of {len(with_due)}, without "
        "the date ever moving later",
        f"Late, date kept    {len(missed_unchanged)} finished after a date "
        "that was never moved",
    ]

    offenders = sorted(
        pushed.items(), key=lambda item: len(item[1]), reverse=True
    )
    if offenders:
        detail.append("Pushed back most:")
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
                    f'"{facts.label_for(uuid)}" has had its due date pushed '
                    f"back {times(total_pushes)}. Decide whether you're really "
                    "going to do it: book it properly, shrink it, or delete "
                    "it.",
                    # The strongest routine finding: a deadline moved three
                    # times is a decision nobody has made yet.
                    weight=4.0,
                    already_true_for=_periods_already_true(timelines[uuid], facts),
                )
            )
            break
    if not suggestions and missed_unchanged:
        suggestions.append(
            Suggestion(
                f"{plural(len(missed_unchanged), 'task')} finished after a "
                "due date that was never moved. If dates keep being missed "
                "quietly, they aren't helping: set fewer, or set them "
                "honestly.",
                weight=2.4,
            )
        )

    return Section(
        key=KEY,
        label="Dates",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="finished tasks had a due date",
                observed=len(with_due),
                total=len(completed),
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


# How far back to look for the same finding. Past this, "you have been told
# this before" is the finding and the exact count stops mattering.
_REPEAT_LOOKBACK = 12


def _periods_already_true(timeline, facts) -> int:
    """How many whole periods back this task already qualified.

    Exact, because every push carries the instant it happened: the same test
    is applied to the pushes that existed as of each earlier period's end.
    """
    for back in range(1, _REPEAT_LOOKBACK + 1):
        cutoff = facts.period.shifted(-back).end
        earlier = [c for c in timeline.pushes() if c.at < cutoff]
        if len(earlier) < _REPEAT_OFFENDER:
            return back - 1
    return _REPEAT_LOOKBACK

