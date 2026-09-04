"""What happens when something outside task-gcal touches one of our blocks.

Two halves. First that we notice at all — comparing an event against the stamp
we left on it, with no history involved. Then that noticing is a one-off: the
run adopts what it found, so the same hand-move is reported once and not on
every run until the block is over.
"""

from __future__ import annotations

from datetime import timedelta

from task_gcal import drift
from task_gcal.gcal import BY_HUMAN, Expectation

from conftest import (
    FRI_5PM,
    NOW,
    WED_5PM,
    at,
    hand_moved,
    managed_event,
    task_row,
    unstamped,
)


def ours(**kwargs):
    base = dict(id="ev1", task_uuid="u1", start=at(1, 9), end=at(1, 10))
    base.update(kwargs)
    return managed_event(**base)


# ---------------------------------------------------------------------------
# Noticing
# ---------------------------------------------------------------------------

def test_a_block_nobody_touched_is_not_drift():
    assert drift.detect([ours()], now=NOW) == []


def test_a_block_somebody_moved_is_drift():
    (found,) = drift.detect(
        [hand_moved(ours(), start=at(1, 14), end=at(1, 15))], now=NOW
    )
    assert found.kinds == (drift.MOVED,)
    assert found.expected_start == at(1, 9)
    assert found.event.start == at(1, 14)


def test_a_block_somebody_renamed_is_drift():
    (found,) = drift.detect([hand_moved(ours(), summary="mine now")], now=NOW)
    assert found.kinds == (drift.RETITLED,)


def test_a_block_both_moved_and_renamed_reports_both():
    (found,) = drift.detect(
        [hand_moved(ours(), start=at(1, 14), end=at(1, 15), summary="mine now")],
        now=NOW,
    )
    assert found.kinds == (drift.MOVED, drift.RETITLED)


def test_a_block_that_has_already_ended_is_history():
    # Moving something that already happened is a note about the past, not a
    # decision to react to — and the task, if it's still open, just gets a
    # fresh slot like any other.
    past = ours(start=at(-1, 9), end=at(-1, 10))
    assert drift.detect([hand_moved(past, start=at(-1, 14))], now=NOW) == []


def test_an_unstamped_block_reads_as_unknown_rather_than_moved():
    # Every event written before stamping existed would otherwise report a
    # hand-move that never happened, on the first run after upgrading.
    assert drift.detect([unstamped(ours())], now=NOW) == []


def test_a_stamp_from_before_summaries_reports_no_rename():
    event = ours(summary="whatever it is called now")
    event.raw["extendedProperties"]["private"].pop("expectedSummary")
    assert drift.detect([event], now=NOW) == []


def test_a_sub_second_difference_is_not_a_move():
    # Google stores seconds; our own arithmetic carries microseconds.
    event = ours(start=at(1, 9) + timedelta(microseconds=500))
    event.raw["extendedProperties"]["private"]["expectedStart"] = at(
        1, 9
    ).isoformat()
    assert drift.detect([event], now=NOW) == []


def test_renaming_the_task_is_not_drift():
    # The stamp holds the title *we* wrote. A task renamed in Taskwarrior
    # changes what we're about to write, not what's on the event.
    assert drift.detect([ours(summary="the old title")], now=NOW) == []


# ---------------------------------------------------------------------------
# Whose position it is
# ---------------------------------------------------------------------------

def test_a_freshly_moved_block_is_pinned_before_anyone_records_it():
    moved = hand_moved(ours(), start=at(1, 14), end=at(1, 15))
    assert drift.is_pinned(moved)


def test_adoption_records_the_position_as_theirs():
    (found,) = drift.detect(
        [hand_moved(ours(), start=at(1, 14), end=at(1, 15))], now=NOW
    )
    adopted = found.adopted()
    assert adopted == Expectation(
        start=at(1, 14), end=at(1, 15), summary="a task", placed_by=BY_HUMAN
    )


def test_a_rename_does_not_change_whose_position_it_is():
    (found,) = drift.detect([hand_moved(ours(), summary="mine now")], now=NOW)
    assert found.adopted().placed_by == "scheduler"


def test_a_block_still_where_we_put_it_is_ours():
    assert not drift.is_pinned(ours())


# ---------------------------------------------------------------------------
# Through a scheduling run
# ---------------------------------------------------------------------------

def test_a_block_you_moved_stays_where_you_put_it(harness):
    # `settle_days=0` means nothing is sticky by age, so the only reason to
    # keep this block is that somebody put it there.
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60))
    harness.events(hand_moved(ours(), start=at(1, 14), end=at(1, 15)))
    harness.configure(settle_days=0).run()

    assert harness.gcal.event_for("u1").start == at(1, 14)


def test_a_moved_block_is_reported_once_and_not_again(harness):
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60))
    harness.events(hand_moved(ours(), start=at(1, 14), end=at(1, 15)))
    harness.configure(settle_days=0)

    first = harness.run()
    assert any(
        "moved from" in line
        for line in first.section("Changed outside task-gcal")
    )

    second = harness.run()
    assert second.section("Changed outside task-gcal") == []


def test_the_move_is_adopted_rather_than_undone(harness):
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60))
    harness.events(hand_moved(ours(), start=at(1, 14), end=at(1, 15)))
    harness.configure(settle_days=0).run()

    event = harness.gcal.event_for("u1")
    assert event.expectation.start == at(1, 14)
    assert event.placed_by == BY_HUMAN
    # Adopting means stamping, not moving: the block itself was left alone.
    (_, body) = harness.gcal.patched[0]
    assert "start" not in body


def test_a_block_you_moved_stays_put_on_later_runs_too(harness):
    # The pin is the whole point of recording who chose the position. Without
    # it a hand-move would win exactly once and spring back the next day.
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60))
    harness.events(hand_moved(ours(), start=at(1, 14), end=at(1, 15)))
    harness.configure(settle_days=0).run()
    harness.run()

    assert harness.gcal.event_for("u1").start == at(1, 14)


def test_a_moved_block_still_yields_when_its_position_stops_working(harness):
    # Deference isn't unconditional: a meeting on top of the block means it
    # can't stay, and the report says why.
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60))
    harness.events(hand_moved(ours(), start=at(1, 14), end=at(1, 15)))
    harness.busy((at(1, 14), at(1, 15)))
    res = harness.configure(settle_days=0).run()

    assert harness.gcal.event_for("u1").start != at(1, 14)
    assert "overlaps a calendar event" in res.section("Scheduled")[0]


def test_moving_a_block_ourselves_makes_it_ours_again(harness):
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60))
    harness.events(hand_moved(ours(), start=at(1, 14), end=at(1, 15)))
    harness.busy((at(1, 14), at(1, 15)))
    harness.configure(settle_days=0).run()

    assert harness.gcal.event_for("u1").placed_by == "scheduler"


def test_renaming_an_event_by_hand_is_reported_and_put_back(harness):
    # The event description says edits to the title may be overwritten, and
    # they are — but not silently.
    harness.tasks(task_row(uuid="u1", description="a task", due=FRI_5PM, estimate=60))
    harness.events(hand_moved(ours(), summary="mine now"))
    res = harness.run()

    assert any(
        "renamed" in line for line in res.section("Changed outside task-gcal")
    )
    assert harness.gcal.event_for("u1").summary == "a task"


def test_renaming_a_task_does_not_unpin_the_block_you_moved(harness):
    harness.tasks(task_row(uuid="u1", description="a task", due=FRI_5PM, estimate=60))
    harness.events(hand_moved(ours(), start=at(1, 14), end=at(1, 15)))
    harness.configure(settle_days=0).run()

    harness.tasks(
        task_row(uuid="u1", description="renamed", due=FRI_5PM, estimate=60)
    )
    harness.run()

    event = harness.gcal.event_for("u1")
    assert event.summary == "renamed"
    assert event.start == at(1, 14)
    assert event.placed_by == BY_HUMAN


def test_a_dry_run_notices_drift_but_adopts_nothing(harness):
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60))
    harness.events(hand_moved(ours(), start=at(1, 14), end=at(1, 15)))
    res = harness.configure(settle_days=0).run(dry_run=True)

    assert res.section("Changed outside task-gcal")
    assert harness.gcal.patched == []


# ---------------------------------------------------------------------------
# What the journal keeps
# ---------------------------------------------------------------------------

def test_the_journal_records_the_move_against_the_block(harness):
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60))
    harness.events(hand_moved(ours(), start=at(1, 14), end=at(1, 15)))
    harness.configure(settle_days=0).run()

    (record,) = harness.journal()
    (placement,) = record.placements
    assert placement.event_id == "ev1"
    assert placement.drift == (drift.MOVED,)
    assert placement.drifted_from == at(1, 9)
    assert placement.start == at(1, 14)


def test_the_journal_records_the_move_only_once(harness):
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60))
    harness.events(hand_moved(ours(), start=at(1, 14), end=at(1, 15)))
    harness.configure(settle_days=0).run()
    harness.run()

    first, second = harness.journal()
    assert first.placements[0].drift == (drift.MOVED,)
    assert second.placements[0].drift == ()


def test_a_moved_block_whose_task_left_the_report_is_still_recorded(harness):
    # The task is done, so the block is about to be deleted — but somebody
    # moved it before that, and that's a fact about how the week actually went.
    moved = hand_moved(ours(), start=at(1, 14), end=at(1, 15))
    harness.tasks(task_row(uuid="u2", due=WED_5PM, estimate=60)).events(moved)
    harness.run()

    (record,) = harness.journal()
    drifted = [p for p in record.placements if p.drift]
    assert [p.event_id for p in drifted] == ["ev1"]


def test_a_block_about_to_be_deleted_is_not_restamped(harness):
    # Adopting a position on an event we're removing in the same run would be
    # a wasted call against the API.
    moved = hand_moved(ours(), start=at(1, 14), end=at(1, 15))
    harness.tasks(task_row(uuid="u2", due=WED_5PM, estimate=60)).events(moved)
    harness.run()

    assert "ev1" in harness.gcal.deleted
    assert [event_id for event_id, _body in harness.gcal.patched] == []
