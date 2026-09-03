"""The stagnation queue: what am I lying to myself about?

A list, not a metric — and stronger triage candidates than "old tasks", which
mostly finds things that are fine where they are. A task qualifies when
evidence has accumulated against it: blocks that passed, deadlines that kept
moving, an estimate that doubled, or age *plus* a deadline that keeps moving.

Lives outside `metrics/` because two commands consume it: the review's
Stagnation section, and `review --triage`, which turns each entry into
Taskwarrior commands you can read and paste.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..taskw import TaskInfo
from .observed import Timeline

# Thresholds. Each is "enough evidence that a person should decide something",
# not a measurement — so they're few, round, and named.
PUSH_THRESHOLD = 3
PASSED_BLOCK_THRESHOLD = 2
ESTIMATE_GROWTH = 2.0
OLD_DAYS = 90


@dataclass(frozen=True)
class Stagnant:
    """One task with the evidence against it, and nothing inferred."""

    task: TaskInfo
    reasons: tuple[str, ...] = ()
    pushes: int = 0
    days_pushed: float = 0.0
    passed_blocks: int = 0
    estimate_growth: Optional[float] = None
    age_days: Optional[int] = None
    # Ordering weight: how much evidence, not how bad the person is.
    score: float = 0.0

    @property
    def summary(self) -> str:
        return f"{self.task.ref} {self.task.description} — {', '.join(self.reasons)}"


def find(
    tasks: list[TaskInfo],
    *,
    timelines: dict[str, Timeline],
    blocks_by_task: dict[str, list],
    now,
) -> list[Stagnant]:
    """Pending tasks with evidence against them, strongest first."""
    out: list[Stagnant] = []
    for task in tasks:
        if task.status != "pending":
            continue
        # A recurring chore's dates are generated, and its perpetual
        # openness is the feature. Attributing that to personal behaviour
        # would fill the triage list with the things working correctly.
        if task.is_recurring:
            continue
        timeline = timelines.get(task.uuid)
        pushes = len(timeline.pushes()) if timeline else 0
        days_pushed = timeline.days_pushed if timeline else 0.0
        passed = sum(
            1 for b in blocks_by_task.get(task.uuid, []) if b.end <= now
        )
        growth = _growth(timeline)
        age = (now - task.entry).days if task.entry else None

        reasons: list[str] = []
        score = 0.0
        if pushes >= PUSH_THRESHOLD:
            reasons.append(f"due pushed {pushes}x, +{days_pushed:.0f}d")
            score += pushes * 2
        if passed >= PASSED_BLOCK_THRESHOLD:
            reasons.append(f"{passed} blocks passed")
            score += passed
        if growth is not None and growth >= ESTIMATE_GROWTH:
            reasons.append(f"estimate grew {growth:.1f}x")
            score += 3
        # Age alone is not evidence — plenty of old tasks are fine where they
        # are. Age *plus* a moving deadline is a task being avoided.
        if age is not None and age >= OLD_DAYS and pushes:
            reasons.append(f"open {age}d with a moving deadline")
            score += 2

        if reasons:
            out.append(
                Stagnant(
                    task=task,
                    reasons=tuple(reasons),
                    pushes=pushes,
                    days_pushed=round(days_pushed, 1),
                    passed_blocks=passed,
                    estimate_growth=growth,
                    age_days=age,
                    score=score,
                )
            )

    out.sort(key=lambda s: s.score, reverse=True)
    return out


def _growth(timeline: Optional[Timeline]) -> Optional[float]:
    if timeline is None:
        return None
    first, last = timeline.first_estimate, timeline.last_estimate
    if not first or not last:
        return None
    return last / first


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------

# Four honest options, which is the whole of triage: do it, shrink it, hand it
# off, or kill it. "Hand it off" has no Taskwarrior command, so it's the one
# left to you.
@dataclass(frozen=True)
class Prescription:
    """Commands that would resolve one stagnant task, and what each means."""

    entry: Stagnant
    commands: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def prescribe(entry: Stagnant) -> Prescription:
    """Exact `task ...` invocations for one stagnant task.

    Printed, never run. This tool never writes to Taskwarrior, which is also
    what makes triage safe to run casually: there's no confirmation to
    misclick and no `--dry-run` to forget.
    """
    ref = entry.task.id or entry.task.uuid[:8]
    commands: list[tuple[str, str]] = []
    if entry.pushes >= PUSH_THRESHOLD:
        commands.append(
            (f"task {ref} modify wait:someday", "not now, and stop pretending")
        )
    if entry.passed_blocks >= PASSED_BLOCK_THRESHOLD or entry.estimate_growth:
        smaller = _smaller_estimate(entry.task.estimate_minutes)
        if smaller:
            commands.append(
                (
                    f"task {ref} modify estimate:{smaller}",
                    "shrink it to a first step you'd actually start",
                )
            )
    commands.append((f"task {ref} delete", "be honest"))
    return Prescription(entry=entry, commands=tuple(commands))


def _smaller_estimate(minutes: Optional[int]) -> Optional[int]:
    """A first step rather than the whole thing: a quarter, floored at 15."""
    if not minutes or minutes <= 15:
        return None
    return max(15, (minutes // 4 // 5) * 5)
