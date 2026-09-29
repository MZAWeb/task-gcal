"""The full-screen review, driven headlessly.

A front end over the same `Review` the plain report renders, so these check
what the screen adds — moving between sections and periods, caching, triage
copy, handing off to the check-in — and that it adds nothing to the numbers.
"""

from __future__ import annotations

import asyncio
import io
import os
from datetime import date, timedelta
from pathlib import Path

import pytest
from rich.console import Console

from task_gcal.review.stagnation import Prescription, Stagnant
from task_gcal.tui import review_app
from task_gcal.tui.review_app import ReviewApp, ReviewPage

from conftest import a_block, a_task, at


def text_of(widget) -> str:
    console = Console(width=200, file=io.StringIO(), record=True)
    console.print(widget.content)
    return console.export_text()


@pytest.fixture
def pages(review):
    """A source over the review harness, recording every period asked for."""
    review.tasks(
        a_task(uuid="a", status="completed", end=at(0, 9, 30), project="fraud"),
        a_task(uuid="b", status="completed", end=at(1, 16), project="fraud"),
        a_task(uuid="d", entry=at(0, 8)),
    )
    review.blocks(a_block("a", at(0, 9), 60), a_block("d", at(2, 10), 60))
    review.meetings((at(0, 11), at(0, 13)))

    class Source:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []
            self.stuck: tuple = ()
            self.fail = False

        def __call__(self, kind, anchor):
            self.calls.append((kind, anchor))
            if self.fail:
                raise RuntimeError("the calendar fell over")
            review.kind, review.anchor = kind, anchor
            return ReviewPage(review=review.review(), stuck=self.stuck)

    return Source()


def drive(app, *keys, inspect=None):
    seen = {}

    async def go():
        async with app.run_test(size=(130, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            for key in keys:
                await pilot.press(key)
                await app.workers.wait_for_complete()
                await pilot.pause()
            if inspect is not None:
                inspect(app.screen, seen)
            if app.is_running:
                await pilot.press("q")

    asyncio.run(go())
    return seen


def page_text(screen, seen):
    seen["title"] = str(screen.query_one("#right").border_title)
    seen["page"] = text_of(screen.query_one("#page"))
    seen["closing"] = text_of(screen.query_one("#closing"))
    seen["period"] = text_of(screen.query_one("#period"))


# ---------------------------------------------------------------------------
# What it shows
# ---------------------------------------------------------------------------

def test_it_opens_on_the_one_screen_summary(pages):
    seen = drive(ReviewApp(pages), inspect=page_text)

    assert seen["title"] == "Overview"
    assert "Time" in seen["page"] and "Finished" in seen["page"]
    assert "Week 37" in seen["period"]
    assert pages.calls == [("week", None)]


def test_a_section_asked_for_on_the_command_line_opens_first(pages):
    seen = drive(ReviewApp(pages, focus="sec:finished"), inspect=page_text)
    assert seen["title"].startswith("Finished")


def test_moving_down_the_list_opens_each_section(pages):
    seen = drive(ReviewApp(pages), "down", inspect=page_text)
    # Overview, a separator and a group heading are skipped over.
    assert seen["title"].startswith("Time")
    assert "working" in seen["page"]


def test_a_section_that_does_not_exist_falls_back_to_the_overview(pages):
    seen = drive(ReviewApp(pages, focus="sec:vibes"), inspect=page_text)
    assert seen["title"] == "Overview"


def test_the_numbers_are_the_reports_numbers(pages, review):
    # Same model, so the same sentence: the screen can't disagree.
    seen = drive(ReviewApp(pages, focus="sec:time"), inspect=page_text)
    summary = review.review().section("time").summary
    assert " ".join(summary.split()) in " ".join(seen["page"].split())


# ---------------------------------------------------------------------------
# Moving between periods
# ---------------------------------------------------------------------------

def test_left_goes_back_a_week_by_naming_it(pages):
    drive(ReviewApp(pages), "left")
    assert pages.calls[-1] == ("week", date(2026, 8, 31))


def test_right_past_the_present_is_refused(pages):
    drive(ReviewApp(pages), "right")
    assert pages.calls == [("week", None)]


def test_month_keeps_the_moment_being_looked_at(pages):
    drive(ReviewApp(pages), "m")
    kind, anchor = pages.calls[-1]
    assert kind == "month" and anchor.month == 9


def test_now_returns_to_the_default_period(pages):
    seen = drive(ReviewApp(pages), "left", "t", inspect=page_text)
    assert "Week 37" in seen["period"]


def test_a_period_already_seen_is_not_fetched_again(pages):
    # This week by default, last week, this week by name, last week again.
    drive(ReviewApp(pages), "left", "right", "left")
    assert pages.calls == [("week", None), ("week", date(2026, 8, 31))]


def test_reload_fetches_again(pages):
    drive(ReviewApp(pages), "r")
    assert pages.calls == [("week", None), ("week", None)]


def test_a_failure_is_shown_rather_than_crashing(pages):
    pages.fail = True

    def inspect(screen, seen):
        seen["message"] = text_of(screen.query_one("#message"))

    seen = drive(ReviewApp(pages), inspect=inspect)
    assert "the calendar fell over" in seen["message"]


# ---------------------------------------------------------------------------
# Triage: copied, never run
# ---------------------------------------------------------------------------

def _stuck(description="Prepare PIR"):
    task = a_task(uuid="s", id=40, description=description, estimate=120)
    entry = Stagnant(task=task, reasons=("due pushed 4x, +20d",), pushes=4)
    return Prescription(
        entry=entry,
        commands=(
            ("task 40 modify wait:someday", "not now"),
            ("task 40 delete", "be honest"),
        ),
    )


def test_triage_copies_the_highlighted_command_and_runs_nothing(
    pages, monkeypatch
):
    pages.stuck = (_stuck(),)
    copied, ran = [], []
    monkeypatch.setattr(review_app, "_system_copy", copied.append)
    monkeypatch.setattr(review_app.subprocess, "run", lambda *a, **k: ran.append(a))

    drive(ReviewApp(pages, focus="triage"), "enter", "down", "enter")

    assert copied == ["task 40 modify wait:someday"]
    assert ran == []


def test_triage_shows_task_titles_literally(pages):
    pages.stuck = (_stuck("[red]not markup[/red]"),)

    def inspect(screen, seen):
        commands = screen.query_one("#commands")
        seen["rows"] = [
            str(commands.get_option_at_index(i).prompt)
            for i in range(commands.option_count)
        ]

    seen = drive(ReviewApp(pages, focus="triage"), inspect=inspect)
    assert any("[red]not markup[/red]" in row for row in seen["rows"])


def test_an_empty_triage_says_so(pages):
    def inspect(screen, seen):
        seen["intro"] = text_of(screen.query_one("#triage-intro"))

    seen = drive(ReviewApp(pages, focus="triage"), inspect=inspect)
    assert "Nothing looks stuck" in seen["intro"]


# ---------------------------------------------------------------------------
# Handing off
# ---------------------------------------------------------------------------

def test_the_browser_gets_a_private_file_with_every_section(pages):
    opened = []
    drive(ReviewApp(pages, opener=opened.append), "o")

    (uri,) = opened
    path = Path(uri.removeprefix("file://"))
    try:
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        html = path.read_text()
        assert "Lead time" in html  # not only the summary sections
    finally:
        path.unlink()


def test_a_check_in_opened_from_the_review_refreshes_it_after(pages):
    from task_gcal.tui.checkin_app import CheckinData, CheckinScreen

    def checkin():
        return CheckinScreen(
            lambda: CheckinData(
                episodes=(), since=at(0, 0) - timedelta(days=14), tz=None
            ),
            lambda _r: None,
        )

    drive(ReviewApp(pages, checkin=checkin), "c", "q")
    # Answers feed the reasons section, so the cached page is thrown away.
    assert pages.calls == [("week", None), ("week", None)]
