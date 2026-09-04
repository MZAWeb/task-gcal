"""Read-only adapter over TaskChampion's `operations` table.

Taskwarrior 3.x records every field change as an operation, timestamped and
exact. That is the same history `task <uuid> info` renders — verifiably so: a
task with fifteen due-date pushes shows fifteen operations and fifteen `info`
lines at the same instants — except the table is structured and one query
instead of one subprocess per task (38ms vs 26s across 1,800 tasks).

Three things this module refuses to pretend:

- **It is not a public API.** This is TaskChampion's private storage, with its
  own `version` table (currently 2), so it migrates. Every read is gated on a
  version we've actually seen, and a mismatch degrades instead of guessing.
- **It is not durable history.** It is a *synchronisation* log; `purge.on-sync`
  can prune operations once they've been synced. So it is a source to harvest
  *from*, never the place reviews read. See `changes/`.
- **It is not ours to touch.** Read-only, `mode=ro`, one short transaction, a
  small busy timeout, closed immediately. Nothing here may ever slow down or
  fail a `task` command.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_FILENAME = "taskchampion.sqlite3"

# TaskChampion's `version` table is `(singleton, major, minor)`. We gate on the
# major: a minor bump is additive in practice, so blinding the user over one
# would cost more than it protects, while an unknown major means the storage
# was restructured and reading on regardless is how you invent history.
KNOWN_SCHEMA_MAJOR = 0
TESTED_SCHEMA = (0, 2)

KIND_UPDATE = "Update"
KIND_CREATE = "Create"
KIND_UNDO_POINT = "UndoPoint"

# Changes on this property are pure noise: Taskwarrior touches `modified` on
# every edit, so it doubles the volume and says nothing a real change doesn't.
NOISE_PROPERTIES = frozenset({"modified"})

# Reading a live WAL database is safe, but a reader can hold up WAL truncation,
# so the transaction stays short and the timeout small. A busy database is a
# missed harvest, not an error.
_BUSY_TIMEOUT_MS = 250


class OperationsUnavailable(Exception):
    """The operations table couldn't be read, and the caller should degrade.

    Carries a human-readable reason because `doctor` prints it verbatim.
    """


@dataclass(frozen=True)
class Operation:
    """One row of `operations`, parsed no further than we can justify."""

    op_id: int
    kind: str
    uuid: Optional[str] = None
    # Mirrors TaskChampion's `property`, spelled short because a field called
    # `property` shadows the builtin inside the class body and silently breaks
    # every `@property` below it.
    prop: Optional[str] = None
    # Values are kept exactly as TaskChampion stored them — epoch seconds as
    # strings for dates, plain strings otherwise. Interpreting them is the
    # reader's job, so the harvested record stays faithful to the source.
    old_value: Optional[str] = None
    value: Optional[str] = None
    at: Optional[datetime] = None
    # Digest of the raw row, so a rebuilt database that reuses an id can be
    # told apart from an ordinary continuation.
    fingerprint: str = ""

    @property
    def is_noise(self) -> bool:
        return self.prop in NOISE_PROPERTIES

    @property
    def understood(self) -> bool:
        """False for anything we can't place — the caller records a gap."""
        if self.kind in (KIND_UNDO_POINT, KIND_CREATE):
            return True
        if self.kind != KIND_UPDATE:
            return False
        return bool(self.uuid) and bool(self.prop) and self.at is not None


@dataclass(frozen=True)
class OperationsMeta:
    """What we could tell about the log itself, for honest coverage reporting."""

    # `(major, minor)`, or None when the version table couldn't be read.
    schema_version: Optional[tuple[int, int]]
    oldest_op_id: Optional[int]
    newest_op_id: Optional[int]
    total: int
    # True when a sync server is configured, which means `purge.on-sync` can
    # prune operations we haven't harvested yet.
    sync_configured: bool

    @property
    def empty(self) -> bool:
        return self.total == 0

    @property
    def schema_is_tested(self) -> bool:
        """False when we're reading a version we've never actually seen."""
        return self.schema_version == TESTED_SCHEMA


def data_location() -> Path:
    """Taskwarrior's data directory, as Taskwarrior itself reports it.

    Asked rather than guessed: it honours `TASKDATA`, the rc file and any
    `rc.data.location` override, none of which we can infer.
    """
    if shutil.which("task") is None:
        raise OperationsUnavailable("`task` is not on PATH")
    try:
        proc = subprocess.run(
            ["task", "rc.verbose=nothing", "rc.hooks=off", "_show"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        raise OperationsUnavailable(f"`task _show` failed: {detail}") from None
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "data.location" and value.strip():
            return Path(value.strip()).expanduser()
    raise OperationsUnavailable("`task _show` reported no data.location")


def database_path(location: Optional[Path] = None) -> Path:
    return (location or data_location()) / DB_FILENAME


def _fingerprint(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def _parse(op_id: int, data: str) -> Operation:
    """One row into an `Operation`; unknown shapes come back not-understood."""
    fingerprint = _fingerprint(data)
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return Operation(op_id=op_id, kind="unparseable", fingerprint=fingerprint)

    # `UndoPoint` is a bare string rather than an object.
    if isinstance(payload, str):
        return Operation(op_id=op_id, kind=payload, fingerprint=fingerprint)
    if not isinstance(payload, dict) or len(payload) != 1:
        return Operation(op_id=op_id, kind="unknown", fingerprint=fingerprint)

    kind, body = next(iter(payload.items()))
    if not isinstance(body, dict):
        return Operation(op_id=op_id, kind=kind, fingerprint=fingerprint)
    return Operation(
        op_id=op_id,
        kind=kind,
        uuid=body.get("uuid"),
        prop=body.get("property"),
        old_value=_as_text(body.get("old_value")),
        value=_as_text(body.get("value")),
        at=_parse_stamp(body.get("timestamp")),
        fingerprint=fingerprint,
    )


def _as_text(raw) -> Optional[str]:
    if raw is None:
        return None
    return raw if isinstance(raw, str) else str(raw)


def _parse_stamp(raw) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise OperationsUnavailable(f"no operations database at {path}")
    try:
        # `mode=ro` and not `immutable=1`: immutable asserts the file cannot
        # change, which is false for a live WAL database and would risk
        # reading a torn view.
        con = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=0.5
        )
        con.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    except sqlite3.Error as e:
        raise OperationsUnavailable(f"could not open {path}: {e}") from None
    return con


def _schema_version(con: sqlite3.Connection) -> Optional[tuple[int, int]]:
    try:
        row = con.execute("select major, minor from version").fetchone()
    except sqlite3.Error:
        return None
    if not row or not all(isinstance(part, int) for part in row):
        return None
    return (row[0], row[1])


def read(
    *,
    path: Optional[Path] = None,
    after_id: int = 0,
    limit: Optional[int] = None,
    require_known_schema: bool = True,
) -> tuple[list[Operation], OperationsMeta]:
    """Operations with `op_id > after_id`, oldest first, plus log metadata.

    One transaction, so the rows and the oldest/newest bounds all describe the
    same snapshot — otherwise a concurrent `task` command could make the
    coverage numbers disagree with the rows they describe.
    """
    target = path or database_path()
    con = _connect(target)
    try:
        con.execute("BEGIN DEFERRED")
        version = _schema_version(con)
        if require_known_schema and (
            version is None or version[0] != KNOWN_SCHEMA_MAJOR
        ):
            raise OperationsUnavailable(
                f"unrecognized TaskChampion schema version {version!r}; "
                f"this reader understands major {KNOWN_SCHEMA_MAJOR} "
                f"(tested against {TESTED_SCHEMA[0]}.{TESTED_SCHEMA[1]})"
            )
        bounds = con.execute(
            "select min(id), max(id), count(*) from operations"
        ).fetchone()
        sql = "select id, data from operations where id > ? order by id"
        params: tuple = (after_id,)
        if limit is not None:
            sql += " limit ?"
            params = (after_id, limit)
        rows = con.execute(sql, params).fetchall()
        synced = con.execute("select count(*) from sync_meta").fetchone()[0]
    except sqlite3.Error as e:
        raise OperationsUnavailable(f"could not read {target}: {e}") from None
    finally:
        try:
            con.rollback()
        finally:
            con.close()

    meta = OperationsMeta(
        schema_version=version,
        oldest_op_id=bounds[0],
        newest_op_id=bounds[1],
        total=bounds[2] or 0,
        sync_configured=bool(synced),
    )
    return [_parse(op_id, data) for op_id, data in rows], meta


def find(op_id: int, *, path: Optional[Path] = None) -> Optional[Operation]:
    """One operation by id, for verifying a stored anchor still matches.

    Used to tell "operations were pruned" from "the database was rebuilt and
    reused this id", which the id alone cannot distinguish.
    """
    target = path or database_path()
    con = _connect(target)
    try:
        row = con.execute(
            "select id, data from operations where id = ?", (op_id,)
        ).fetchone()
    except sqlite3.Error as e:
        raise OperationsUnavailable(f"could not read {target}: {e}") from None
    finally:
        con.close()
    return _parse(row[0], row[1]) if row else None
