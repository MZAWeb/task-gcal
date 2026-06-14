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
    description: str
    urgency: float
    due: Optional[datetime]  # tz-aware UTC
    scheduled: Optional[datetime]  # tz-aware UTC; don't start before this
    wait: Optional[datetime]  # tz-aware UTC; deferred until this
    estimate_minutes: Optional[int]
    project: Optional[str]
    tags: list[str]

    @property
    def earliest_start(self) -> Optional[datetime]:
        """Floor on when this task may be scheduled (tz-aware UTC).

        Both `scheduled` and `wait` mean "not before this date"; the later
        of the two wins. ``None`` when neither is set.
        """
        floors = [d for d in (self.scheduled, self.wait) if d is not None]
        return max(floors) if floors else None


def _coerce_estimate(raw) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    return int(round(val))


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


def load_next_tasks(
    report: str = "next", *, estimate_uda: str = "estimate"
) -> list[TaskInfo]:
    """Load tasks from the given Taskwarrior report.

    Order is whatever the report defines; the caller re-sorts by urgency
    defensively.
    """
    if shutil.which("task") is None:
        raise SystemExit(
            "`task` not found on PATH. Install Taskwarrior or adjust PATH."
        )
    try:
        proc = subprocess.run(
            ["task", "rc.verbose=nothing", "rc.confirmation=no", "export", report],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or e.stdout or "").strip()
        raise SystemExit(f"`task export {report}` failed: {msg}") from None

    payload = proc.stdout.strip() or "[]"
    try:
        rows = json.loads(payload)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"Could not parse `task export {report}` output as JSON: {e}"
        ) from None

    tasks: list[TaskInfo] = []
    for row in rows:
        uuid = row.get("uuid")
        if not uuid:
            continue
        tasks.append(
            TaskInfo(
                uuid=uuid,
                description=row.get("description") or "(no description)",
                urgency=float(row.get("urgency") or 0.0),
                due=_parse_tw_datetime(row.get("due")),
                scheduled=_parse_tw_datetime(row.get("scheduled")),
                wait=_parse_tw_datetime(row.get("wait")),
                estimate_minutes=_coerce_estimate(row.get(estimate_uda)),
                project=row.get("project"),
                tags=list(row.get("tags") or []),
            )
        )
    return tasks
