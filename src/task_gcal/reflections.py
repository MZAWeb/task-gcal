"""Where check-in answers live.

Kept apart from the run journal, because it is a different kind of data. The
journal records what the tool observed; this records what *you* said happened,
and the difference matters in three ways:

- **Two dimensions, never collapsed.** What happened (`done`, `partial`,
  `not_started`, `cancelled`, `unknown`) and why (`estimate`, `blocked`,
  `capacity`, `reprioritized`, `avoided`, `unknown`). A single "status" field
  would force one of them to stand in for the other.
- **Answers are retrospective labels, not immutable facts**, so they can be
  corrected. The file stays append-only and the latest answer for an episode
  wins, which keeps corrections cheap and history intact.
- **`unknown` stays unknown.** It is reported as missing classification and
  never quietly folded into a plausible bucket, because a metric built on a
  guess is worse than one with a hole in it.

Nothing here is ever inferred. A record exists only because someone answered.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import __version__
from .journal.paths import data_dir, ensure_private

SCHEMA_VERSION = 1

FILENAME = "reflections.jsonl"

# What happened. Deliberately not a scale: `partial` is not "half of done",
# and `progressed` is not a lesser `done`.
OUTCOME_PARTIAL = "partial"
OUTCOME_NOT_STARTED = "not_started"
# The session did what it was for and the task continues — deliberate
# multi-session work, or something finished but awaiting follow-up. Not a
# miss, so the friction mix leaves it out.
OUTCOME_PROGRESSED = "progressed"
# Retained so records already on disk stay readable, but no longer offered:
# a task you actually finished gets `task done`, and one you gave up on gets
# `task delete` — neither is something you'd come here to say.
OUTCOME_DONE = "done"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_UNKNOWN = "unknown"
OUTCOMES = (
    OUTCOME_PARTIAL,
    OUTCOME_NOT_STARTED,
    OUTCOME_PROGRESSED,
    OUTCOME_DONE,
    OUTCOME_CANCELLED,
    OUTCOME_UNKNOWN,
)

# Why. One primary reason keeps the interaction fast; the optional note
# preserves nuance without this becoming a taxonomy editor.
REASON_ESTIMATE = "estimate"
REASON_BLOCKED = "blocked"
REASON_CAPACITY = "capacity"
REASON_REPRIORITIZED = "reprioritized"
REASON_AVOIDED = "avoided"
# Nothing went wrong: the work was always going to take more than one
# sitting. Counting it as friction would make good planning look like a
# problem.
REASON_FOLLOW_UP = "follow_up"
REASON_UNKNOWN = "unknown"
REASONS = (
    REASON_ESTIMATE,
    REASON_BLOCKED,
    REASON_CAPACITY,
    REASON_REPRIORITIZED,
    REASON_AVOIDED,
    REASON_FOLLOW_UP,
    REASON_UNKNOWN,
)

REASON_LABELS = {
    REASON_ESTIMATE: "estimate or scope was wrong",
    REASON_BLOCKED: "blocked by a dependency",
    REASON_CAPACITY: "week changed or capacity disappeared",
    REASON_REPRIORITIZED: "consciously reprioritized",
    REASON_AVOIDED: "avoided it",
    REASON_FOLLOW_UP: "always needed more than one sitting",
    REASON_UNKNOWN: "unknown or other",
}

OUTCOME_LABELS = {
    OUTCOME_PARTIAL: "partial",
    OUTCOME_NOT_STARTED: "not started",
    OUTCOME_PROGRESSED: "progressed",
    OUTCOME_DONE: "done",
    OUTCOME_CANCELLED: "cancelled",
    OUTCOME_UNKNOWN: "unknown",
}


@dataclass(frozen=True)
class Reflection:
    """One answered episode."""

    episode: str
    task_uuid: str
    outcome: str
    reason: str
    at: datetime
    # The latest evidence this answer accounts for. Evidence after it opens a
    # new episode, so a task that goes wrong again is asked about again.
    covers_until: datetime
    actual_minutes: Optional[int] = None
    note: Optional[str] = None
    # Block time the episode set aside, recorded alongside the answer. None
    # can't say whether the actual was more or less than planned.
    planned_minutes: Optional[int] = None

    @property
    def classified(self) -> bool:
        """False for `unknown`, which is missing data rather than a bucket."""
        return self.reason != REASON_UNKNOWN

    def to_dict(self) -> dict:
        out = {
            "schema": SCHEMA_VERSION,
            "tool_version": __version__,
            "episode": self.episode,
            "task_uuid": self.task_uuid,
            "outcome": self.outcome,
            "reason": self.reason,
            "at": _iso(self.at),
            "covers_until": _iso(self.covers_until),
        }
        if self.actual_minutes is not None:
            out["actual_minutes"] = self.actual_minutes
        if self.planned_minutes is not None:
            out["planned_minutes"] = self.planned_minutes
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["Reflection"]:
        episode = raw.get("episode")
        at = _dt(raw.get("at"))
        if not isinstance(episode, str) or at is None:
            return None
        outcome = raw.get("outcome")
        reason = raw.get("reason")
        minutes = raw.get("actual_minutes")
        planned = raw.get("planned_minutes")
        return cls(
            episode=episode,
            task_uuid=raw.get("task_uuid") or "",
            outcome=outcome if outcome in OUTCOMES else OUTCOME_UNKNOWN,
            reason=reason if reason in REASONS else REASON_UNKNOWN,
            at=at,
            covers_until=_dt(raw.get("covers_until")) or at,
            actual_minutes=_minutes(minutes),
            planned_minutes=_minutes(planned),
            note=raw.get("note") or None,
        )


def path() -> Path:
    return data_dir() / FILENAME


def append(reflection: Reflection) -> Path:
    """Append one answer. A correction is just a later line for the same key."""
    target = path()
    ensure_private(target.parent)
    line = (
        json.dumps(reflection.to_dict(), ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return target


def load() -> dict[str, Reflection]:
    """The current answer for each episode: latest line wins.

    Latest by file order rather than by `at`, so an explicit correction made
    on a machine with a skewed clock still takes effect.
    """
    target = path()
    if not target.is_file():
        return {}
    answers: dict[str, Reflection] = {}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue  # a truncated final line, most likely
        if not isinstance(raw, dict):
            continue
        reflection = Reflection.from_dict(raw)
        if reflection is not None:
            answers[reflection.episode] = reflection
    return answers


def for_task(answers: dict[str, Reflection], uuid: str) -> list[Reflection]:
    return sorted(
        (r for r in answers.values() if r.task_uuid == uuid),
        key=lambda r: r.covers_until,
    )


def answered_until(answers: dict[str, Reflection], uuid: str) -> Optional[datetime]:
    """The latest evidence already accounted for on this task, if any."""
    existing = for_task(answers, uuid)
    return existing[-1].covers_until if existing else None


def _minutes(raw) -> Optional[int]:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dt(raw) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None
