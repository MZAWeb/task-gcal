"""Tests for the `task info` modification-log parser.

The fixtures below are real `task <uuid> info` output, trimmed. The format is
built for humans and shifts with terminal width, which is exactly why the
parser has to be pinned: it is the only source of past due-date pushes, and
a silent parse failure looks like "you never moved a deadline".
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from task_gcal.history import (
    FIELD_DESCRIPTION,
    FIELD_DUE,
    FIELD_ESTIMATE,
    FIELD_PROJECT,
    FIELD_SCHEDULED,
    FIELD_STATUS,
    KIND_CHANGED,
    KIND_DELETED,
    KIND_SET,
    parse_info,
    parse_local_value,
)

TZ = ZoneInfo("Europe/Madrid")

# Wide layout: one modification per line, 19-character date column.
WIDE = """\
Name          Value
UUID          20fc0bec-a508-44cf-a46d-9469d4cfc931
Urgency       24.44

Date                Modification
------------------- ----------------------------------------------------------
2026-08-31 00:20:41 Description set to 'Calefa'.
                    Due set to '2026-08-31 00:00:00'.
                    Entry set to '2026-08-31 00:20:41'.
                    Priority set to 'H'.
                    Status set to 'pending'.
2026-08-31 00:21:54 Estimate set to '15'.
2026-08-31 00:23:00 Project set to 'personal'.
2026-09-01 16:05:46 Due changed from '2026-08-31 00:00:00' to '2026-09-03 00:00:00'.
2026-09-03 10:42:13 Due changed from '2026-09-03 00:00:00' to '2026-09-07 00:00:00'.

"""

# Narrow layout: the date itself wraps onto a second line, and a long
# modification wraps too. Both are what a default 80-column terminal gives.
NARROW = """\
Date       Modification
---------- -----------------------------------------------------------------
2026-08-31 Description set to 'Calefa'.
00:20:41   Due set to '2026-08-31 00:00:00'.
           Status set to 'pending'.
2026-09-01 Due changed from '2026-08-31 00:00:00' to '2026-09-03
16:05:46   00:00:00'.

"""


def changes(text, **kwargs):
    return parse_info(text, uuid="u1", tz=TZ, **kwargs).changes


def local(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=TZ)


# ---------------------------------------------------------------------------
# The wide layout
# ---------------------------------------------------------------------------

def test_a_due_date_push_is_captured_with_both_values():
    pushes = [c for c in changes(WIDE) if c.field == FIELD_DUE and c.old]
    assert len(pushes) == 2
    first = pushes[0]
    assert first.at == local(2026, 9, 1, 16, 5, 46)
    assert first.kind == KIND_CHANGED
    assert parse_local_value(first.old, TZ) == local(2026, 8, 31)
    assert parse_local_value(first.new, TZ) == local(2026, 9, 3)


def test_modifications_sharing_a_timestamp_are_all_kept():
    # Taskwarrior prints the date only on the first of a group.
    at_creation = [c for c in changes(WIDE) if c.at == local(2026, 8, 31, 0, 20, 41)]
    assert {c.field for c in at_creation} == {
        FIELD_DESCRIPTION, FIELD_DUE, FIELD_STATUS
    }


def test_an_initial_set_has_no_old_value():
    (due_set,) = [
        c for c in changes(WIDE) if c.field == FIELD_DUE and c.kind == KIND_SET
    ]
    assert due_set.old is None
    assert parse_local_value(due_set.new, TZ) == local(2026, 8, 31)


def test_the_estimate_uda_is_read():
    (est,) = [c for c in changes(WIDE) if c.field == FIELD_ESTIMATE]
    assert est.new == "15"


def test_a_renamed_estimate_uda_is_followed():
    text = WIDE.replace("Estimate set to '15'.", "Est set to '15'.")
    assert [c.new for c in changes(text, estimate_uda="est") if c.field == FIELD_ESTIMATE] == ["15"]
    # And the default name no longer matches, rather than matching loosely.
    assert [c for c in changes(text) if c.field == FIELD_ESTIMATE] == []


def test_the_project_is_read():
    (project,) = [c for c in changes(WIDE) if c.field == FIELD_PROJECT]
    assert project.new == "personal"


def test_a_description_change_is_recorded_without_its_text():
    # Descriptions are free text and can contain the quote every other
    # pattern relies on, so only the fact of the change is kept.
    (desc,) = [c for c in changes(WIDE) if c.field == FIELD_DESCRIPTION]
    assert desc.new is None
    assert desc.old is None


def test_changes_come_back_in_chronological_order():
    parsed = changes(WIDE)
    assert [c.at for c in parsed] == sorted(c.at for c in parsed)


def test_unrecognized_lines_are_counted_not_guessed_at():
    # `Entry set to` and `Priority set to` are real modifications we don't
    # model. Counting them makes a sudden jump visible.
    history = parse_info(WIDE, uuid="u1", tz=TZ)
    assert history.unrecognized == 2


# ---------------------------------------------------------------------------
# The narrow layout
# ---------------------------------------------------------------------------

def test_a_wrapped_date_column_still_groups_correctly():
    parsed = changes(NARROW)
    assert {c.field for c in parsed if c.at == local(2026, 8, 31, 0, 20, 41)} == {
        FIELD_DESCRIPTION, FIELD_DUE, FIELD_STATUS
    }


def test_a_wrapped_value_is_rejoined_not_lost():
    # The push whose second timestamp wrapped onto the next line. Losing it
    # would silently understate deadline churn.
    (push,) = [c for c in changes(NARROW) if c.field == FIELD_DUE and c.old]
    assert parse_local_value(push.old, TZ) == local(2026, 8, 31)
    assert parse_local_value(push.new, TZ) == local(2026, 9, 3)


# ---------------------------------------------------------------------------
# Degenerate input
# ---------------------------------------------------------------------------

def test_output_with_no_modification_table_yields_nothing():
    history = parse_info("Name  Value\nUUID  u1\n", uuid="u1", tz=TZ)
    assert history.changes == []
    assert history.unrecognized == 0


def test_empty_output_is_not_an_error():
    assert parse_info("", uuid="u1", tz=TZ).changes == []


def test_the_table_ends_at_the_first_blank_line():
    # The urgency breakdown table follows the modification table in real
    # output; nothing after the blank line may be read as a modification.
    text = WIDE + "----- -----\n  due  18.4 Due set to '2030-01-01 00:00:00'.\n"
    assert not any(
        c.new == "2030-01-01 00:00:00" for c in changes(text)
    )


def test_a_deletion_is_recorded_as_a_deletion():
    text = (
        "Date                Modification\n"
        "------------------- ------------\n"
        "2026-05-13 22:54:00 Scheduled deleted.\n"
        "\n"
    )
    (change,) = changes(text)
    assert change.field == FIELD_SCHEDULED
    assert change.kind == KIND_DELETED
    assert change.new is None


def test_a_deletion_that_names_its_old_value_keeps_it():
    text = (
        "Date                Modification\n"
        "------------------- ------------\n"
        "2026-05-13 22:54:00 Due deleted (was '2026-05-20 00:00:00').\n"
        "\n"
    )
    (change,) = changes(text)
    assert change.kind == KIND_DELETED
    assert parse_local_value(change.old, TZ) == local(2026, 5, 20)


def test_an_unparseable_date_is_counted_not_dated_wrongly():
    text = (
        "Date                Modification\n"
        "------------------- ------------\n"
        "not-a-date          Due set to '2026-05-20 00:00:00'.\n"
        "\n"
    )
    history = parse_info(text, uuid="u1", tz=TZ)
    assert history.changes == []
    assert history.unrecognized == 1


@pytest.mark.parametrize("value", ["", "   ", "nonsense"])
def test_parse_local_value_declines_junk(value):
    assert parse_local_value(value, TZ) is None


def test_parse_local_value_attaches_the_given_zone():
    got = parse_local_value("2026-08-31 09:30:00", TZ)
    assert got == local(2026, 8, 31, 9, 30)
    assert got.tzinfo is TZ
