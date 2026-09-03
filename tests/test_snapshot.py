"""`snapshot`, and the observation `schedule` leaves behind.

The scheduling path's contract with the journal is one-directional and
easy to break by accident, so these tests assert both halves of it: a run
records what it did, and nothing about a run depends on what was recorded.
"""

from __future__ import annotations

from datetime import timedelta

from task_gcal import journal

from conftest import FRI_5PM, NOW, WED_5PM, at, managed_event, task_row


# ---------------------------------------------------------------------------
# snapshot changes nothing
# ---------------------------------------------------------------------------

def test_snapshot_makes_no_calendar_writes(harness):
    orphan = managed_event(id="ev-gone", task_uuid="vanished", start=at(1, 9), end=at(1, 10))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60)).events(orphan)
    harness.snapshot()

    assert harness.gcal.mutations == 0


def test_snapshot_records_what_it_saw(harness):
    block = managed_event(id="ev1", task_uuid="u1", start=at(1, 14), end=at(1, 15))
    harness.tasks(
        task_row(uuid="u1", description="a thing", due=WED_5PM, estimate=60)
    ).events(block)
    harness.snapshot()

    (record,) = harness.journal()
    assert record.mode == journal.MODE_SNAPSHOT
    (task,) = record.tasks
    assert task.uuid == "u1"
    assert task.description == "a thing"
    assert task.block.start == at(1, 14)


def test_snapshot_does_not_claim_an_action(harness):
    # It observed the block; it didn't decide it. "unchanged" would be a
    # claim it hasn't earned, and placement churn would read it as a run
    # that considered the block and kept it.
    block = managed_event(id="ev1", task_uuid="u1", start=at(1, 14), end=at(1, 15))
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60)).events(block)
    harness.snapshot()

    (record,) = harness.journal()
    assert record.tasks[0].block.action is None


def test_snapshot_reports_a_task_with_no_block(harness):
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    res = harness.snapshot()

    (record,) = harness.journal()
    assert record.tasks[0].block is None
    assert "1 task(s), 0 with a block" in res.out


def test_snapshot_flags_an_empty_task_source(harness):
    # The same ambiguity the removal guard exists for. Nothing is deleted
    # here, but a review must not read this as a day you had no work.
    ours = managed_event(id="ev1", task_uuid="u1", start=at(1, 14), end=at(1, 15))
    harness.tasks().events(ours)
    res = harness.snapshot()

    (record,) = harness.journal()
    assert record.source_ok is False
    assert "task source came back empty" in res.out


def test_a_genuinely_empty_setup_is_healthy(harness):
    # No tasks *and* no events we own is a fresh install, not a broken one.
    harness.tasks()
    harness.snapshot()

    (record,) = harness.journal()
    assert record.source_ok is True


def test_snapshot_records_only_the_keeper_block(harness):
    keeper = managed_event(id="ev1", task_uuid="u1", start=at(1, 9), end=at(1, 10))
    dupe = managed_event(id="ev2", task_uuid="u1", start=at(2, 9), end=at(2, 10))
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60)).events(keeper, dupe)
    harness.snapshot()

    (record,) = harness.journal()
    assert record.tasks[0].block.start == at(1, 9)


def test_snapshot_says_so_when_the_journal_is_off(harness):
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    res = harness.configure(journal_detail="off").snapshot()

    assert harness.journal() == []
    assert "Journal is off" in res.out


def test_repeated_snapshots_accumulate(harness):
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    harness.snapshot()
    harness.snapshot()

    assert len(harness.journal()) == 2


# ---------------------------------------------------------------------------
# What a scheduling run records
# ---------------------------------------------------------------------------

def test_a_scheduling_run_records_the_placement_it_made(harness):
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    harness.run()

    (record,) = harness.journal()
    assert record.mode == journal.MODE_SCHEDULE
    (task,) = record.tasks
    assert task.block.start == at(0, 9)
    assert task.block.action == "create"


def test_a_scheduling_run_records_why_a_block_moved(harness):
    settled = managed_event(id="ev1", task_uuid="u1", start=at(1, 14), end=at(1, 15))
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60)).events(settled)
    harness.busy((at(1, 14), at(1, 15)))
    harness.run()

    (record,) = harness.journal()
    assert record.tasks[0].block.moved_reason == "overlaps a calendar event"


def test_a_run_records_tasks_it_could_not_place(harness):
    # A task with no estimate has no block, but its state is still the
    # observation that explains why nothing was scheduled for it.
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=None))
    harness.run()

    (record,) = harness.journal()
    (task,) = record.tasks
    assert task.block is None
    assert task.estimate_minutes is None


def test_a_dry_run_records_nothing(harness):
    # Its placements were never made. Recording them would put moves that
    # never happened into placement churn.
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    harness.run(dry_run=True)

    assert harness.journal() == []


def test_the_journal_never_changes_what_a_run_does(harness):
    # The invariant that makes a corrupt journal harmless: scheduling is
    # stateless where it matters. A run with a full journal and a run with
    # none must place the task identically.
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    for _ in range(3):
        harness.snapshot()
    before = len(harness.journal())
    harness.run()

    (created,) = harness.gcal.created
    assert (created["start"], created["end"]) == (at(0, 9), at(0, 10))
    assert len(harness.journal()) == before + 1


def test_a_run_still_succeeds_when_the_journal_cannot_be_written(
    harness, isolated_journal
):
    (isolated_journal).mkdir(parents=True, exist_ok=True)
    (isolated_journal / "runs").write_text("not a directory")

    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    res = harness.run()

    assert res.code == 0
    assert len(harness.gcal.created) == 1
    assert "journal not written" in res.err


def test_the_record_carries_the_settings_it_was_written_under(harness):
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    harness.configure(work_end_hour=20).run()

    (record,) = harness.journal()
    assert record.settings_hash == journal.settings_hash(harness.settings)
    assert record.calendar_id == "primary"
    assert record.report == "next"
    assert record.tool_version


def test_a_settings_change_shows_up_as_a_boundary(harness):
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    harness.run()
    harness.configure(work_end_hour=21).run()

    boundaries = journal.definition_boundaries(harness.journal())
    assert boundaries == [NOW]


def test_successive_records_show_a_due_date_moving(harness):
    # The thing the journal exists for: this is not reconstructable from
    # Taskwarrior or the calendar after the fact.
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    harness.snapshot()
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60))
    harness.snapshot()

    observed = [r.tasks[0].due for r in harness.journal()]
    assert observed == [WED_5PM, FRI_5PM]
    assert observed[1] - observed[0] == timedelta(days=2)
