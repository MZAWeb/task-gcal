"""Tests for the read-only TaskChampion `operations` adapter.

The fixture builds a database with the real schema — `(singleton, major,
minor)` in `version`, JSON blobs in `operations` — rather than mocking, because
the whole risk this module carries is that we're reading someone else's private
storage. A test against a mock of our own assumptions would prove nothing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from task_gcal import taskchampion as tc

UTC = timezone.utc


def make_db(tmp_path, ops, *, version=(0, 2), synced=False, wal=False):
    """A database shaped like TaskChampion's."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / tc.DB_FILENAME
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE version (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 0),
            major INTEGER, minor INTEGER);
        CREATE TABLE operations (id INTEGER PRIMARY KEY AUTOINCREMENT,
            data STRING, synced BOOL);
        CREATE TABLE sync_meta (key STRING PRIMARY KEY, value STRING);
        """
    )
    if version is not None:
        con.execute("INSERT INTO version VALUES (0, ?, ?)", version)
    for data in ops:
        blob = data if isinstance(data, str) else json.dumps(data)
        con.execute("INSERT INTO operations (data, synced) VALUES (?, 0)", (blob,))
    if synced:
        con.execute("INSERT INTO sync_meta VALUES ('server', 'https://x')")
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.commit()
    con.close()
    return path


def update(uuid="u1", prop="due", old=None, new="1788472800", at="2026-09-01T10:00:00Z"):
    return {
        "Update": {
            "uuid": uuid, "property": prop,
            "old_value": old, "value": new, "timestamp": at,
        }
    }


# ---------------------------------------------------------------------------
# Parsing the three real variants
# ---------------------------------------------------------------------------

def test_an_update_is_parsed_in_full(tmp_path):
    path = make_db(tmp_path, [update(old="1788472800", new="1788732000")])
    (op,), _meta = tc.read(path=path)

    assert op.kind == tc.KIND_UPDATE
    assert (op.uuid, op.prop) == ("u1", "due")
    assert (op.old_value, op.value) == ("1788472800", "1788732000")
    assert op.at == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    assert op.understood


def test_values_are_kept_exactly_as_stored(tmp_path):
    # Epoch seconds as strings. Converting here would bake one interpretation
    # into the raw record; the reader decides what a value means.
    path = make_db(tmp_path, [update(new="1788732000")])
    (op,), _meta = tc.read(path=path)
    assert op.value == "1788732000"
    assert isinstance(op.value, str)


def test_a_create_carries_only_a_uuid(tmp_path):
    # Notably no timestamp — creation time arrives via the `entry` update.
    path = make_db(tmp_path, [{"Create": {"uuid": "u1"}}])
    (op,), _meta = tc.read(path=path)

    assert op.kind == tc.KIND_CREATE
    assert op.uuid == "u1"
    assert op.at is None
    assert op.understood


def test_an_undo_point_is_a_bare_string(tmp_path):
    path = make_db(tmp_path, ['"UndoPoint"'])
    (op,), _meta = tc.read(path=path)

    assert op.kind == tc.KIND_UNDO_POINT
    assert op.understood  # known and ignorable, not a gap


def test_a_null_old_value_stays_none(tmp_path):
    path = make_db(tmp_path, [update(old=None)])
    (op,), _meta = tc.read(path=path)
    assert op.old_value is None


# ---------------------------------------------------------------------------
# Things we don't understand are flagged, never guessed
# ---------------------------------------------------------------------------

def test_an_unknown_variant_is_not_understood(tmp_path):
    path = make_db(tmp_path, [{"Rearrange": {"uuid": "u1", "how": "sideways"}}])
    (op,), _meta = tc.read(path=path)

    assert op.kind == "Rearrange"
    assert not op.understood


def test_unparseable_json_is_not_understood(tmp_path):
    path = make_db(tmp_path, ["{not json"])
    (op,), _meta = tc.read(path=path)

    assert op.kind == "unparseable"
    assert not op.understood


def test_an_update_missing_its_timestamp_is_not_understood(tmp_path):
    # We can't place it in time, so it can't contribute to any history.
    path = make_db(tmp_path, [update(at=None)])
    (op,), _meta = tc.read(path=path)
    assert not op.understood


def test_an_update_with_an_unreadable_timestamp_is_not_understood(tmp_path):
    path = make_db(tmp_path, [update(at="whenever")])
    (op,), _meta = tc.read(path=path)
    assert not op.understood


def test_modified_is_flagged_as_noise(tmp_path):
    # Taskwarrior touches it on every edit; it says nothing a real change
    # doesn't, and it's the single largest property in a real log.
    path = make_db(tmp_path, [update(prop="modified"), update(prop="due")])
    ops, _meta = tc.read(path=path)

    assert [op.is_noise for op in ops] == [True, False]


# ---------------------------------------------------------------------------
# Incremental reads
# ---------------------------------------------------------------------------

def test_operations_come_back_oldest_first(tmp_path):
    path = make_db(tmp_path, [update(new=str(n)) for n in range(5)])
    ops, _meta = tc.read(path=path)
    assert [op.op_id for op in ops] == [1, 2, 3, 4, 5]


def test_after_id_is_exclusive(tmp_path):
    path = make_db(tmp_path, [update() for _ in range(5)])
    ops, _meta = tc.read(path=path, after_id=3)
    assert [op.op_id for op in ops] == [4, 5]


def test_limit_bounds_the_read(tmp_path):
    path = make_db(tmp_path, [update() for _ in range(5)])
    ops, _meta = tc.read(path=path, after_id=1, limit=2)
    assert [op.op_id for op in ops] == [2, 3]


def test_meta_describes_the_whole_log_not_just_the_slice(tmp_path):
    # Coverage reporting needs the real bounds even on an incremental read.
    path = make_db(tmp_path, [update() for _ in range(5)])
    ops, meta = tc.read(path=path, after_id=4)

    assert len(ops) == 1
    assert (meta.oldest_op_id, meta.newest_op_id, meta.total) == (1, 5, 5)


# ---------------------------------------------------------------------------
# Anchoring: pruning vs a rebuilt database
# ---------------------------------------------------------------------------

def test_find_returns_one_operation_by_id(tmp_path):
    path = make_db(tmp_path, [update(new="a"), update(new="b")])
    op = tc.find(2, path=path)
    assert op is not None and op.value == "b"


def test_find_returns_none_for_a_pruned_id(tmp_path):
    path = make_db(tmp_path, [update()])
    assert tc.find(99, path=path) is None


def test_the_same_id_with_different_content_fingerprints_differently(tmp_path):
    # AUTOINCREMENT ids get reused after a recovery, so the id alone cannot
    # tell "pruned" from "rebuilt".
    first = tc.find(1, path=make_db(tmp_path / "a", [update(new="a")]))
    rebuilt = tc.find(1, path=make_db(tmp_path / "b", [update(new="b")]))

    assert first.op_id == rebuilt.op_id == 1
    assert first.fingerprint != rebuilt.fingerprint


def test_identical_content_fingerprints_the_same(tmp_path):
    a = tc.find(1, path=make_db(tmp_path / "a", [update(new="x")]))
    b = tc.find(1, path=make_db(tmp_path / "b", [update(new="x")]))
    assert a.fingerprint == b.fingerprint


# ---------------------------------------------------------------------------
# The schema gate
# ---------------------------------------------------------------------------

def test_the_tested_schema_is_accepted(tmp_path):
    path = make_db(tmp_path, [update()], version=tc.TESTED_SCHEMA)
    _ops, meta = tc.read(path=path)

    assert meta.schema_version == tc.TESTED_SCHEMA
    assert meta.schema_is_tested


def test_a_newer_minor_is_read_but_flagged_untested(tmp_path):
    # Minor bumps are additive in practice; refusing would blind the user for
    # no gain, but the coverage report shouldn't pretend we've seen it.
    path = make_db(tmp_path, [update()], version=(0, 99))
    _ops, meta = tc.read(path=path)

    assert meta.schema_version == (0, 99)
    assert not meta.schema_is_tested


def test_an_unknown_major_is_refused(tmp_path):
    # The storage was restructured; reading on regardless is how you invent
    # history that never happened.
    path = make_db(tmp_path, [update()], version=(1, 0))
    with pytest.raises(tc.OperationsUnavailable, match="unrecognized"):
        tc.read(path=path)


def test_a_missing_version_table_is_refused(tmp_path):
    path = make_db(tmp_path, [update()], version=None)
    with pytest.raises(tc.OperationsUnavailable):
        tc.read(path=path)


def test_the_gate_can_be_bypassed_deliberately(tmp_path):
    # `doctor` wants to describe an unreadable database, not just fail.
    path = make_db(tmp_path, [update()], version=(9, 9))
    ops, meta = tc.read(path=path, require_known_schema=False)

    assert len(ops) == 1
    assert meta.schema_version == (9, 9)


# ---------------------------------------------------------------------------
# Never our file to damage
# ---------------------------------------------------------------------------

def test_a_missing_database_degrades_with_a_reason(tmp_path):
    with pytest.raises(tc.OperationsUnavailable, match="no operations database"):
        tc.read(path=tmp_path / "nope.sqlite3")


def test_the_connection_is_read_only(tmp_path):
    path = make_db(tmp_path, [update()])
    con = tc._connect(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("delete from operations")
    finally:
        con.close()


def test_reading_leaves_the_database_untouched(tmp_path):
    path = make_db(tmp_path, [update() for _ in range(3)])
    before = (path.stat().st_mtime_ns, path.read_bytes())
    tc.read(path=path)
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before


def test_a_wal_database_is_readable(tmp_path):
    # The live database runs in WAL mode; a reader must not need to convert it.
    path = make_db(tmp_path, [update()], wal=True)
    ops, _meta = tc.read(path=path)
    assert len(ops) == 1


def test_a_corrupt_database_degrades_rather_than_raising_sqlite(tmp_path):
    path = tmp_path / tc.DB_FILENAME
    path.write_bytes(b"this is not a database")
    with pytest.raises(tc.OperationsUnavailable):
        tc.read(path=path)


# ---------------------------------------------------------------------------
# Sync detection
# ---------------------------------------------------------------------------

def test_an_unsynced_log_is_reported_as_such(tmp_path):
    path = make_db(tmp_path, [update()], synced=False)
    _ops, meta = tc.read(path=path)
    assert meta.sync_configured is False


def test_a_configured_sync_server_is_detected(tmp_path):
    # It means `purge.on-sync` can prune operations we haven't harvested, so
    # coverage can no longer be claimed as exact.
    path = make_db(tmp_path, [update()], synced=True)
    _ops, meta = tc.read(path=path)
    assert meta.sync_configured is True


def test_an_empty_log_is_not_an_error(tmp_path):
    path = make_db(tmp_path, [])
    ops, meta = tc.read(path=path)

    assert ops == []
    assert meta.empty
    assert meta.newest_op_id is None
