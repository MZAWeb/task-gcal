"""The append-only observation journal.

Almost every interesting number in a review is a *diff between two runs*:
did `due` move? did the block we placed survive? was the task still open the
next day? None of that is reconstructable after the fact, which is why the
journal exists and why it has to be written before the reviews that read it.

Two invariants, enforced by the module split rather than by discipline:

- `observe.py` writes and cannot read. Scheduling appends observations and
  never consults history to make a placement decision, so a corrupt or
  deleted journal can never produce a wrong calendar.
- `store.py` reads and returns raw records. Nothing derived is ever stored,
  so every metric is recomputed from observations and a definition change
  can't leave stale numbers behind.
"""

from __future__ import annotations

from .observe import (
    DETAIL_CHOICES,
    DETAIL_FULL,
    DETAIL_MINIMAL,
    DETAIL_OFF,
    build_record,
    record_run,
)
from .paths import data_dir, run_file, runs_dir
from .records import (
    METRICS_VERSION,
    MODE_BACKFILL,
    MODE_SCHEDULE,
    MODE_SNAPSHOT,
    SCHEMA_VERSION,
    ObservedBlock,
    RunRecord,
    TaskObservation,
    settings_hash,
)
from .store import (
    JournalRead,
    JournalWriteError,
    append,
    definition_boundaries,
    iter_observed_days,
    latest_settings_hash,
    load,
    run_files,
)

__all__ = [
    "DETAIL_CHOICES",
    "DETAIL_FULL",
    "DETAIL_MINIMAL",
    "DETAIL_OFF",
    "JournalRead",
    "JournalWriteError",
    "METRICS_VERSION",
    "MODE_BACKFILL",
    "MODE_SCHEDULE",
    "MODE_SNAPSHOT",
    "ObservedBlock",
    "RunRecord",
    "SCHEMA_VERSION",
    "TaskObservation",
    "append",
    "build_record",
    "data_dir",
    "definition_boundaries",
    "iter_observed_days",
    "latest_settings_hash",
    "load",
    "record_run",
    "run_file",
    "run_files",
    "runs_dir",
    "settings_hash",
]
