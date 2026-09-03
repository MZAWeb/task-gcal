"""Turning a run's live state into a journal record.

The one-way boundary that makes the journal safe lives here: this module
knows how to *write* an observation and has no way to read one. Scheduling
appends and never reads history to make a placement decision, so a corrupt
or deleted journal can never produce a wrong calendar.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from typing import Optional

from .. import __version__
from ..config import Settings
from ..taskw import TaskInfo
from .records import (
    ObservedBlock,
    RunRecord,
    TaskObservation,
    new_run_id,
    settings_hash,
)
from .store import JournalWriteError, append

# `journal_detail` values.
DETAIL_FULL = "full"
DETAIL_MINIMAL = "minimal"
DETAIL_OFF = "off"
DETAIL_CHOICES = (DETAIL_FULL, DETAIL_MINIMAL, DETAIL_OFF)


def _hash_description(text: str) -> str:
    """A stable stand-in for a description we've been asked not to store.

    Enough to tell two tasks apart and to notice a title being rewritten,
    which is all the churn metrics need; not enough to read.
    """
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def observe_task(
    t: TaskInfo, *, block: Optional[ObservedBlock], detail: str
) -> TaskObservation:
    """One task's current state, at the configured level of detail."""
    minimal = detail == DETAIL_MINIMAL
    return TaskObservation(
        uuid=t.uuid,
        id=t.id,
        description=None if minimal else t.description,
        description_hash=_hash_description(t.description) if minimal else None,
        project=t.project,
        tags=tuple(t.tags),
        estimate_minutes=t.estimate_minutes,
        due=t.due,
        scheduled=t.scheduled,
        wait=t.wait,
        urgency=t.urgency,
        status=t.status,
        entry=t.entry,
        end=t.end,
        overrides=t.overrides_raw,
        block=block,
    )


def build_record(
    *,
    settings: Settings,
    mode: str,
    at: datetime,
    tasks: list[TaskInfo],
    blocks: dict[str, ObservedBlock],
    source_ok: bool = True,
) -> RunRecord:
    """Assemble the record for one run. Pure — writes nothing."""
    detail = settings.journal_detail
    return RunRecord(
        run_id=new_run_id(at),
        at=at,
        mode=mode,
        timezone_name=str(settings.timezone or settings.resolve_timezone()),
        settings_hash=settings_hash(settings),
        calendar_id=settings.calendar_id,
        report=settings.report,
        tasks=tuple(
            observe_task(t, block=blocks.get(t.uuid), detail=detail)
            for t in tasks
        ),
        source_ok=source_ok,
        tool_version=__version__,
    )


def record_run(
    *,
    settings: Settings,
    mode: str,
    at: datetime,
    tasks: list[TaskInfo],
    blocks: dict[str, ObservedBlock],
    source_ok: bool = True,
) -> bool:
    """Append one observation, never letting the journal break the run.

    Returns whether anything was written. A failed write warns and carries
    on: losing an observation costs history, but failing a calendar
    reconcile because a disk filled up would cost you the calendar.
    """
    if settings.journal_detail == DETAIL_OFF:
        return False
    record = build_record(
        settings=settings,
        mode=mode,
        at=at,
        tasks=tasks,
        blocks=blocks,
        source_ok=source_ok,
    )
    try:
        append(record)
    except JournalWriteError as e:
        print(f"  ! journal not written: {e}", file=sys.stderr)
        return False
    return True
