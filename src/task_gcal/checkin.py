"""`task-gcal checkin`: the optional retrospective.

Optional and retrospective, not a daily habit requirement. It walks scheduled
blocks that have ended and haven't been explained yet — whether from yesterday
or from a week ago — and asks two questions about each. Run it daily, every
few days, or not at all; reflections stay open until answered, so the weekly
review works just as well as a morning routine.

What it will not do, and why each matters:

- **It never records an outcome without confirmation.** Existing evidence may
  suggest a commitment wasn't met; only you can say what happened.
- **It never guesses a reason.** `unknown` is offered, kept, and reported as
  missing classification rather than folded into a plausible bucket.
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
    OUTCOME_CANCELLED,
    OUTCOME_DONE,
    OUTCOME_NOT_STARTED,
    OUTCOME_PARTIAL,
    OUTCOME_UNKNOWN,
    REASON_AVOIDED,
    REASON_BLOCKED,
    REASON_CAPACITY,
    REASON_ESTIMATE,
    REASON_REPRIORITIZED,
    REASON_UNKNOWN,
    REASON_LABELS,
    Reflection,
)
from .review.episodes import DEFAULT_SINCE_DAYS, Episode, find_unresolved
from .review.facts import collect
from .review.observed import build_timelines
from .review.periods import Period

# Single-key answers, in the order the prompt lists them.
_OUTCOME_KEYS = {
    "d": OUTCOME_DONE,
    "p": OUTCOME_PARTIAL,
    "n": OUTCOME_NOT_STARTED,
    "x": OUTCOME_CANCELLED,
    "u": OUTCOME_UNKNOWN,
}

_REASON_KEYS = {
    "e": REASON_ESTIMATE,
    "b": REASON_BLOCKED,
    "w": REASON_CAPACITY,
    "r": REASON_REPRIORITIZED,
    "a": REASON_AVOIDED,
    "u": REASON_UNKNOWN,
}

# Outcomes that mean no commitment was actually missed, so asking *why* it was
# missed would be a question with no answer.
_NO_REASON_NEEDED = (OUTCOME_DONE, OUTCOME_CANCELLED)

# Outcomes where time may have been spent, so an actual is worth offering.
_TIME_MAY_HAVE_PASSED = (OUTCOME_DONE, OUTCOME_PARTIAL)


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
    outcome = _ask_choice(
        console,
        "  What happened? [d]one / [p]artial / [n]ot started / "
        "[x]cancelled / [u]nknown: ",
        _OUTCOME_KEYS,
    )
    if outcome is None:
        return None

    reason = REASON_UNKNOWN
    if outcome not in _NO_REASON_NEEDED:
        console.write("  Primary reason:")
        for key, value in _REASON_KEYS.items():
            console.write(f"    [{key}] {REASON_LABELS[value]}")
        reason = _ask_choice(console, "  Reason: ", _REASON_KEYS) or REASON_UNKNOWN

    actual = None
    if outcome in _TIME_MAY_HAVE_PASSED:
        actual = _ask_minutes(console)

    note = _ask_text(console, "  Note (optional): ")

    return Reflection(
        episode=episode.key,
        task_uuid=episode.task.uuid,
        outcome=outcome,
        reason=reason,
        at=now,
        covers_until=episode.covers_until,
        actual_minutes=actual,
        note=note,
    )


def _ask_choice(
    console: Console, prompt: str, choices: dict[str, str]
) -> Optional[str]:
    while True:
        raw = _read(console, prompt).strip().lower()
        if not raw:
            return None  # skip
        if raw in ("q", "quit"):
            raise _Abort
        if raw in choices:
            return choices[raw]
        # Also accept the full word, so `partial` works as well as `p`.
        for value in choices.values():
            if value == raw:
                return value
        console.write(f"  ? one of {', '.join(sorted(choices))}, or Enter to skip")


def _ask_minutes(console: Console) -> Optional[int]:
    while True:
        raw = _read(console, "  Actual minutes (optional): ").strip()
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
