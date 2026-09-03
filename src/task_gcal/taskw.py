"""Taskwarrior glue.

We invoke `task export <report>` once and parse the JSON. This honors the
user's own report definition (filters, sort, etc.) while remaining a
single subprocess and side-stepping the `_uuids` helper, which doesn't
compose with report names.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from dateutil import parser as dtparser


@dataclass
class TaskInfo:
    uuid: str
    id: int  # Taskwarrior short id; 0 when the task isn't pending
    description: str
    urgency: float
    due: Optional[datetime]  # tz-aware UTC
    scheduled: Optional[datetime]  # tz-aware UTC; don't start before this
    wait: Optional[datetime]  # tz-aware UTC; deferred until this
    estimate_minutes: Optional[int]
    project: Optional[str]
    tags: list[str]
    annotations: list[str]  # annotation texts, in Taskwarrior order
    overrides_raw: Optional[str]  # raw per-task override UDA value, unparsed
    # Lifecycle facts. Scheduling ignores them; reviews are built on them —
    # `entry`/`end` give lead time and backlog flow, and `end` is the one
    # unambiguous "did this actually happen" signal we have.
    status: Optional[str] = None  # pending | completed | deleted | waiting
    entry: Optional[datetime] = None  # tz-aware UTC; when it was created
    end: Optional[datetime] = None  # tz-aware UTC; completed/deleted at

    @property
    def ref(self) -> str:
        """Human-facing identifier for reports.

        Prefer the short Taskwarrior id (e.g. `#42`, what you'd type to
        act on the task); fall back to a uuid prefix when there's no
        short id (Taskwarrior accepts uuid prefixes too).
        """
        return f"#{self.id}" if self.id else self.uuid[:8]

    @property
    def earliest_start(self) -> Optional[datetime]:
        """Floor on when this task may be scheduled (tz-aware UTC).

        Both `scheduled` and `wait` mean "not before this date"; the later
        of the two wins. ``None`` when neither is set.
        """
        floors = [d for d in (self.scheduled, self.wait) if d is not None]
        return max(floors) if floors else None


def _parse_annotations(raw) -> list[str]:
    """Extract annotation texts from a Taskwarrior `annotations` array.

    Each entry is `{"entry": <ts>, "description": <text>}`; we keep the
    text in export order (chronological). Non-dict / empty entries are
    skipped.
    """
    out: list[str] = []
    for a in raw or []:
        if isinstance(a, dict):
            text = a.get("description")
            if text:
                out.append(text)
    return out


def _coerce_estimate(raw) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    # Round before the sign check: a fractional estimate like 0.4 is positive
    # but rounds to zero minutes, and a zero-length calendar event is worse
    # than treating the estimate as missing.
    minutes = int(round(val))
    if minutes <= 0:
        return None
    return minutes


def _parse_tw_datetime(raw: Optional[str]) -> Optional[datetime]:
    """Taskwarrior export emits ISO-ish 'YYYYMMDDTHHMMSSZ'."""
    if not raw:
        return None
    try:
        # Inserting separators makes it dateutil-friendly; dateutil
        # handles the compact form too but be explicit.
        if "T" in raw and "-" not in raw:
            d, t = raw.split("T", 1)
            raw = f"{d[0:4]}-{d[4:6]}-{d[6:8]}T{t[0:2]}:{t[2:4]}:{t[4:]}"
        return dtparser.isoparse(raw).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _export(args: list[str], *, what: str) -> list[dict]:
    """Run one `task ... export ...` and return its rows."""
    if shutil.which("task") is None:
        raise SystemExit(
            "`task` not found on PATH. Install Taskwarrior or adjust PATH."
        )
    try:
        proc = subprocess.run(
            ["task", "rc.verbose=nothing", "rc.confirmation=no", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or e.stdout or "").strip()
        raise SystemExit(f"`{what}` failed: {msg}") from None

    payload = proc.stdout.strip() or "[]"
    try:
        rows = json.loads(payload)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"Could not parse `{what}` output as JSON: {e}"
        ) from None
    return rows


def _to_tasks(
    rows: list[dict], *, estimate_uda: str, override_uda: str
) -> list[TaskInfo]:
    tasks: list[TaskInfo] = []
    for row in rows:
        uuid = row.get("uuid")
        if not uuid:
            continue
        tasks.append(
            TaskInfo(
                uuid=uuid,
                id=int(row.get("id") or 0),
                description=row.get("description") or "(no description)",
                urgency=float(row.get("urgency") or 0.0),
                due=_parse_tw_datetime(row.get("due")),
                scheduled=_parse_tw_datetime(row.get("scheduled")),
                wait=_parse_tw_datetime(row.get("wait")),
                estimate_minutes=_coerce_estimate(row.get(estimate_uda)),
                project=row.get("project"),
                tags=list(row.get("tags") or []),
                annotations=_parse_annotations(row.get("annotations")),
                overrides_raw=row.get(override_uda) or None,
                status=row.get("status"),
                entry=_parse_tw_datetime(row.get("entry")),
                end=_parse_tw_datetime(row.get("end")),
            )
        )
    return tasks


def load_next_tasks(
    report: str = "next", *, estimate_uda: str = "estimate",
    override_uda: str = "gcal",
) -> list[TaskInfo]:
    """Load tasks from the given Taskwarrior report.

    Order is whatever the report defines; the caller re-sorts by urgency
    defensively.
    """
    rows = _export(["export", report], what=f"task export {report}")
    return _to_tasks(rows, estimate_uda=estimate_uda, override_uda=override_uda)


def load_all_tasks(
    *, estimate_uda: str = "estimate", override_uda: str = "gcal"
) -> list[TaskInfo]:
    """Every task Taskwarrior knows about, completed and deleted included.

    Reviews need the closed ones — throughput, lead time and backlog flow
    are all about tasks that left the list — so this deliberately ignores
    both the configured report and any active context. A context here would
    silently narrow history to one project and make every count wrong.
    """
    rows = _export(
        ["rc.context=none", "export"], what="task export (all tasks)"
    )
    return _to_tasks(rows, estimate_uda=estimate_uda, override_uda=override_uda)
