"""Follow-through — did the plan survive contact?

For every block that has already ended: was the task complete by the time
the block was over? Completion is a Taskwarrior timestamp, so this is a fact
rather than an inference — which is what makes it load-bearing while
check-ins are optional.

The failure modes are more useful than the rate:

- a block passed with the task still open — over-committed, or the block was
  at a bad time (and *which hour* is itself a finding);
- a task completed with no block at all — you're working off-plan;
- one task needing several blocks — see blocks-to-completion, which reports
  the count without guessing whether it means a bad estimate, an
  interruption, or deliberate multi-session work.
"""

from __future__ import annotations

from collections import Counter

from ..model import Coverage, Section, Suggestion
from ..words import plural

KEY = "blocks"

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "The blocks booked for your tasks that have already passed, and whether "
    "each task was finished by the time its block ended. Tasks finished "
    "without any block show how much work happens outside the plan."
)

# Below this rate the week is worth a closing note. Not a grade: a low rate
# with a meeting-heavy capacity line is an explanation, not a failing.
_LOW_RATE = 0.6


def _completed_by(task, moment) -> bool:
    return task is not None and task.end is not None and task.end <= moment


def build(facts) -> Section:
    if not facts.calendar_ok:
        return Section(
            key=KEY,
            label="Blocks",
            summary="calendar not read",
            measured=False,
        )

    ended = facts.blocks_ended_in_period()
    tasks = facts.by_uuid()

    honored = []
    passed_open = []
    for block in ended:
        task = tasks.get(block.task_uuid or "")
        (honored if _completed_by(task, block.end) else passed_open).append(block)

    # Completed inside the period having never had a block at all: work that
    # happened entirely off-plan. Deliberately "no block existed" rather than
    # "no block had ended" — a task finished half an hour into its own
    # hour-long block is the ideal case, not an unplanned one.
    blocks_by_task = facts.blocks_by_task()
    off_plan = [
        t for t in facts.completed_in_period() if not blocks_by_task.get(t.uuid)
    ]

    if not ended:
        return Section(
            key=KEY,
            label="Blocks",
            summary=(
                f"no blocks ended this {facts.period.kind}"
                + (
                    f"; {plural(len(off_plan), 'task')} finished without one"
                    if off_plan
                    else ""
                )
            ),
            measured=False,
            data={"blocks": 0, "off_plan": len(off_plan)},
        )

    rate = len(honored) / len(ended)
    # "Honoured" is a promise-keeping word, and the label was doing the
    # moralising rather than the number. Saying what happened to the others
    # removes the need for a rate as well: "15 still open" and "(12%)" carry
    # the same information, and only one of them reads as a grade.
    if not honored:
        summary = f"{plural(len(ended), 'block')} ended, none with the task done."
    elif not passed_open:
        summary = f"{plural(len(ended), 'block')} ended, all with the task done."
    else:
        summary = (
            f"{plural(len(ended), 'block')} ended: the task was done for "
            f"{len(honored)} and not for {len(passed_open)}."
        )
    if off_plan:
        summary += (
            f" {plural(len(off_plan), 'task')} "
            f"{'was' if len(off_plan) == 1 else 'were'} finished without a "
            "block."
        )

    worst_hour = Counter(
        block.start.astimezone(facts.period.tz).hour for block in passed_open
    )
    detail = [
        f"Blocks ended      {len(ended)}",
        f"Task done         {len(honored)} ({rate:.0%})",
        f"Task not done     {len(passed_open)}",
        f"Done without one  {plural(len(off_plan), 'task')} finished with no "
        "block booked",
    ]
    # One block at an hour is a Tuesday, not a pattern.
    if worst_hour and worst_hour.most_common(1)[0][1] >= 2:
        hour, count = worst_hour.most_common(1)[0]
        detail.append(
            f"Worst start time  {hour:02d}:00, where {count} blocks ended "
            "with the task not done"
        )

    suggestions: tuple[Suggestion, ...] = ()
    if rate < _LOW_RATE and len(ended) >= 3:
        if worst_hour and worst_hour.most_common(1)[0][1] >= 2:
            hour = worst_hour.most_common(1)[0][0]
            suggestions = (
                Suggestion(
                    f"Blocks starting at {hour:02d}:00 keep ending with the "
                    "task not done. That hour isn't working for focused "
                    "work; keep it for something else.",
                    weight=3.0,
                ),
            )
        else:
            suggestions = (
                Suggestion(
                    f"{len(passed_open)} of {len(ended)} blocks ended with "
                    "the task not done. Either the estimates are too small, "
                    "or more is booked than the days can hold.",
                    weight=2.2,
                ),
            )

    return Section(
        key=KEY,
        label="Blocks",
        summary=summary,
        detail=tuple(detail),
        # Only when it says something: a task deleted from Taskwarrior leaves
        # its blocks behind, and those can't be judged either way.
        coverage=tuple(
            Coverage(
                label="ended blocks belong to a task that still exists",
                qualifies="Blocks ended",
                observed=found,
                total=len(ended),
            )
            for found in [sum(1 for b in ended if (b.task_uuid or "") in tasks)]
            if found < len(ended)
        ),
        data={
            "blocks": len(ended),
            "honored": len(honored),
            "passed_open": len(passed_open),
            "rate": round(rate, 4),
            "off_plan": len(off_plan),
            "passed_open_by_hour": dict(sorted(worst_hour.items())),
        },
        suggestions=suggestions,
    )
