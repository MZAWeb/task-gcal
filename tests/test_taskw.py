"""Parsing `task export` output, and failing loudly when we can't."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import pytest

from task_gcal import taskw as taskw_mod
from task_gcal.taskw import TaskInfo, load_next_tasks

from conftest import NOW, task_row, tw_stamp

UTC = timezone.utc


@pytest.fixture
def tw(monkeypatch):
    from conftest import FakeTaskwarrior

    fake = FakeTaskwarrior()
    monkeypatch.setattr(taskw_mod.shutil, "which", lambda _n: "/usr/bin/task")
    monkeypatch.setattr(taskw_mod.subprocess, "run", fake.run)
    return fake


# ---------------------------------------------------------------------------
# Datetime parsing
# ---------------------------------------------------------------------------

def test_parses_compact_taskwarrior_stamps():
    got = taskw_mod._parse_tw_datetime("20260907T090000Z")
    assert got == datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


def test_parses_ordinary_iso_stamps():
    got = taskw_mod._parse_tw_datetime("2026-09-07T09:00:00+00:00")
    assert got == datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


def test_parsed_datetimes_are_always_utc_aware():
    got = taskw_mod._parse_tw_datetime("2026-09-07T10:00:00+01:00")
    assert got.tzinfo is UTC
    assert got.hour == 9


@pytest.mark.parametrize("raw", [None, "", "not a date", "20261301T000000Z"])
def test_unparseable_dates_become_none(raw):
    assert taskw_mod._parse_tw_datetime(raw) is None


# ---------------------------------------------------------------------------
# Estimate coercion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (60, 60),
        ("60", 60),
        (60.0, 60),
        (59.6, 60),   # rounded, not truncated
        (0.4, 0),     # rounds to zero minutes but was a positive value
        (1, 1),
    ],
)
def test_estimates_are_coerced_to_whole_minutes(raw, expected):
    assert taskw_mod._coerce_estimate(raw) == expected


@pytest.mark.parametrize("raw", [None, "", 0, -5, "abc", "", [], {}])
def test_absent_or_nonpositive_estimates_become_none(raw):
    assert taskw_mod._coerce_estimate(raw) is None


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

def test_annotations_keep_export_order():
    raw = [
        {"entry": "20260101T000000Z", "description": "first"},
        {"entry": "20260102T000000Z", "description": "second"},
    ]
    assert taskw_mod._parse_annotations(raw) == ["first", "second"]


@pytest.mark.parametrize("raw", [None, [], [{}], ["a string"], [{"entry": "x"}]])
def test_malformed_annotations_are_skipped(raw):
    assert taskw_mod._parse_annotations(raw) == []


# ---------------------------------------------------------------------------
# TaskInfo behavior
# ---------------------------------------------------------------------------

def _info(**kwargs) -> TaskInfo:
    base = dict(
        uuid="f25a00fa-e24f-4b49-8504-de6997291209",
        id=42,
        description="a task",
        urgency=1.0,
        due=None,
        scheduled=None,
        wait=None,
        estimate_minutes=60,
        project=None,
        tags=[],
        annotations=[],
        overrides_raw=None,
    )
    base.update(kwargs)
    return TaskInfo(**base)  # type: ignore[arg-type]


def test_ref_prefers_the_short_id():
    assert _info(id=42).ref == "#42"


def test_ref_falls_back_to_a_uuid_prefix():
    # id 0 means the task isn't pending; a uuid prefix still addresses it.
    assert _info(id=0).ref == "f25a00fa"


def test_earliest_start_is_none_without_scheduled_or_wait():
    assert _info().earliest_start is None


def test_earliest_start_uses_scheduled():
    when = datetime(2026, 9, 8, tzinfo=UTC)
    assert _info(scheduled=when).earliest_start == when


def test_earliest_start_uses_wait():
    when = datetime(2026, 9, 8, tzinfo=UTC)
    assert _info(wait=when).earliest_start == when


def test_earliest_start_takes_the_later_of_scheduled_and_wait():
    early = datetime(2026, 9, 8, tzinfo=UTC)
    late = datetime(2026, 9, 20, tzinfo=UTC)
    assert _info(scheduled=early, wait=late).earliest_start == late
    assert _info(scheduled=late, wait=early).earliest_start == late


# ---------------------------------------------------------------------------
# load_next_tasks
# ---------------------------------------------------------------------------

def test_loads_and_parses_a_row(tw):
    due = datetime(2026, 9, 9, 17, 0, tzinfo=UTC)
    tw.rows = [
        task_row(
            uuid="u1",
            id=7,
            description="write the thing",
            urgency=12.5,
            due=due,
            estimate=90,
            project="proj",
            tags=["work", "next"],
            annotations=["a note"],
            gcal="buffer_minutes=0",
        )
    ]
    (task,) = load_next_tasks("next")
    assert task.uuid == "u1"
    assert task.id == 7
    assert task.description == "write the thing"
    assert task.urgency == 12.5
    assert task.due == due
    assert task.estimate_minutes == 90
    assert task.project == "proj"
    assert task.tags == ["work", "next"]
    assert task.annotations == ["a note"]
    assert task.overrides_raw == "buffer_minutes=0"


def test_passes_the_report_name_through(tw):
    load_next_tasks("myreport")
    assert tw.calls[0][-2:] == ["export", "myreport"]


def test_suppresses_taskwarrior_chrome(tw):
    load_next_tasks("next")
    argv = tw.calls[0]
    assert "rc.verbose=nothing" in argv
    assert "rc.confirmation=no" in argv


def test_reads_a_custom_estimate_uda(tw):
    tw.rows = [task_row(uuid="u1", estimate=45, estimate_key="est")]
    (task,) = load_next_tasks("next", estimate_uda="est")
    assert task.estimate_minutes == 45


def test_estimate_under_the_wrong_uda_name_is_invisible(tw):
    # The failure mode the removal guard exists for.
    tw.rows = [task_row(uuid="u1", estimate=45, estimate_key="est")]
    (task,) = load_next_tasks("next", estimate_uda="estimate")
    assert task.estimate_minutes is None


def test_reads_a_custom_override_uda(tw):
    tw.rows = [task_row(uuid="u1")]
    tw.rows[0]["sched"] = "buffer_minutes=0"
    (task,) = load_next_tasks("next", override_uda="sched")
    assert task.overrides_raw == "buffer_minutes=0"


def test_empty_override_string_becomes_none(tw):
    tw.rows = [task_row(uuid="u1", gcal="")]
    (task,) = load_next_tasks("next")
    assert task.overrides_raw is None


def test_rows_without_a_uuid_are_skipped(tw):
    tw.rows = [{"description": "orphan"}, task_row(uuid="u1")]
    tasks = load_next_tasks("next")
    assert [t.uuid for t in tasks] == ["u1"]


def test_missing_fields_get_sensible_defaults(tw):
    tw.rows = [{"uuid": "u1"}]
    (task,) = load_next_tasks("next")
    assert task.description == "(no description)"
    assert task.urgency == 0.0
    assert task.id == 0
    assert task.due is None
    assert task.estimate_minutes is None
    assert task.tags == []


def test_empty_output_is_no_tasks(tw):
    tw.stdout_override = ""
    assert load_next_tasks("next") == []


def test_report_order_is_preserved(tw):
    tw.rows = [task_row(uuid=f"u{i}", urgency=float(i)) for i in range(4)]
    assert [t.uuid for t in load_next_tasks("next")] == ["u0", "u1", "u2", "u3"]


# --------------------------- failure modes ---------------------------------

def test_missing_task_binary_exits(monkeypatch):
    monkeypatch.setattr(taskw_mod.shutil, "which", lambda _n: None)
    with pytest.raises(SystemExit, match="not found on PATH"):
        load_next_tasks("next")


def test_nonzero_exit_is_fatal(tw):
    tw.exit_code = 2
    with pytest.raises(SystemExit, match="failed"):
        load_next_tasks("next")


def test_unparseable_json_is_fatal(tw):
    tw.stdout_override = "not json at all"
    with pytest.raises(SystemExit, match="Could not parse"):
        load_next_tasks("next")


def test_stamp_helper_round_trips():
    # Guards the test helper itself: a wrong stamp format would silently make
    # every due date None and quietly change what the suite is testing.
    assert taskw_mod._parse_tw_datetime(tw_stamp(NOW)) == NOW
