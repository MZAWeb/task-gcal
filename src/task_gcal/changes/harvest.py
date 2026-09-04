"""Copying TaskChampion's operations into our own durable history.

Runs on any command that already talks to Taskwarrior, which is what removes
the need for a cron job: field changes happen when *you* edit a task, and this
picks them up next time we run, exactly, however long the gap was.

Three failure modes get explicit treatment rather than optimism:

- **An operation we can't read** must never wedge the harvester. It's recorded
  as a gap and harvesting continues past it, because stopping would freeze
  history on one unrecognised row until someone shipped a fix.
- **Pruning.** If `purge.on-sync` removed operations before we saw them, the
  history has a hole. We detect it and say so instead of reporting a suspicious
  quiet patch.
- **A rebuilt database** reuses AUTOINCREMENT ids, so the id we stored can name
  a different operation. We anchor on `(op_id, fingerprint)` and re-harvest from
  the start when it doesn't match — safe, because identity is the change itself,
  so duplicates deduplicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .. import taskchampion as tc
from ..config import Settings
from . import store
from .records import (
    PROPERTY_FIELDS,
    SOURCE_TASKCHAMPION,
    FIELD_ESTIMATE,
    Gap,
    TaskChange,
)

# How the log related to what we'd already stored.
CONTINUED = "continued"
STARTED = "started"
REBUILT = "rebuilt"
UNAVAILABLE = "unavailable"


@dataclass
class HarvestReport:
    """What one harvest did, in terms `doctor` and coverage lines can print."""

    outcome: str = UNAVAILABLE
    reason: Optional[str] = None
    changes_added: int = 0
    gaps_added: int = 0
    operations_read: int = 0
    skipped_noise: int = 0
    # True when operations were pruned before we could harvest them, so the
    # history has a hole nothing can fill.
    lost_operations: bool = False
    # True when a sync server is configured, so pruning can happen in future
    # and coverage can no longer be promised as exact.
    sync_configured: bool = False
    schema_untested: bool = False
    earliest_known: Optional[datetime] = None

    @property
    def exact(self) -> bool:
        """Whether the stored history can be described as complete."""
        return (
            self.outcome in (CONTINUED, STARTED)
            and not self.lost_operations
            and not self.gaps_added
        )


def harvest(
    settings: Settings, *, db_path=None, history=None
) -> HarvestReport:
    """Copy any unseen operations into our history. Never raises."""
    existing = history if history is not None else store.load()
    report = HarvestReport(earliest_known=existing.earliest)

    try:
        state = _resume_point(existing, db_path=db_path)
    except tc.OperationsUnavailable as e:
        report.reason = str(e)
        return report

    after_id, report.outcome, report.lost_operations = state
    try:
        operations, meta = tc.read(path=db_path, after_id=after_id)
    except tc.OperationsUnavailable as e:
        report.outcome = UNAVAILABLE
        report.reason = str(e)
        return report

    report.operations_read = len(operations)
    report.sync_configured = meta.sync_configured
    report.schema_untested = not meta.schema_is_tested

    known = existing.known_keys()
    changes: list[TaskChange] = []
    gaps: list[Gap] = []
    estimate_property = settings.estimate_uda

    for op in operations:
        if not op.understood:
            gaps.append(
                Gap(
                    op_id=op.op_id,
                    kind=op.kind,
                    prop=op.prop,
                    fingerprint=op.fingerprint,
                )
            )
            continue
        if op.kind != tc.KIND_UPDATE:
            continue  # `Create` carries only a uuid; `UndoPoint` carries nothing
        if op.is_noise:
            report.skipped_noise += 1
            continue
        field = _field_for(op.prop, estimate_property)
        if field is None:
            continue  # a real property no metric has asked for
        change = TaskChange(
            at=op.at,
            uuid=op.uuid,
            field=field,
            old=op.old_value,
            new=op.value,
            source=SOURCE_TASKCHAMPION,
            op_id=op.op_id,
            fingerprint=op.fingerprint,
        )
        if change.key in known:
            continue  # already harvested, under a previous id after a rebuild
        known.add(change.key)
        changes.append(change)

    # Gaps are written alongside the changes, in operation order, so the
    # high-water mark advances past an unreadable row instead of stalling on it.
    try:
        store.append(sorted(changes + gaps, key=_op_order))
    except store.ChangeWriteError as e:
        report.outcome = UNAVAILABLE
        report.reason = str(e)
        return report

    report.changes_added = len(changes)
    report.gaps_added = len(gaps)
    if changes:
        report.earliest_known = min(
            [c.at for c in changes] + ([existing.earliest] if existing.earliest else [])
        )
    return report


def _op_order(record) -> int:
    return record.op_id or 0


def _field_for(prop: Optional[str], estimate_property: str) -> Optional[str]:
    if prop is None:
        return None
    if prop == estimate_property:
        return FIELD_ESTIMATE
    return PROPERTY_FIELDS.get(prop)


def _resume_point(existing, *, db_path) -> tuple[int, str, bool]:
    """`(after_id, outcome, lost_operations)` — where to resume, and why.

    The four cases the anchor distinguishes:

    - nothing stored yet: start from the beginning;
    - anchor still present with the same content: ordinary continuation;
    - anchor gone but everything remaining is newer than it: pruning that cost
      us nothing, because we'd already harvested past it;
    - anchor gone or changed with older operations still present, or the newest
      id having gone backwards: the database was rebuilt, so ids mean something
      else and we re-harvest from scratch.
    """
    anchor = existing.anchor
    if anchor is None:
        return 0, STARTED, False

    op_id, fingerprint = anchor
    found = tc.find(op_id, path=db_path)
    if found is not None and found.fingerprint == fingerprint:
        return op_id, CONTINUED, False

    _ops, meta = tc.read(path=db_path, after_id=0, limit=0)
    if found is None and meta.oldest_op_id is not None and meta.oldest_op_id > op_id:
        # Everything still there is newer than our anchor: pruning that only
        # removed rows we already have.
        return op_id, CONTINUED, False
    return 0, REBUILT, True


def describe(report: HarvestReport) -> str:
    """One line for `doctor`, honest about what it can and can't claim."""
    if report.outcome == UNAVAILABLE:
        return f"not harvested: {report.reason}"
    parts = [f"{report.changes_added} new change(s)"]
    if report.gaps_added:
        parts.append(f"{report.gaps_added} unreadable operation(s)")
    if report.outcome == REBUILT:
        parts.append("Taskwarrior's database was rebuilt; re-harvested")
    if report.lost_operations:
        parts.append("some operations were pruned before we saw them")
    if report.sync_configured:
        parts.append("sync is configured, so future pruning is possible")
    if report.schema_untested:
        parts.append("schema version is newer than tested")
    return "; ".join(parts)
