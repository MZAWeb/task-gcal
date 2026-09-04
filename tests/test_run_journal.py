"""The observation a scheduling run leaves behind.

The scheduling path's contract with the journal is one-directional and easy to
break by accident, so these tests assert both halves of it: a run records what
it did, and nothing about a run depends on what was recorded.
"""

from __future__ import annotations


from task_gcal import journal

from conftest import FRI_5PM, NOW, WED_5PM, at, managed_event, task_row


# ---------------------------------------------------------------------------
# What a scheduling run records
# ---------------------------------------------------------------------------

def test_a_scheduling_run_records_the_placement_it_made(harness):
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    harness.run()

    (record,) = harness.journal()
    assert record.mode == journal.MODE_SCHEDULE
    (placement,) = record.placements
    assert placement.start == at(0, 9)
    assert placement.action == "create"


def test_a_placement_is_recorded_against_the_event_it_landed_in(harness):
    # Identity is the event, not the task: a block deleted and recreated
    # elsewhere has to read as a new block rather than as the old one moving.
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=60))
    harness.run()

    (placement,) = harness.journal()[0].placements
    assert placement.event_id == harness.gcal.created[0]["id"]
    assert placement.task_uuid == "u1"


def test_a_scheduling_run_records_why_a_block_moved(harness):
    settled = managed_event(id="ev1", task_uuid="u1", start=at(1, 14), end=at(1, 15))
    harness.tasks(task_row(uuid="u1", due=FRI_5PM, estimate=60)).events(settled)
    harness.busy((at(1, 14), at(1, 15)))
    harness.run()

    (record,) = harness.journal()
    assert record.placements[0].moved_reason == "overlaps a calendar event"


def test_a_task_with_no_block_is_not_in_the_placement_log(harness):
    # The log describes blocks, and a task with no estimate never got one.
    # Why it wasn't scheduled is a question about the task's fields, which
    # come from Taskwarrior's own history rather than from here.
    harness.tasks(task_row(uuid="u1", due=WED_5PM, estimate=None))
    harness.run()

    (record,) = harness.journal()
    assert record.placements == ()


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
    harness.run()
    first = harness.gcal.created[0]
    harness.gcal.managed.clear()
    harness.gcal.created.clear()
    before = len(harness.journal())

    harness.run()
    (again,) = harness.gcal.created

    assert (again["start"], again["end"]) == (first["start"], first["end"])
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
