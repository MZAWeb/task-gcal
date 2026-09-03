"""Parsing Taskwarrior's human-readable modification log.

`task <uuid> info` prints a per-field change log with dates, which is the
only place a *past* due-date push is recorded. That makes it the one way to
seed history before the journal existed — and a bad permanent dependency:
it is one subprocess per task, it is formatted for humans, and it renders
timestamps in the local zone with no offset. So this module exists to be
used once, by `backfill.py`, and never on the scheduling path.

Everything here is deliberately conservative. Values are captured only for
fields whose values can't contain a quote (timestamps and numbers); a
description change is recorded as *having happened* without capturing the
text. A line we don't recognize is counted and ignored, never guessed at.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Optional

# Fields worth reconstructing. `description` is included because repeated
# rewrites identify a task that was really a project, but its values are
# never captured.
FIELD_DUE = "due"
FIELD_ESTIMATE = "estimate"
FIELD_SCHEDULED = "scheduled"
FIELD_WAIT = "wait"
FIELD_STATUS = "status"
FIELD_PROJECT = "project"
FIELD_DESCRIPTION = "description"

KIND_SET = "set"
KIND_CHANGED = "changed"
KIND_DELETED = "deleted"

# Taskwarrior renders timestamps in the info report as local wall clock with
# no offset, so a zone has to be supplied from outside.
_STAMP = "%Y-%m-%d %H:%M:%S"

# The header that opens the modification table, and the column the text
# starts in. Widths shift with terminal width, so the position is measured
# rather than assumed.
_HEADER = re.compile(r"^(Date\s+)Modification\s*$")

# Loosely "this line starts a new modification", used to split wrapped text
# before any value is parsed. A wrapped continuation like `00:00:00'.` can't
# match it, so it joins the entry it belongs to.
_STARTS_ENTRY = re.compile(
    r"^[A-Z][A-Za-z_.]*\s+(?:set to|changed from|deleted)\b"
)

# "This line opens a new timestamp group." At 80 columns the date column is
# too narrow for a full timestamp, so Taskwarrior puts the date on one line
# and the time on the next — the date half is what starts a group.
_DATE_START = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class FieldChange:
    """One observed change to one field, as Taskwarrior recorded it."""

    at: datetime  # tz-aware UTC
    field: str
    kind: str  # set | changed | deleted
    old: Optional[str] = None  # raw, as printed; None when set from nothing
    new: Optional[str] = None  # raw, as printed; None when deleted


@dataclass
class TaskHistory:
    """A task's change log, plus what we couldn't read of it."""

    uuid: str
    changes: list[FieldChange] = field(default_factory=list)
    # Modification lines we didn't recognize. Expected to be non-zero —
    # annotations, tags, starts and stops all land here — and reported so a
    # sudden jump is visible rather than silent.
    unrecognized: int = 0

    def of(self, field_name: str) -> list[FieldChange]:
        return [c for c in self.changes if c.field == field_name]


@dataclass
class _Group:
    """The modifications Taskwarrior printed under one timestamp."""

    date_text: str
    entries: list[str] = field(default_factory=list)


def _split_entries(
    lines: list[str], text_col: int
) -> tuple[list[_Group], int]:
    """Regroup the table's lines by timestamp, folding wrapping away.

    Three kinds of continuation, all of which look the same to a naive
    line-by-line reader:

    - Several modifications share one timestamp; the date is printed once.
    - The timestamp itself wraps, date on one line and time on the next, so
      a group's own timestamp isn't complete until its second line.
    - A single modification's text wraps, splitting a quoted value in half.

    Also returns how many modification lines arrived before any timestamp we
    could recognize. That count matters: a table whose date column we don't
    understand at all would otherwise come back empty *and* healthy.
    """
    groups: list[_Group] = []
    orphans = 0
    for raw in lines:
        if not raw.strip():
            break  # the table ends at the first blank line
        date_part = raw[:text_col].strip()
        text_part = raw[text_col:].strip()

        if _DATE_START.match(date_part):
            groups.append(_Group(date_text=date_part))
        elif date_part and groups:
            # The time half of the timestamp above it.
            groups[-1].date_text = f"{groups[-1].date_text} {date_part}"

        if not text_part:
            continue
        if not groups:
            orphans += 1
            continue
        group = groups[-1]
        if group.entries and not _STARTS_ENTRY.match(text_part):
            group.entries[-1] = f"{group.entries[-1]} {text_part}"
        else:
            group.entries.append(text_part)
    return groups, orphans


def _field_patterns(estimate_uda: str) -> list[tuple[str, re.Pattern]]:
    """`(field, pattern)` pairs, in the order they should be tried.

    The estimate's phrasing follows its UDA name, so a user who calls theirs
    `est` gets `Est set to '30'.` and this has to be built per run.
    """
    def word(name: str) -> str:
        return re.escape(name)

    quoted = r"'([^']*)'"
    pairs: list[tuple[str, re.Pattern]] = []
    for field_name, printed in (
        (FIELD_DUE, "due"),
        (FIELD_SCHEDULED, "scheduled"),
        (FIELD_WAIT, "wait"),
        (FIELD_STATUS, "status"),
        (FIELD_PROJECT, "project"),
        (FIELD_ESTIMATE, estimate_uda),
    ):
        pairs.append((
            field_name,
            re.compile(
                rf"^{word(printed)}\s+(?:"
                rf"set to {quoted}"
                rf"|changed from {quoted} to {quoted}"
                rf"|deleted(?:\s+\(was {quoted}\))?"
                rf")",
                re.IGNORECASE,
            ),
        ))
    # Values are never captured for a description: they are free text and can
    # contain the quote the other patterns rely on.
    pairs.append((
        FIELD_DESCRIPTION,
        re.compile(r"^description\s+(?:set to|changed from|deleted)", re.IGNORECASE),
    ))
    return pairs


def _classify(text: str) -> str:
    """Which kind of change a matched modification's wording describes."""
    lowered = text.lower()
    if " changed from " in lowered:
        return KIND_CHANGED
    if " deleted" in lowered:
        return KIND_DELETED
    return KIND_SET


def parse_info(
    text: str, *, uuid: str, tz: tzinfo, estimate_uda: str = "estimate"
) -> TaskHistory:
    """Extract the change log from one `task <uuid> info` output.

    Timestamps are local wall clock in the output, so `tz` is required and a
    machine that has moved timezones will be off by the difference. That is
    a real limitation of the source, not something to paper over.
    """
    history = TaskHistory(uuid=uuid)
    lines = text.splitlines()
    text_col = None
    for index, line in enumerate(lines):
        match = _HEADER.match(line)
        if match:
            text_col = len(match.group(1))
            # Skip the rule of dashes under the header.
            start = index + 2
            break
    if text_col is None:
        return history

    patterns = _field_patterns(estimate_uda)
    groups, orphans = _split_entries(lines[start:], text_col)
    history.unrecognized += orphans
    for group in groups:
        at = _parse_stamp(group.date_text, tz)
        if at is None:
            # An undatable group is unusable: a change we can't place in time
            # can't contribute to any observation.
            history.unrecognized += len(group.entries)
            continue
        for mod_text in group.entries:
            change = _match_change(mod_text, at, patterns)
            if change is None:
                history.unrecognized += 1
            else:
                history.changes.append(change)
    history.changes.sort(key=lambda c: c.at)
    return history


def _match_change(
    mod_text: str, at: datetime, patterns: list[tuple[str, re.Pattern]]
) -> Optional[FieldChange]:
    """One modification line as a `FieldChange`, or None if we don't model it."""
    for field_name, pattern in patterns:
        match = pattern.match(mod_text)
        if match is None:
            continue
        kind = _classify(mod_text)
        old, new = _values(kind, [g for g in match.groups() if g is not None])
        described = field_name == FIELD_DESCRIPTION
        return FieldChange(
            at=at,
            field=field_name,
            kind=kind,
            old=None if described else old,
            new=None if described else new,
        )
    return None


def _values(kind: str, groups: list[str]) -> tuple[Optional[str], Optional[str]]:
    """Map a pattern's captured groups onto `(old, new)` by wording."""
    if kind == KIND_SET:
        return None, groups[0] if groups else None
    if kind == KIND_CHANGED:
        if len(groups) >= 2:
            return groups[0], groups[1]
        return None, None
    return (groups[0] if groups else None), None


def _parse_stamp(text: str, tz: tzinfo) -> Optional[datetime]:
    try:
        naive = datetime.strptime(text.strip(), _STAMP)
    except ValueError:
        return None
    return naive.replace(tzinfo=tz)


def parse_local_value(raw: Optional[str], tz: tzinfo) -> Optional[datetime]:
    """Parse a timestamp *value* out of a modification line.

    Same format and the same local-zone caveat as the change's own date.
    """
    if not raw:
        return None
    return _parse_stamp(raw, tz)


# ---------------------------------------------------------------------------
# Running the command
# ---------------------------------------------------------------------------

# A width wide enough that a modification almost never wraps, and colour off
# so no escape sequence lands in the middle of a value. `_split_entries`
# still handles wrapping, because "almost never" isn't never.
_INFO_ARGV = (
    "rc.verbose=nothing",
    "rc.confirmation=no",
    "rc.color=off",
    "rc.defaultwidth=2000",
)


class HistoryUnavailable(Exception):
    """`task info` could not be read for a task."""


def fetch_info(uuid: str) -> str:
    """Raw `task <uuid> info` output.

    Separate from parsing so the parser can be tested against captured
    output rather than a live Taskwarrior.
    """
    if shutil.which("task") is None:
        raise HistoryUnavailable("`task` not found on PATH")
    try:
        proc = subprocess.run(
            ["task", *_INFO_ARGV, uuid, "info"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        raise HistoryUnavailable(f"`task {uuid} info` failed: {detail}") from None
    return proc.stdout


def load_history(
    uuid: str, *, tz: tzinfo, estimate_uda: str = "estimate"
) -> TaskHistory:
    return parse_info(
        fetch_info(uuid), uuid=uuid, tz=tz, estimate_uda=estimate_uda
    )
