"""Reading and writing the JSONL journal.

Append-only, one JSON object per line, one file per UTC month. The volume is
single-digit megabytes a year, so there is no rotation, no pruning, and
deliberately no database — a text file you can `grep`, and deleting it costs
you history and nothing else.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .paths import ensure_private, run_file, runs_dir
from .records import MODE_SCHEDULE, RunRecord

# Modes whose records describe the setup that was actually in force.
_LIVE_MODES = (MODE_SCHEDULE,)


class JournalWriteError(Exception):
    """The journal could not be appended to.

    Callers on the scheduling path must catch this and carry on: losing an
    observation is a cost, but failing a calendar reconcile because a disk
    was full would be a much worse one.
    """


def append(record: RunRecord) -> Path:
    """Append one record and return the file it landed in.

    The write is a single `os.write` of one line, which is what makes
    concurrent appends safe: POSIX guarantees an `O_APPEND` write of less
    than `PIPE_BUF` lands atomically, so two runs finishing at once
    interleave records but never halves of a record.
    """
    line = (record.to_line() + "\n").encode()
    path = run_file(record.at)
    try:
        ensure_private(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as e:
        raise JournalWriteError(f"could not write {path}: {e}") from e
    return path


@dataclass
class JournalRead:
    """Records, and how much of the journal we couldn't read.

    Coverage is a first-class part of every review, so "how many lines were
    unreadable" has to come back with the data rather than being swallowed.
    """

    records: list[RunRecord] = field(default_factory=list)
    files_read: int = 0
    # Lines that weren't valid JSON, or were valid JSON but not a record.
    unreadable_lines: int = 0
    # A truncated final line is expected — a run killed mid-append — and is
    # counted separately because it is not a sign of corruption.
    truncated_tails: int = 0

    @property
    def healthy(self) -> bool:
        return self.unreadable_lines == 0


def _month_keys_in_range(
    since: Optional[datetime], until: Optional[datetime]
) -> Optional[tuple[str, str]]:
    """`(low, high)` month keys to keep, or None for "everything"."""
    if since is None and until is None:
        return None
    low = f"{since.astimezone(timezone.utc):%Y-%m}" if since else "0000-00"
    high = f"{until.astimezone(timezone.utc):%Y-%m}" if until else "9999-99"
    return low, high


def run_files(
    *, since: Optional[datetime] = None, until: Optional[datetime] = None
) -> list[Path]:
    """Monthly files that could hold records in the range, oldest first."""
    directory = runs_dir()
    if not directory.is_dir():
        return []
    bounds = _month_keys_in_range(since, until)
    files = sorted(p for p in directory.glob("*.jsonl") if p.is_file())
    if bounds is None:
        return files
    low, high = bounds
    return [p for p in files if low <= p.stem <= high]


def load(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    modes: Optional[tuple[str, ...]] = None,
) -> JournalRead:
    """Read records in `[since, until)`, oldest first.

    Tolerant on purpose. A record written by a newer tool keeps its unknown
    fields; a line we can't parse at all is counted and skipped; a truncated
    last line — a run killed mid-append — is expected rather than fatal.
    """
    out = JournalRead()
    for path in run_files(since=since, until=until):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines(
                keepends=True
            )
        except OSError:
            out.unreadable_lines += 1
            continue
        out.files_read += 1
        for index, raw in enumerate(lines):
            text = raw.strip()
            if not text:
                continue
            is_last = index == len(lines) - 1
            record = _parse_line(text)
            if record is None:
                # An unterminated last line is a run that died mid-append.
                if is_last and not raw.endswith("\n"):
                    out.truncated_tails += 1
                else:
                    out.unreadable_lines += 1
                continue
            if since is not None and record.at < since:
                continue
            if until is not None and record.at >= until:
                continue
            if modes is not None and record.mode not in modes:
                continue
            out.records.append(record)
    out.records.sort(key=lambda r: r.at)
    return out


def _parse_line(text: str) -> Optional[RunRecord]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return RunRecord.from_dict(raw)


def iter_observed_days(records: list[RunRecord], tz) -> Iterator[str]:
    """Local dates the journal actually observed, for coverage lines.

    Only healthy runs count: a run whose source came back empty saw nothing,
    and letting it stand in for a day would turn missing data into zero.
    """
    seen: set[str] = set()
    for r in records:
        if not r.source_ok:
            continue
        day = r.at.astimezone(tz).date().isoformat()
        if day not in seen:
            seen.add(day)
            yield day


def latest_settings_hash(records: list[RunRecord]) -> Optional[str]:
    for r in reversed(records):
        if r.settings_hash:
            return r.settings_hash
    return None


def definition_boundaries(records: list[RunRecord]) -> list[datetime]:
    """When settings or metric definitions changed inside this range.

    A review annotates these rather than averaging across them: the same
    field can mean two different things on either side of one.
    """
    boundaries: list[datetime] = []
    previous: Optional[tuple[str, int]] = None
    for r in records:
        if r.mode not in _LIVE_MODES:
            continue
        marker = (r.settings_hash, r.metrics_version)
        if previous is not None and marker != previous:
            boundaries.append(r.at)
        previous = marker
    return boundaries
