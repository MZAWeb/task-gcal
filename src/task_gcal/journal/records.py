"""What one journal record is, and how it round-trips through JSON.

The journal records *observations*, not omniscient history. If a due date
moves twice between runs we see one move; if it moves out and back we see
none. Every metric derived from it must therefore say "observed" and report
its sampling period.

Two rules keep it honest, and they belong here because this is where the
temptation lives:

- Nothing derived is ever stored. No scores, no running totals, no counts.
  Reviews recompute everything from the raw observations, so a metric whose
  definition changes doesn't leave a trail of numbers that meant something
  else.
- Every record carries the versions and settings it was written under, so a
  review can *refuse* to draw a trend across a boundary where the meaning
  changed instead of quietly averaging two different things.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import Settings

# Bumped when the record layout changes incompatibly. Readers tolerate
# unknown *fields* without a bump; a bump means "an old reader would get
# this wrong", which is a different thing.
SCHEMA_VERSION = 1

# Bumped when a metric *definition* changes, even though the record layout
# didn't. Phase 1's "capacity" is one work window; a later lane-aware one is
# a union of windows. Same field, different meaning, so trends must not span
# the boundary.
METRICS_VERSION = 1

# What a run was doing when it wrote the record. `dry-run` is deliberately
# absent: a dry run's placements were never made, so recording them would
# put phantom moves into placement churn.
MODE_SCHEDULE = "schedule"
MODE_SNAPSHOT = "snapshot"
MODE_BACKFILL = "backfill"

# Settings that change what a metric *means*, hashed into every record.
# Run-mechanical ones (lookback_days, removal_guard_ratio, event_color_id)
# are excluded: changing them doesn't make last week incomparable.
_MEANINGFUL_SETTINGS = (
    "work_start_hour",
    "work_end_hour",
    "work_days",
    "slot_align_minutes",
    "buffer_minutes",
    "overdue_horizon_days",
    "settle_days",
    "calendar_id",
    "report",
    "estimate_uda",
    "timezone",
)


def settings_hash(settings: Settings) -> str:
    """A short stable digest of the settings that affect metric meaning."""
    parts = []
    for name in _MEANINGFUL_SETTINGS:
        value = getattr(settings, name)
        if isinstance(value, frozenset):
            value = sorted(value)
        parts.append(f"{name}={value!r}")
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
    return digest[:12]


def new_run_id(now: datetime) -> str:
    """Sortable, collision-resistant, and readable in a text file."""
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dt(raw: Any) -> Optional[datetime]:
    """Parse a timestamp we wrote ourselves; None on anything unexpected.

    A record we can't fully parse is worth keeping for its other fields, so
    a bad value degrades to a missing one rather than dropping the line.
    """
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _int(raw: Any) -> Optional[int]:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


@dataclass(frozen=True)
class ObservedBlock:
    """A calendar block belonging to a task at the moment of observation."""

    start: datetime
    end: datetime
    # What this run did to it. None when the block was only observed
    # (`snapshot`), which is not the same as "nothing changed".
    action: Optional[str] = None
    # Why a settled placement had to be given up, when it did.
    moved_reason: Optional[str] = None


@dataclass(frozen=True)
class TaskObservation:
    """One task's state as a single run saw it."""

    uuid: str
    id: int = 0
    description: Optional[str] = None
    description_hash: Optional[str] = None
    project: Optional[str] = None
    tags: tuple[str, ...] = ()
    estimate_minutes: Optional[int] = None
    due: Optional[datetime] = None
    scheduled: Optional[datetime] = None
    wait: Optional[datetime] = None
    urgency: Optional[float] = None
    status: Optional[str] = None
    entry: Optional[datetime] = None
    end: Optional[datetime] = None
    overrides: Optional[str] = None
    block: Optional[ObservedBlock] = None

    @property
    def label(self) -> str:
        """Something safe to print, whatever the detail level was."""
        if self.description:
            return self.description
        if self.description_hash:
            return f"<{self.description_hash}>"
        return self.uuid[:8]

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"uuid": self.uuid}
        if self.id:
            out["id"] = self.id
        for key, value in (
            ("description", self.description),
            ("description_hash", self.description_hash),
            ("project", self.project),
            ("estimate_minutes", self.estimate_minutes),
            ("urgency", self.urgency),
            ("status", self.status),
            ("overrides", self.overrides),
        ):
            if value is not None:
                out[key] = value
        if self.tags:
            out["tags"] = list(self.tags)
        for key, value in (
            ("due", self.due),
            ("scheduled", self.scheduled),
            ("wait", self.wait),
            ("entry", self.entry),
            ("end", self.end),
        ):
            if value is not None:
                out[key] = _iso(value)
        if self.block is not None:
            out["placed_start"] = _iso(self.block.start)
            out["placed_end"] = _iso(self.block.end)
            if self.block.action:
                out["action"] = self.block.action
            if self.block.moved_reason:
                out["moved_reason"] = self.block.moved_reason
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["TaskObservation"]:
        """Build from a record line, or None if it has no usable identity."""
        uuid = raw.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            return None
        start, end = _dt(raw.get("placed_start")), _dt(raw.get("placed_end"))
        block = (
            ObservedBlock(
                start=start,
                end=end,
                action=raw.get("action"),
                moved_reason=raw.get("moved_reason"),
            )
            if start and end
            else None
        )
        tags = raw.get("tags")
        urgency = raw.get("urgency")
        return cls(
            uuid=uuid,
            id=_int(raw.get("id")) or 0,
            description=raw.get("description"),
            description_hash=raw.get("description_hash"),
            project=raw.get("project"),
            tags=tuple(tags) if isinstance(tags, list) else (),
            estimate_minutes=_int(raw.get("estimate_minutes")),
            due=_dt(raw.get("due")),
            scheduled=_dt(raw.get("scheduled")),
            wait=_dt(raw.get("wait")),
            urgency=float(urgency) if isinstance(urgency, (int, float)) else None,
            status=raw.get("status"),
            entry=_dt(raw.get("entry")),
            end=_dt(raw.get("end")),
            overrides=raw.get("overrides"),
            block=block,
        )


@dataclass(frozen=True)
class RunRecord:
    """Everything one run observed, plus how much to trust it."""

    run_id: str
    at: datetime
    mode: str
    timezone_name: str
    settings_hash: str
    calendar_id: str
    report: str
    tasks: tuple[TaskObservation, ...] = ()
    # False when the task source or the calendar came back wrong. A review
    # must not count an unhealthy run as an observed day.
    source_ok: bool = True
    schema: int = SCHEMA_VERSION
    metrics_version: int = METRICS_VERSION
    tool_version: str = ""
    # Fields a newer version wrote that this one doesn't know about, kept so
    # a round-trip through an older tool doesn't silently drop them.
    unknown: dict = field(default_factory=dict, compare=False)

    def to_dict(self) -> dict:
        out = {
            "schema": self.schema,
            "metrics_version": self.metrics_version,
            "tool_version": self.tool_version,
            "run_id": self.run_id,
            "at": _iso(self.at),
            "mode": self.mode,
            "timezone": self.timezone_name,
            "settings_hash": self.settings_hash,
            "calendar_id": self.calendar_id,
            "report": self.report,
            "source_ok": self.source_ok,
            "tasks": [t.to_dict() for t in self.tasks],
        }
        out.update(self.unknown)
        return out

    def to_line(self) -> str:
        """One JSON object, one line, no stray newlines from descriptions."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["RunRecord"]:
        at = _dt(raw.get("at"))
        run_id = raw.get("run_id")
        if at is None or not isinstance(run_id, str):
            return None
        known = {
            "schema", "metrics_version", "tool_version", "run_id", "at", "mode",
            "timezone", "settings_hash", "calendar_id", "report", "source_ok",
            "tasks",
        }
        tasks_raw = raw.get("tasks")
        tasks = []
        if isinstance(tasks_raw, list):
            for item in tasks_raw:
                if isinstance(item, dict):
                    obs = TaskObservation.from_dict(item)
                    if obs is not None:
                        tasks.append(obs)
        return cls(
            run_id=run_id,
            at=at,
            mode=raw.get("mode") or MODE_SCHEDULE,
            timezone_name=raw.get("timezone") or "UTC",
            settings_hash=raw.get("settings_hash") or "",
            calendar_id=raw.get("calendar_id") or "",
            report=raw.get("report") or "",
            tasks=tuple(tasks),
            source_ok=bool(raw.get("source_ok", True)),
            schema=_int(raw.get("schema")) or SCHEMA_VERSION,
            metrics_version=_int(raw.get("metrics_version")) or METRICS_VERSION,
            tool_version=raw.get("tool_version") or "",
            unknown={k: v for k, v in raw.items() if k not in known},
        )
