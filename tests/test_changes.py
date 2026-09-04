"""Tests for the durable task-change history and its harvester.

The properties that matter are all about honesty under damage: harvesting twice
must not double anything, an operation we can't read must not freeze history,
pruning must be reported rather than looking like a quiet week, and a rebuilt
database must not silently reuse ids into the wrong changes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from task_gcal import changes
from task_gcal import taskchampion as tc
from task_gcal.changes import records, store
from task_gcal.config import Settings

from conftest import taskchampion_db, tc_update

UTC = timezone.utc
SETTINGS = Settings(timezone="UTC")


def db(tmp_path, ops, **kwargs):
    return taskchampion_db(tmp_path / "task", ops, **kwargs)


def harvest(tmp_path, ops, *, settings=SETTINGS, **kwargs):
    return changes.harvest(settings, db_path=db(tmp_path, ops, **kwargs))


def a_change(**kwargs):
    base = dict(
        at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        uuid="u1",
        field=records.FIELD_DUE,
        old="1788472800",
        new="1788732000",
        op_id=7,
        fingerprint="abc123",
    )
    base.update(kwargs)
    return records.TaskChange(**base)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

def test_a_change_round_trips():
    import json

    back = records.TaskChange.from_dict(json.loads(a_change().to_line()))
    assert back == a_change()


def test_values_stay_raw_in_the_record():
    # Converting on the way in would bake one interpretation into a record we
    # can never re-derive once the upstream log is pruned.
    assert a_change().to_dict()["new"] == "1788732000"


def test_a_timestamp_field_is_interpreted_as_epoch_seconds():
    assert a_change().value_at() == datetime(2026, 9, 6, 22, 0, tzinfo=UTC)
    assert a_change().value_at(before=True) == datetime(2026, 9, 3, 22, 0, tzinfo=UTC)


def test_an_estimate_is_interpreted_as_minutes():
    change = a_change(field=records.FIELD_ESTIMATE, old="60", new="180")
    assert change.value_at() == 180
    assert change.value_at(before=True) == 60


def test_a_text_field_is_left_alone():
    change = a_change(field=records.FIELD_PROJECT, old=None, new="fraud")
    assert change.value_at() == "fraud"


@pytest.mark.parametrize("raw", ["", "not-a-number", None])
def test_an_unreadable_value_is_missing_not_a_guess(raw):
    # A due date we can't read is missing, not 1970.
    assert records.interpret(records.FIELD_DUE, raw) is None


def test_a_zero_estimate_counts_as_missing():
    assert records.interpret(records.FIELD_ESTIMATE, "0") is None


def test_identity_is_the_change_not_the_operation_id():
    # A rebuilt database restarts AUTOINCREMENT, so the same id can name a
    # different change; keying on content makes re-harvesting idempotent.
    assert a_change(op_id=7).key == a_change(op_id=999).key
    assert a_change(new="1").key != a_change(new="2").key


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def test_appending_and_loading_round_trips(isolated_journal):
    store.append([a_change()])
    history = store.load()

    assert history.changes == [a_change()]
    assert history.unreadable_lines == 0


def test_the_file_is_private(isolated_journal):
    store.append([a_change()])
    assert oct(store.path().stat().st_mode)[-3:] == "600"


def test_high_water_and_anchor_come_from_the_records(isolated_journal):
    # Not a separate checkpoint file: a crash between appending and updating one
    # would silently skip operations, whereas re-reading a few is harmless.
    store.append([a_change(op_id=3, fingerprint="aaa"), a_change(op_id=9, new="x", fingerprint="zzz")])
    history = store.load()

    assert history.high_water == 9
    assert history.anchor == (9, "zzz")


def test_an_empty_store_has_no_anchor(isolated_journal):
    history = store.load()
    assert history.high_water == 0
    assert history.anchor is None


def test_a_gap_still_advances_the_high_water_mark(isolated_journal):
    # Otherwise one unreadable operation freezes history until someone ships a
    # fix for it.
    store.append([records.Gap(op_id=12, kind="Rearrange", fingerprint="ggg")])
    history = store.load()

    assert history.high_water == 12
    assert history.anchor == (12, "ggg")
    assert len(history.gaps) == 1


def test_a_truncated_final_line_is_expected_not_corruption(isolated_journal):
    store.append([a_change()])
    with open(store.path(), "a") as fh:
        fh.write('{"at":"2026-09')

    history = store.load()
    assert len(history.changes) == 1
    assert history.truncated_tail
    assert history.unreadable_lines == 0


def test_a_damaged_line_in_the_middle_is_counted(isolated_journal):
    store.append([a_change(), a_change(new="other")])
    lines = store.path().read_text().splitlines(keepends=True)
    store.path().write_text(lines[0] + "{nonsense}\n" + lines[1])

    history = store.load()
    assert len(history.changes) == 2
    assert history.unreadable_lines == 1


def test_changes_come_back_in_time_order(isolated_journal):
    late = a_change(at=datetime(2026, 9, 5, tzinfo=UTC), op_id=2)
    early = a_change(at=datetime(2026, 9, 1, tzinfo=UTC), op_id=1)
    store.append([late, early])

    assert [c.at for c in store.load().changes] == [early.at, late.at]


# ---------------------------------------------------------------------------
# Harvesting
# ---------------------------------------------------------------------------

def test_a_first_harvest_stores_what_it_finds(tmp_path, isolated_journal):
    report = harvest(tmp_path, [tc_update(prop="due", new="1788732000")])

    assert report.outcome == changes.STARTED
    assert report.changes_added == 1
    (stored,) = store.load().changes
    assert (stored.field, stored.new) == ("due", "1788732000")


def test_harvesting_twice_adds_nothing(tmp_path, isolated_journal):
    ops = [tc_update()]
    first = harvest(tmp_path, ops)
    second = changes.harvest(SETTINGS, db_path=tmp_path / "task" / tc.DB_FILENAME)

    assert first.changes_added == 1
    assert second.changes_added == 0
    assert second.outcome == changes.CONTINUED
    assert len(store.load().changes) == 1


def test_only_new_operations_are_read_on_the_second_pass(tmp_path, isolated_journal):
    path = db(tmp_path, [tc_update(new=str(n)) for n in range(3)])
    changes.harvest(SETTINGS, db_path=path)
    second = changes.harvest(SETTINGS, db_path=path)

    assert second.operations_read == 0


def test_noise_is_skipped_but_counted(tmp_path, isolated_journal):
    report = harvest(
        tmp_path, [tc_update(prop="modified"), tc_update(prop="due")]
    )

    assert report.changes_added == 1
    assert report.skipped_noise == 1


def test_a_property_no_metric_wants_is_ignored_silently(tmp_path, isolated_journal):
    # Not a gap: we understood it perfectly and chose not to keep it.
    report = harvest(tmp_path, [tc_update(prop="priority", new="H")])

    assert report.changes_added == 0
    assert report.gaps_added == 0


def test_the_estimate_uda_is_canonicalised(tmp_path, isolated_journal):
    # `estimate_uda = "est"` and `= "estimate"` must produce one continuous
    # history, not two halves.
    from dataclasses import replace

    report = harvest(
        tmp_path,
        [tc_update(prop="est", new="90")],
        settings=replace(SETTINGS, estimate_uda="est"),
    )

    assert report.changes_added == 1
    (stored,) = store.load().changes
    assert stored.field == records.FIELD_ESTIMATE
    assert stored.value_at() == 90


def test_create_and_undo_points_are_not_changes(tmp_path, isolated_journal):
    report = harvest(
        tmp_path, [{"Create": {"uuid": "u1"}}, '"UndoPoint"', tc_update()]
    )

    assert report.changes_added == 1
    assert report.gaps_added == 0


# ---------------------------------------------------------------------------
# Damage, pruning and rebuilds
# ---------------------------------------------------------------------------

def test_an_unreadable_operation_becomes_a_gap_and_does_not_stop_harvesting(
    tmp_path, isolated_journal
):
    report = harvest(
        tmp_path,
        [tc_update(new="1"), {"Rearrange": {"how": "sideways"}}, tc_update(new="2")],
    )

    assert report.gaps_added == 1
    assert report.changes_added == 2  # the operation after the gap was still read
    assert store.load().high_water == 3
    assert not report.exact  # and coverage says so


def test_pruning_that_removed_only_harvested_rows_is_a_clean_continuation(
    tmp_path, isolated_journal
):
    early = db(tmp_path, [tc_update(new="1"), tc_update(new="2")])
    changes.harvest(SETTINGS, db_path=early)

    # The sync pruned both, and a newer operation arrived with a higher id.
    import sqlite3

    con = sqlite3.connect(early)
    con.execute("delete from operations")
    con.execute(
        "insert into operations (id, data, synced) values (9, ?, 0)",
        (__import__("json").dumps(tc_update(new="3")),),
    )
    con.commit()
    con.close()

    report = changes.harvest(SETTINGS, db_path=early)
    assert report.outcome == changes.CONTINUED
    assert report.lost_operations is False
    assert report.changes_added == 1


def test_a_rebuilt_database_is_detected_and_does_not_double_history(
    tmp_path, isolated_journal
):
    # A recovery restarts AUTOINCREMENT, so the id we anchored on now names a
    # different operation. We re-harvest from scratch and content-dedup absorbs
    # the overlap.
    original = db(tmp_path / "first", [tc_update(new="1"), tc_update(new="2")])
    changes.harvest(SETTINGS, db_path=original)
    assert len(store.load().changes) == 2

    # Same ids, different content at the anchor — plus one of the originals,
    # so we can prove the overlap deduplicates rather than doubling.
    rebuilt = db(
        tmp_path / "second",
        [tc_update(new="9"), tc_update(new="8"), tc_update(new="1")],
    )
    report = changes.harvest(SETTINGS, db_path=rebuilt)

    assert report.outcome == changes.REBUILT
    assert report.lost_operations is True
    assert report.changes_added == 2  # "9" and "8"; "1" we already had
    assert len(store.load().changes) == 4


def test_a_rebuild_whose_content_still_matches_is_just_a_continuation(
    tmp_path, isolated_journal
):
    # If the anchor operation is byte-identical, the log is indistinguishable
    # from one that was never rebuilt — and treating it as a rebuild would
    # re-read everything for nothing.
    original = db(tmp_path / "first", [tc_update(new="1"), tc_update(new="2")])
    changes.harvest(SETTINGS, db_path=original)

    same = db(
        tmp_path / "second",
        [tc_update(new="1"), tc_update(new="2"), tc_update(new="3")],
    )
    report = changes.harvest(SETTINGS, db_path=same)

    assert report.outcome == changes.CONTINUED
    assert report.changes_added == 1
    assert len(store.load().changes) == 3


def test_an_unreadable_database_degrades_without_raising(tmp_path, isolated_journal):
    report = changes.harvest(SETTINGS, db_path=tmp_path / "nowhere.sqlite3")

    assert report.outcome == changes.UNAVAILABLE
    assert report.reason
    assert "not harvested" in changes.describe(report)


def test_an_unknown_schema_degrades_without_raising(tmp_path, isolated_journal):
    report = harvest(tmp_path, [tc_update()], version=(9, 0))

    assert report.outcome == changes.UNAVAILABLE
    assert "unrecognized" in report.reason


def test_a_newer_minor_schema_is_harvested_but_flagged(tmp_path, isolated_journal):
    report = harvest(tmp_path, [tc_update()], version=(0, 99))

    assert report.changes_added == 1
    assert report.schema_untested


# ---------------------------------------------------------------------------
# How much detail is kept
# ---------------------------------------------------------------------------

MINIMAL = replace(SETTINGS, journal_detail="minimal")


def test_minimal_detail_keeps_a_digest_instead_of_the_title(
    tmp_path, isolated_journal
):
    report = harvest(
        tmp_path,
        [tc_update(prop="description", old="water the plants", new="water them")],
        settings=MINIMAL,
    )

    (stored,) = store.load().changes
    assert report.changes_added == 1
    assert "water" not in store.path().read_text()
    assert stored.redacted
    # Still a rename: the two sides differ, which is all the metric asks.
    assert stored.old != stored.new


def test_minimal_detail_still_keeps_dates_and_estimates(tmp_path, isolated_journal):
    # There is nothing private about a deadline moving, and hashing it would
    # make the number unusable.
    harvest(tmp_path, [tc_update(prop="due", new="1788732000")], settings=MINIMAL)

    (stored,) = store.load().changes
    assert stored.new == "1788732000"
    assert not stored.redacted


def test_the_same_title_hashes_the_same_way(tmp_path, isolated_journal):
    assert records.redact("a title") == records.redact("a title")
    assert records.redact("a title") != records.redact("another title")


def test_a_redacted_change_is_the_same_change_as_the_readable_one(
    tmp_path, isolated_journal
):
    # Turning the dial down and then re-harvesting — which a rebuilt
    # Taskwarrior database forces — must not store one rename twice, once
    # readable and once hashed, and double every count of it.
    rename = tc_update(prop="description", old="before", new="after")
    harvest(tmp_path / "first", [rename])

    # The same rename, but with an earlier operation in front of it: ids no
    # longer mean what we stored, which is what "rebuilt" looks like.
    rebuilt = db(tmp_path / "second", [tc_update(prop="due", new="1788732000"), rename])
    again = changes.harvest(MINIMAL, db_path=rebuilt)

    assert again.outcome == changes.REBUILT
    stored = store.load().changes
    assert [c.field for c in stored] == ["description", "due"]


def test_detail_off_harvests_nothing(tmp_path, isolated_journal):
    report = harvest(
        tmp_path,
        [tc_update(prop="description", new="water the plants")],
        settings=replace(SETTINGS, journal_detail="off"),
    )

    assert report.changes_added == 0
    assert report.outcome == changes.UNAVAILABLE
    assert "off" in report.reason
    assert store.load().changes == []


def test_a_configured_sync_server_ends_the_promise_of_exactness(
    tmp_path, isolated_journal
):
    report = harvest(tmp_path, [tc_update()], synced=True)

    assert report.sync_configured
    assert "pruning is possible" in changes.describe(report)


def test_a_clean_harvest_reports_itself_exact(tmp_path, isolated_journal):
    report = harvest(tmp_path, [tc_update()])
    assert report.exact


def test_the_earliest_known_change_is_reported(tmp_path, isolated_journal):
    report = harvest(
        tmp_path,
        [
            tc_update(new="1", at="2026-06-01T09:00:00Z"),
            tc_update(new="2", at="2026-07-01T09:00:00Z"),
        ],
    )
    assert report.earliest_known == datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def test_a_write_failure_degrades_without_raising(tmp_path, isolated_journal):
    isolated_journal.mkdir(parents=True, exist_ok=True)
    store.path().mkdir()  # a directory where the file should be

    report = harvest(tmp_path, [tc_update()])
    assert report.outcome == changes.UNAVAILABLE
