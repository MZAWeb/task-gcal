"""Turning a run's placements into a journal record.

The one-way boundary that makes the journal safe lives here: this module knows
how to *write* an observation and has no way to read one. Scheduling appends
and never reads history to make a placement decision, so a corrupt or deleted
journal can never produce a wrong calendar.

Task *fields* are not written here at all — those come from Taskwarrior's own
change log via `changes/`. What's left is the one thing Taskwarrior cannot
know: where each block was, and what happened to it.
"""

from __future__ import annotations

import sys
from datetime import datetime

from .. import __version__
from ..config import Settings
from .records import PlacementObservation, RunRecord, new_run_id, settings_hash
from .store import JournalWriteError, append

# `journal_detail` values.
DETAIL_FULL = "full"
DETAIL_MINIMAL = "minimal"
DETAIL_OFF = "off"
DETAIL_CHOICES = (DETAIL_FULL, DETAIL_MINIMAL, DETAIL_OFF)


def build_record(
    *,
    settings: Settings,
    mode: str,
    at: datetime,
    placements: tuple[PlacementObservation, ...],
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
        placements=placements,
        source_ok=source_ok,
        tool_version=__version__,
    )


def record_run(
    *,
    settings: Settings,
    mode: str,
    at: datetime,
    placements: tuple[PlacementObservation, ...],
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
        placements=placements,
        source_ok=source_ok,
    )
    try:
        append(record)
    except JournalWriteError as e:
        print(f"  ! journal not written: {e}", file=sys.stderr)
        return False
    return True
