"""Durable task-change history, harvested from Taskwarrior.

The division of labour that makes this trustworthy:

- `taskchampion.py` reads Taskwarrior's operation log — exact, fast, and
  *not durable*, because a sync can prune it.
- `harvest.py` copies unseen operations here, on any command that already
  talks to Taskwarrior. That's why there's no cron: field changes happen when
  you edit a task, and the next run picks them up however long the gap was.
- `store.py` is the file reviews actually read, so a report of last month
  can't change its answer after a sync.

Nothing derived is stored — these are raw field transitions, and every metric
is recomputed from them.
"""

from __future__ import annotations

from .harvest import (
    CONTINUED,
    REBUILT,
    STARTED,
    UNAVAILABLE,
    HarvestReport,
    describe,
    harvest,
)
from .records import (
    FIELD_DESCRIPTION,
    FIELD_DUE,
    FIELD_END,
    FIELD_ENTRY,
    FIELD_ESTIMATE,
    FIELD_PROJECT,
    FIELD_SCHEDULED,
    FIELD_STATUS,
    FIELD_WAIT,
    PROPERTY_FIELDS,
    SCHEMA_VERSION,
    SOURCE_JOURNAL,
    SOURCE_TASKCHAMPION,
    Gap,
    TaskChange,
    interpret,
)
from .store import ChangeHistory, ChangeWriteError, append, load, path

__all__ = [
    "CONTINUED",
    "ChangeHistory",
    "ChangeWriteError",
    "FIELD_DESCRIPTION",
    "FIELD_DUE",
    "FIELD_END",
    "FIELD_ENTRY",
    "FIELD_ESTIMATE",
    "FIELD_PROJECT",
    "FIELD_SCHEDULED",
    "FIELD_STATUS",
    "FIELD_WAIT",
    "Gap",
    "HarvestReport",
    "PROPERTY_FIELDS",
    "REBUILT",
    "SCHEMA_VERSION",
    "SOURCE_JOURNAL",
    "SOURCE_TASKCHAMPION",
    "STARTED",
    "TaskChange",
    "UNAVAILABLE",
    "append",
    "describe",
    "harvest",
    "interpret",
    "load",
    "path",
]
