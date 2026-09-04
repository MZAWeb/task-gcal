"""What one harvested task change is.

Deliberately close to the source: a field, when it changed, and the raw values
either side. Epoch-second strings stay strings, because converting on the way in
would bake one interpretation into a record we can never re-derive if the
upstream log has since been pruned.

Property names are canonicalised on the way in, though — the estimate lives in
a user-named UDA, and renaming it shouldn't split its history in two.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA_VERSION = 1

# Canonical field names. The estimate is mapped from whatever the UDA is
# called, so `estimate_uda = "est"` and `estimate_uda = "estimate"` produce one
# continuous history rather than two halves.
FIELD_DUE = "due"
FIELD_ESTIMATE = "estimate"
FIELD_SCHEDULED = "scheduled"
FIELD_WAIT = "wait"
FIELD_DESCRIPTION = "description"
FIELD_PROJECT = "project"
FIELD_STATUS = "status"
FIELD_ENTRY = "entry"
FIELD_END = "end"

# TaskChampion property -> our field name. Anything absent here is either noise
# or something no metric has asked for; see `harvest.py`.
PROPERTY_FIELDS = {
    "due": FIELD_DUE,
    "scheduled": FIELD_SCHEDULED,
    "wait": FIELD_WAIT,
    "description": FIELD_DESCRIPTION,
    "project": FIELD_PROJECT,
    "status": FIELD_STATUS,
    "entry": FIELD_ENTRY,
    "end": FIELD_END,
}

# Fields whose raw values are epoch seconds.
TIMESTAMP_FIELDS = frozenset(
    {FIELD_DUE, FIELD_SCHEDULED, FIELD_WAIT, FIELD_ENTRY, FIELD_END}
)

# Fields whose values are free text you might not want on disk.
TEXT_FIELDS = frozenset({FIELD_DESCRIPTION, FIELD_PROJECT})

SOURCE_TASKCHAMPION = "taskchampion"
SOURCE_JOURNAL = "journal"

# Marks a value stored as a digest rather than as itself, so nobody reading
# the file — or this code — mistakes one for a very short title.
REDACTED_PREFIX = "sha256:"


@dataclass(frozen=True)
class TaskChange:
    """One field of one task changing, at a known instant."""

    at: datetime
    uuid: str
    field: str
    old: Optional[str] = None
    new: Optional[str] = None
    # Provenance. `op_id` is only a read optimisation — identity is the natural
    # key below, because a rebuilt database reuses ids.
    source: str = SOURCE_TASKCHAMPION
    op_id: Optional[int] = None
    fingerprint: str = ""

    @property
    def key(self) -> tuple:
        """Natural identity, so re-harvesting is idempotent.

        Not `op_id`: a recovered database restarts its AUTOINCREMENT, so the
        same id can name a different change. Keying on the change itself means
        a full re-harvest after a rebuild silently deduplicates instead of
        doubling every count.

        Text fields are identified without their values, because those may be
        stored redacted: turning `journal_detail` down and then re-harvesting
        would otherwise store the same rename twice, once each way, and every
        count of it would double.
        """
        if self.field in TEXT_FIELDS:
            return (self.uuid, self.field, self.at)
        return (self.uuid, self.field, self.at, self.old, self.new)

    @property
    def redacted(self) -> bool:
        """True if this change's values are digests rather than the text."""
        return any(
            isinstance(v, str) and v.startswith(REDACTED_PREFIX)
            for v in (self.old, self.new)
        )

    def value_at(self, *, before: bool = False) -> Optional[datetime | int | str]:
        """The old or new value, interpreted according to the field."""
        raw = self.old if before else self.new
        return interpret(self.field, raw)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "at": _iso(self.at),
            "uuid": self.uuid,
            "field": self.field,
            "source": self.source,
        }
        if self.old is not None:
            out["old"] = self.old
        if self.new is not None:
            out["new"] = self.new
        if self.op_id is not None:
            out["op_id"] = self.op_id
        if self.fingerprint:
            out["fingerprint"] = self.fingerprint
        return out

    def to_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["TaskChange"]:
        at = _dt(raw.get("at"))
        uuid, field = raw.get("uuid"), raw.get("field")
        if at is None or not isinstance(uuid, str) or not isinstance(field, str):
            return None
        op_id = raw.get("op_id")
        return cls(
            at=at,
            uuid=uuid,
            field=field,
            old=_text(raw.get("old")),
            new=_text(raw.get("new")),
            source=raw.get("source") or SOURCE_TASKCHAMPION,
            op_id=op_id if isinstance(op_id, int) else None,
            fingerprint=raw.get("fingerprint") or "",
        )


@dataclass(frozen=True)
class Gap:
    """An operation we couldn't understand, recorded rather than skipped.

    A silently ignored operation is indistinguishable from one that never
    existed, so coverage would look complete while history had a hole in it.
    The fingerprint makes it identifiable if a later version learns to read it.
    """

    op_id: int
    kind: str
    prop: Optional[str] = None
    fingerprint: str = ""

    def to_dict(self) -> dict:
        out = {
            "schema": SCHEMA_VERSION,
            "gap": True,
            "op_id": self.op_id,
            "kind": self.kind,
        }
        if self.prop:
            out["property"] = self.prop
        if self.fingerprint:
            out["fingerprint"] = self.fingerprint
        return out

    def to_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["Gap"]:
        op_id = raw.get("op_id")
        if not isinstance(op_id, int):
            return None
        return cls(
            op_id=op_id,
            kind=raw.get("kind") or "unknown",
            prop=raw.get("property"),
            fingerprint=raw.get("fingerprint") or "",
        )


def redact(value: Optional[str]) -> Optional[str]:
    """A value replaced by a short digest of itself.

    Enough to tell two titles apart and so to notice a rename; not enough to
    read one. What `journal_detail = "minimal"` stores.
    """
    if value is None or value == "":
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    return f"{REDACTED_PREFIX}{digest}"


def interpret(field: str, raw: Optional[str]):
    """A raw stored value as the field means it.

    Timestamp fields are epoch seconds; the estimate is minutes. Anything
    unparseable comes back None rather than a guess — a due date we can't read
    is missing, not 1970.
    """
    if raw is None or raw == "":
        return None
    if field in TIMESTAMP_FIELDS:
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if field == FIELD_ESTIMATE:
        try:
            minutes = int(round(float(raw)))
        except ValueError:
            return None
        return minutes if minutes > 0 else None
    return raw


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dt(raw) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _text(raw) -> Optional[str]:
    if raw is None:
        return None
    return raw if isinstance(raw, str) else str(raw)
