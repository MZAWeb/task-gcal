"""Turning state into a journal record.

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


def detail_fields(
    description: str, detail: str
) -> tuple[Optional[str], Optional[str]]:
    """`(description, description_hash)` at the configured detail level.

    The hash is a stable stand-in for a title we've been asked not to store:
    enough to tell two tasks apart and to notice a retitle, which is all the
    churn metrics need, and not enough to read.
    """
    if detail != DETAIL_MINIMAL:
        return description, None
    return None, hashlib.sha256(description.encode()).hexdigest()[:12]


def observe_task(
    t: TaskInfo, *, block: Optional[ObservedBlock], detail: str
) -> TaskObservation:
    """One task's current state, at the configured level of detail."""
    description, description_hash = detail_fields(t.description, detail)
    return TaskObservation(
        uuid=t.uuid,
        id=t.id,
        description=description,
        description_hash=description_hash,
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


def observe_tasks(
    tasks: list[TaskInfo],
    *,
    blocks: dict[str, ObservedBlock],
    detail: str,
) -> tuple[TaskObservation, ...]:
    return tuple(
        observe_task(t, block=blocks.get(t.uuid), detail=detail) for t in tasks
    )


def build_record(
    *,
    settings: Settings,
    mode: str,
    at: datetime,
    observations: tuple[TaskObservation, ...],
    source_ok: bool = True,
) -> RunRecord:
    """Assemble the record for one observation. Pure — writes nothing."""
    return RunRecord(
        run_id=new_run_id(at),
        at=at,
        mode=mode,
        timezone_name=str(settings.timezone or settings.resolve_timezone()),
        settings_hash=settings_hash(settings),
        calendar_id=settings.calendar_id,
        report=settings.report,
        tasks=observations,
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
    """Append one live observation, never letting the journal break the run.

    Returns whether anything was written. A failed write warns and carries
    on: losing an observation costs history, but failing a calendar
    reconcile because a disk filled up would cost you the calendar. Backfill
    deliberately does *not* go through here — writing is its whole job, so a
    failure there is an error.
    """
    if settings.journal_detail == DETAIL_OFF:
        return False
    record = build_record(
        settings=settings,
        mode=mode,
        at=at,
        observations=observe_tasks(
            tasks, blocks=blocks, detail=settings.journal_detail
        ),
        source_ok=source_ok,
    )
    try:
        append(record)
    except JournalWriteError as e:
        print(f"  ! journal not written: {e}", file=sys.stderr)
        return False
    return True
