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

# Bumped when the record layout changes incompatibly. Readers tolerate
# unknown *fields* without a bump; a bump means "an old reader would get
# this wrong", which is a different thing.
SCHEMA_VERSION = 1

# Bumped when a metric *definition* changes, even though the record layout
# didn't. Phase 1's "capacity" is one work window; a later lane-aware one is
# a union of windows. Same field, different meaning, so trends must not span
# the boundary.
METRICS_VERSION = 1

# What a run was doing when it wrote the record. Only scheduling writes now,
# but the field stays: it's what lets a reader tell records written by a run
# that actually touched the calendar from anything a later version adds. A dry
# run is deliberately not a mode — its placements were never made, so
# recording them would put phantom moves into placement churn.
MODE_SCHEDULE = "schedule"

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


def settings_hash(settings) -> str:
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


def _strings(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _int(raw: Any) -> Optional[int]:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


@dataclass(frozen=True)
class PlacementObservation:
    """Where one task's block was, and what happened to it.

    Deliberately holds no task fields — not the description, not the due date,
    not the estimate. Those come from Taskwarrior's own change history now, so
    duplicating them here would be two sources to disagree with each other.
    It also means this file contains no task titles at all.

    Identity is the Google event id, not the task uuid: a block deleted and
    recreated elsewhere is a different block, and diffing by task alone would
    silently merge the two.
    """

    task_uuid: str
    event_id: str
    start: datetime
    end: datetime
    # What this run did: create | update | unchanged.
    action: Optional[str] = None
    # Why a settled placement had to be given up, when it was.
    moved_reason: Optional[str] = None
    # What had changed under us before this run touched anything: `moved`,
    # `retitled`, or both. Recorded once, when it's noticed — the run adopts
    # the new position, so a hand-move isn't re-reported for the rest of the
    # block's life.
    drift: tuple[str, ...] = ()
    # Where we had left it, when drift was noticed.
    drifted_from: Optional[datetime] = None
    # Where we found it. Only written when it isn't `start` — that is, when the
    # run went on to move the block somewhere else — so the three together are
    # the whole sequence: where we had put it, where somebody moved it to, and
    # where it ended up.
    drifted_to: Optional[datetime] = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "uuid": self.task_uuid,
            "event": self.event_id,
            "start": _iso(self.start),
            "end": _iso(self.end),
        }
        for key, value in (
            ("action", self.action),
            ("moved_reason", self.moved_reason),
        ):
            if value:
                out[key] = value
        if self.drift:
            out["drift"] = list(self.drift)
        if self.drifted_from is not None:
            out["drifted_from"] = _iso(self.drifted_from)
        if self.drifted_to is not None and self.drifted_to != self.start:
            out["drifted_to"] = _iso(self.drifted_to)
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["PlacementObservation"]:
        uuid, event = raw.get("uuid"), raw.get("event")
        start, end = _dt(raw.get("start")), _dt(raw.get("end"))
        if not isinstance(uuid, str) or not isinstance(event, str):
            return None
        if start is None or end is None:
            return None
        return cls(
            task_uuid=uuid,
            event_id=event,
            start=start,
            end=end,
            action=raw.get("action"),
            moved_reason=raw.get("moved_reason"),
            drift=_strings(raw.get("drift")),
            drifted_from=_dt(raw.get("drifted_from")),
            drifted_to=_dt(raw.get("drifted_to")),
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
    placements: tuple[PlacementObservation, ...] = ()
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
            "placements": [p.to_dict() for p in self.placements],
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
            "placements",
        }
        placements = []
        raw_placements = raw.get("placements")
        if isinstance(raw_placements, list):
            for item in raw_placements:
                if isinstance(item, dict):
                    observed = PlacementObservation.from_dict(item)
                    if observed is not None:
                        placements.append(observed)
        return cls(
            run_id=run_id,
            at=at,
            mode=raw.get("mode") or MODE_SCHEDULE,
            timezone_name=raw.get("timezone") or "UTC",
            settings_hash=raw.get("settings_hash") or "",
            calendar_id=raw.get("calendar_id") or "",
            report=raw.get("report") or "",
            placements=tuple(placements),
            source_ok=bool(raw.get("source_ok", True)),
            schema=_int(raw.get("schema")) or SCHEMA_VERSION,
            metrics_version=_int(raw.get("metrics_version")) or METRICS_VERSION,
            tool_version=raw.get("tool_version") or "",
            unknown={k: v for k, v in raw.items() if k not in known},
        )
