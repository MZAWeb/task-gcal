"""Plan churn: reconstructing block moves from the run journal, and reporting them.

The reconstruction is where this can go quietly wrong — merge two blocks and
you invent a move; miss the drift half and every hand-move is attributed to the
scheduler. So most of what's checked here is attribution rather than counting.
"""

from __future__ import annotations

from task_gcal import drift, journal
from task_gcal.config import Settings
from task_gcal.review import placements
from task_gcal.review.metrics import churn

from conftest import a_task, at

SETTINGS = Settings(timezone="UTC")


def a_placement(**kwargs) -> journal.PlacementObservation:
    base = dict(
        task_uuid="a",
        event_id="ev1",
        start=at(1, 9),
        end=at(1, 10),
        action="unchanged",
    )
    base.update(kwargs)
    return journal.PlacementObservation(**base)


def a_run(when, *placements_) -> journal.RunRecord:
    return journal.build_record(
        settings=SETTINGS,
        mode=journal.MODE_SCHEDULE,
        at=when,
        placements=tuple(placements_),
    )


def blocks_from(*records):
    return placements.build_blocks(records)


def moves_from(*records):
    return [m for b in blocks_from(*records).values() for m in b.moves]


# ---------------------------------------------------------------------------
# Reconstructing what happened to a block
# ---------------------------------------------------------------------------

def test_a_block_seen_once_has_not_moved():
    assert moves_from(a_run(at(0, 12), a_placement())) == []


def test_a_block_in_the_same_place_twice_has_not_moved():
    assert moves_from(
        a_run(at(0, 12), a_placement()), a_run(at(0, 13), a_placement())
    ) == []


def test_the_scheduler_moving_a_block_is_one_move():
    (move,) = moves_from(
        a_run(at(0, 12), a_placement()),
        a_run(
            at(0, 13),
            a_placement(
                start=at(2, 9),
                end=at(2, 10),
                action="update",
                moved_reason="overlaps a calendar event",
            ),
        ),
    )
    assert move.by == placements.BY_SCHEDULER
    assert (move.frm, move.to) == (at(1, 9), at(2, 9))
    assert move.cause == "overlaps a calendar event"
    assert move.at == at(0, 13)


def test_a_move_with_no_recorded_reason_is_replanning():
    # An unsettled block being placed somewhere better is the scheduler doing
    # its job, and must not be reported as a disruption.
    (move,) = moves_from(
        a_run(at(0, 12), a_placement()),
        a_run(at(0, 13), a_placement(start=at(2, 9), end=at(2, 10), action="update")),
    )
    assert move.cause == placements.REPLANNED


def test_a_block_you_moved_is_attributed_to_you():
    (move,) = moves_from(
        a_run(at(0, 12), a_placement()),
        a_run(
            at(0, 13),
            a_placement(
                start=at(1, 14),
                end=at(1, 15),
                drift=(drift.MOVED,),
                drifted_from=at(1, 9),
            ),
        ),
    )
    assert move.by_human
    assert (move.frm, move.to) == (at(1, 9), at(1, 14))
    assert move.reason is None
    assert move.cause == "you moved it"


def test_a_hand_move_the_scheduler_then_overrode_is_two_moves():
    # Both happened, and collapsing them would hide either the disruption or
    # the fact that the plan didn't survive it.
    mine, theirs = moves_from(
        a_run(at(0, 12), a_placement()),
        a_run(
            at(0, 13),
            a_placement(
                start=at(3, 9),
                end=at(3, 10),
                action="update",
                moved_reason="overlaps a calendar event",
                drift=(drift.MOVED,),
                drifted_from=at(1, 9),
                drifted_to=at(1, 14),
            ),
        ),
    )
    assert mine.by_human and (mine.frm, mine.to) == (at(1, 9), at(1, 14))
    assert theirs.by == placements.BY_SCHEDULER
    assert (theirs.frm, theirs.to) == (at(1, 14), at(3, 9))


def test_a_rename_alone_is_not_a_move():
    assert moves_from(
        a_run(at(0, 12), a_placement()),
        a_run(at(0, 13), a_placement(drift=(drift.RETITLED,))),
    ) == []


def test_two_blocks_for_one_task_are_not_one_block_that_moved():
    # A block deleted and recreated elsewhere is a new block. Keying on the
    # task would report a move that never happened.
    blocks = blocks_from(
        a_run(at(0, 12), a_placement(event_id="ev1")),
        a_run(
            at(0, 13),
            a_placement(event_id="ev2", start=at(2, 9), end=at(2, 10), action="create"),
        ),
    )
    assert set(blocks) == {"ev1", "ev2"}
    assert all(not b.moves for b in blocks.values())


def test_moves_are_dated_by_the_run_that_saw_them():
    block = blocks_from(
        a_run(at(-2, 12), a_placement()),
        a_run(at(1, 12), a_placement(start=at(2, 9), end=at(2, 10))),
    )["ev1"]
    assert block.moves_in((at(0, 0), at(3, 0))) == block.moves
    assert block.moves_in((at(-3, 0), at(0, 0))) == []


# ---------------------------------------------------------------------------
# The section
# ---------------------------------------------------------------------------

def section(review):
    got = review.review().section(churn.KEY)
    assert got is not None
    return got


def test_churn_is_unmeasured_without_any_runs(review):
    got = section(review)
    assert got.measured is False
    assert "no scheduling runs" in got.summary


def test_runs_from_before_the_placement_log_are_unknown_not_calm(review):
    # The old records logged task state, not block positions. Reading them as
    # "no blocks, so nothing moved" would turn missing data into a quiet week.
    old = journal.RunRecord(
        run_id="old",
        at=at(0, 12),
        mode=journal.MODE_SCHEDULE,
        timezone_name="UTC",
        settings_hash="aaa",
        calendar_id="primary",
        report="next",
        unknown={"tasks": [{"uuid": "a"}]},
    )
    review.records(old)
    got = section(review)

    assert got.measured is False
    assert "no placements" in got.summary


def test_a_quiet_period_says_nothing_moved_rather_than_nothing(review):
    review.records(a_run(at(0, 12), a_placement()))
    got = section(review)
    assert got.measured
    assert "nothing moved" in got.summary
    assert got.data["moves"] == 0


def test_the_causes_are_broken_down(review):
    review.tasks(a_task(uuid="a", description="Prepare PIR"))
    review.records(
        a_run(at(0, 12), a_placement()),
        a_run(
            at(0, 13),
            a_placement(
                start=at(2, 9), end=at(2, 10), moved_reason="estimate changed"
            ),
        ),
        a_run(
            at(0, 14),
            a_placement(
                start=at(2, 14),
                end=at(2, 15),
                drift=(drift.MOVED,),
                drifted_from=at(2, 9),
            ),
        ),
    )
    got = section(review)

    assert got.data["moves"] == 2
    assert got.data["causes"] == {"estimate changed": 1, "you moved it": 1}
    assert got.data["by_human"] == 1
    assert "counted once per run" in "\n".join(got.detail)


def test_the_most_moved_block_is_named_from_taskwarrior(review):
    review.tasks(a_task(uuid="a", description="Prepare PIR"))
    review.records(
        a_run(at(0, 12), a_placement()),
        a_run(at(0, 13), a_placement(start=at(2, 9), end=at(2, 10))),
        a_run(at(0, 14), a_placement(start=at(3, 9), end=at(3, 10))),
    )
    detail = "\n".join(section(review).detail)
    assert "2x" in detail
    assert "Prepare PIR" in detail


def test_a_block_that_keeps_moving_is_worth_saying_out_loud(review):
    review.tasks(a_task(uuid="a", description="Prepare PIR"))
    review.records(
        a_run(at(0, 9), a_placement()),
        a_run(at(0, 10), a_placement(start=at(2, 9), end=at(2, 10))),
        a_run(at(0, 11), a_placement(start=at(3, 9), end=at(3, 10))),
        a_run(at(0, 12), a_placement(start=at(4, 9), end=at(4, 10))),
    )
    (suggestion,) = section(review).suggestions
    assert "Prepare PIR" in suggestion.text
    assert "3 times" in suggestion.text


def test_one_move_is_not_worth_a_suggestion(review):
    review.tasks(a_task(uuid="a"))
    review.records(
        a_run(at(0, 12), a_placement()),
        a_run(at(0, 13), a_placement(start=at(2, 9), end=at(2, 10))),
    )
    assert section(review).suggestions == ()


def test_the_coverage_line_is_days_a_run_observed(review):
    review.records(a_run(at(0, 12), a_placement()))
    (coverage,) = section(review).coverage
    assert coverage.observed == 1
    assert coverage.total > 1
