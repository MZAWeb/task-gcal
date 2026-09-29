"""The full-screen check-in, driven headlessly.

The same restraint as the line prompt, checked through the keyboard: nothing is
recorded without an answer, skipping leaves an episode open, and unknown time
stays unknown. On top of that, what the screen adds: moving around freely,
correcting an answer, and drafts that survive being moved away from.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from task_gcal.checkin import reflection_for, ANSWERS
from task_gcal.config import Settings
from task_gcal.review.episodes import find_unresolved
from task_gcal.review.observed import build_timelines
from task_gcal.tui.checkin_app import CheckinApp, CheckinData

from conftest import NOW, a_block, a_task, at

TZ = Settings(timezone="UTC").resolve_timezone()
FRIDAY = NOW + timedelta(days=4, hours=6)


def two_episodes():
    tasks = [
        a_task(uuid="u1", id=1, description="Write the proposal", estimate=60),
        a_task(uuid="u2", id=2, description="Call the bank", estimate=30),
    ]
    blocks = {
        "u1": [a_block("u1", at(1, 9), 60)],
        "u2": [a_block("u2", at(2, 9), 30)],
    }
    return find_unresolved(
        tasks=tasks,
        blocks_by_task=blocks,
        timelines=build_timelines(()),
        answers={},
        now=FRIDAY,
        since=NOW - timedelta(days=14),
    )


class Run:
    def __init__(self, episodes=None, *, calendar_ok=True):
        self.episodes = two_episodes() if episodes is None else episodes
        self.saved = []
        self.data = CheckinData(
            episodes=tuple(self.episodes),
            since=NOW - timedelta(days=14),
            tz=TZ,
            calendar_ok=calendar_ok,
        )
        self.app = CheckinApp(
            lambda: self.data, self.saved.append, clock=lambda: FRIDAY
        )
        self.seen = {}

    def keys(self, *keys, inspect=None, finish=True):
        async def go():
            async with self.app.run_test(size=(120, 40)) as pilot:
                await self.app.workers.wait_for_complete()
                await pilot.pause()
                for key in keys:
                    await pilot.press(key)
                await pilot.pause()
                if inspect is not None:
                    inspect(self.app.screen, self.seen)
                if finish and self.app.is_running:
                    await pilot.press("escape", "q")
                    await pilot.pause()

        asyncio.run(go())
        return self.app.return_value


def text_of(screen, selector) -> str:
    """What a Static shows, as plain text, whatever renderable it holds."""
    import io

    from rich.console import Console

    console = Console(width=200, file=io.StringIO(), record=True)
    console.print(screen.query_one(selector).content)
    return console.export_text()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def test_a_number_and_enter_record_an_answer():
    run = Run()
    summary = run.keys("2", "enter")

    (reflection,) = run.saved
    assert (reflection.outcome, reflection.reason) == ("not_started", "avoided")
    assert reflection.episode == run.episodes[0].key
    assert reflection.actual_minutes is None
    assert (summary.recorded, summary.found) == (1, 2)


def test_work_that_happened_asks_for_minutes():
    run = Run()
    run.keys("1", "3", "0", "enter", "enter")

    (reflection,) = run.saved
    assert reflection.outcome == "partial"
    assert reflection.actual_minutes == 30


def test_blank_minutes_stay_unknown_and_never_become_the_estimate():
    run = Run()
    run.keys("1", "enter", "enter")

    (reflection,) = run.saved
    assert reflection.actual_minutes is None
    assert reflection.planned_minutes == 60


def test_minutes_are_only_asked_for_when_work_happened():
    def inspect(screen, seen):
        seen["hidden"] = screen.query_one("#minutes-field").has_class("hidden")

    run = Run()
    run.keys("3", inspect=inspect)
    assert run.seen["hidden"] is True

    run = Run()
    run.keys("5", inspect=inspect)
    assert run.seen["hidden"] is False


def test_saving_without_an_answer_records_nothing_and_says_why():
    def inspect(screen, seen):
        seen["error"] = text_of(screen, "#error")

    run = Run()
    run.keys("ctrl+s", inspect=inspect)

    assert run.saved == []
    assert "Pick what happened" in run.seen["error"]


def test_a_note_is_kept_and_q_inside_it_is_just_a_letter():
    run = Run()
    summary = run.keys("4", "q", "u", "i", "t", "enter")

    (reflection,) = run.saved
    assert reflection.note == "quit"
    assert summary.recorded == 1


def test_saving_moves_on_to_the_next_open_episode():
    def inspect(screen, seen):
        seen["title"] = str(screen.query_one("#right").border_title)

    run = Run()
    run.keys("2", "enter", inspect=inspect)
    assert run.seen["title"] == "2 of 2"


# ---------------------------------------------------------------------------
# Skipping, moving, correcting
# ---------------------------------------------------------------------------

def test_skipping_records_nothing_and_leaves_it_open():
    run = Run()
    summary = run.keys("s")

    assert run.saved == []
    assert (summary.recorded, summary.skipped, summary.found) == (0, 1, 2)


def test_an_answer_can_be_corrected_and_counts_once():
    run = Run()
    summary = run.keys("2", "enter", "p", "3", "enter")

    first, second = run.saved
    assert first.episode == second.episode == run.episodes[0].key
    assert (second.outcome, second.reason) == ("not_started", "capacity")
    assert summary.recorded == 1


def test_a_draft_survives_moving_away_and_back():
    def inspect(screen, seen):
        seen["chosen"] = screen._state.drafts[run.episodes[0].key].answer
        seen["minutes"] = screen.query_one("#minutes").value

    run = Run()
    run.keys("1", "4", "5", "escape", "n", "p", inspect=inspect)

    assert run.seen["chosen"].key == "1"
    assert run.seen["minutes"] == "45"
    assert run.saved == []


def test_quitting_keeps_what_was_answered():
    run = Run()
    summary = run.keys("2", "enter", "escape", "q", finish=False)

    assert len(run.saved) == 1
    assert (summary.recorded, summary.found) == (1, 2)


# ---------------------------------------------------------------------------
# Empty and broken states
# ---------------------------------------------------------------------------

def test_nothing_to_review_says_so():
    def inspect(screen, seen):
        seen["message"] = text_of(screen, "#message")

    run = Run(episodes=[])
    summary = run.keys(inspect=inspect)

    assert "Nothing to review" in run.seen["message"]
    assert summary.found == 0


def test_an_unreadable_calendar_records_nothing():
    def inspect(screen, seen):
        seen["message"] = text_of(screen, "#message")

    run = Run(calendar_ok=False)
    run.keys("2", "enter", inspect=inspect)

    assert "could not be read" in run.seen["message"]
    assert run.saved == []


def test_a_task_description_is_shown_literally_not_as_markup():
    tasks = [a_task(uuid="u1", description="[bold]not markup[/bold]", estimate=30)]
    episodes = find_unresolved(
        tasks=tasks,
        blocks_by_task={"u1": [a_block("u1", at(1, 9), 30)]},
        timelines=build_timelines(()),
        answers={},
        now=FRIDAY,
        since=NOW - timedelta(days=14),
    )

    def inspect(screen, seen):
        seen["episode"] = text_of(screen, "#episode")

    run = Run(episodes=episodes)
    run.keys(inspect=inspect)
    assert "[bold]not markup[/bold]" in run.seen["episode"]


# ---------------------------------------------------------------------------
# The shared record builder
# ---------------------------------------------------------------------------

def test_minutes_are_dropped_for_an_answer_that_did_not_ask_for_them():
    (episode, _) = two_episodes()
    not_started = next(a for a in ANSWERS if not a.ask_minutes)
    reflection = reflection_for(
        episode, not_started, now=FRIDAY, actual_minutes=20, note="  "
    )
    assert reflection.actual_minutes is None
    assert reflection.note is None
