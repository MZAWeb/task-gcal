"""The durable task-change history: our copy, in our file.

Reviews read only this. They never query TaskChampion directly, and the reason
matters: the operation log is a *synchronisation* log, so `purge.on-sync` can
remove rows once they've been synced. Reading it live would mean a review of
last month could answer differently after a sync — which makes a report
irreproducible, and reproducibility is the whole reason nothing derived is ever
stored.

One file rather than monthly shards. Records arrive in operation order, not
time order, so a month-named shard would be a lie about its contents; and every
consumer reads a wide window anyway, so sharding would bound nothing in
practice. At the observed rate (~24k operations over four months, ~4MB) this
stays a file you can `grep` for years. If it ever stops being that, sharding is
an optimisation we can add without changing the record.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from ..journal.paths import data_dir, ensure_private
from .records import Gap, TaskChange

FILENAME = "changes.jsonl"


class ChangeWriteError(Exception):
    """The change history couldn't be appended to."""


@dataclass
class ChangeHistory:
    """Everything we've harvested, and how much of it we could read."""

    changes: list[TaskChange] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    unreadable_lines: int = 0
    truncated_tail: bool = False

    # ------------------------------ state ---------------------------------
    # Derived from the records themselves rather than a separate checkpoint
    # file: a crash between appending and updating a checkpoint would other-
    # wise silently skip operations, whereas re-reading a few is harmless.

    @property
    def high_water(self) -> int:
        """Highest operation id we've stored. 0 when we've stored none."""
        ids = [c.op_id for c in self.changes if c.op_id is not None]
        ids += [g.op_id for g in self.gaps]
        return max(ids, default=0)

    @property
    def anchor(self) -> Optional[tuple[int, str]]:
        """`(op_id, fingerprint)` of the newest record that carries one.

        Checked against the upstream log on the next harvest: a matching
        fingerprint means ordinary continuation, a different one at the same id
        means the database was rebuilt and its ids reused.
        """
        best: Optional[tuple[int, str]] = None
        for op_id, fingerprint in [
            (c.op_id, c.fingerprint) for c in self.changes
        ] + [(g.op_id, g.fingerprint) for g in self.gaps]:
            if op_id is None or not fingerprint:
                continue
            if best is None or op_id > best[0]:
                best = (op_id, fingerprint)
        return best

    @property
    def earliest(self) -> Optional[datetime]:
        """When our knowledge starts — the honest floor on any churn metric."""
        return min((c.at for c in self.changes), default=None)

    def known_keys(self) -> set[tuple]:
        return {c.key for c in self.changes}

    def for_uuid(self, uuid: str) -> list[TaskChange]:
        return [c for c in self.changes if c.uuid == uuid]


def path() -> Path:
    return data_dir() / FILENAME


def append(records: Iterable) -> int:
    """Append changes and gaps. Returns how many lines were written.

    One `os.write` per line in append mode, so two processes harvesting at once
    interleave records but never halves of a record.
    """
    lines = [r.to_line() + "\n" for r in records]
    if not lines:
        return 0
    target = path()
    try:
        ensure_private(target.parent)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            for line in lines:
                os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError as e:
        raise ChangeWriteError(f"could not write {target}: {e}") from e
    return len(lines)


def load() -> ChangeHistory:
    """Read the whole history, tolerating damage.

    A truncated final line is what a process killed mid-append leaves behind
    and is reported separately from real corruption.
    """
    history = ChangeHistory()
    target = path()
    if not target.is_file():
        return history
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        history.unreadable_lines += 1
        return history

    lines = text.splitlines(keepends=True)
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        parsed = _parse(stripped)
        if parsed is None:
            if index == len(lines) - 1 and not raw.endswith("\n"):
                history.truncated_tail = True
            else:
                history.unreadable_lines += 1
            continue
        if isinstance(parsed, Gap):
            history.gaps.append(parsed)
        else:
            history.changes.append(parsed)
    history.changes.sort(key=lambda c: (c.at, c.uuid, c.field))
    return history


def _parse(text: str):
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("gap"):
        return Gap.from_dict(raw)
    return TaskChange.from_dict(raw)
