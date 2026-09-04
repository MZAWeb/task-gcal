"""Tests for the observation journal.

The journal is the one part of the tool whose value is entirely in the
future: a bug here isn't noticed until a review is wrong weeks later. So
these lean on the properties that make it trustworthy — round-tripping,
tolerance of damaged and newer files, and the one-way write boundary.
"""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from task_gcal import journal
from task_gcal.config import Settings
from task_gcal.journal import paths, records, store

from conftest import NOW, at

SETTINGS = Settings(timezone="UTC")

# A mode no version of this tool writes, for the "tolerate the unfamiliar"
# cases. Records we don't recognise must be readable but must not be counted.
FOREIGN_MODE = "some-future-mode"


def a_placement(**kwargs) -> journal.PlacementObservation:
    base = dict(
        task_uuid="u1",
        event_id="ev1",
        start=at(0, 9),
        end=at(0, 10),
        action="create",
    )
    base.update(kwargs)
    return journal.PlacementObservation(**base)


def a_record(*, placements=None, settings=SETTINGS, mode=None, at=NOW):
    return journal.build_record(
        settings=settings,
        mode=mode or journal.MODE_SCHEDULE,
        at=at,
        placements=(a_placement(),) if placements is None else tuple(placements),
    )


def write_lines(root, month: str, *lines: str) -> None:
    """Drop raw lines into a run file, bypassing the writer."""
    directory = root / "runs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{month}.jsonl").write_text("".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def test_the_data_dir_env_var_wins(isolated_journal):
    assert paths.data_dir() == isolated_journal


def test_xdg_data_home_is_honored(monkeypatch, tmp_path):
    monkeypatch.delenv("TASK_GCAL_DATA_DIR")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert paths.data_dir() == tmp_path / "share" / "task-gcal"


def test_without_xdg_it_falls_back_to_local_share(monkeypatch, tmp_path):
    monkeypatch.delenv("TASK_GCAL_DATA_DIR")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda _cls: tmp_path))
    assert paths.data_dir() == tmp_path / ".local" / "share" / "task-gcal"


def test_the_run_file_is_named_by_utc_month():
    # 23:30 on the 31st in Madrid is already the next month in UTC. Naming
    # by the reader's zone would file one record in two different months
    # depending on who reads it.
    late = records._dt("2026-01-31T23:30:00Z")
    assert paths.run_file(late).name == "2026-01.jsonl"


# ---------------------------------------------------------------------------
# Round-tripping
# ---------------------------------------------------------------------------

def test_a_record_survives_a_round_trip():
    record = a_record(
        placements=[
            a_placement(moved_reason="a meeting landed on it", action="update")
        ]
    )
    back = records.RunRecord.from_dict(json.loads(record.to_line()))

    assert back is not None
    assert back.run_id == record.run_id
    assert back.at == NOW
    assert back.mode == journal.MODE_SCHEDULE
    (placement,) = back.placements
    assert placement.task_uuid == "u1"
    assert placement.event_id == "ev1"
    assert placement.start == at(0, 9)
    assert placement.end == at(0, 10)
    assert placement.action == "update"
    assert placement.moved_reason == "a meeting landed on it"


def test_a_record_is_exactly_one_line():
    # Nothing we write should be able to split one record into two
    # unparseable halves, whatever a reason string contains.
    record = a_record(placements=[a_placement(moved_reason="line one\nline two")])
    assert "\n" not in record.to_line()


def test_absent_values_are_omitted_rather_than_stored_as_null():
    record = a_record(placements=[a_placement(action=None)])
    (raw,) = record.to_dict()["placements"]
    assert "action" not in raw
    assert "moved_reason" not in raw


def test_no_task_titles_reach_the_journal():
    # Titles live in the change history, harvested from Taskwarrior's own log.
    # Keeping them out of here means one source of truth, and a journal you
    # can hand to someone without handing over what you're working on.
    fields = set(records.PlacementObservation.__dataclass_fields__)
    assert not fields & {"description", "description_hash", "summary", "project"}


def test_nothing_derived_is_stored():
    # The rule that keeps a definition change from leaving numbers behind
    # that meant something else. If a total or score ever appears here,
    # reviews stop being reproducible from raw observations.
    record = a_record()
    keys = set(record.to_dict()) | set(record.to_dict()["placements"][0])
    assert not {
        k for k in keys
        if any(word in k for word in ("total", "score", "streak", "count", "rate"))
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_append_creates_a_private_file_in_a_private_directory(isolated_journal):
    path = journal.append(a_record())

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_appends_accumulate_in_one_monthly_file(isolated_journal):
    for _ in range(3):
        journal.append(a_record())
    (path,) = list((isolated_journal / "runs").glob("*.jsonl"))
    assert len(path.read_text().splitlines()) == 3


def test_records_from_different_months_go_to_different_files(isolated_journal):
    for when in (NOW, NOW + timedelta(days=40)):
        journal.append(a_record(at=when))
    names = sorted(p.name for p in (isolated_journal / "runs").glob("*.jsonl"))
    assert names == ["2026-09.jsonl", "2026-10.jsonl"]


def test_a_write_failure_is_reported_not_swallowed(isolated_journal):
    (isolated_journal / "runs").parent.mkdir(parents=True, exist_ok=True)
    # A file where the runs directory should be: mkdir will fail.
    (isolated_journal / "runs").write_text("not a directory")
    with pytest.raises(store.JournalWriteError):
        journal.append(a_record())


def test_record_run_never_raises_when_the_journal_is_broken(
    isolated_journal, capsys
):
    (isolated_journal / "runs").parent.mkdir(parents=True, exist_ok=True)
    (isolated_journal / "runs").write_text("not a directory")

    written = journal.record_run(
        settings=SETTINGS, mode=journal.MODE_SCHEDULE, at=NOW,
        placements=(a_placement(),),
    )
    assert written is False
    assert "journal not written" in capsys.readouterr().err


def test_detail_off_writes_nothing(isolated_journal):
    written = journal.record_run(
        settings=replace(SETTINGS, journal_detail=journal.DETAIL_OFF),
        mode=journal.MODE_SCHEDULE, at=NOW, placements=(a_placement(),),
    )
    assert written is False
    assert not (isolated_journal / "runs").exists()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def append_at(when, **kwargs):
    journal.append(a_record(at=when, **kwargs))


def test_load_returns_records_oldest_first(isolated_journal):
    append_at(NOW + timedelta(days=2))
    append_at(NOW)
    append_at(NOW + timedelta(days=1))

    got = journal.load()
    assert [r.at for r in got.records] == [
        NOW, NOW + timedelta(days=1), NOW + timedelta(days=2)
    ]


def test_load_reads_across_month_files(isolated_journal):
    append_at(NOW)
    append_at(NOW + timedelta(days=40))
    assert len(journal.load().records) == 2


def test_since_and_until_bound_the_range(isolated_journal):
    for day in range(5):
        append_at(NOW + timedelta(days=day))

    got = journal.load(since=NOW + timedelta(days=1), until=NOW + timedelta(days=3))
    assert [r.at for r in got.records] == [
        NOW + timedelta(days=1), NOW + timedelta(days=2)
    ]


def test_until_is_exclusive(isolated_journal):
    append_at(NOW)
    assert journal.load(until=NOW).records == []


def test_modes_can_be_filtered(isolated_journal):
    append_at(NOW, mode=journal.MODE_SCHEDULE)
    append_at(NOW + timedelta(hours=1), mode=FOREIGN_MODE)

    got = journal.load(modes=(journal.MODE_SCHEDULE,))
    assert [r.mode for r in got.records] == [journal.MODE_SCHEDULE]


def test_an_empty_journal_is_not_an_error(isolated_journal):
    got = journal.load()
    assert got.records == []
    assert got.files_read == 0
    assert got.healthy


def test_a_truncated_final_line_is_expected_not_corruption(
    isolated_journal,
):
    # What a run killed mid-append leaves behind.
    append_at(NOW)
    path = next((isolated_journal / "runs").glob("*.jsonl"))
    with open(path, "a") as fh:
        fh.write('{"schema":1,"run_id":"half","at":"2026-09')

    got = journal.load()
    assert len(got.records) == 1
    assert got.truncated_tails == 1
    assert got.unreadable_lines == 0
    assert got.healthy


def test_a_damaged_line_in_the_middle_is_counted(isolated_journal):
    append_at(NOW)
    append_at(NOW + timedelta(hours=1))
    path = next((isolated_journal / "runs").glob("*.jsonl"))
    lines = path.read_text().splitlines(keepends=True)
    path.write_text(lines[0] + "{not json at all}\n" + lines[1])

    got = journal.load()
    assert len(got.records) == 2
    assert got.unreadable_lines == 1
    assert not got.healthy


def test_blank_lines_are_ignored(isolated_journal):
    append_at(NOW)
    path = next((isolated_journal / "runs").glob("*.jsonl"))
    path.write_text("\n" + path.read_text() + "\n\n")

    got = journal.load()
    assert len(got.records) == 1
    assert got.healthy


def test_unknown_future_fields_are_kept_not_dropped(isolated_journal):
    write_lines(
        isolated_journal, "2026-09",
        json.dumps({
            "schema": 99, "run_id": "future", "at": "2026-09-07T09:00:00Z",
            "mode": "schedule", "lane_capacity": 480, "placements": [],
        }) + "\n",
    )
    (record,) = journal.load().records
    assert record.unknown == {"lane_capacity": 480}
    # And a round-trip doesn't lose it, so an old tool reading and rewriting
    # a newer journal isn't destructive.
    assert json.loads(record.to_line())["lane_capacity"] == 480


def test_a_record_with_no_timestamp_is_unusable(isolated_journal):
    write_lines(
        isolated_journal, "2026-09",
        json.dumps({"schema": 1, "run_id": "x", "placements": []}) + "\n",
    )
    got = journal.load()
    assert got.records == []
    assert got.unreadable_lines == 1


def test_a_placement_with_no_event_is_skipped_but_the_record_stands(
    isolated_journal,
):
    # A block with no event id can't be followed from one run to the next,
    # which is the only thing the placement log is for.
    write_lines(
        isolated_journal, "2026-09",
        json.dumps({
            "schema": 1, "run_id": "x", "at": "2026-09-07T09:00:00Z",
            "placements": [
                {"uuid": "u1", "start": "2026-09-07T09:00:00Z",
                 "end": "2026-09-07T10:00:00Z"},
                {"uuid": "u2", "event": "ev2", "start": "2026-09-07T09:00:00Z",
                 "end": "2026-09-07T10:00:00Z"},
            ],
        }) + "\n",
    )
    (record,) = journal.load().records
    assert [p.task_uuid for p in record.placements] == ["u2"]


def test_a_placement_with_an_unreadable_time_is_skipped(isolated_journal):
    # Unlike a task field, a block with no readable start isn't partially
    # useful: every question asked of it is "when was it".
    write_lines(
        isolated_journal, "2026-09",
        json.dumps({
            "schema": 1, "run_id": "x", "at": "2026-09-07T09:00:00Z",
            "placements": [{"uuid": "u1", "event": "ev1", "start": "whenever",
                            "end": "2026-09-07T10:00:00Z"}],
        }) + "\n",
    )
    (record,) = journal.load().records
    assert record.placements == ()


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def test_the_settings_hash_is_stable_across_calls():
    assert journal.settings_hash(SETTINGS) == journal.settings_hash(SETTINGS)


def test_the_settings_hash_ignores_work_day_ordering():
    a = replace(SETTINGS, work_days=frozenset({0, 1, 2}))
    b = replace(SETTINGS, work_days=frozenset({2, 1, 0}))
    assert journal.settings_hash(a) == journal.settings_hash(b)


@pytest.mark.parametrize(
    "field, value",
    [
        ("work_end_hour", 20),
        ("work_days", frozenset({0, 1})),
        ("settle_days", 5),
        ("calendar_id", "other@x"),
        ("report", "ready"),
        ("timezone", "Europe/London"),
    ],
)
def test_a_meaning_changing_setting_changes_the_hash(field, value):
    # Each of these makes last week's numbers mean something else, so a
    # review must be able to see the boundary.
    assert journal.settings_hash(replace(SETTINGS, **{field: value})) != (
        journal.settings_hash(SETTINGS)
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("lookback_days", 30),
        ("removal_guard_ratio", 0.9),
        ("event_color_id", "5"),
        ("journal_detail", "minimal"),
    ],
)
def test_a_mechanical_setting_does_not_change_the_hash(field, value):
    # Changing how far back we list events doesn't make a past week
    # incomparable, and treating it as a trend boundary would be noise.
    assert journal.settings_hash(replace(SETTINGS, **{field: value})) == (
        journal.settings_hash(SETTINGS)
    )


def test_run_ids_are_unique_and_sortable():
    ids = [records.new_run_id(NOW + timedelta(seconds=i)) for i in range(3)]
    assert len(set(ids)) == 3
    assert ids == sorted(ids)
    assert len(set(records.new_run_id(NOW) for _ in range(50))) == 50


def test_definition_boundaries_flag_a_settings_change():
    def record(hash_value, offset):
        return records.RunRecord(
            run_id=f"r{offset}", at=NOW + timedelta(days=offset),
            mode=journal.MODE_SCHEDULE, timezone_name="UTC",
            settings_hash=hash_value, calendar_id="primary", report="next",
        )

    changed = store.definition_boundaries(
        [record("aaa", 0), record("aaa", 1), record("bbb", 2), record("bbb", 3)]
    )
    assert changed == [NOW + timedelta(days=2)]


def test_a_record_in_an_unfamiliar_mode_is_not_a_definition_boundary():
    # Only a run that actually touched the calendar tells us what settings
    # were in force. Anything else — a mode a later version writes, whatever
    # it means — must not be read as "your settings changed and changed back".
    def record(mode, hash_value, offset):
        return records.RunRecord(
            run_id=f"r{offset}", at=NOW + timedelta(days=offset), mode=mode,
            timezone_name="UTC", settings_hash=hash_value,
            calendar_id="primary", report="next",
        )

    assert store.definition_boundaries([
        record(journal.MODE_SCHEDULE, "aaa", 0),
        record(FOREIGN_MODE, "zzz", 1),
        record(journal.MODE_SCHEDULE, "aaa", 2),
    ]) == []


def test_observed_days_ignore_unhealthy_runs():
    # A run whose source came back empty saw nothing. Letting it stand in
    # for a day would turn missing data into zero.
    def record(offset, ok):
        return records.RunRecord(
            run_id=f"r{offset}", at=NOW + timedelta(days=offset),
            mode=journal.MODE_SCHEDULE, timezone_name="UTC",
            settings_hash="aaa", calendar_id="primary", report="next",
            source_ok=ok,
        )

    days = list(
        store.iter_observed_days(
            [record(0, True), record(1, False), record(2, True)], timezone.utc
        )
    )
    assert days == ["2026-09-07", "2026-09-09"]


def test_observed_days_are_deduplicated():
    def record(hours):
        return records.RunRecord(
            run_id=f"r{hours}", at=NOW + timedelta(hours=hours),
            mode=journal.MODE_SCHEDULE, timezone_name="UTC",
            settings_hash="aaa", calendar_id="primary", report="next",
        )

    days = list(
        store.iter_observed_days([record(0), record(1), record(2)], timezone.utc)
    )
    assert days == ["2026-09-07"]


# ---------------------------------------------------------------------------
# The write-only boundary
# ---------------------------------------------------------------------------

def test_the_observer_cannot_read_history():
    # Scheduling appends and never consults history to place anything, so a
    # corrupt or deleted journal can't produce a wrong calendar. Enforced by
    # the module split rather than by discipline, so assert the split.
    from task_gcal.journal import observe

    source = observe.__file__
    with open(source) as fh:
        text = fh.read()
    for reader in ("load(", "run_files(", "iter_observed_days", "read_text"):
        assert reader not in text, f"{reader} leaked into the write path"


def test_scheduling_does_not_import_the_journal_reader():
    from task_gcal import placement, schedule

    for module in (schedule, placement):
        assert not hasattr(module, "load")
