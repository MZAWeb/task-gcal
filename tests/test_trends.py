"""Tests for the trend series and its sparklines.

The rule that matters most: do not compare incompatible periods. A settings
or definition change means the same field measured two different things, so
the trend has to annotate the boundary and refuse to average across it.
"""

from __future__ import annotations

from dataclasses import replace

from task_gcal import journal
from task_gcal.config import Settings
from task_gcal.review.metrics import trends
from task_gcal.review.render.charts import sparklines

from conftest import a_block, a_task, at

SETTINGS = Settings(timezone="UTC")


def snapshot(when, *, settings=SETTINGS):
    return journal.build_record(
        settings=settings,
        mode=journal.MODE_SCHEDULE,
        at=when,
        observations=(),
    )


def completions(week: int, count: int):
    """`count` tasks completed in the week `week` weeks before NOW's."""
    day = -7 * week
    return [
        a_task(
            uuid=f"w{week}-{i}",
            status="completed",
            end=at(day + (i % 5), 11),
            estimate=60,
            entry=at(day - 10, 9),
        )
        for i in range(count)
    ]


def monthly(review):
    review.kind = "month"
    return review


def section(review):
    result = monthly(review).review().section(trends.KEY)
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# The sparkline
# ---------------------------------------------------------------------------

def test_a_sparkline_scales_from_zero_not_from_the_minimum():
    # A flat-but-high series must not look like a flat-but-low one.
    assert trends.sparkline([8.0, 8.0, 8.0]) == "███"
    # An eighth of the peak sits near the bottom, not at the minimum.
    assert trends.sparkline([1.0, 1.0, 8.0]) == "▂▂█"


def test_a_sparkline_shows_the_shape():
    bars = trends.sparkline([0.0, 4.0, 8.0])
    assert bars[0] < bars[1] < bars[2]


def test_an_all_zero_series_is_flat_but_present():
    # Blanks would read as missing data, which is a different claim.
    assert trends.sparkline([0.0, 0.0, 0.0]) == "▁▁▁"


def test_a_missing_value_is_a_gap():
    assert trends.sparkline([4.0, None, 8.0]) == "▅ █"


def test_an_entirely_missing_series_renders_as_nothing():
    assert trends.sparkline([None, None]) == ""


# ---------------------------------------------------------------------------
# The series
# ---------------------------------------------------------------------------

def test_twelve_weeks_are_reported_oldest_first(review):
    monthly(review).tasks(*completions(3, 5))
    points = trends.series(review.facts())

    assert len(points) == trends.WEEKS
    assert points[-1].label == "Week 37"  # the week the period ends in
    assert points[-4].completed == 5


def test_a_past_week_review_ends_its_trend_on_that_week(review):
    # The period's end is exclusive, so naive `week_of(end)` lands a week
    # late: it would drop the oldest week and add one outside the period.
    review.offset = 1
    points = trends.series(review.facts())
    assert points[-1].label == review.period().label


def test_a_past_month_review_does_not_end_on_a_week_outside_it(review):
    # August's last day is a Monday, so the week containing it runs to 6 Sep.
    # Ending the trend there would measure a week with real completions but
    # no blocks and no pushes, reading as a productive week that was
    # entirely missed.
    monthly(review).offset = 1
    period = review.period()
    last = trends.series(review.facts())[-1]

    assert period.label == "August 2026"
    assert last.label == "Week 35"


def test_an_in_progress_period_keeps_its_own_partial_week(review):
    # It's partial by definition, and it's the week being reported on.
    monthly(review)
    points = trends.series(review.facts())
    assert points[-1].label == "Week 37"
    assert review.period().in_progress is True


def test_a_finished_periods_trend_never_reaches_past_it(review):
    # The invariant behind both cases above: blocks and journal records are
    # only loaded up to the period's end, so a week extending past it would
    # be scored with inputs that stop halfway through.
    for kind, offset in (("week", 1), ("month", 1), ("week", 3), ("month", 2)):
        review.kind, review.offset = kind, offset
        period = review.period()
        assert not period.in_progress, (kind, offset)
        assert trends._anchor(period).end <= period.end, (kind, offset)


def test_completions_land_in_the_right_week(review):
    monthly(review).tasks(*completions(1, 2), *completions(5, 7))
    by_label = {p.label: p.completed for p in trends.series(review.facts())}

    assert by_label["Week 36"] == 2
    assert by_label["Week 32"] == 7


def test_follow_through_is_a_rate_or_nothing(review):
    monthly(review).tasks(
        a_task(uuid="done", status="completed", end=at(-7, 9, 30)),
        a_task(uuid="open"),
    )
    review.blocks(
        a_block("done", at(-7, 9), 60), a_block("open", at(-7, 11), 60)
    )
    points = {p.label: p for p in trends.series(review.facts())}

    assert points["Week 36"].follow_through == 0.5
    # A week with no blocks has no rate, rather than a rate of zero.
    assert points["Week 30"].follow_through is None


def test_a_trend_needs_more_than_one_period(review, monkeypatch):
    monkeypatch.setattr(trends, "WEEKS", 1)
    assert len(trends.series(monthly(review).facts())) == 1
    assert section(review).measured is False


# ---------------------------------------------------------------------------
# Definition boundaries
# ---------------------------------------------------------------------------

def test_all_weeks_are_comparable_under_one_definition(review, isolated_journal):
    monthly(review).tasks(*completions(2, 4))
    review.records(*[snapshot(at(-7 * w, 12)) for w in range(6)])

    result = section(review)
    assert result.data["comparable_from"] is None
    assert "definition changed" not in result.summary


def test_a_settings_change_marks_the_earlier_weeks_incomparable(
    review, isolated_journal
):
    monthly(review).tasks(*completions(2, 4))
    changed = replace(SETTINGS, work_end_hour=21)
    review.records(
        *[snapshot(at(-7 * w, 12), settings=changed) for w in range(4, 8)],
        *[snapshot(at(-7 * w, 12)) for w in range(0, 4)],
    )

    result = section(review)
    # Weeks 8-11 back have no observations at all, so they can't disagree
    # with anything — but the change five weeks ago still interrupts the run,
    # and the trend can only claim the tail after it.
    assert result.data["comparable_from"] == "Week 34"
    assert "definition changed mid-trend" in result.summary
    (coverage,) = result.coverage
    assert coverage.observed < coverage.total


def test_the_median_ignores_the_incomparable_weeks(review, isolated_journal):
    # Averaging across a boundary produces a number that describes nothing.
    monthly(review).tasks(*completions(1, 2), *completions(6, 40))
    changed = replace(SETTINGS, work_end_hour=21)
    review.records(
        *[snapshot(at(-7 * w, 12), settings=changed) for w in range(4, 9)],
        *[snapshot(at(-7 * w, 12)) for w in range(0, 4)],
    )

    result = section(review)
    assert result.data["median_completed"] < 40


def test_a_week_with_no_observations_stays_comparable(review, isolated_journal):
    # It can't disagree with the current definition, so excluding it for
    # missing metadata would throw away usable history.
    monthly(review).tasks(*completions(2, 4))
    review.records(snapshot(at(0, 12)))

    result = section(review)
    (coverage,) = result.coverage
    assert coverage.observed == coverage.total


# ---------------------------------------------------------------------------
# Where it appears
# ---------------------------------------------------------------------------

def test_a_month_summary_includes_the_trend(review):
    monthly(review).tasks(*completions(2, 4))
    assert "Trend" in review.render("terminal")


def test_a_week_summary_does_not(review):
    review.tasks(*completions(2, 4))
    assert "Trend" not in review.render("terminal")


def test_a_week_can_still_ask_for_it(review):
    review.tasks(*completions(2, 4))
    assert "Trend" in review.render("terminal", sections=("trends",))


def test_the_terminal_draws_block_characters(review):
    monthly(review).tasks(*completions(2, 4))
    out = review.render("terminal", sections=("trends",))
    assert any(bar in out for bar in "▁▂▃▄▅▆▇█")


def test_the_html_draws_svg_instead_of_block_characters(review):
    # The same characters render in a browser as a row of solid boxes.
    monthly(review).tasks(*completions(2, 4))
    out = review.render("html", sections=("trends",))

    assert "<polyline" in out
    assert not any(bar in out for bar in "▁▂▃▄▅▆▇█")


def polyline_points(html: str) -> int:
    import re

    (points,) = re.findall(r'points="([^"]+)"', html)[:1]
    return len(points.split())


def test_the_html_plots_only_the_comparable_tail(review, isolated_journal):
    monthly(review).tasks(*completions(2, 4))
    review.records(*[snapshot(at(-7 * w, 12)) for w in range(12)])
    unbroken = polyline_points(review.render("html", sections=("trends",)))

    changed = replace(SETTINGS, work_end_hour=21)
    review.records(
        *[snapshot(at(-7 * w, 12), settings=changed) for w in range(6, 12)],
        *[snapshot(at(-7 * w, 12)) for w in range(0, 6)],
    )
    after_change = polyline_points(review.render("html", sections=("trends",)))

    assert unbroken == trends.WEEKS
    assert after_change == 6  # only the weeks since the change


# ---------------------------------------------------------------------------
# The SVG itself
# ---------------------------------------------------------------------------

def test_an_all_zero_series_does_not_claim_a_peak_of_one():
    out = sparklines([("Pushes", [0.0, 0.0, 0.0], "")], labels=["a", "b", "c"])
    assert "peak 0" in out


def test_a_series_reports_its_real_peak():
    out = sparklines([("Done", [1.0, 9.0, 4.0], "")], labels=["a", "b", "c"])
    assert "peak 9" in out
    assert "1 to 4" in out


def test_an_empty_series_draws_nothing():
    assert sparklines([("Done", [], "")], labels=[]) == ""


def test_each_series_gets_its_own_chart():
    # Two measures of different scale never share a y axis.
    out = sparklines(
        [("Done", [1.0, 2.0], ""), ("Rate", [50.0, 90.0], "%")],
        labels=["a", "b"],
    )
    assert out.count("<svg") == 2


def test_the_charts_are_well_formed_xml():
    import re
    from xml.etree import ElementTree

    out = sparklines([("Done", [1.0, 9.0, 4.0], "")], labels=["a", "b", "c"])
    for svg in re.findall(r"<svg.*?</svg>", out, re.S):
        ElementTree.fromstring(svg)


def test_the_end_marker_is_at_the_last_value():
    out = sparklines([("Done", [0.0, 10.0], "")], labels=["a", "b"])
    # Last point is the peak, so the marker sits at the top inset.
    assert 'cy="6.0"' in out


def test_a_flat_series_still_draws_a_line():
    out = sparklines([("Done", [3.0, 3.0, 3.0], "")], labels=["a", "b", "c"])
    assert "<polyline" in out


def test_a_single_point_series_does_not_divide_by_zero():
    out = sparklines([("Done", [3.0], "")], labels=["a"])
    assert "<polyline" in out
