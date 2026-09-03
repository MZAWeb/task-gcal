"""Miss episodes and the check-in that resolves them.

The rules under test are all about restraint: coalesce evidence into one
prompt, present a suggestion as a suggestion, never record an outcome or a
reason without being told, and never turn unknown time into the estimate.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from task_gcal import checkin as checkin_mod
from task_gcal import journal, reflections
from task_gcal.config import Settings
from task_gcal.review.episodes import (
    KIND_BLOCK_PASSED,
    KIND_DEADLINE_PUSHED,
    find_unresolved,
)
from task_gcal.review.observed import build_timelines

from conftest import NOW, a_block, a_task, at

SETTINGS = Settings(timezone="UTC")
TZ = SETTINGS.resolve_timezone()
FRIDAY = NOW + timedelta(days=4, hours=6)


def blocks_by(*blocks):
    out: dict[str, list] = {}
    for block in blocks:
        out.setdefault(block.task_uuid, []).append(block)
    for value in out.values():
        value.sort(key=lambda b: b.start)
    return out


def timelines_from(*records):
    return build_timelines(records)


def snapshot(when, *tasks):
    return journal.build_record(
        settings=SETTINGS,
        mode=journal.MODE_SNAPSHOT,
        at=when,
        observations=journal.observe_tasks(list(tasks), blocks={}, detail="full"),
    )


def episodes(
    *, tasks, blocks=(), records=(), answers=None, now=FRIDAY, since=None
):
    return find_unresolved(
        tasks=list(tasks),
        blocks_by_task=blocks_by(*blocks),
        timelines=timelines_from(*records),
        answers=answers or {},
        now=now,
        since=since or (NOW - timedelta(days=14)),
    )


# ---------------------------------------------------------------------------
# What counts as evidence
# ---------------------------------------------------------------------------

def test_a_block_that_ended_with_the_task_open_is_evidence():
    (episode,) = episodes(
        tasks=[a_task(uuid="a")], blocks=[a_block("a", at(0, 9), 60)]
    )
    (evidence,) = episode.evidence
    assert evidence.kind == KIND_BLOCK_PASSED
    assert "still open" in evidence.detail


def test_a_block_whose_task_was_done_in_time_is_not():
    assert (
        episodes(
            tasks=[a_task(uuid="a", status="completed", end=at(0, 9, 30))],
            blocks=[a_block("a", at(0, 9), 60)],
        )
        == []
    )


def test_a_block_that_has_not_ended_yet_is_not():
    # It hasn't had its chance. Asking about it would be asking about a plan.
    assert (
        episodes(
            tasks=[a_task(uuid="a")], blocks=[a_block("a", at(4, 14, 30), 90)]
        )
        == []
    )


def test_a_reactive_deadline_push_is_evidence():
    (episode,) = episodes(
        tasks=[a_task(uuid="a", due=at(4, 17))],
        records=[
            snapshot(at(0, 9), a_task(uuid="a", due=at(0, 17))),
            snapshot(at(2, 9), a_task(uuid="a", due=at(4, 17))),
        ],
    )
    (evidence,) = episode.evidence
    assert evidence.kind == KIND_DEADLINE_PUSHED


def test_a_deadline_push_alone_does_not_claim_no_work_happened():
    # "A due-date push alone means a deadline moved, not that no work
    # happened." The wording has to leave that open.
    (episode,) = episodes(
        tasks=[a_task(uuid="a", due=at(4, 17))],
        records=[
            snapshot(at(0, 9), a_task(uuid="a", due=at(0, 17))),
            snapshot(at(2, 9), a_task(uuid="a", due=at(4, 17))),
        ],
    )
    assert episode.suggestion == (
        "deadline moved; work may or may not have happened"
    )


def test_a_proactive_push_is_not_evidence_of_a_miss():
    # Moving Friday's deadline on Monday is renegotiating, not missing.
    assert (
        episodes(
            tasks=[a_task(uuid="a", due=at(6, 17))],
            records=[
                snapshot(at(0, 9), a_task(uuid="a", due=at(4, 17))),
                snapshot(at(0, 18), a_task(uuid="a", due=at(6, 17))),
            ],
        )
        == []
    )


# ---------------------------------------------------------------------------
# Coalescing
# ---------------------------------------------------------------------------

def test_several_blocks_and_pushes_become_one_episode():
    # Five prompts for one task is how a retrospective becomes a chore and
    # starts collecting whatever answer ends it fastest.
    found = episodes(
        tasks=[a_task(uuid="a", due=at(4, 17))],
        blocks=[a_block("a", at(0, 9), 60), a_block("a", at(2, 9), 60)],
        records=[
            snapshot(at(0, 18), a_task(uuid="a", due=at(0, 17))),
            snapshot(at(1, 18), a_task(uuid="a", due=at(2, 17))),
            snapshot(at(3, 18), a_task(uuid="a", due=at(4, 17))),
        ],
    )
    assert len(found) == 1
    assert len(found[0].evidence) == 4


def test_different_tasks_stay_separate():
    found = episodes(
        tasks=[a_task(uuid="a"), a_task(uuid="b")],
        blocks=[a_block("a", at(0, 9), 60), a_block("b", at(1, 9), 60)],
    )
    assert {e.task.uuid for e in found} == {"a", "b"}


def test_the_strongest_evidence_is_asked_about_first():
    found = episodes(
        tasks=[a_task(uuid="weak"), a_task(uuid="strong", due=at(4, 17))],
        blocks=[
            a_block("weak", at(0, 9), 60),
            a_block("strong", at(0, 9), 60),
            a_block("strong", at(2, 9), 60),
        ],
        records=[
            snapshot(at(0, 18), a_task(uuid="strong", due=at(0, 17))),
            snapshot(at(1, 18), a_task(uuid="strong", due=at(4, 17))),
        ],
    )
    assert [e.task.uuid for e in found] == ["strong", "weak"]


def test_a_block_and_a_push_together_are_the_strongest_suggestion():
    (episode,) = episodes(
        tasks=[a_task(uuid="a", due=at(4, 17))],
        blocks=[a_block("a", at(0, 9), 60)],
        records=[
            snapshot(at(0, 18), a_task(uuid="a", due=at(0, 17))),
            snapshot(at(1, 18), a_task(uuid="a", due=at(4, 17))),
        ],
    )
    assert episode.suggestion == "likely unfinished, then deferred"


def test_repeated_blocks_suggest_an_underestimate():
    (episode,) = episodes(
        tasks=[a_task(uuid="a")],
        blocks=[a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60)],
    )
    assert "under-estimated" in episode.suggestion


def test_every_suggestion_is_hedged():
    # None of them may read as a verdict; confirmation is the only way an
    # outcome is ever recorded.
    for episode in episodes(
        tasks=[a_task(uuid="a"), a_task(uuid="b")],
        blocks=[
            a_block("a", at(0, 9), 60),
            a_block("b", at(0, 9), 60),
            a_block("b", at(1, 9), 60),
        ],
    ):
        assert any(
            word in episode.suggestion for word in ("likely", "may")
        ), episode.suggestion


# ---------------------------------------------------------------------------
# Answering settles it
# ---------------------------------------------------------------------------

def test_an_answered_episode_stops_being_asked_about():
    tasks = [a_task(uuid="a")]
    blocks = [a_block("a", at(0, 9), 60)]
    (episode,) = episodes(tasks=tasks, blocks=blocks)
    answer = reflections.Reflection(
        episode=episode.key,
        task_uuid="a",
        outcome=reflections.OUTCOME_PARTIAL,
        reason=reflections.REASON_AVOIDED,
        at=FRIDAY,
        covers_until=episode.covers_until,
    )

    assert episodes(tasks=tasks, blocks=blocks, answers={episode.key: answer}) == []


def test_new_evidence_after_an_answer_opens_a_new_episode():
    # A task that goes wrong again is asked about again, rather than being
    # permanently excused by one answer.
    tasks = [a_task(uuid="a")]
    first = [a_block("a", at(0, 9), 60)]
    (episode,) = episodes(tasks=tasks, blocks=first)
    answer = reflections.Reflection(
        episode=episode.key,
        task_uuid="a",
        outcome=reflections.OUTCOME_NOT_STARTED,
        reason=reflections.REASON_CAPACITY,
        at=at(1, 9),
        covers_until=episode.covers_until,
    )

    (later,) = episodes(
        tasks=tasks,
        blocks=first + [a_block("a", at(3, 9), 60)],
        answers={episode.key: answer},
    )
    assert later.key != episode.key
    assert len(later.evidence) == 1  # only the new block


def test_the_episode_key_is_stable_across_runs():
    tasks = [a_task(uuid="a")]
    blocks = [a_block("a", at(0, 9), 60)]
    first = episodes(tasks=tasks, blocks=blocks, now=FRIDAY)[0]
    later = episodes(
        tasks=tasks, blocks=blocks, now=FRIDAY + timedelta(hours=5)
    )[0]
    assert first.key == later.key


def test_evidence_before_the_window_is_not_asked_about():
    assert (
        episodes(
            tasks=[a_task(uuid="a")],
            blocks=[a_block("a", at(-30, 9), 60)],
            since=NOW - timedelta(days=14),
        )
        == []
    )


def test_an_explicit_since_reaches_further_back():
    found = episodes(
        tasks=[a_task(uuid="a")],
        blocks=[a_block("a", at(-30, 9), 60)],
        since=NOW - timedelta(days=60),
    )
    assert len(found) == 1


# ---------------------------------------------------------------------------
# The reflection store
# ---------------------------------------------------------------------------

def a_reflection(**kwargs):
    base = dict(
        episode="a@2026-09-07",
        task_uuid="a",
        outcome=reflections.OUTCOME_PARTIAL,
        reason=reflections.REASON_AVOIDED,
        at=FRIDAY,
        covers_until=at(0, 10),
    )
    base.update(kwargs)
    return reflections.Reflection(**base)


def test_a_reflection_round_trips(isolated_journal):
    reflections.append(a_reflection(actual_minutes=20, note="ran out of week"))
    (stored,) = reflections.load().values()

    assert stored.outcome == reflections.OUTCOME_PARTIAL
    assert stored.reason == reflections.REASON_AVOIDED
    assert stored.actual_minutes == 20
    assert stored.note == "ran out of week"
    assert stored.covers_until == at(0, 10)


def test_the_file_is_private(isolated_journal):
    path = reflections.append(a_reflection())
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_a_later_answer_corrects_an_earlier_one(isolated_journal):
    # Retrospective labels, not immutable facts.
    reflections.append(a_reflection(reason=reflections.REASON_AVOIDED))
    reflections.append(a_reflection(reason=reflections.REASON_BLOCKED))

    (stored,) = reflections.load().values()
    assert stored.reason == reflections.REASON_BLOCKED
    # And the history is intact on disk, since the file is append-only.
    assert len(reflections.path().read_text().splitlines()) == 2


def test_unknown_is_reported_as_unclassified_not_bucketed():
    assert a_reflection(reason=reflections.REASON_UNKNOWN).classified is False
    assert a_reflection(reason=reflections.REASON_CAPACITY).classified is True


def test_an_unrecognized_reason_degrades_to_unknown():
    # Never silently mapped onto a plausible bucket.
    stored = reflections.Reflection.from_dict(
        {
            "episode": "a@2026-09-07",
            "at": "2026-09-11T15:00:00Z",
            "outcome": "vibes",
            "reason": "mercury retrograde",
        }
    )
    assert stored.outcome == reflections.OUTCOME_UNKNOWN
    assert stored.reason == reflections.REASON_UNKNOWN


def test_a_truncated_final_line_is_tolerated(isolated_journal):
    reflections.append(a_reflection())
    with open(reflections.path(), "a") as fh:
        fh.write('{"episode":"half')
    assert len(reflections.load()) == 1


def test_no_file_is_no_answers(isolated_journal):
    assert reflections.load() == {}


def test_answered_until_is_the_latest_evidence_accounted_for(isolated_journal):
    reflections.append(a_reflection(episode="a@1", covers_until=at(0, 10)))
    reflections.append(a_reflection(episode="a@2", covers_until=at(2, 10)))
    assert reflections.answered_until(reflections.load(), "a") == at(2, 10)


# ---------------------------------------------------------------------------
# The check-in interaction
# ---------------------------------------------------------------------------

class FakeCalendar:
    def __init__(self, blocks):
        self.blocks = list(blocks)

    def list_scheduler_events(self, time_min, time_max):
        return [b for b in self.blocks if b.end > time_min and b.start < time_max]

    def list_busy_events(self, time_min, time_max, exclude_event_ids):
        return []


@pytest.fixture
def ask(monkeypatch, isolated_journal):
    """Drive `checkin` with scripted answers and capture what it printed."""
    from task_gcal.review import facts as facts_mod

    class Asker:
        def __init__(self) -> None:
            self.tasks = []
            self.blocks = []
            self.output: list[str] = []
            self.prompts: list[str] = []

        def setup(self, *, tasks, blocks=()):
            self.tasks = list(tasks)
            self.blocks = list(blocks)
            return self

        def run(self, *answers, interactive=True, since=None):
            script = iter(answers)

            def read(prompt):
                self.prompts.append(prompt)
                try:
                    return next(script)
                except StopIteration:
                    raise EOFError from None

            console = checkin_mod.Console(
                read=read,
                write=lambda text: self.output.append(text),
                interactive=interactive,
            )
            code, summary = checkin_mod.checkin(
                SETTINGS,
                since=since or (NOW - timedelta(days=14)),
                now=FRIDAY,
                gcal=FakeCalendar(self.blocks),
                console=console,
            )
            self.code, self.summary = code, summary
            self.text = "\n".join(self.output)
            return summary

        def stored(self):
            return list(reflections.load().values())

    asker = Asker()
    monkeypatch.setattr(
        facts_mod, "load_all_tasks", lambda **_kwargs: asker.tasks
    )
    return asker


def test_a_full_answer_is_recorded(ask):
    ask.setup(tasks=[a_task(uuid="a", estimate=60)], blocks=[a_block("a", at(0, 9), 60)])
    summary = ask.run("p", "a", "20", "ran out of week")

    assert summary.recorded == 1
    (stored,) = ask.stored()
    assert (stored.outcome, stored.reason) == ("partial", "avoided")
    assert stored.actual_minutes == 20
    assert stored.note == "ran out of week"


def test_the_prompt_shows_the_evidence_and_labels_the_suggestion(ask):
    ask.setup(tasks=[a_task(uuid="a", estimate=60)], blocks=[a_block("a", at(0, 9), 60)])
    ask.run("")

    assert "block ended with the task still open" in ask.text
    assert "Suggests: likely unfinished" in ask.text


def test_an_empty_answer_skips_and_leaves_the_episode_open(ask):
    ask.setup(tasks=[a_task(uuid="a")], blocks=[a_block("a", at(0, 9), 60)])
    summary = ask.run("")

    assert (summary.recorded, summary.skipped) == (0, 1)
    assert ask.stored() == []


def test_q_stops_and_keeps_what_was_answered(ask):
    ask.setup(
        tasks=[a_task(uuid="a"), a_task(uuid="b")],
        blocks=[a_block("a", at(0, 9), 60), a_block("b", at(1, 9), 60)],
    )
    summary = ask.run("n", "w", "", "q")

    assert summary.recorded == 1
    assert "Stopped" in ask.text


def test_actual_minutes_are_never_defaulted_to_the_estimate(ask):
    # "Unknown remains unknown and never silently becomes the estimate."
    ask.setup(tasks=[a_task(uuid="a", estimate=60)], blocks=[a_block("a", at(0, 9), 60)])
    ask.run("p", "e", "", "")

    (stored,) = ask.stored()
    assert stored.actual_minutes is None


def test_a_done_outcome_is_not_asked_why_it_was_missed(ask):
    # There was no miss to explain; the task record was just late.
    ask.setup(tasks=[a_task(uuid="a")], blocks=[a_block("a", at(0, 9), 60)])
    ask.run("d", "", "")

    assert not any("Primary reason" in line for line in ask.output)
    (stored,) = ask.stored()
    assert stored.outcome == "done"
    assert stored.classified is False


def test_a_bad_key_re_asks_rather_than_guessing(ask):
    ask.setup(tasks=[a_task(uuid="a")], blocks=[a_block("a", at(0, 9), 60)])
    ask.run("z", "n", "w", "")

    assert any("one of" in line for line in ask.output)
    (stored,) = ask.stored()
    assert stored.outcome == "not_started"


def test_a_full_word_works_as_well_as_a_key(ask):
    ask.setup(tasks=[a_task(uuid="a")], blocks=[a_block("a", at(0, 9), 60)])
    ask.run("partial", "blocked", "", "")
    (stored,) = ask.stored()
    assert (stored.outcome, stored.reason) == ("partial", "blocked")


def test_nonsense_minutes_are_re_asked(ask):
    ask.setup(tasks=[a_task(uuid="a")], blocks=[a_block("a", at(0, 9), 60)])
    ask.run("p", "e", "loads", "45", "")
    (stored,) = ask.stored()
    assert stored.actual_minutes == 45


def test_zero_minutes_is_refused(ask):
    ask.setup(tasks=[a_task(uuid="a")], blocks=[a_block("a", at(0, 9), 60)])
    ask.run("p", "e", "0", "30", "")
    (stored,) = ask.stored()
    assert stored.actual_minutes == 30


def test_nothing_to_review_says_so(ask):
    ask.setup(tasks=[a_task(uuid="a", status="completed", end=at(0, 9, 30))],
              blocks=[a_block("a", at(0, 9), 60)])
    summary = ask.run()

    assert summary.found == 0
    assert "Nothing to review" in ask.text


def test_a_non_interactive_run_lists_instead_of_guessing(ask):
    ask.setup(tasks=[a_task(uuid="a")], blocks=[a_block("a", at(0, 9), 60)])
    summary = ask.run(interactive=False)

    assert summary.recorded == 0
    assert "Run this from a terminal" in ask.text
    assert "block ended with the task still open" in ask.text
    assert ask.stored() == []


def test_running_twice_does_not_ask_again(ask):
    ask.setup(tasks=[a_task(uuid="a")], blocks=[a_block("a", at(0, 9), 60)])
    ask.run("n", "w", "")
    second = ask.run()

    assert second.found == 0


def test_the_check_in_writes_nothing_but_reflections(ask, isolated_journal):
    ask.setup(tasks=[a_task(uuid="a")], blocks=[a_block("a", at(0, 9), 60)])
    ask.run("n", "w", "")

    assert journal.load().records == []
    assert reflections.path().is_file()
