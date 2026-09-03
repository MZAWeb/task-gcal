"""Tests for the deeper review sections and the triage list.

These are the metrics most able to mislead, so most of what's checked here is
restraint: churn described as *observed*, narrowing overrides not counted as
erosion, a block count reported without a cause attached, and no answer
carrying a reward or a penalty.
"""

from __future__ import annotations

from datetime import timedelta

from task_gcal import journal, reflections
from task_gcal.config import Settings
from task_gcal.review import triage
from task_gcal.review.metrics import (
    attempts,
    boundaries,
    deadlines,
    friction,
    scope,
    stagnation_section,
)
from task_gcal.review.observed import build_timelines
from task_gcal.review.stagnation import find, prescribe

from conftest import NOW, a_block, a_task, at

SETTINGS = Settings(timezone="UTC")
FRIDAY = NOW + timedelta(days=4, hours=6)


def snapshot(when, *tasks):
    return journal.build_record(
        settings=SETTINGS,
        mode=journal.MODE_SNAPSHOT,
        at=when,
        observations=journal.observe_tasks(list(tasks), blocks={}, detail="full"),
    )


def data(review, key):
    section = review.review().section(key)
    assert section is not None
    return section


# ---------------------------------------------------------------------------
# Deadlines / the promise ledger
# ---------------------------------------------------------------------------

def test_a_push_is_counted_and_measured_in_days(review):
    review.tasks(a_task(uuid="a", due=at(4, 17)))
    review.records(
        snapshot(at(0, 9), a_task(uuid="a", due=at(1, 17))),
        snapshot(at(1, 9), a_task(uuid="a", due=at(4, 17))),
    )
    section = data(review, deadlines.KEY)

    assert section.data["observed_pushes"] == 1
    assert section.data["tasks_pushed"] == 1
    assert section.data["days_pushed"] == 3.0


def test_pushes_are_always_described_as_observed(review):
    review.tasks(a_task(uuid="a", due=at(4, 17)))
    review.records(
        snapshot(at(0, 9), a_task(uuid="a", due=at(1, 17))),
        snapshot(at(1, 9), a_task(uuid="a", due=at(4, 17))),
    )
    assert "observed" in data(review, deadlines.KEY).summary


def test_reactive_and_proactive_pushes_are_separated(review):
    review.tasks(a_task(uuid="late", due=at(4, 17)), a_task(uuid="early", due=at(6, 17)))
    review.records(
        snapshot(at(0, 9), a_task(uuid="late", due=at(0, 17)),
                 a_task(uuid="early", due=at(4, 17))),
        snapshot(at(2, 9), a_task(uuid="late", due=at(4, 17)),
                 a_task(uuid="early", due=at(6, 17))),
    )
    section = data(review, deadlines.KEY)

    assert section.data["reactive_pushes"] == 1
    assert section.data["proactive_pushes"] == 1
    # And neither is called worse than the other.
    text = "\n".join(section.detail)
    assert "a miss being reported" in text
    assert "being renegotiated" in text


def test_the_ledger_separates_the_original_promise_from_the_final_one(review):
    # Promised Tuesday, delivered Thursday against a renegotiated Friday.
    review.tasks(
        a_task(uuid="a", status="completed", due=at(4, 17), end=at(3, 16))
    )
    review.records(
        snapshot(at(0, 9), a_task(uuid="a", due=at(1, 17))),
        snapshot(at(1, 9), a_task(uuid="a", due=at(4, 17))),
    )
    section = data(review, deadlines.KEY)

    assert section.data["met_final"] == 1
    assert section.data["met_original"] == 0


def test_being_late_on_a_date_that_never_moved_is_counted(review):
    # The pushes are the visible failure mode; reporting only them would
    # miss every task simply finished late.
    review.tasks(
        a_task(uuid="a", status="completed", due=at(1, 17), end=at(3, 16))
    )
    review.records(snapshot(at(0, 9), a_task(uuid="a", due=at(1, 17))))
    section = data(review, deadlines.KEY)

    assert section.data["late_on_unchanged_date"] == 1
    assert section.data["observed_pushes"] == 0


def test_a_third_push_becomes_the_reviews_closing_line(review):
    review.tasks(a_task(uuid="a", description="Prepare PIR", due=at(6, 17)))
    review.records(
        snapshot(at(0, 9), a_task(uuid="a", description="Prepare PIR", due=at(0, 17))),
        snapshot(at(1, 9), a_task(uuid="a", description="Prepare PIR", due=at(1, 17))),
        snapshot(at(2, 9), a_task(uuid="a", description="Prepare PIR", due=at(2, 17))),
        snapshot(at(3, 9), a_task(uuid="a", description="Prepare PIR", due=at(6, 17))),
    )
    adjustment = review.review().adjustment
    assert "Prepare PIR" in adjustment
    assert "3rd time" in adjustment


def test_deadlines_are_unmeasured_without_any_history(review):
    review.tasks(a_task(uuid="a"))
    assert data(review, deadlines.KEY).measured is False


def test_a_task_with_no_journal_history_falls_back_to_its_current_date(review):
    # With nothing observed, the only promise we know about is the current
    # one — claiming the original was missed would be inventing history.
    review.tasks(
        a_task(uuid="a", status="completed", due=at(4, 17), end=at(3, 16))
    )
    review.records(snapshot(at(0, 9), a_task(uuid="b")))
    section = data(review, deadlines.KEY)
    assert section.data["met_original"] == 1


# ---------------------------------------------------------------------------
# Attempts / blocks-to-completion
# ---------------------------------------------------------------------------

def test_blocks_before_completion_are_counted(review):
    review.tasks(a_task(uuid="a", status="completed", end=at(2, 11), estimate=60))
    review.blocks(
        a_block("a", at(0, 9), 60),
        a_block("a", at(1, 9), 60),
        a_block("a", at(2, 10), 60),
    )
    section = data(review, attempts.KEY)

    assert section.data["total_blocks"] == 3
    assert section.data["average_blocks"] == 3.0


def test_a_block_scheduled_after_completion_was_never_an_attempt(review):
    review.tasks(a_task(uuid="a", status="completed", end=at(0, 10)))
    review.blocks(a_block("a", at(0, 9), 60), a_block("a", at(2, 9), 60))
    assert data(review, attempts.KEY).data["total_blocks"] == 1


def test_attempts_reports_a_count_without_a_cause(review):
    review.tasks(a_task(uuid="a", status="completed", end=at(2, 11)))
    review.blocks(a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60))
    text = "\n".join(data(review, attempts.KEY).detail)

    # An underestimate, an interruption and deliberate multi-session work all
    # produce the same number, and the report has to say so.
    assert "all look the same here" in text


def test_a_small_sample_does_not_become_a_calibration_claim(review):
    review.tasks(a_task(uuid="a", status="completed", end=at(2, 11)))
    review.blocks(a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60))
    assert data(review, attempts.KEY).suggestions == ()


def test_a_real_sample_of_stretched_tasks_does(review):
    review.tasks(
        *[
            a_task(uuid=f"u{i}", status="completed", end=at(3, 11), estimate=60)
            for i in range(5)
        ]
    )
    review.blocks(
        *[
            a_block(f"u{i}", at(day, 9), 60)
            for i in range(5)
            for day in range(3)
        ]
    )
    assert "estimates" in data(review, attempts.KEY).suggestions[0].text


# ---------------------------------------------------------------------------
# Scope churn
# ---------------------------------------------------------------------------

def test_an_estimate_doubling_is_reported_as_a_project(review):
    review.tasks(a_task(uuid="a", description="Analyze peakon", estimate=240))
    review.records(
        snapshot(at(0, 9), a_task(uuid="a", description="Analyze peakon", estimate=60)),
        snapshot(at(1, 9), a_task(uuid="a", description="Analyze peakon", estimate=240)),
    )
    section = data(review, scope.KEY)

    assert section.data["estimates_up"] == 1
    assert section.data["doubled"][0]["to_minutes"] == 240
    # Stagnation sees the same growth and outranks this, so check the
    # section's own proposal rather than which one the review closed on.
    assert "split it" in section.suggestions[0].text


def test_deferral_is_reported_apart_from_deadline_churn(review):
    review.tasks(a_task(uuid="a", scheduled=at(4, 9)))
    review.records(
        snapshot(at(0, 9), a_task(uuid="a", scheduled=at(1, 9))),
        snapshot(at(1, 9), a_task(uuid="a", scheduled=at(4, 9))),
    )
    section = data(review, scope.KEY)

    assert section.data["deferred"] == 1
    assert data(review, deadlines.KEY).data["observed_pushes"] == 0


# ---------------------------------------------------------------------------
# Boundary erosion
# ---------------------------------------------------------------------------

def test_an_evening_block_is_counted_as_an_evening(review):
    review.blocks(a_block("a", at(0, 19), 95))
    section = data(review, boundaries.KEY)

    assert section.data["evenings"] == 1
    assert section.data["evening_minutes"] == 95


def test_a_saturday_block_is_counted_as_a_weekend_day(review):
    review.whole_week().blocks(a_block("a", at(5, 10), 90))
    section = data(review, boundaries.KEY)

    assert section.data["weekend_days"] == 1
    assert section.data["weekend_minutes"] == 90


def test_only_the_part_outside_working_hours_counts(review):
    # An override that opened three hours and then placed a 30-minute block
    # cost half an hour, not an evening.
    review.blocks(a_block("a", at(0, 17), 90))  # 17:00-18:30, work ends 18:00
    assert data(review, boundaries.KEY).data["outside_minutes"] == 30


def test_a_widening_override_is_counted_as_widening(review):
    review.tasks(a_task(uuid="a", overrides="work_end_hour=21"))
    section = data(review, boundaries.KEY)

    assert section.data["widening_overrides"] == 1
    assert section.data["capacity_bought_minutes"] == 180


def test_a_narrowing_override_is_not_erosion(review):
    # The largest single group in the real data narrows the window. Counting
    # overrides would report it as a boundary violation; classifying intent
    # reports it as the opposite.
    review.tasks(a_task(uuid="a", overrides="work_days=4"))
    section = data(review, boundaries.KEY)

    assert section.data["narrowing_overrides"] == 1
    assert section.data["widening_overrides"] == 0


def test_a_density_override_is_neither(review):
    review.tasks(a_task(uuid="a", overrides="buffer_minutes=0"))
    section = data(review, boundaries.KEY)

    assert section.data["density_overrides"] == 1
    assert section.data["widening_overrides"] == 0


def test_an_overdue_horizon_override_belongs_to_deadlines_not_here(review):
    # Extending a deadline's tolerance is deferral, not boundary loss.
    review.tasks(a_task(uuid="a", overrides="overdue_horizon_days=60"))
    section = data(review, boundaries.KEY)
    assert section.data["widening_overrides"] == 0
    assert section.data["narrowing_overrides"] == 0


def test_a_weekend_adding_override_is_widening(review):
    review.tasks(a_task(uuid="a", overrides="work_days=0,1,2,3,4,5"))
    assert data(review, boundaries.KEY).data["widening_overrides"] == 1


def test_the_project_that_took_the_weekend_is_named(review):
    review.tasks(
        a_task(uuid="a", project="PIR", overrides="work_end_hour=21"),
        a_task(uuid="b", project="PIR", overrides="work_end_hour=21"),
    )
    review.whole_week().blocks(a_block("a", at(5, 10), 90))
    section = data(review, boundaries.KEY)

    assert section.data["paid_for_by"] == {"PIR": 2}
    assert "PIR" in review.review().adjustment
    assert "not your weekend" in review.review().adjustment


def test_a_malformed_override_does_not_crash_the_metric(review):
    review.tasks(a_task(uuid="a", overrides="work_end_hour=elevenses"))
    assert data(review, boundaries.KEY).measured is True


def test_nothing_outside_hours_is_reported_as_nothing(review):
    review.blocks(a_block("a", at(0, 10), 60))
    assert "nothing placed outside" in data(review, boundaries.KEY).summary


# ---------------------------------------------------------------------------
# Friction
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


def test_friction_is_unmeasured_without_check_ins(review, isolated_journal):
    section = data(review, friction.KEY)
    assert section.measured is False
    assert "checkin" in "\n".join(section.detail)


def test_the_reason_mix_is_reported(review, isolated_journal):
    reflections.append(a_reflection(episode="a@1", reason="capacity"))
    reflections.append(a_reflection(episode="a@2", reason="capacity"))
    reflections.append(a_reflection(episode="a@3", reason="avoided"))
    review.tasks(a_task(uuid="a"))

    section = data(review, friction.KEY)
    assert section.data["reasons"] == {"capacity": 2, "avoided": 1}
    assert section.summary.startswith("capacity 2")


def test_unknown_is_reported_as_unclassified(review, isolated_journal):
    reflections.append(a_reflection(episode="a@1", reason="unknown"))
    reflections.append(a_reflection(episode="a@2", reason="capacity"))
    review.tasks(a_task(uuid="a"))

    section = data(review, friction.KEY)
    assert section.data["unclassified"] == 1
    assert "unclassified" in section.summary
    (coverage, _actuals) = section.coverage
    assert (coverage.observed, coverage.total) == (1, 2)


def test_no_answer_is_scored(review, isolated_journal):
    # A metric that costs you something for admitting avoidance trains you to
    # lie to it, and then the whole thing is worthless.
    reflections.append(a_reflection(reason="avoided"))
    review.tasks(a_task(uuid="a"))

    text = "\n".join(data(review, friction.KEY).detail)
    assert "avoided` costs nothing to admit" in text


def test_a_done_outcome_is_not_counted_as_a_miss(review, isolated_journal):
    reflections.append(a_reflection(episode="a@1", outcome="done"))
    reflections.append(a_reflection(episode="a@2", outcome="not_started",
                                    reason="capacity"))
    review.tasks(a_task(uuid="a"))

    section = data(review, friction.KEY)
    assert section.data["reasons"] == {"capacity": 1}


def test_actual_time_uses_finished_work_only(review, isolated_journal):
    # 20 minutes spent on something unfinished says nothing about whether the
    # estimate was right.
    reflections.append(
        a_reflection(episode="a@1", outcome="partial", actual_minutes=20)
    )
    review.tasks(a_task(uuid="a", estimate=120))

    text = "\n".join(data(review, friction.KEY).detail)
    assert "no finished work with an actual" in text


def test_actual_time_is_reported_for_finished_work(review, isolated_journal):
    reflections.append(
        a_reflection(episode="a@1", outcome="done", actual_minutes=90)
    )
    review.tasks(a_task(uuid="a", estimate=60))

    text = "\n".join(data(review, friction.KEY).detail)
    assert "1.50x" in text
    assert "these tasks only" in text


def test_patterns_wait_for_a_sample(review, isolated_journal):
    reflections.append(a_reflection(reason="avoided"))
    review.tasks(a_task(uuid="a"))

    text = "\n".join(data(review, friction.KEY).detail)
    assert "need 8 confirmed misses" in text


def test_patterns_are_presented_as_correlations(review, isolated_journal):
    for i in range(8):
        reflections.append(
            a_reflection(episode=f"a@{i}", reason="avoided", task_uuid="a")
        )
    review.tasks(a_task(uuid="a", project="fraud"))

    text = "\n".join(data(review, friction.KEY).detail)
    assert "correlations, not diagnoses" in text
    assert "avoided in fraud" in text


def test_the_dominant_reason_becomes_a_change_not_a_verdict(review, isolated_journal):
    reflections.append(a_reflection(episode="a@1", reason="estimate"))
    reflections.append(a_reflection(episode="a@2", reason="estimate"))
    review.tasks(a_task(uuid="a"))

    adjustment = review.review().adjustment
    assert "estimate" in adjustment
    assert "halving the scope" in adjustment


# ---------------------------------------------------------------------------
# Stagnation and triage
# ---------------------------------------------------------------------------

def stagnant_for(review):
    facts = review.facts()
    return find(
        list(facts.tasks),
        timelines=build_timelines(facts.journal.records),
        blocks_by_task=facts.blocks_by_task(),
        now=facts.now,
    )


def test_repeated_pushes_make_a_task_stagnant(review):
    review.tasks(a_task(uuid="a", due=at(6, 17)))
    review.records(
        snapshot(at(0, 9), a_task(uuid="a", due=at(0, 17))),
        snapshot(at(1, 9), a_task(uuid="a", due=at(1, 17))),
        snapshot(at(2, 9), a_task(uuid="a", due=at(2, 17))),
        snapshot(at(3, 9), a_task(uuid="a", due=at(6, 17))),
    )
    (entry,) = stagnant_for(review)
    assert entry.pushes == 3
    assert "due pushed 3x" in entry.summary


def test_passed_blocks_make_a_task_stagnant(review):
    review.tasks(a_task(uuid="a"))
    review.blocks(a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60))
    (entry,) = stagnant_for(review)
    assert entry.passed_blocks == 2


def test_age_alone_is_not_evidence(review):
    # Plenty of old tasks are fine where they are. Age plus a moving deadline
    # is a different thing.
    review.tasks(a_task(uuid="a", entry=NOW - timedelta(days=400)))
    assert stagnant_for(review) == []


def test_a_completed_task_is_never_stagnant(review):
    review.tasks(a_task(uuid="a", status="completed", end=at(2, 11)))
    review.blocks(a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60))
    assert stagnant_for(review) == []


def test_the_most_evidence_comes_first(review):
    review.tasks(a_task(uuid="mild"), a_task(uuid="bad"))
    review.blocks(
        a_block("mild", at(0, 9), 60),
        a_block("mild", at(1, 9), 60),
        *[a_block("bad", at(day, 9), 60) for day in range(4)],
    )
    assert [e.task.uuid for e in stagnant_for(review)] == ["bad", "mild"]


def test_triage_prints_commands_and_changes_nothing(review):
    review.tasks(a_task(uuid="a", id=40, description="Analyze peakon", estimate=120))
    review.blocks(a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60))
    out = triage.render(review.facts())

    assert "task 40 modify estimate:30" in out
    assert "task 40 delete" in out
    assert "Nothing above has been run" in out


def test_triage_suggests_waiting_only_for_a_repeatedly_pushed_task(review):
    review.tasks(a_task(uuid="a", id=40, due=at(6, 17)))
    review.records(
        snapshot(at(0, 9), a_task(uuid="a", due=at(0, 17))),
        snapshot(at(1, 9), a_task(uuid="a", due=at(1, 17))),
        snapshot(at(2, 9), a_task(uuid="a", due=at(2, 17))),
        snapshot(at(3, 9), a_task(uuid="a", due=at(6, 17))),
    )
    assert "modify wait:someday" in triage.render(review.facts())


def test_triage_always_offers_the_honest_option(review):
    review.tasks(a_task(uuid="a", id=40))
    review.blocks(a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60))
    assert "# be honest" in triage.render(review.facts())


def test_triage_with_nothing_stagnant_says_so(review):
    review.tasks(a_task(uuid="a"))
    assert "Nothing is stagnant" in triage.render(review.facts())


def test_a_shrunk_estimate_is_a_first_step_not_the_whole_thing(review):
    entry = find(
        [a_task(uuid="a", id=40, estimate=240)],
        timelines={},
        blocks_by_task={
            "a": [a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60)]
        },
        now=FRIDAY,
    )[0]
    commands = dict(prescribe(entry).commands)
    assert "task 40 modify estimate:60" in commands


def test_a_tiny_estimate_is_not_shrunk_further(review):
    entry = find(
        [a_task(uuid="a", id=40, estimate=15)],
        timelines={},
        blocks_by_task={
            "a": [a_block("a", at(0, 9), 15), a_block("a", at(1, 9), 15)]
        },
        now=FRIDAY,
    )[0]
    assert not any("estimate:" in c for c, _why in prescribe(entry).commands)


def test_the_stagnation_section_points_at_triage(review):
    review.tasks(a_task(uuid="a", id=40))
    review.blocks(a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60))
    section = data(review, stagnation_section.KEY)

    assert section.data["stagnant"] == 1
    assert "--triage" in "\n".join(section.detail)


def test_a_clean_backlog_is_reported_as_clean(review):
    review.tasks(a_task(uuid="a"))
    section = data(review, stagnation_section.KEY)
    assert "nothing has accumulated" in section.summary
    assert section.suggestions == ()


# ---------------------------------------------------------------------------
# Source awareness: automated changes are not personal behaviour
# ---------------------------------------------------------------------------

def test_a_recurring_task_is_never_stagnant(review):
    # A weekly chore that is perpetually open is doing exactly what it was
    # set up to do. Counting it would fill triage with what's working.
    review.tasks(a_task(uuid="a", recur="weekly"))
    review.blocks(
        a_block("a", at(0, 9), 60),
        a_block("a", at(1, 9), 60),
        a_block("a", at(2, 9), 60),
    )
    assert stagnant_for(review) == []


def test_a_recurring_instance_is_recognized_by_its_parent(review):
    review.tasks(a_task(uuid="a", parent="template-uuid"))
    review.blocks(a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60))
    assert stagnant_for(review) == []


def test_the_stagnation_coverage_says_recurring_ones_were_excluded(review):
    review.tasks(a_task(uuid="a", recur="weekly"), a_task(uuid="b"))
    section = data(review, stagnation_section.KEY)
    (coverage,) = section.coverage

    assert (coverage.observed, coverage.total) == (1, 2)
    assert "recurring ones excluded" in coverage.label


def test_recurring_completions_are_reported_separately(review):
    from task_gcal.review.metrics import throughput

    review.tasks(
        a_task(uuid="a", status="completed", end=at(0, 11), recur="weekly"),
        a_task(uuid="b", status="completed", end=at(1, 11)),
    )
    section = data(review, throughput.KEY)

    assert section.data["completed"] == 2
    assert section.data["recurring"] == 1
    assert "1 were recurring" in "\n".join(section.detail)


def test_a_normal_task_is_not_treated_as_recurring(review):
    review.tasks(a_task(uuid="a"))
    review.blocks(a_block("a", at(0, 9), 60), a_block("a", at(1, 9), 60))
    assert len(stagnant_for(review)) == 1


# ---------------------------------------------------------------------------
# Pattern discovery
# ---------------------------------------------------------------------------

def test_patterns_cut_by_project_size_and_time_of_day(review, isolated_journal):
    for i in range(8):
        reflections.append(
            a_reflection(
                episode=f"a@{i}",
                task_uuid="big",
                reason="avoided",
                covers_until=at(0, 20),  # an evening
            )
        )
    review.tasks(a_task(uuid="big", project="fraud", estimate=240))

    text = "\n".join(data(review, friction.KEY).detail)
    assert "project      8 x avoided in fraud" in text
    assert "task size    8 x avoided in large" in text
    assert "time of day  8 x avoided in evening" in text


def test_patterns_report_the_meeting_load_as_the_denominator(
    review, isolated_journal
):
    for i in range(8):
        reflections.append(a_reflection(episode=f"a@{i}", reason="capacity"))
    review.tasks(a_task(uuid="a"))
    review.meetings(*[(at(day, 9), at(day, 15)) for day in range(5)])

    text = "\n".join(data(review, friction.KEY).detail)
    assert "meeting load" in text
    assert "denominator" in text


def test_a_task_with_no_estimate_is_its_own_size_bucket(review, isolated_journal):
    for i in range(8):
        reflections.append(a_reflection(episode=f"a@{i}", reason="blocked"))
    review.tasks(a_task(uuid="a", estimate=None))

    text = "\n".join(data(review, friction.KEY).detail)
    assert "no estimate" in text
