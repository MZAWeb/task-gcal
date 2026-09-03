"""Tests for the one-time history importer.

Backfill reconstructs the past, which means it can invent it. So most of
these check the *limits* rather than the reconstruction: no blocks, no
guessed urgency, no overwriting a day the tool actually observed, and a
field the change log never mentions treated as stable rather than as having
appeared out of nowhere.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from task_gcal import backfill as backfill_mod
from task_gcal import journal
from task_gcal.config import Settings
from task_gcal.history import TaskHistory
from task_gcal.taskw import TaskInfo

from conftest import NOW, at

SETTINGS = Settings(timezone="UTC")
TZ = SETTINGS.resolve_timezone()

# A window ending at NOW (Monday 09:00 UTC) and starting the Thursday before.
SINCE = NOW - timedelta(days=4)


def a_task(**kwargs) -> TaskInfo:
    base = dict(
        uuid="u1",
        id=7,
        description="write the thing",
        urgency=12.5,
        due=at(2, 17),
        scheduled=None,
        wait=None,
        estimate_minutes=60,
        project="work",
        tags=["deep"],
        annotations=[],
        overrides_raw=None,
        status="pending",
        entry=NOW - timedelta(days=10),
        end=None,
    )
    base.update(kwargs)
    return TaskInfo(**base)


@pytest.fixture
def importer(monkeypatch, capsys, isolated_journal):
    """Backfill with Taskwarrior faked: a task list and a history per uuid."""

    class Importer:
        def __init__(self) -> None:
            self.tasks: list[TaskInfo] = []
            self.histories: dict[str, TaskHistory] = {}
            self.info_calls: list[str] = []
            self.settings = SETTINGS

        def configure(self, **kwargs):
            from dataclasses import replace

            self.settings = replace(self.settings, **kwargs)
            return self

        def with_tasks(self, *tasks: TaskInfo):
            self.tasks = list(tasks)
            return self

        def with_history(self, uuid: str, *changes):
            self.histories[uuid] = TaskHistory(uuid=uuid, changes=list(changes))
            return self

        def run(self, *, since=SINCE, now=NOW):
            code, summary = backfill_mod.backfill(
                self.settings, since=since, now=now, show_progress=False
            )
            self.out = capsys.readouterr().out
            self.code = code
            return summary

        def records(self):
            return journal.load().records

    imp = Importer()

    def fake_load_all(**_kwargs):
        return imp.tasks

    def fake_load_history(uuid, **_kwargs):
        imp.info_calls.append(uuid)
        return imp.histories.get(uuid) or TaskHistory(uuid=uuid)

    monkeypatch.setattr(backfill_mod, "load_all_tasks", fake_load_all)
    monkeypatch.setattr(backfill_mod, "load_history", fake_load_history)
    return imp


def change(field, when, *, kind="changed", old=None, new=None):
    from task_gcal.history import FieldChange

    return FieldChange(at=when, field=field, kind=kind, old=old, new=new)


# ---------------------------------------------------------------------------
# What gets written
# ---------------------------------------------------------------------------

def test_one_record_per_day_in_the_window(importer):
    summary = importer.with_tasks(a_task()).run()

    # Thursday through Monday: `since` lands mid-Thursday, and a partial day
    # still gets an observation of the state it ended in.
    assert summary.days_written == 5
    assert [r.mode for r in importer.records()] == [journal.MODE_BACKFILL] * 5


def test_records_land_at_the_end_of_each_local_day(importer):
    # End of day, not start: it captures every change made during that day,
    # which is what a snapshot run late in the day would have seen.
    importer.with_tasks(a_task()).run()

    moments = [r.at for r in importer.records()]
    # Each record's own local date is the day it describes, which only holds
    # if the timestamp is the last instant of that day rather than the
    # midnight after it.
    assert [m.astimezone(TZ).date() for m in moments] == [
        (SINCE + timedelta(days=d)).date() for d in range(5)
    ]
    assert moments[0].astimezone(TZ).strftime("%H:%M:%S") == "23:59:59"
    assert moments[-1] == NOW  # today is clamped to now, not to midnight


def test_the_final_record_never_runs_past_now(importer):
    importer.with_tasks(a_task()).run()
    assert all(r.at <= NOW for r in importer.records())


def test_a_backfilled_record_carries_no_blocks(importer):
    # Past calendar events show only their final times, so a reconstructed
    # placement would be fiction. Placement churn must read as unobserved.
    importer.with_tasks(a_task()).run()

    for record in importer.records():
        assert all(t.block is None for t in record.tasks)


def test_urgency_is_left_unset_rather_than_guessed(importer):
    # Urgency is a function of the clock; a reconstructed value would be a
    # number nobody ever computed.
    importer.with_tasks(a_task()).run()
    assert all(t.urgency is None for t in importer.records()[0].tasks)


def test_the_report_name_is_not_claimed(importer):
    # The reconstructed population is "tasks open at that moment", not what
    # today's report would have returned.
    importer.with_tasks(a_task()).run()
    assert importer.records()[0].report == ""


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------

def test_a_due_date_push_is_reconstructed_on_the_right_side_of_the_day(importer):
    # Pushed on Saturday: Thursday and Friday should still show the old date.
    importer.with_tasks(a_task(due=at(2, 17))).with_history(
        "u1",
        change("due", at(-2, 12), old="2026-09-04 17:00:00",
               new="2026-09-09 17:00:00"),
    )
    importer.run()

    dues = [r.tasks[0].due for r in importer.records()]
    assert dues[0] == at(-3, 17)  # before the push: the old date
    assert dues[-1] == at(2, 17)  # after: the new one


def test_a_field_the_log_never_mentions_is_treated_as_stable(importer):
    # Not "it appeared later". The log is what we know, and inventing a
    # change would be worse than assuming stability.
    importer.with_tasks(a_task(estimate_minutes=90))
    importer.run()

    assert {t.estimate_minutes for r in importer.records() for t in r.tasks} == {90}


def test_an_estimate_revision_is_reconstructed(importer):
    importer.with_tasks(a_task(estimate_minutes=180)).with_history(
        "u1", change("estimate", at(-2, 12), old="60", new="180")
    )
    importer.run()

    estimates = [r.tasks[0].estimate_minutes for r in importer.records()]
    assert estimates[0] == 60
    assert estimates[-1] == 180


def test_a_deleted_field_reconstructs_as_absent(importer):
    importer.with_tasks(a_task(scheduled=None)).with_history(
        "u1",
        change("scheduled", at(-3, 20), kind="set", new="2026-09-08 09:00:00"),
        change("scheduled", at(-1, 10), kind="deleted"),
    )
    importer.run()

    scheduled = [r.tasks[0].scheduled for r in importer.records()]
    assert scheduled[0] is None   # Thursday: the log says it wasn't set yet
    assert scheduled[1] == at(1, 9)  # Friday, after it was set
    assert scheduled[-1] is None  # Monday, after it was deleted


def test_an_unparseable_estimate_becomes_missing_not_zero(importer):
    importer.with_tasks(a_task()).with_history(
        "u1", change("estimate", at(-3, 12), kind="set", new="lots")
    )
    importer.run()
    assert importer.records()[-1].tasks[0].estimate_minutes is None


def test_a_task_created_mid_window_is_absent_before_it_existed(importer):
    importer.with_tasks(a_task(entry=at(-1, 10)))
    importer.run()

    counts = [len(r.tasks) for r in importer.records()]
    assert counts == [0, 0, 0, 1, 1]  # created on Sunday


def test_a_task_closed_mid_window_is_absent_afterwards(importer):
    importer.with_tasks(a_task(status="completed", end=at(-2, 10)))
    importer.run()

    counts = [len(r.tasks) for r in importer.records()]
    assert counts == [1, 1, 0, 0, 0]  # closed on Saturday


def test_a_task_closed_before_the_window_costs_no_subprocess(importer):
    # One `task info` per task is the reason this is a one-time import.
    importer.with_tasks(
        a_task(uuid="old", status="completed", end=SINCE - timedelta(days=5)),
        a_task(uuid="recent"),
    )
    summary = importer.run()

    assert importer.info_calls == ["recent"]
    assert summary.tasks_considered == 1


# ---------------------------------------------------------------------------
# Safety and re-runs
# ---------------------------------------------------------------------------

def test_a_day_already_observed_is_never_overwritten(importer):
    # A live snapshot is better data than a reconstruction, so backfill only
    # fills gaps.
    journal.append(
        journal.build_record(
            settings=SETTINGS, mode=journal.MODE_SNAPSHOT, at=at(-2, 15),
            observations=(),
        )
    )
    summary = importer.with_tasks(a_task()).run()

    assert summary.days_skipped == 1
    assert summary.days_written == 4
    modes = [r.mode for r in importer.records()]
    assert modes.count(journal.MODE_SNAPSHOT) == 1


def test_re_running_backfill_writes_nothing_new(importer):
    first = importer.with_tasks(a_task()).run()
    second = importer.run()

    assert first.days_written == 5
    assert second.days_written == 0
    assert second.days_skipped == 5
    assert len(importer.records()) == 5


def test_backfill_is_read_only_upstream(importer):
    # No calendar client is even constructed: the only writes are journal
    # lines, and nothing in Taskwarrior is touched.
    importer.with_tasks(a_task()).run()
    assert not hasattr(backfill_mod, "GCal")


def test_an_unreadable_history_skips_the_task_and_says_so(
    importer, monkeypatch
):
    from task_gcal.history import HistoryUnavailable

    def explode(uuid, **_kwargs):
        raise HistoryUnavailable("nope")

    monkeypatch.setattr(backfill_mod, "load_history", explode)
    summary = importer.with_tasks(a_task()).run()

    assert summary.histories_failed == 1
    assert "skipping" in importer.out
    # The days are still written, just without that task.
    assert summary.days_written == 5
    assert all(r.tasks == () for r in importer.records())


def test_the_journal_being_off_stops_it(importer):
    summary = importer.configure(journal_detail="off").with_tasks(a_task()).run()

    assert summary.days_written == 0
    assert "Journal is off" in importer.out
    assert importer.records() == []


def test_minimal_detail_is_honored(importer):
    importer.configure(journal_detail="minimal").with_tasks(a_task()).run()

    task = importer.records()[0].tasks[0]
    assert task.description is None
    assert task.description_hash


def test_a_since_in_the_future_does_nothing(importer):
    summary = importer.with_tasks(a_task()).run(since=NOW + timedelta(days=1))

    assert summary.days_written == 0
    assert "not in the past" in importer.out


def test_unrecognized_modification_lines_are_reported(importer):
    from task_gcal.history import TaskHistory

    importer.with_tasks(a_task())
    importer.histories["u1"] = TaskHistory(uuid="u1", changes=[], unrecognized=5)
    summary = importer.run()

    assert summary.unrecognized_lines == 5
    assert "5 modification line(s) not recognized" in importer.out


def test_the_summary_warns_that_blocks_are_missing(importer):
    importer.with_tasks(a_task()).run()
    assert "carry no calendar blocks" in importer.out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_observation_times_are_one_per_local_day():
    times = backfill_mod._observation_times(SINCE, NOW, TZ)
    assert len(times) == 5
    assert all(a < b for a, b in zip(times, times[1:]))


def test_observation_times_respect_the_timezone():
    from zoneinfo import ZoneInfo

    tokyo = ZoneInfo("Asia/Tokyo")
    times = backfill_mod._observation_times(SINCE, NOW, tokyo)
    # Each is the last instant of a Tokyo day (bar the last, clamped to now).
    for moment in times[:-1]:
        local = moment.astimezone(tokyo)
        assert (local.hour, local.minute, local.second) == (23, 59, 59)


def test_relevant_tasks_keeps_everything_still_open():
    tasks = [
        a_task(uuid="open", end=None),
        a_task(uuid="closed-inside", end=SINCE + timedelta(days=1)),
        a_task(uuid="closed-before", end=SINCE - timedelta(days=1)),
    ]
    kept = {t.uuid for t in backfill_mod.relevant_tasks(tasks, SINCE)}
    assert kept == {"open", "closed-inside"}
