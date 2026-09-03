"""Miss episodes: evidence that something didn't happen, and nothing more.

The single rule this module exists to enforce is the evidence ladder:

- **Fact:** a scheduled block ended while the task was still open.
- **Fact:** a due date moved later, after its old date had already passed.
- **Suggestion:** taken together, likely skipped, partial, or deferred.
- **Confirmation:** only a check-in records what actually happened.

So nothing here decides an outcome or a reason. A due-date push on its own
means a deadline moved — not that no work happened — and the strongest
combined signal is still a suggestion.

The other rule is coalescing. Several passed blocks and deadline changes for
the same still-open task are **one** episode, not five prompts. Asking five
times is how a retrospective becomes a chore and starts collecting whatever
answer ends it fastest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from ..reflections import Reflection, answered_until
from ..taskw import TaskInfo

KIND_BLOCK_PASSED = "block_passed"
KIND_DEADLINE_PUSHED = "deadline_pushed"

# A push this soon after a block ended is probably about the same episode.
# Beyond it, they're two separate things that happened to the same task.
_LINK_WINDOW = timedelta(days=3)

# How far back an episode may reach by default. Reflections stay open until
# answered, so this bounds the *prompt*, not the record: running the check-in
# every few days and running it at the weekly review both work.
DEFAULT_SINCE_DAYS = 14


@dataclass(frozen=True)
class Evidence:
    """One observed fact. Never a conclusion."""

    kind: str
    at: datetime
    detail: str
    # For a block: when it was scheduled and how long for, so the prompt can
    # show what you'd actually planned.
    start: Optional[datetime] = None
    minutes: Optional[int] = None


@dataclass(frozen=True)
class Episode:
    """One *unexplained* stretch of evidence against one task.

    Always unexplained, by construction: evidence an answer already accounts
    for is excluded before an episode is assembled, so there is no such thing
    as a resolved `Episode` to filter out later.
    """

    key: str
    task: TaskInfo
    evidence: tuple[Evidence, ...] = ()
    # Ordering only: how much evidence there is, so the strongest is asked
    # about first. Not a severity, and never shown as a score.
    strength: float = 0.0

    @property
    def covers_until(self) -> datetime:
        return max(e.at for e in self.evidence)

    @property
    def first_at(self) -> datetime:
        return min(e.at for e in self.evidence)

    @property
    def blocks(self) -> tuple[Evidence, ...]:
        return tuple(e for e in self.evidence if e.kind == KIND_BLOCK_PASSED)

    @property
    def pushes(self) -> tuple[Evidence, ...]:
        return tuple(e for e in self.evidence if e.kind == KIND_DEADLINE_PUSHED)

    @property
    def suggestion(self) -> str:
        """What the evidence *suggests*, phrased so it can't read as a verdict.

        The strongest form needs all three facts: a block ended with the task
        open, the old due day then passed, and the date moved later.
        """
        if self.blocks and self.pushes:
            return "likely unfinished, then deferred"
        if len(self.blocks) > 1:
            return "likely under-estimated or repeatedly skipped"
        if self.blocks:
            return "likely unfinished"
        return "deadline moved; work may or may not have happened"

    @property
    def planned_minutes(self) -> Optional[int]:
        minutes = [e.minutes for e in self.blocks if e.minutes]
        return sum(minutes) if minutes else None

    def lines(self, tz) -> tuple[str, ...]:
        """The evidence, as the check-in prints it."""
        return tuple(
            f"{e.at.astimezone(tz):%a %d %b %H:%M}  {e.detail}"
            for e in sorted(self.evidence, key=lambda e: e.at)
        )


@dataclass
class _Draft:
    task: TaskInfo
    evidence: list[Evidence] = field(default_factory=list)


def find_unresolved(
    *,
    tasks: list[TaskInfo],
    blocks_by_task: dict[str, list],
    timelines: dict,
    answers: dict[str, Reflection],
    now: datetime,
    since: Optional[datetime] = None,
) -> list[Episode]:
    """Miss episodes still needing an answer, strongest evidence first.

    Only evidence *after* the last answer for that task counts, so answering
    once settles it — a task with nothing new is silently gone from the list,
    and a task that goes wrong again is asked about again rather than being
    permanently excused by one answer.
    """
    since = since or (now - timedelta(days=DEFAULT_SINCE_DAYS))
    drafts: dict[str, _Draft] = {}

    for task in tasks:
        floor = _floor_for(task, answers, since)
        evidence: list[Evidence] = []

        for block in blocks_by_task.get(task.uuid, []):
            if block.end > now or block.end <= floor:
                continue
            if _completed_by(task, block.end):
                continue
            minutes = int((block.end - block.start).total_seconds() // 60)
            evidence.append(
                Evidence(
                    kind=KIND_BLOCK_PASSED,
                    at=block.end,
                    detail=(
                        f"block ended with the task still open "
                        f"({minutes}m planned)"
                    ),
                    start=block.start,
                    minutes=minutes,
                )
            )

        timeline = timelines.get(task.uuid)
        if timeline is not None:
            for change in timeline.reactive_pushes():
                if change.at <= floor or change.at > now:
                    continue
                evidence.append(
                    Evidence(
                        kind=KIND_DEADLINE_PUSHED,
                        at=change.at,
                        detail=(
                            "due date moved later, after the old date had "
                            f"passed (+{change.days:.0f}d)"
                        ),
                    )
                )

        if evidence:
            drafts[task.uuid] = _Draft(task=task, evidence=evidence)

    episodes = [_assemble(draft) for draft in drafts.values() if draft.evidence]
    episodes.sort(key=lambda e: (-e.strength, e.first_at))
    return episodes


def _floor_for(
    task: TaskInfo, answers: dict[str, Reflection], since: datetime
) -> datetime:
    """The earliest evidence still worth asking about for this task."""
    already = answered_until(answers, task.uuid)
    return max(since, already) if already else since


def _completed_by(task: TaskInfo, moment: datetime) -> bool:
    return task.end is not None and task.end <= moment


def _assemble(draft: _Draft) -> Episode:
    evidence = sorted(draft.evidence, key=lambda e: e.at)
    key = _key(draft.task.uuid, evidence[0].at)
    blocks = [e for e in evidence if e.kind == KIND_BLOCK_PASSED]
    pushes = [e for e in evidence if e.kind == KIND_DEADLINE_PUSHED]

    # The strongest combined signal is a block that ended with the task open
    # *and* a deadline that then moved. Linked pushes — ones that follow a
    # block closely — weigh more than unrelated ones, because they are much
    # more likely to be about the same episode.
    strength = 2.0 * len(blocks)
    for push in pushes:
        near = any(
            timedelta(0) <= (push.at - b.at) <= _LINK_WINDOW for b in blocks
        )
        strength += 3.0 if near else 1.0

    return Episode(
        key=key, task=draft.task, evidence=tuple(evidence), strength=strength
    )


def _key(uuid: str, first_at: datetime) -> str:
    """Stable identity for an episode: the task, and when it opened.

    Dated to the day rather than the instant, so re-running a check-in a few
    hours later doesn't invent a new episode out of the same evidence.
    """
    return f"{uuid}@{first_at.date().isoformat()}"
