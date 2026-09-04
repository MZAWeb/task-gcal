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

KEY = "blocks"

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "Counts blocks of time that have already passed: when one came and went, "
    "was the task actually done by the end of it?"
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
                "no blocks ended in this period"
                + (f" · {len(off_plan)} completed off-plan" if off_plan else "")
            ),
            measured=False,
            data={"blocks": 0, "off_plan": len(off_plan)},
        )

    rate = len(honored) / len(ended)
    # "Honoured" is a promise-keeping word, and the label was doing the
    # moralising rather than the number. Saying what happened to the others
    # removes the need for a rate as well: "15 still open" and "(12%)" carry
    # the same information, and only one of them reads as a grade.
    summary = (
        f"{len(ended)} block{'' if len(ended) == 1 else 's'} came and went. "
        f"{len(honored)} ended with the task done"
    )
    summary += f", {len(passed_open)} are still open." if passed_open else "."
    if off_plan:
        summary += (
            f" {len(off_plan)} task{'' if len(off_plan) == 1 else 's'} "
            "finished with no block at all."
        )

    worst_hour = Counter(
        block.start.astimezone(facts.period.tz).hour for block in passed_open
    )
    detail = [
        f"Blocks ended      {len(ended)}",
        f"Task done by then {len(honored)} ({rate:.0%})",
        f"Passed still open {len(passed_open)}",
        f"Completed off-plan {len(off_plan)} (never had a block at all)",
    ]
    if worst_hour:
        hour, count = worst_hour.most_common(1)[0]
        detail.append(
            f"Most common hour  {hour:02d}:00 — {count} block(s) passed "
            "with the task still open"
        )

    suggestions: tuple[Suggestion, ...] = ()
    if rate < _LOW_RATE and len(ended) >= 3:
        if worst_hour and worst_hour.most_common(1)[0][1] >= 2:
            hour = worst_hour.most_common(1)[0][0]
            suggestions = (
                Suggestion(
                    f"Blocks at {hour:02d}:00 kept passing with the task "
                    "still open — stop scheduling work there.",
                    weight=3.0,
                ),
            )
        else:
            suggestions = (
                Suggestion(
                    f"{len(passed_open)} of {len(ended)} blocks passed with "
                    "the task still open — the estimates or the load are off.",
                    weight=2.2,
                ),
            )

    return Section(
        key=KEY,
        label="Blocks",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="ended blocks belonged to a task we could still find",
                observed=sum(
                    1 for b in ended if (b.task_uuid or "") in tasks
                ),
                total=len(ended),
            ),
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
