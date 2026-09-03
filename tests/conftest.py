"""Shared fixtures: a fake calendar, a fake `task export`, and a frozen clock.

Nothing here touches the network, Taskwarrior, the system timezone, or the
real journal. Tests always set `timezone` explicitly in Settings — a test
that passes only in Europe/London isn't a test.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from task_gcal import schedule as schedule_mod
from task_gcal import snapshot as snapshot_mod
from task_gcal import taskw as taskw_mod
from task_gcal.config import Settings
from task_gcal.gcal import CalEvent

# Monday 2026-09-07 09:00 UTC: the first minute of a default working day, so a
# task with room today lands at exactly NOW and the arithmetic stays readable.
NOW = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


MIDNIGHT = utc(2026, 9, 7)  # the Monday NOW falls on


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """Wall-clock UTC, `day` days after Monday 2026-09-07."""
    return MIDNIGHT + timedelta(days=day, hours=hour, minutes=minute)


WED_5PM = at(2, 17)
FRI_5PM = at(4, 17)


def tw_stamp(dt: datetime) -> str:
    """Taskwarrior's export format: compact ISO, always UTC."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Taskwarrior side
# ---------------------------------------------------------------------------

def task_row(
    *,
    uuid: str,
    id: int = 1,
    description: str = "a task",
    urgency: float = 10.0,
    due: Optional[datetime] = None,
    scheduled: Optional[datetime] = None,
    wait: Optional[datetime] = None,
    estimate: Optional[int] = 60,
    project: Optional[str] = None,
    tags: Optional[list[str]] = None,
    annotations: Optional[list[str]] = None,
    gcal: Optional[str] = None,
    estimate_key: str = "estimate",
) -> dict:
    """One row shaped like `task export` output."""
    row: dict = {
        "uuid": uuid,
        "id": id,
        "description": description,
        "urgency": urgency,
        "status": "pending",
    }
    if due is not None:
        row["due"] = tw_stamp(due)
    if scheduled is not None:
        row["scheduled"] = tw_stamp(scheduled)
    if wait is not None:
        row["wait"] = tw_stamp(wait)
    if estimate is not None:
        row[estimate_key] = estimate
    if project is not None:
        row["project"] = project
    if tags:
        row["tags"] = tags
    if annotations:
        row["annotations"] = [
            {"entry": tw_stamp(NOW), "description": a} for a in annotations
        ]
    if gcal is not None:
        row["gcal"] = gcal
    return row


class FakeTaskwarrior:
    """Stands in for the `task export` subprocess call."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.calls: list[list[str]] = []
        # Set to raise instead of returning rows.
        self.exit_code: Optional[int] = None
        self.stdout_override: Optional[str] = None

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.exit_code is not None:
            raise subprocess.CalledProcessError(
                self.exit_code, argv, output="", stderr="boom"
            )
        payload = (
            self.stdout_override
            if self.stdout_override is not None
            else json.dumps(self.rows)
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")


# ---------------------------------------------------------------------------
# Calendar side
# ---------------------------------------------------------------------------

def managed_event(
    *,
    id: str,
    task_uuid: Optional[str],
    start: datetime,
    end: datetime,
    summary: str = "a task",
    description: str = "",
    color_id: str = "9",
    visibility: str = "private",
    attendees: Optional[list[dict]] = None,
) -> CalEvent:
    """A scheduler-owned event, with the `raw` fields reconcile diffs against."""
    raw: dict = {
        "id": id,
        "summary": summary,
        "description": description,
        "colorId": color_id,
        "visibility": visibility,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "extendedProperties": {
            "private": {"scheduler": "task-gcal", "taskUuid": task_uuid}
        },
    }
    if attendees is not None:
        raw["attendees"] = attendees
    return CalEvent(
        id=id,
        summary=summary,
        start=start,
        end=end,
        task_uuid=task_uuid,
        raw=raw,
    )


class FakeGCal:
    """In-memory calendar implementing the five methods reconcile uses.

    Mutations are applied to `managed` as well as recorded, so a test can run
    reconcile twice and assert the second run is a no-op.
    """

    def __init__(self) -> None:
        self.managed: list[CalEvent] = []
        self.busy: list[tuple[datetime, datetime]] = []
        self.created: list[dict] = []
        self.patched: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.scheduler_list_calls: list[tuple[datetime, datetime]] = []
        self.busy_list_calls: list[tuple[datetime, datetime, set]] = []
        # event ids whose patch should report "already gone"
        self.vanished: set[str] = set()
        self._next_id = 1

    # ------------------------------- queries ---------------------------------

    def list_scheduler_events(self, time_min, time_max) -> list[CalEvent]:
        self.scheduler_list_calls.append((time_min, time_max))
        # Google returns events overlapping the window, not contained by it.
        return [e for e in self.managed if e.end > time_min and e.start < time_max]

    def list_busy_events(self, time_min, time_max, exclude_event_ids):
        self.busy_list_calls.append((time_min, time_max, set(exclude_event_ids)))
        out = [
            (s, e)
            for s, e in self.busy
            if e > time_min and s < time_max
        ]
        out.sort()
        return out

    # ------------------------------ mutations --------------------------------

    def create_event(
        self,
        *,
        task_uuid,
        summary,
        description,
        start,
        end,
        color_id,
        visibility="private",
        attendees=(),
    ) -> str:
        event_id = f"ev{self._next_id}"
        self._next_id += 1
        self.created.append(
            {
                "id": event_id,
                "task_uuid": task_uuid,
                "summary": summary,
                "description": description,
                "start": start,
                "end": end,
                "color_id": color_id,
                "visibility": visibility,
                "attendees": tuple(attendees),
            }
        )
        self.managed.append(
            managed_event(
                id=event_id,
                task_uuid=task_uuid,
                start=start,
                end=end,
                summary=summary,
                description=description,
                color_id=color_id,
                visibility=visibility,
                attendees=[{"email": e} for e in attendees] or None,
            )
        )
        return event_id

    def patch_event(
        self,
        event_id,
        *,
        summary=None,
        description=None,
        start=None,
        end=None,
        color_id=None,
        visibility=None,
        attendees=None,
    ) -> bool:
        body = {
            k: v
            for k, v in dict(
                summary=summary,
                description=description,
                start=start,
                end=end,
                color_id=color_id,
                visibility=visibility,
                attendees=attendees,
            ).items()
            if v is not None
        }
        self.patched.append((event_id, body))
        if event_id in self.vanished:
            return False
        for ev in self.managed:
            if ev.id != event_id:
                continue
            if summary is not None:
                ev.summary = ev.raw["summary"] = summary
            if description is not None:
                ev.raw["description"] = description
            if start is not None:
                ev.start = start
            if end is not None:
                ev.end = end
            if color_id is not None:
                ev.raw["colorId"] = color_id
            if visibility is not None:
                ev.raw["visibility"] = visibility
            if attendees is not None:
                ev.raw["attendees"] = attendees
        return True

    def delete_event(self, event_id) -> bool:
        self.deleted.append(event_id)
        self.managed = [e for e in self.managed if e.id != event_id]
        return True

    # ------------------------------- helpers ---------------------------------

    @property
    def mutations(self) -> int:
        return len(self.created) + len(self.patched) + len(self.deleted)

    def event_for(self, task_uuid: str) -> Optional[CalEvent]:
        for ev in self.managed:
            if ev.task_uuid == task_uuid:
                return ev
        return None


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

class _FrozenDatetime(datetime):
    """`datetime` whose `now()` is pinned to NOW."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return NOW if tz is None else NOW.astimezone(tz)


class Harness:
    """Everything a reconcile test needs, plus a `run()` that returns the report."""

    def __init__(self, tw: FakeTaskwarrior, gcal: FakeGCal, capsys) -> None:
        self.tw = tw
        self.gcal = gcal
        self._capsys = capsys
        self.settings = Settings(timezone="UTC")

    # Convenience passthroughs, so tests read as a story.
    def tasks(self, *rows: dict) -> "Harness":
        self.tw.rows = list(rows)
        return self

    def events(self, *evs: CalEvent) -> "Harness":
        self.gcal.managed = list(evs)
        return self

    def busy(self, *intervals: tuple[datetime, datetime]) -> "Harness":
        self.gcal.busy = sorted(intervals)
        return self

    def configure(self, **kwargs) -> "Harness":
        from dataclasses import replace

        self.settings = replace(self.settings, **kwargs)
        return self

    def run(self, *, dry_run: bool = False, force: bool = False):
        code = schedule_mod.reconcile(self.settings, dry_run=dry_run, force=force)
        captured = self._capsys.readouterr()
        return RunResult(code=code, out=captured.out, err=captured.err)

    def snapshot(self):
        code = snapshot_mod.snapshot(self.settings)
        captured = self._capsys.readouterr()
        return RunResult(code=code, out=captured.out, err=captured.err)

    def journal(self, **kwargs):
        """Everything the journal recorded so far, oldest first."""
        from task_gcal import journal as journal_mod

        return journal_mod.load(**kwargs).records


class RunResult:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    def section(self, header_startswith: str) -> list[str]:
        """The indented lines under the first header starting with the prefix."""
        lines = self.out.splitlines()
        for i, line in enumerate(lines):
            if line.startswith(header_startswith):
                body = []
                for rest in lines[i + 1:]:
                    if rest and not rest.startswith(" "):
                        break
                    if rest.strip():
                        body.append(rest.strip())
                return body
        return []


@pytest.fixture(autouse=True)
def isolated_journal(tmp_path, monkeypatch):
    """Point the journal at a temp dir for *every* test.

    Autouse and unconditional: anything that runs a command may append an
    observation, and a test suite that writes to the developer's real
    history is a bug that only shows up as mysterious extra data.
    """
    root = tmp_path / "data"
    monkeypatch.setenv("TASK_GCAL_DATA_DIR", str(root))
    return root


@pytest.fixture
def harness(monkeypatch, capsys) -> Harness:
    tw = FakeTaskwarrior()
    gcal = FakeGCal()

    monkeypatch.setattr(taskw_mod.shutil, "which", lambda _name: "/usr/bin/task")
    monkeypatch.setattr(taskw_mod.subprocess, "run", tw.run)
    monkeypatch.setattr(schedule_mod, "GCal", lambda _settings: gcal)
    monkeypatch.setattr(schedule_mod, "datetime", _FrozenDatetime)
    monkeypatch.setattr(snapshot_mod, "GCal", lambda _settings: gcal)
    monkeypatch.setattr(snapshot_mod, "datetime", _FrozenDatetime)

    return Harness(tw, gcal, capsys)


@pytest.fixture
def hour() -> timedelta:
    return timedelta(hours=1)


# ---------------------------------------------------------------------------
# Review side
# ---------------------------------------------------------------------------

def a_task(
    *,
    uuid: str = "u1",
    id: int = 1,
    description: str = "a task",
    urgency: float = 5.0,
    due: Optional[datetime] = None,
    scheduled: Optional[datetime] = None,
    wait: Optional[datetime] = None,
    estimate: Optional[int] = 60,
    project: Optional[str] = None,
    tags: Optional[list[str]] = None,
    overrides: Optional[str] = None,
    status: str = "pending",
    entry: Optional[datetime] = None,
    end: Optional[datetime] = None,
    recur: Optional[str] = None,
    parent: Optional[str] = None,
):
    """A `TaskInfo` as `load_all_tasks` would build it.

    Distinct from `task_row`, which fakes the *export JSON* for the
    scheduling path; reviews consume the parsed objects directly.
    """
    from task_gcal.taskw import TaskInfo

    return TaskInfo(
        uuid=uuid,
        id=id,
        description=description,
        urgency=urgency,
        due=due,
        scheduled=scheduled,
        wait=wait,
        estimate_minutes=estimate,
        project=project,
        tags=list(tags or []),
        annotations=[],
        overrides_raw=overrides,
        status=status,
        entry=entry if entry is not None else NOW - timedelta(days=30),
        end=end,
        recur=recur,
        parent=parent,
    )


def a_block(
    task_uuid: Optional[str],
    start: datetime,
    minutes: int = 60,
    *,
    id: Optional[str] = None,
    summary: str = "a task",
) -> CalEvent:
    """One of our managed calendar blocks, as the review side sees it."""
    return CalEvent(
        id=id or f"ev-{task_uuid}-{start:%m%d%H%M}",
        summary=summary,
        start=start,
        end=start + timedelta(minutes=minutes),
        task_uuid=task_uuid,
        raw={},
    )


class ReviewHarness:
    """Facts assembled by hand, so metrics are tested without any I/O."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.settings = Settings(timezone="UTC")
        self._tasks: list = []
        self._blocks: list[CalEvent] = []
        self._meetings: list[tuple[datetime, datetime]] = []
        self._records: list = []
        self.calendar_ok = True
        self.kind = "week"
        self.offset = 0

    def tasks(self, *tasks) -> "ReviewHarness":
        self._tasks = list(tasks)
        return self

    def blocks(self, *blocks: CalEvent) -> "ReviewHarness":
        self._blocks = list(blocks)
        return self

    def meetings(self, *intervals: tuple[datetime, datetime]) -> "ReviewHarness":
        self._meetings = sorted(intervals)
        return self

    def records(self, *records) -> "ReviewHarness":
        self._records = list(records)
        return self

    def configure(self, **kwargs) -> "ReviewHarness":
        from dataclasses import replace

        self.settings = replace(self.settings, **kwargs)
        return self

    def whole_week(self) -> "ReviewHarness":
        """Report on NOW's week as a *finished* one.

        The default fixture reviews a week in progress, clamped to Friday
        afternoon — the awkward case, and worth being the default. But it
        means Saturday and Friday evening fall outside the period, so
        anything about weekends or out-of-hours time needs the full week.
        """
        self.now = NOW + timedelta(days=7)
        self.offset = 1
        return self

    def period(self):
        from task_gcal.review.periods import resolve

        return resolve(
            self.kind,
            self.settings.resolve_timezone(),
            now=self.now,
            offset=self.offset,
        )

    def facts(self):
        from task_gcal.journal import JournalRead
        from task_gcal.review.facts import Facts

        return Facts(
            period=self.period(),
            settings=self.settings,
            now=self.now,
            tasks=tuple(self._tasks),
            blocks=tuple(self._blocks),
            meetings=tuple(self._meetings),
            journal=JournalRead(records=list(self._records)),
            calendar_ok=self.calendar_ok,
        )

    def review(self):
        from task_gcal.review import build

        return build(self.facts())

    def render(self, fmt: str = "terminal", *, sections=(), detailed=False) -> str:
        from task_gcal.review import metrics as metrics_mod
        from task_gcal.review.render import render

        review = self.review()
        chosen = (
            metrics_mod.selected(review.sections, sections)
            if sections
            else metrics_mod.summary_sections(review.sections)
        )
        return render(
            review, fmt=fmt, sections=chosen, detailed=detailed or bool(sections)
        )


@pytest.fixture
def review():
    """A review harness whose clock is Friday of NOW's week, 15:00."""
    return ReviewHarness(now=NOW + timedelta(days=4, hours=6))
