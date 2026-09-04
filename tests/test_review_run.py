"""The review command end to end: collection, filtering, and where it writes.

The invariant worth the most here is negative — a review reads Taskwarrior,
the calendar and the journal, and writes to none of them.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from task_gcal import journal
from task_gcal.config import Settings
from task_gcal.review import ReviewRequest, facts as facts_mod, run

from conftest import NOW, a_block, a_task, at

SETTINGS = Settings(timezone="UTC")
FRIDAY = NOW + timedelta(days=4, hours=6)


class FakeCalendar:
    """Only the two read methods a review is allowed to call."""

    def __init__(self, blocks=(), meetings=()) -> None:
        self.blocks = list(blocks)
        self.meetings = list(meetings)
        self.calls: list[str] = []

    def list_scheduler_events(self, time_min, time_max):
        self.calls.append("list_scheduler_events")
        return [b for b in self.blocks if b.end > time_min and b.start < time_max]

    def list_busy_events(self, time_min, time_max, exclude_event_ids):
        self.calls.append("list_busy_events")
        return sorted(
            (s, e) for s, e in self.meetings if e > time_min and s < time_max
        )


@pytest.fixture
def runner(monkeypatch, capsys, isolated_journal):
    class Runner:
        def __init__(self) -> None:
            self.tasks = []
            self.calendar = FakeCalendar()
            self.settings = SETTINGS
            self.export_calls: list[dict] = []

        def with_tasks(self, *tasks):
            self.tasks = list(tasks)
            return self

        def with_calendar(self, *, blocks=(), meetings=()):
            self.calendar = FakeCalendar(blocks, meetings)
            return self

        def run(self, request=None, *, now=FRIDAY):
            code = run(
                self.settings,
                request or ReviewRequest(),
                now=now,
                gcal=self.calendar,
            )
            captured = capsys.readouterr()
            self.out, self.err, self.code = captured.out, captured.err, code
            return code

    r = Runner()

    def fake_export(**kwargs):
        r.export_calls.append(kwargs)
        return r.tasks

    monkeypatch.setattr(facts_mod, "load_all_tasks", fake_export)
    return r


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------

def test_a_review_only_reads_the_calendar(runner):
    runner.with_calendar(blocks=[a_block("a", at(0, 9), 60)]).run()
    assert set(runner.calendar.calls) == {
        "list_scheduler_events",
        "list_busy_events",
    }


def test_a_review_writes_nothing_to_the_journal(runner, isolated_journal):
    runner.with_tasks(a_task(uuid="a")).run()
    assert journal.load().records == []


def test_a_review_exports_every_task_not_just_the_report(runner):
    # Throughput, lead time and backlog flow are all about tasks that have
    # left the list, so the configured report would hide them.
    runner.with_tasks(a_task(uuid="a")).run()
    assert runner.export_calls  # `load_all_tasks`, which ignores the report
    assert "report" not in runner.export_calls[0]


def test_a_review_never_touches_the_scheduler(runner):
    import task_gcal.review as review_mod

    for name in ("reconcile", "plan_placements", "GCal"):
        assert not hasattr(review_mod, name)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def test_the_default_output_goes_to_stdout(runner):
    runner.run()
    assert "Week" in runner.out


def test_an_output_path_is_written_and_named(runner, tmp_path):
    target = tmp_path / "week.md"
    runner.run(ReviewRequest(fmt="markdown", output=target))

    assert target.read_text().startswith("# Week")
    assert str(target) in runner.out


def test_open_writes_a_private_temp_file(runner, monkeypatch):
    opened: list[Path] = []
    monkeypatch.setattr(
        "task_gcal.review.webbrowser.open", lambda uri: opened.append(uri)
    )
    runner.run(ReviewRequest(fmt="html", open_in_browser=True))

    assert opened and opened[0].startswith("file://")
    path = Path(opened[0].removeprefix("file://"))
    # Task titles are in there, so /tmp isn't an excuse.
    assert oct(path.stat().st_mode)[-3:] == "600"
    path.unlink()


def test_open_with_an_output_path_opens_that_file(runner, monkeypatch, tmp_path):
    opened: list[str] = []
    monkeypatch.setattr(
        "task_gcal.review.webbrowser.open", lambda uri: opened.append(uri)
    )
    target = tmp_path / "report.html"
    runner.run(ReviewRequest(fmt="html", output=target, open_in_browser=True))

    assert target.exists()
    assert opened == [target.resolve().as_uri()]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_a_section_filter_shows_that_section_in_full(runner):
    runner.run(ReviewRequest(sections=("time",)))
    assert "Working hours" in runner.out
    assert "Lead time" not in runner.out


def test_an_unmatched_section_is_an_error_that_lists_the_real_ones(runner):
    code = runner.run(ReviewRequest(sections=("vibes",)))
    assert code == 2
    assert "time" in runner.err


def test_last_reports_the_previous_period(runner):
    runner.with_tasks(
        a_task(uuid="then", status="completed", end=at(-6, 11)),
        a_task(uuid="now", status="completed", end=at(0, 11)),
    )
    runner.run(ReviewRequest(offset=1, fmt="json"))
    payload = json.loads(runner.out)
    through = next(s for s in payload["sections"] if s["key"] == "finished")

    assert through["data"]["completed"] == 1
    assert payload["period"]["in_progress"] is False


def test_a_month_review_covers_the_month_that_finished(runner):
    runner.run(ReviewRequest(kind="month", fmt="json"))
    payload = json.loads(runner.out)
    assert payload["period"]["kind"] == "month"
    assert payload["period"]["start"].endswith("-08-01T00:00:00Z")
    assert payload["period"]["in_progress"] is False


def test_a_named_month_beats_the_default(runner):
    from datetime import date

    runner.run(ReviewRequest(kind="month", anchor=date(2026, 9, 1), fmt="json"))
    payload = json.loads(runner.out)
    assert payload["period"]["start"].endswith("-09-01T00:00:00Z")


# ---------------------------------------------------------------------------
# Degraded inputs
# ---------------------------------------------------------------------------

def test_a_calendar_failure_degrades_rather_than_crashing(runner, monkeypatch):
    class Broken(FakeCalendar):
        def list_scheduler_events(self, time_min, time_max):
            raise RuntimeError("nope")

    runner.calendar = Broken()
    code = runner.run(ReviewRequest(fmt="json"))
    payload = json.loads(runner.out)
    capacity = next(s for s in payload["sections"] if s["key"] == "time")

    assert code == 0
    assert capacity["measured"] is False
    assert any("could not be read" in c for c in payload["caveats"])


def test_missing_credentials_degrade_rather_than_aborting(runner, monkeypatch):
    # The most common calendar failure of all: a machine that has never run
    # `--setup`. Building the client has to be inside the guarded block, and
    # `SystemExit` named explicitly, or the documented caveat is unreachable.
    from task_gcal.review import facts as facts_mod

    def no_credentials(_settings, **_kwargs):
        raise SystemExit("Missing OAuth client secrets at ...")

    monkeypatch.setattr(facts_mod, "GCal", no_credentials)
    runner.calendar = None
    code = runner.run(ReviewRequest(fmt="json"))
    payload = json.loads(runner.out)

    assert code == 0
    assert any("could not be read" in c for c in payload["caveats"])


def test_a_review_never_opens_a_browser_to_authorize(runner, monkeypatch):
    # A review run from cron must degrade, not block on an OAuth flow that
    # nobody is watching.
    from task_gcal.review import facts as facts_mod

    seen = {}

    def record(_settings, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("no auth")

    monkeypatch.setattr(facts_mod, "GCal", record)
    runner.calendar = None
    runner.run()

    assert seen == {"allow_interactive": False}


def test_checkin_does_not_claim_nothing_to_review_without_a_calendar(
    monkeypatch, capsys, isolated_journal
):
    # Blocks are where nearly all the evidence comes from.
    from task_gcal import checkin as checkin_mod
    from task_gcal.review import facts as facts_mod

    monkeypatch.setattr(facts_mod, "load_all_tasks", lambda **_kwargs: [])
    monkeypatch.setattr(
        facts_mod, "GCal", lambda *_a, **_kw: (_ for _ in ()).throw(
            RuntimeError("no calendar")
        )
    )
    console = checkin_mod.Console(
        read=lambda _prompt: "", write=print, interactive=True
    )
    code, summary = checkin_mod.checkin(
        SETTINGS, since=NOW - timedelta(days=7), now=FRIDAY, console=console
    )
    out = capsys.readouterr().out

    assert code == 1
    assert summary.recorded == 0
    assert "could not be read" in out
    assert "Nothing to review" not in out


def test_an_empty_setup_says_it_has_no_history(runner):
    runner.run()
    assert "no scheduling runs recorded" in runner.out
    assert "No task-change history yet" in runner.out


def test_journal_records_in_the_period_are_read(runner, isolated_journal):
    journal.append(
        journal.build_record(
            settings=SETTINGS,
            mode=journal.MODE_SCHEDULE,
            at=at(0, 12),
            placements=(),
        )
    )
    runner.run(ReviewRequest(fmt="json"))
    payload = json.loads(runner.out)

    assert not any("no scheduling runs" in c for c in payload["caveats"])
    # The day count itself now lives beside the title, so the caveat says only
    # what a count can't: what a day nobody observed means.
    assert payload["observed"] == [1, 5]
    assert any("missing, not zero" in c for c in payload["caveats"])


def test_a_settings_change_inside_the_period_is_annotated(
    runner, isolated_journal
):
    from dataclasses import replace

    for offset, settings in ((0, SETTINGS), (1, replace(SETTINGS, settle_days=9))):
        journal.append(
            journal.build_record(
                settings=settings,
                mode=journal.MODE_SCHEDULE,
                at=at(offset, 12),
                placements=(),
            )
        )
    runner.run(ReviewRequest(fmt="json"))
    payload = json.loads(runner.out)

    assert any("definitions changed" in c for c in payload["caveats"])


def test_triage_prints_commands_instead_of_a_report(runner):
    runner.with_tasks(a_task(uuid="a", id=40)).with_calendar(
        blocks=[a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60)]
    )
    runner.run(ReviewRequest(triage=True))

    assert "task 40 delete" in runner.out
    assert "Capacity" not in runner.out


def test_all_shows_every_section(runner):
    runner.run(ReviewRequest(all_sections=True, fmt="json"))
    keys = {s["key"] for s in json.loads(runner.out)["sections"]}
    from task_gcal.review import section_keys

    assert keys == set(section_keys())


def test_every_section_has_a_plain_language_definition():
    # The registry carries the definition next to the builder so a new metric
    # can't ship without one. A section nobody can define is a section nobody
    # can act on.
    from task_gcal.review import section_keys
    from task_gcal.review.metrics import glossary

    meanings = glossary()
    assert set(meanings) == set(section_keys())
    for key, text in meanings.items():
        assert text.strip(), key
        assert text.strip()[-1] in ".?", key


def test_no_definition_explains_itself_in_our_own_vocabulary():
    # These are the words a first-time reader has no reason to know. If a
    # definition needs one, it isn't a definition yet.
    from task_gcal.review.metrics import glossary

    jargon = (
        "denominator", "coverage", "uda", "uuid", "journal", "harvest",
        "taskchampion", "metric", "section", "settle", "drift",
    )
    for key, text in glossary().items():
        lowered = text.lower()
        assert not [word for word in jargon if word in lowered], key


def test_all_shows_every_section_in_full(runner):
    # Thirteen summary lines you then have to re-run one at a time, each with a
    # name you had to remember, is worse than either the summary or the detail.
    runner.run(ReviewRequest(all_sections=True, fmt="json"))
    sections = json.loads(runner.out)["sections"]
    assert any(s.get("detail") for s in sections)


def test_the_default_summary_stays_a_summary(runner):
    runner.run(ReviewRequest(fmt="json"))
    sections = json.loads(runner.out)["sections"]
    assert all("detail" not in s for s in sections)


def test_the_default_is_the_headline_sections_only(runner):
    runner.run(ReviewRequest(fmt="json"))
    keys = {s["key"] for s in json.loads(runner.out)["sections"]}
    assert "lead_time" not in keys
    assert "time" in keys


def test_a_review_never_prompts(runner, monkeypatch):
    # `checkin` is the one entry point to the retrospective. A review that
    # could block on questions isn't a read-only document, and two ways in
    # was two things to explain.
    called: list[str] = []
    monkeypatch.setattr(
        "task_gcal.checkin.checkin",
        lambda *args, **kwargs: (called.append("x"), (0, None))[1],
    )
    runner.run()
    runner.run(ReviewRequest(triage=True))

    assert called == []
