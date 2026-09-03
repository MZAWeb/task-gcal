"""Tests for the metric functions.

Facts are assembled by hand, so nothing here touches the calendar,
Taskwarrior, or the journal. The `review` fixture's clock is Friday 15:00 of
NOW's week, so the period under test is a week in progress — the awkward
case, and the normal one.
"""

from __future__ import annotations


from task_gcal.review.metrics import capacity, flow, followthrough, throughput

from conftest import a_block, a_task, at

# A full working week under the defaults is 5 x 9h; clipped at Friday 15:00
# it is 4 x 9h + 6h = 42h.
WEEK_TO_FRIDAY_AFTERNOON = 42 * 60


def data(review, key):
    section = review.review().section(key)
    assert section is not None
    return section


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------

def test_capacity_counts_only_hours_that_have_happened(review):
    section = data(review, capacity.KEY)
    assert section.data["available_minutes"] == WEEK_TO_FRIDAY_AFTERNOON


def test_capacity_subtracts_meeting_time(review):
    review.meetings((at(0, 9), at(0, 11)), (at(1, 14), at(1, 15)))
    section = data(review, capacity.KEY)

    assert section.data["meeting_minutes"] == 180
    assert section.data["schedulable_minutes"] == WEEK_TO_FRIDAY_AFTERNOON - 180


def test_a_double_booking_costs_one_slot_of_capacity(review):
    review.meetings((at(0, 9), at(0, 10)), (at(0, 9), at(0, 10)))
    assert data(review, capacity.KEY).data["meeting_minutes"] == 60


def test_a_meeting_outside_working_hours_does_not_eat_the_denominator(review):
    # It cost you an evening, which is boundary erosion's business. It did
    # not consume working time, which is this line's denominator.
    review.meetings((at(0, 20), at(0, 22)))
    assert data(review, capacity.KEY).data["meeting_minutes"] == 0


def test_a_meeting_straddling_the_start_of_work_counts_only_its_tail(review):
    review.meetings((at(0, 8), at(0, 10)))
    assert data(review, capacity.KEY).data["meeting_minutes"] == 60


def test_planned_time_is_the_blocks_we_own(review):
    review.blocks(a_block("u1", at(0, 9), 60), a_block("u2", at(1, 11), 90))
    assert data(review, capacity.KEY).data["planned_minutes"] == 150


def test_planning_outside_working_hours_is_tracked_separately(review):
    review.blocks(a_block("u1", at(0, 9), 60), a_block("u2", at(0, 20), 60))
    section = data(review, capacity.KEY)

    assert section.data["planned_minutes"] == 120
    assert section.data["planned_in_hours_minutes"] == 60


def test_capacity_says_so_when_the_calendar_could_not_be_read(review):
    review.calendar_ok = False
    section = data(review, capacity.KEY)

    assert section.measured is False
    assert "not measured" in section.summary


def test_a_meeting_heavy_week_becomes_the_closing_suggestion(review):
    # 5 x 6h of meetings against 42h of working time.
    review.meetings(*[(at(day, 9), at(day, 15)) for day in range(5)])
    assert "meetings" in review.review().adjustment


def test_overplanning_outranks_a_meeting_heavy_week(review):
    # Both findings are true here — 57% meetings, and 20h planned into the
    # 18h that were left. The tasks are completed so follow-through stays
    # quiet, leaving the two capacity findings to be arbitrated by weight
    # rather than by which branch ran first.
    review.meetings(*[(at(day, 9), at(day, 15)) for day in range(4)])
    review.tasks(
        *[
            a_task(uuid=f"u{d}", status="completed", end=at(d, 15, 30))
            for d in range(4)
        ]
    )
    review.blocks(*[a_block(f"u{d}", at(d, 15), 300) for d in range(4)])

    section = data(review, capacity.KEY)
    assert len(section.suggestions) == 2
    assert "planned" in review.review().adjustment


def test_capacity_reports_days_observed(review):
    (coverage,) = data(review, capacity.KEY).coverage
    assert coverage.total == 5  # Monday through Friday so far
    assert coverage.observed == 0


# ---------------------------------------------------------------------------
# Throughput
# ---------------------------------------------------------------------------

def test_throughput_counts_tasks_completed_in_the_period(review):
    review.tasks(
        a_task(uuid="a", status="completed", end=at(0, 11)),
        a_task(uuid="b", status="completed", end=at(2, 16)),
        a_task(uuid="c", status="completed", end=at(-7, 9)),  # last week
        a_task(uuid="d"),  # still open
    )
    section = data(review, throughput.KEY)
    assert section.data["completed"] == 2


def test_throughput_sums_estimates_and_never_calls_them_time_spent(review):
    review.tasks(
        a_task(uuid="a", status="completed", end=at(0, 11), estimate=60),
        a_task(uuid="b", status="completed", end=at(1, 11), estimate=90),
    )
    section = data(review, throughput.KEY)

    assert section.data["planned_minutes"] == 150
    assert "planned" in section.summary
    assert "worked" not in section.summary.lower()
    assert any("not time spent" in line for line in section.detail)


def test_a_missing_estimate_is_missing_not_zero(review):
    review.tasks(
        a_task(uuid="a", status="completed", end=at(0, 11), estimate=60),
        a_task(uuid="b", status="completed", end=at(1, 11), estimate=None),
    )
    (coverage,) = data(review, throughput.KEY).coverage

    assert (coverage.observed, coverage.total) == (1, 2)
    assert coverage.complete is False


def test_throughput_compares_with_the_previous_period(review):
    review.tasks(
        a_task(uuid="a", status="completed", end=at(0, 11)),
        a_task(uuid="p1", status="completed", end=at(-7, 11)),
        a_task(uuid="p2", status="completed", end=at(-6, 11)),
    )
    section = data(review, throughput.KEY)

    assert section.data["previous_completed"] == 2
    assert "-1 vs previous week" in section.summary


def test_throughput_breaks_down_by_project(review):
    review.tasks(
        a_task(uuid="a", status="completed", end=at(0, 11), project="fraud"),
        a_task(uuid="b", status="completed", end=at(1, 11), project="fraud"),
        a_task(uuid="c", status="completed", end=at(2, 11), project=None),
    )
    by_project = data(review, throughput.KEY).data["by_project"]

    assert by_project == {"fraud": 2, "(no project)": 1}


def test_poor_estimate_coverage_becomes_a_suggestion(review):
    review.tasks(
        *[
            a_task(uuid=f"u{i}", status="completed", end=at(0, 11), estimate=None)
            for i in range(4)
        ]
    )
    assert "estimate" in review.review().adjustment


# ---------------------------------------------------------------------------
# Backlog flow
# ---------------------------------------------------------------------------

def test_flow_counts_created_completed_and_deleted(review):
    review.tasks(
        a_task(uuid="new", entry=at(0, 9)),
        a_task(uuid="done", status="completed", entry=at(-30, 9), end=at(1, 9)),
        a_task(uuid="gone", status="deleted", entry=at(-30, 9), end=at(2, 9)),
    )
    section = data(review, flow.KEY_FLOW)

    assert (section.data["created"], section.data["completed"]) == (1, 1)
    assert section.data["deleted"] == 1
    assert section.data["net"] == -1


def test_a_growing_backlog_becomes_a_suggestion(review):
    review.tasks(*[a_task(uuid=f"u{i}", entry=at(0, 9)) for i in range(6)])
    assert "backlog grew" in review.review().adjustment


# ---------------------------------------------------------------------------
# Lead time
# ---------------------------------------------------------------------------

def test_lead_time_reports_the_median_and_the_tail(review):
    review.tasks(
        a_task(uuid="a", status="completed", entry=at(-2, 9), end=at(0, 9)),
        a_task(uuid="b", status="completed", entry=at(-4, 9), end=at(0, 9)),
        a_task(uuid="c", status="completed", entry=at(-40, 9), end=at(0, 9)),
    )
    section = data(review, flow.KEY_LEAD_TIME)

    assert section.data["median_days"] == 4
    assert section.data["longest_days"] == 40


def test_lead_time_with_an_even_sample_averages_the_middle_two(review):
    review.tasks(
        a_task(uuid="a", status="completed", entry=at(-2, 9), end=at(0, 9)),
        a_task(uuid="b", status="completed", entry=at(-4, 9), end=at(0, 9)),
    )
    assert data(review, flow.KEY_LEAD_TIME).data["median_days"] == 3


def test_lead_time_is_unmeasured_with_nothing_completed(review):
    review.tasks(a_task(uuid="a"))
    section = data(review, flow.KEY_LEAD_TIME)

    assert section.measured is False
    assert "nothing completed" in section.summary


def test_a_task_completed_before_it_was_created_is_excluded(review):
    # Clock skew or an edited timestamp; a negative lead time is noise.
    review.tasks(
        a_task(uuid="ok", status="completed", entry=at(-2, 9), end=at(0, 9)),
        a_task(uuid="odd", status="completed", entry=at(1, 9), end=at(0, 9)),
    )
    (coverage,) = data(review, flow.KEY_LEAD_TIME).coverage
    assert (coverage.observed, coverage.total) == (1, 2)


# ---------------------------------------------------------------------------
# Follow-through
# ---------------------------------------------------------------------------

def test_a_block_whose_task_finished_by_its_end_is_honoured(review):
    review.tasks(a_task(uuid="a", status="completed", end=at(0, 9, 45)))
    review.blocks(a_block("a", at(0, 9), 60))
    section = data(review, followthrough.KEY)

    assert (section.data["honored"], section.data["blocks"]) == (1, 1)


def test_a_block_that_passed_with_the_task_open_is_not(review):
    review.tasks(a_task(uuid="a"))
    review.blocks(a_block("a", at(0, 9), 60))
    section = data(review, followthrough.KEY)

    assert section.data["passed_open"] == 1
    assert section.data["rate"] == 0.0


def test_a_task_completed_after_its_block_ended_is_not_honoured(review):
    # Completion is a fact; "done eventually" is not "done as planned".
    review.tasks(a_task(uuid="a", status="completed", end=at(2, 16)))
    review.blocks(a_block("a", at(0, 9), 60))
    assert data(review, followthrough.KEY).data["passed_open"] == 1


def test_a_block_still_running_has_not_been_missed(review):
    review.tasks(a_task(uuid="a"))
    # The fixture's now is Friday 15:00; this block ends at 16:00.
    review.blocks(a_block("a", at(4, 14, 30), 90))
    section = data(review, followthrough.KEY)

    assert section.data["blocks"] == 0
    assert section.measured is False


def test_work_with_no_block_is_reported_as_off_plan(review):
    review.tasks(
        a_task(uuid="planned", status="completed", end=at(0, 9, 30)),
        a_task(uuid="ad-hoc", status="completed", end=at(1, 11)),
    )
    review.blocks(a_block("planned", at(0, 9), 60))
    section = data(review, followthrough.KEY)

    assert section.data["off_plan"] == 1
    assert "1 completed off-plan" in section.summary


def test_the_worst_hour_is_reported(review):
    review.tasks(a_task(uuid="a"), a_task(uuid="b"), a_task(uuid="c"))
    review.blocks(
        a_block("a", at(0, 16), 60),
        a_block("b", at(1, 16), 60),
        a_block("c", at(2, 9), 60),
    )
    section = data(review, followthrough.KEY)

    assert section.data["passed_open_by_hour"] == {9: 1, 16: 2}
    assert "16:00" in "\n".join(section.detail)


def test_a_repeatedly_failing_hour_becomes_the_suggestion(review):
    review.tasks(a_task(uuid="a"), a_task(uuid="b"), a_task(uuid="c"))
    review.blocks(
        a_block("a", at(0, 16), 60),
        a_block("b", at(1, 16), 60),
        a_block("c", at(2, 16), 60),
    )
    assert "16:00" in review.review().adjustment


def test_follow_through_is_unmeasured_without_the_calendar(review):
    review.calendar_ok = False
    assert data(review, followthrough.KEY).measured is False


def test_a_block_for_a_task_we_can_no_longer_find_is_still_counted(review):
    # A purged task leaves its blocks behind. They passed with nothing to
    # show for them, and hiding that would flatter the rate.
    review.blocks(a_block("vanished", at(0, 9), 60))
    section = data(review, followthrough.KEY)

    assert section.data["blocks"] == 1
    assert section.data["passed_open"] == 1
    (coverage,) = section.coverage
    assert (coverage.observed, coverage.total) == (0, 1)


def test_one_task_with_several_blocks_counts_each_block(review):
    # Blocks-to-completion reports the count without guessing whether it
    # means a bad estimate, an interruption, or deliberate multi-session work.
    review.tasks(a_task(uuid="a", status="completed", end=at(2, 11)))
    review.blocks(
        a_block("a", at(0, 9), 60),
        a_block("a", at(1, 9), 60),
        a_block("a", at(2, 10), 60),
    )
    section = data(review, followthrough.KEY)

    assert section.data["blocks"] == 3
    assert section.data["honored"] == 1
    assert section.data["passed_open"] == 2


# ---------------------------------------------------------------------------
# The report as a whole
# ---------------------------------------------------------------------------

def test_sections_come_back_with_capacity_first(review):
    keys = [s.key for s in review.review().sections]
    assert keys[0] == capacity.KEY


def test_a_review_with_nothing_in_it_has_no_adjustment(review):
    # No suggestion beats an invented one.
    assert review.review().adjustment is None


def test_the_heaviest_suggestion_wins(review):
    review.tasks(a_task(uuid="a"), a_task(uuid="b"), a_task(uuid="c"))
    review.blocks(
        a_block("a", at(0, 16), 60),
        a_block("b", at(1, 16), 60),
        a_block("c", at(2, 16), 60),
    )
    review.meetings(*[(at(day, 9), at(day, 15)) for day in range(5)])
    # Follow-through's hour finding (weight 3.0) over meeting load (2.0).
    assert "16:00" in review.review().adjustment


def test_every_section_key_is_registered(review):
    from task_gcal.review import metrics

    keys = {s.key for s in review.review().sections}
    assert keys == set(metrics.section_keys())


def test_a_month_period_is_supported(review):
    review.kind = "month"
    assert review.review().period.kind == "month"
    assert "2026" in review.review().period.label


def test_looking_back_a_period_reports_the_completed_one(review):
    review.offset = 1
    review.tasks(a_task(uuid="a", status="completed", end=at(-6, 11)))
    assert data(review, throughput.KEY).data["completed"] == 1
    assert review.review().period.in_progress is False
