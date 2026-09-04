"""Blocks-to-completion — how many sittings a task actually needs.

This is the substitute for estimate calibration, and it's worth being clear
about why. Calibration needs actual minutes; with check-ins optional we can't
count on having them. But completions are Taskwarrior timestamps, so we *can*
count blocks: "writing tasks take 2.4 blocks on average, so your 60m estimate
is really about 150m" is the same actionable finding derived from facts
instead of stopwatch discipline.

Coarser, and honest about it: quantised to whole blocks, blind to a task you
finished early, and it never claims to know *why* a task needed three blocks.
An underestimate, an interruption, partial progress and deliberate
multi-session work all produce the same count.
"""

from __future__ import annotations

from collections import Counter

from ...intervals import humanize_minutes
from ..model import Coverage, Section, Suggestion

KEY = "sittings"

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "How many separate sittings a finished task actually took. Two blocks "
    "for a task you called an hour means it was really two hours."
)

# Below this many completed tasks the average is a rumour, not a finding.
_MIN_SAMPLE = 4

# Where "needs more sittings than planned" starts being worth saying.
_STRETCHED = 2.0


def build(facts) -> Section:
    if not facts.calendar_ok:
        return Section(
            key=KEY,
            label="Sittings",
            summary="calendar not read",
            measured=False,
        )

    blocks_by_task = facts.blocks_by_task()
    completed = facts.completed_in_period()
    counts: dict[str, int] = {}
    for task in completed:
        # Blocks that had ended by the time it was done. A block scheduled
        # after completion was never an attempt.
        blocks = [
            b
            for b in blocks_by_task.get(task.uuid, [])
            if task.end is not None and b.end <= task.end
        ]
        if blocks:
            counts[task.uuid] = len(blocks)

    if not counts:
        return Section(
            key=KEY,
            label="Sittings",
            summary="no completed task had a block that had ended",
            measured=False,
            coverage=(
                Coverage(
                    label="completed tasks had an observed block",
                    observed=0,
                    total=len(completed),
                ),
            ),
            data={},
        )

    total_blocks = sum(counts.values())
    average = total_blocks / len(counts)
    multi = {uuid: n for uuid, n in counts.items() if n > 1}
    distribution = Counter(counts.values())

    summary = (
        f"{average:.1f} blocks per completed task · "
        f"{len(multi)} needed more than one"
    )

    by_uuid = {t.uuid: t for t in completed}
    detail = [
        f"Completed with a block  {len(counts)} of {len(completed)}",
        f"Blocks used             {total_blocks}",
        f"Average per task        {average:.1f}",
        "Distribution:",
    ]
    for blocks, tasks in sorted(distribution.items()):
        detail.append(f"  {blocks} block(s): {tasks} task(s)")

    worst = sorted(multi.items(), key=lambda item: item[1], reverse=True)[:5]
    if worst:
        detail.append("Most sittings:")
        for uuid, blocks in worst:
            task = by_uuid[uuid]
            estimate = humanize_minutes(task.estimate_minutes)
            detail.append(
                f"  {blocks}x  estimated {estimate}  "
                f"≈{humanize_minutes((task.estimate_minutes or 0) * blocks)} "
                f"of planned time  {task.description}"
            )
    detail.append(
        "A count, not a cause: an underestimate, an interruption and "
        "deliberate multi-session work all look the same here."
    )

    suggestions: tuple[Suggestion, ...] = ()
    if len(counts) >= _MIN_SAMPLE and average >= _STRETCHED:
        suggestions = (
            Suggestion(
                f"Tasks took {average:.1f} blocks on average — your estimates "
                f"are about {average:.0f}x short, or the blocks are too small.",
                weight=2.6,
            ),
        )

    return Section(
        key=KEY,
        label="Sittings",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="completed tasks had an observed block",
                observed=len(counts),
                total=len(completed),
            ),
        ),
        data={
            "tasks_with_blocks": len(counts),
            "total_blocks": total_blocks,
            "average_blocks": round(average, 2),
            "multi_block_tasks": len(multi),
            "distribution": {str(k): v for k, v in sorted(distribution.items())},
        },
        suggestions=suggestions,
    )
