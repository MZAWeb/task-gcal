"""`task-gcal checkin`: the optional retrospective.

Optional and retrospective, not a daily habit requirement. It walks scheduled
blocks that have ended and haven't been explained yet — whether from yesterday
or from a week ago — and asks *one* question about each. Run it daily, every
few days, or not at all; reflections stay open until answered, so the weekly
review works just as well as a morning routine.

**One question, five answers.** The stored record still has two dimensions —
what happened, and why — because the metrics need them apart. But the prompt
doesn't: of the thirty combinations those two axes allow, about five actually
happen, so the question offers those five in the words you'd use out loud and
maps each to a pair. Two questions per episode was twice the friction for no
extra truth.

Two outcomes are deliberately not offered. A task you genuinely finished gets
`task done` and one you gave up on gets `task delete` — neither is something
you'd come here to say. Nor is "unknown": pressing Enter already skips, which
leaves the episode open to answer later, and that is the honest version of
not knowing.

What it will not do, and why each matters:

- **It never records an outcome without confirmation.** Existing evidence may
  suggest a commitment wasn't met; only you can say what happened.
- **It never guesses.** Skipping leaves the episode open rather than filing it
  under a plausible cause.
- **Unknown time stays unknown.** Actual minutes are optional and never
  default to the estimate — a fabricated actual is worse than no actual.
- **It asks about an episode, not a block.** Several passed blocks and a
  deadline change on the same task are one prompt.
- **Everything is skippable**, and skipping leaves the episode open.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from . import reflections as reflections_mod
from .config import Settings
from .reflections import (
    OUTCOME_NOT_STARTED,
    OUTCOME_PARTIAL,
    OUTCOME_PROGRESSED,
    REASON_AVOIDED,
    REASON_BLOCKED,
    REASON_CAPACITY,
    REASON_ESTIMATE,
    REASON_FOLLOW_UP,
    Reflection,
)
from .review.episodes import DEFAULT_SINCE_DAYS, Episode, find_unresolved
from .review.facts import collect
from .review.observed import build_timelines
from .review.periods import Period


@dataclass(frozen=True)
class _Answer:
    """One offered answer, and the `(outcome, reason)` pair it records."""

    key: str
    label: str
    outcome: str
    reason: str
    # Whether to follow up with "how long did it take?". Only worth asking
    # when work actually happened.
    ask_minutes: bool = False


# The five things that actually happen, in the words you'd use out loud. Order
# is roughly how often each comes up. Each maps to a two-dimensional record,
# so the friction mix and its coverage are unchanged by the shorter prompt.
_ANSWERS: tuple[_Answer, ...] = (
    _Answer(
        "1",
        "Made a start, but it needs more time than I set aside",
        OUTCOME_PARTIAL,
        REASON_ESTIMATE,
        ask_minutes=True,
    ),
    _Answer(
        "2",
        "Didn't feel like starting it, so I put it off",
        OUTCOME_NOT_STARTED,
        REASON_AVOIDED,
    ),
    _Answer(
        "3",
        "Was busy with something else — needs rescheduling",
        OUTCOME_NOT_STARTED,
        REASON_CAPACITY,
    ),
    _Answer(
        "4",
        "Blocked on someone or something else, so it has to wait",
        OUTCOME_NOT_STARTED,
        REASON_BLOCKED,
    ),
    _Answer(
        "5",
        "Did this session's work; there's a follow-up still to come",
        OUTCOME_PROGRESSED,
        REASON_FOLLOW_UP,
        ask_minutes=True,
    ),
)

_BY_KEY = {answer.key: answer for answer in _ANSWERS}


@dataclass
class Console:
    """Injected I/O, so the interaction is testable without a terminal."""

    read: Callable[[str], str] = input
    write: Callable[[str], None] = print
    interactive: bool = field(default_factory=lambda: sys.stdin.isatty())


@dataclass
class CheckinSummary:
    recorded: int = 0
    skipped: int = 0
    found: int = 0


class _Abort(Exception):
    """The user asked to stop. What's answered so far is kept."""


def checkin(
    settings: Settings,
    *,
    since: Optional[datetime] = None,
    now: Optional[datetime] = None,
    gcal=None,
    console: Optional[Console] = None,
) -> tuple[int, CheckinSummary]:
    """Ask about unexplained missed commitments. Writes only reflections."""
    console = console or Console()
    now = now or datetime.now(timezone.utc)
    since = since or (now - timedelta(days=DEFAULT_SINCE_DAYS))
    facts = collect_window(settings, since=since, now=now, gcal=gcal)
    episodes = open_episodes(facts, since=since, now=now)
    summary = CheckinSummary(found=len(episodes))

    if not facts.calendar_ok:
        # Blocks are where nearly all the evidence comes from, so with no
        # calendar "nothing to review" would be a claim we can't make.
        console.write(
            "The calendar could not be read, so blocks that passed can't be "
            "seen. Nothing has been recorded."
        )
        return 1, summary

    if not episodes:
        console.write(
            "Nothing to review: every block that has ended is either done or "
            "already explained."
        )
        return 0, summary

    if not console.interactive:
        # A listing is still useful from a script or a pipe; guessing answers
        # would not be.
        console.write(
            f"{len(episodes)} unexplained commitment(s). "
            "Run this from a terminal to answer them.\n"
        )
        for episode in episodes:
            console.write(_header(episode, settings.resolve_timezone()))
        return 0, summary

    console.write(
        f"{len(episodes)} unexplained commitment(s) since "
        f"{since.astimezone(settings.resolve_timezone()):%a %d %b}. "
        "Enter to skip, `q` to stop.\n"
    )

    tz = settings.resolve_timezone()
    for episode in episodes:
        console.write(_header(episode, tz))
        try:
            reflection = _ask(episode, console=console, now=now)
        except _Abort:
            console.write("Stopped. Everything unanswered stays open.")
            break
        if reflection is None:
            summary.skipped += 1
            continue
        reflections_mod.append(reflection)
        summary.recorded += 1

    left = summary.found - summary.recorded
    console.write(
        f"\nRecorded {summary.recorded}, left {left} open. "
        "Answers can be corrected by running this again."
    )
    return 0, summary


def collect_window(
    settings: Settings, *, since: datetime, now: datetime, gcal=None
):
    """Facts over the check-in window. The only I/O on this path."""
    window = Period(
        kind="window",
        label="check-in window",
        start=since,
        end=now,
        tz=settings.resolve_timezone(),
    )
    return collect(settings, window, now=now, gcal=gcal)


def open_episodes(facts, *, since: datetime, now: datetime) -> list[Episode]:
    """Unresolved episodes in `[since, now)`, strongest evidence first."""
    return find_unresolved(
        tasks=list(facts.tasks),
        blocks_by_task=facts.blocks_by_task(),
        timelines=build_timelines(facts.journal.records),
        answers=reflections_mod.load(),
        now=now,
        since=since,
    )


def _header(episode: Episode, tz) -> str:
    task = episode.task
    blocks = episode.blocks
    when = (
        f"{blocks[0].start.astimezone(tz):%a %d %b %H:%M}"
        if blocks and blocks[0].start
        else f"{episode.first_at.astimezone(tz):%a %d %b}"
    )
    planned = (
        f" (estimated {task.estimate_minutes}m)"
        if task.estimate_minutes
        else ""
    )
    lines = [f"\n{when}  {task.description}{planned}"]
    for line in episode.lines(tz):
        lines.append(f"  {line}")
    # Labelled as a suggestion, because that is all it is.
    lines.append(f"  Suggests: {episode.suggestion}")
    return "\n".join(lines)


def _ask(
    episode: Episode, *, console: Console, now: datetime
) -> Optional[Reflection]:
    for answer in _ANSWERS:
        console.write(f"    {answer.key}. {answer.label}")
    chosen = _ask_answer(console)
    if chosen is None:
        return None

    actual = (
        _ask_minutes(console, planned=episode.planned_minutes)
        if chosen.ask_minutes
        else None
    )
    note = _ask_text(console, "  Anything worth remembering? ")

    return Reflection(
        episode=episode.key,
        task_uuid=episode.task.uuid,
        outcome=chosen.outcome,
        reason=chosen.reason,
        at=now,
        covers_until=episode.covers_until,
        actual_minutes=actual,
        planned_minutes=episode.planned_minutes,
        note=note,
    )


def _ask_answer(console: Console) -> Optional[_Answer]:
    while True:
        raw = _read(console, "  Which of these? ").strip().lower()
        if not raw:
            return None  # skip; the episode stays open
        if raw in ("q", "quit"):
            raise _Abort
        if raw in _BY_KEY:
            return _BY_KEY[raw]
        console.write(
            f"  ? {', '.join(_BY_KEY)}, or Enter to skip and come back to it"
        )


def _ask_minutes(console: Console, *, planned: Optional[int]) -> Optional[int]:
    # Naming the time set aside matters when an episode coalesced several
    # blocks: "how long did it take" is otherwise ambiguous about whether it
    # means one sitting or all of them.
    aside = f" ({planned}m set aside)" if planned else ""
    while True:
        raw = _read(
            console, f"  How many minutes did you spend on it{aside}? "
        ).strip()
        if not raw:
            # Unknown stays unknown, and never becomes the estimate.
            return None
        if raw.lower() in ("q", "quit"):
            raise _Abort
        try:
            minutes = int(raw)
        except ValueError:
            console.write("  ? a whole number of minutes, or Enter to skip")
            continue
        if minutes <= 0:
            console.write("  ? more than zero, or Enter to skip")
            continue
        return minutes


def _ask_text(console: Console, prompt: str) -> Optional[str]:
    raw = _read(console, prompt).strip()
    if raw.lower() in ("q", "quit"):
        raise _Abort
    return raw or None


def _read(console: Console, prompt: str) -> str:
    try:
        return console.read(prompt)
    except EOFError:
        raise _Abort from None
