"""Tests for the deeper review sections and the triage list.

These are the metrics most able to mislead, so most of what's checked here is
restraint: churn described as *observed*, narrowing overrides not counted as
erosion, a block count reported without a cause attached, and no answer
carrying a reward or a penalty.
"""

from __future__ import annotations

from datetime import timedelta

from task_gcal import reflections
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
from task_gcal.review.stagnation import find, prescribe

from conftest import (
    NOW,
    a_block,
    a_field_change,
    a_task,
    at,
    due_moved,
    estimate_changed,
)

SETTINGS = Settings(timezone="UTC")
FRIDAY = NOW + timedelta(days=4, hours=6)


def redacted_title(uuid, *, at_when, digest):
    """What a rename looks like under `journal_detail = "minimal"`."""
    return a_field_change(
        at=at_when,
        uuid=uuid,
        field="description",
        old="sha256:000000000000",
        new=f"sha256:{digest}",
        op_id=99,
    )


def data(review, key):
    section = review.review().section(key)
    assert section is not None
    return section


# ---------------------------------------------------------------------------
# Deadlines / the promise ledger
# ---------------------------------------------------------------------------

def test_tasks_are_named_from_taskwarrior_rather_than_from_history(review):
    # The change history may be storing digests instead of titles, so a report
    # that took names from it would be a wall of hashes. Taskwarrior always
    # has the current title, in the clear.
    review.tasks(a_task(uuid="a", description="Prepare PIR", due=at(4, 17)))
    review.changes(
        due_moved("a", at=at(1, 9), frm=at(2, 17), to=at(4, 17)),
        redacted_title("a", at_when=at(1, 10), digest="abcdef123456"),
    )

    detail = "\n".join(data(review, deadlines.KEY).detail)
    assert "Prepare PIR" in detail
    assert "sha256" not in detail

def test_a_push_is_counted_and_measured_in_days(review):
    review.tasks(a_task(uuid="a", due=at(4, 17)))
    review.changes(due_moved("a", at(1, 9), at(1, 17), at(4, 17)))
    section = data(review, deadlines.KEY)

    assert section.data["observed_pushes"] == 1
    assert section.data["tasks_pushed"] == 1
    assert section.data["days_pushed"] == 3.0


def test_the_period_coverage_of_the_change_history_is_reported(review):
    # The numbers are exact now, so what needs saying is *reach*: whether the
    # harvested history actually covers the period being reported on.
    review.tasks(a_task(uuid="a", due=at(4, 17)))
    review.changes(due_moved("a", at(1, 9), at(1, 17), at(4, 17)))
    (_due, coverage) = data(review, deadlines.KEY).coverage

    assert "harvested change history" in coverage.label
    assert coverage.observed == 0  # history starts mid-period here
    assert "starts" in "\n".join(data(review, deadlines.KEY).detail)


def test_reactive_and_proactive_pushes_are_separated(review):
    review.tasks(a_task(uuid="late", due=at(4, 17)), a_task(uuid="early", due=at(6, 17)))
    review.changes(
        # Monday's deadline moved on Wednesday: a miss being reported.
        due_moved("late", at(2, 9), at(0, 17), at(4, 17)),
        # Friday's deadline moved on Monday: a commitment renegotiated.
        due_moved("early", at(0, 9), at(4, 17), at(6, 17)),
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
    review.changes(due_moved("a", at(1, 9), at(1, 17), at(4, 17)))
    section = data(review, deadlines.KEY)

    assert section.data["met_final"] == 1
    assert section.data["met_original"] == 0


def test_being_late_on_a_date_that_never_moved_is_counted(review):
    # The pushes are the visible failure mode; reporting only them would
    # miss every task simply finished late.
    review.tasks(
        a_task(uuid="a", status="completed", due=at(1, 17), end=at(3, 16))
    )
    # Some history exists, but none of it is a push for this task, so its
    # date genuinely never moved.
    review.changes(
        a_field_change(at=at(0, 9), uuid="a", field="description",
                       old="old name", new="a task"),
    )
    section = data(review, deadlines.KEY)

    assert section.data["late_on_unchanged_date"] == 1
    assert section.data["observed_pushes"] == 0


def test_a_third_push_becomes_the_reviews_closing_line(review):
    review.tasks(a_task(uuid="a", description="Prepare PIR", due=at(6, 17)))
    review.changes(
        a_field_change(at=at(0, 8), uuid="a", field="description",
                       old=None, new="Prepare PIR"),
        due_moved("a", at(1, 9), at(0, 17), at(1, 17)),
        due_moved("a", at(2, 9), at(1, 17), at(2, 17)),
        due_moved("a", at(3, 9), at(2, 17), at(6, 17)),
    )
    adjustment = review.review().adjustment
    assert "Prepare PIR" in adjustment
    assert "3rd time" in adjustment


def test_the_closing_line_says_when_it_is_repeating_itself(review):
    # The heaviest finding wins every week, so left alone the closing line
    # becomes furniture — and it's the only line with any authority. Positions
    # are derived from the period rather than hard-coded: three pushes that
    # had all happened by the end of last week, but not by the end of the week
    # before, means this is the second week running.
    period = review.period()
    already = period.shifted(-1).end - timedelta(hours=1)
    review.tasks(a_task(uuid="a", description="Prepare PIR", due=at(30, 17)))
    review.changes(
        *[
            due_moved("a", at=already, frm=at(n, 17), to=at(n + 10, 17))
            for n in (0, 10, 20)
        ],
        due_moved("a", at=period.start, frm=at(20, 17), to=at(30, 17)),
    )
    adjustment = review.review().adjustment

    assert "Prepare PIR" in adjustment
    assert "2nd week running that I've closed with this" in adjustment


def test_a_finding_that_is_new_this_week_is_said_plainly(review):
    # No "you have been told this before" when nobody has been told: all three
    # pushes happened inside this period.
    period = review.period()
    review.tasks(a_task(uuid="a", description="Prepare PIR", due=at(30, 17)))
    review.changes(
        *[
            due_moved("a", at=period.start, frm=at(n, 17), to=at(n + 10, 17))
            for n in (0, 10, 20)
        ]
    )
    adjustment = review.review().adjustment

    assert "Prepare PIR" in adjustment
    assert "running" not in adjustment


def test_deadlines_are_unmeasured_without_any_history(review):
    review.tasks(a_task(uuid="a"))
    assert data(review, deadlines.KEY).measured is False


def test_no_change_history_says_so_rather_than_reporting_nothing_moved(review):
    review.tasks(a_task(uuid="a", status="completed", due=at(4, 17), end=at(3, 16)))
    assert "No task-change history yet" in "\n".join(
        data(review, deadlines.KEY).detail
    )


def test_a_task_with_no_journal_history_falls_back_to_its_current_date(review):
    # With nothing observed, the only promise we know about is the current
    # one — claiming the original was missed would be inventing history.
    review.tasks(
        a_task(uuid="a", status="completed", due=at(4, 17), end=at(3, 16))
    )
    review.changes(due_moved("b", at(0, 9), at(1, 17), at(2, 17)))
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
    review.changes(
        a_field_change(at=at(-1, 9), uuid="a", field="description",
                       old=None, new="Analyze peakon"),
        estimate_changed("a", at(1, 9), 60, 240),
    )
    section = data(review, scope.KEY)

    assert section.data["estimates_up"] == 1
    assert section.data["doubled"][0]["to_minutes"] == 240
    # Stagnation sees the same growth and outranks this, so check the
    # section's own proposal rather than which one the review closed on.
    assert "split it" in section.suggestions[0].text


def test_deferral_is_reported_apart_from_deadline_churn(review):
    review.tasks(a_task(uuid="a", scheduled=at(4, 9)))
    review.changes(
        a_field_change(at=at(1, 9), uuid="a", field="scheduled",
                       old=at(1, 9), new=at(4, 9)),
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
    review.tasks(a_task(uuid="a", overrides="work_end_hour=21", entry=at(0, 9)))
    section = data(review, boundaries.KEY)

    assert section.data["widening_overrides"] == 1
    assert section.data["capacity_bought_minutes"] == 180


def test_an_override_from_another_period_is_not_counted(review):
    # The UDA carries no date, so counting it across all of Taskwarrior would
    # put an all-time total inside a per-period section — and let a project
    # that bought an evening a year ago become this week's recommendation.
    review.tasks(
        a_task(
            uuid="old",
            overrides="work_end_hour=21",
            status="completed",
            entry=at(-90, 9),
            end=at(-60, 9),
        )
    )
    assert data(review, boundaries.KEY).data["widening_overrides"] == 0


def test_an_override_on_a_task_with_a_block_this_period_is_counted(review):
    review.tasks(
        a_task(uuid="a", overrides="work_end_hour=21", entry=at(-90, 9))
    )
    review.blocks(a_block("a", at(0, 19), 60))
    assert data(review, boundaries.KEY).data["widening_overrides"] == 1


def test_a_narrowing_override_is_not_erosion(review):
    # The largest single group in the real data narrows the window. Counting
    # overrides would report it as a boundary violation; classifying intent
    # reports it as the opposite.
    review.tasks(a_task(uuid="a", overrides="work_days=4", entry=at(0, 9)))
    section = data(review, boundaries.KEY)

    assert section.data["narrowing_overrides"] == 1
    assert section.data["widening_overrides"] == 0


def test_a_density_override_is_neither(review):
    review.tasks(a_task(uuid="a", overrides="buffer_minutes=0", entry=at(0, 9)))
    section = data(review, boundaries.KEY)

    assert section.data["density_overrides"] == 1
    assert section.data["widening_overrides"] == 0


def test_an_overdue_horizon_override_belongs_to_deadlines_not_here(review):
    # Extending a deadline's tolerance is deferral, not boundary loss.
    review.tasks(a_task(uuid="a", overrides="overdue_horizon_days=60", entry=at(0, 9)))
    section = data(review, boundaries.KEY)
    assert section.data["widening_overrides"] == 0
    assert section.data["narrowing_overrides"] == 0


def test_a_weekend_adding_override_is_widening(review):
    review.tasks(a_task(uuid="a", overrides="work_days=0,1,2,3,4,5", entry=at(0, 9)))
    assert data(review, boundaries.KEY).data["widening_overrides"] == 1


def test_the_project_that_took_the_weekend_is_named(review):
    review.tasks(
        a_task(uuid="a", project="PIR", overrides="work_end_hour=21",
               entry=at(0, 9)),
        a_task(uuid="b", project="PIR", overrides="work_end_hour=21",
               entry=at(1, 9)),
    )
    review.whole_week().blocks(a_block("a", at(5, 10), 90))
    section = data(review, boundaries.KEY)

    assert section.data["paid_for_by"] == {"PIR": 2}
    assert "PIR" in review.review().adjustment
    assert "not your weekend" in review.review().adjustment


def test_a_malformed_override_does_not_crash_the_metric(review):
    review.tasks(a_task(uuid="a", overrides="work_end_hour=elevenses", entry=at(0, 9)))
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


def test_time_used_is_compared_with_the_block_not_the_estimate(review, isolated_journal):
    # None of the offered answers means "the task is finished", so an actual
    # can't say whether an estimate was right. It can say whether the time
    # booked got used, which is a different and answerable question.
    reflections.append(
        a_reflection(
            episode="a@1",
            outcome="partial",
            actual_minutes=20,
            planned_minutes=60,
        )
    )
    review.tasks(a_task(uuid="a", estimate=240))

    text = "\n".join(data(review, friction.KEY).detail)
    assert "20m of the 1h set aside" in text
    assert "33%" in text
    assert "says nothing about the estimates" in text


def test_an_actual_with_no_block_length_is_not_a_share(review, isolated_journal):
    # Older records predate `planned_minutes`; a share needs both halves.
    reflections.append(
        a_reflection(episode="a@1", outcome="partial", actual_minutes=20)
    )
    review.tasks(a_task(uuid="a", estimate=60))

    assert "no actuals recorded" in "\n".join(data(review, friction.KEY).detail)


def test_a_planned_continuation_is_not_counted_as_friction(review, isolated_journal):
    # Work that always needed another sitting isn't friction, and counting it
    # would make good planning look like a problem.
    reflections.append(
        a_reflection(episode="a@1", outcome="progressed", reason="follow_up")
    )
    reflections.append(
        a_reflection(episode="a@2", outcome="not_started", reason="capacity")
    )
    review.tasks(a_task(uuid="a"))

    section = data(review, friction.KEY)
    assert section.data["reasons"] == {"capacity": 1}
    assert section.data["continuations"] == 1
    assert "not friction" in "\n".join(section.detail)


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
        timelines=facts.timelines,
        blocks_by_task=facts.blocks_by_task(),
        now=facts.now,
    )


def test_repeated_pushes_make_a_task_stagnant(review):
    review.tasks(a_task(uuid="a", due=at(6, 17)))
    review.changes(
        due_moved("a", at(1, 9), at(0, 17), at(1, 17)),
        due_moved("a", at(2, 9), at(1, 17), at(2, 17)),
        due_moved("a", at(3, 9), at(2, 17), at(6, 17)),
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
    review.changes(
        due_moved("a", at(1, 9), at(0, 17), at(1, 17)),
        due_moved("a", at(2, 9), at(1, 17), at(2, 17)),
        due_moved("a", at(3, 9), at(2, 17), at(6, 17)),
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
    assert "1 was a recurring task" in "\n".join(section.detail)


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


def test_the_closing_line_says_which_section_raised_it(review):
    # `adjustment` is the finding as a sentence. `closing` is the same finding
    # with the two facts a sentence has to throw away: where it came from, and
    # how many periods it has already been true for. A renderer that wants to
    # link a finding to its own evidence needs the first; one that wants to set
    # the repetition clause apart from the finding needs the second, and used to
    # get it by searching the sentence for a substring.
    review.tasks(a_task(uuid="a", description="Prepare PIR", due=at(30, 17)))
    review.changes(
        *[
            due_moved("a", at=review.period().start, frm=at(n, 17), to=at(n + 10, 17))
            for n in (0, 10, 20)
        ]
    )
    built = review.review()

    origin, suggestion = built.closing
    assert origin == "dates"
    assert "Prepare PIR" in suggestion.text
    # And the sentence is still exactly the sentence, for whoever wants one.
    assert built.adjustment.startswith(suggestion.text)


def test_a_review_with_nothing_to_say_has_no_closing_finding(review):
    assert review.review().closing is None
    assert review.review().adjustment is None
    assert review.review().repetition_note() == ""


def test_the_repetition_clause_is_available_on_its_own(review):
    # One wording, in one place, so no renderer has to reconstruct it.
    period = review.period()
    already = period.shifted(-1).end - timedelta(hours=1)
    review.tasks(a_task(uuid="a", description="Prepare PIR", due=at(30, 17)))
    review.changes(
        *[
            due_moved("a", at=already, frm=at(n, 17), to=at(n + 10, 17))
            for n in (0, 10, 20)
        ],
        due_moved("a", at=period.start, frm=at(20, 17), to=at(30, 17)),
    )
    built = review.review()

    note = built.repetition_note()
    assert note == "This is the 2nd week running that I've closed with this."
    # The sentence is the finding plus the clause, and nothing else.
    _origin, suggestion = built.closing
    assert built.adjustment == f"{suggestion.text} {note}"


def test_a_denominator_can_name_the_figure_it_qualifies(review):
    # "6 of 7 completed tasks had an observed block" is a statement about one
    # figure, not about the whole section. Carrying that link means a renderer
    # can print the two together instead of putting the denominator in a
    # footnote several inches from the number it warrants.
    review.tasks(
        a_task(uuid="a", status="completed", end=at(0, 10)),
        a_task(uuid="b", status="completed", end=at(1, 10)),
    )
    review.blocks(a_block("a", at(0, 9), 60))
    section = review.review().section("sittings")

    qualified = [c for c in section.coverage if c.qualifies]
    assert qualified, "sittings should attach its denominator to a figure"
    # And the row it names is a row the section actually prints.
    for cover in qualified:
        assert any(line.startswith(cover.qualifies) for line in section.detail)
