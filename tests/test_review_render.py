"""Tests for the four renderers.

They share one job: read a `Review`, compute nothing. So the tests check the
properties that hold across all of them (nothing invented, nothing dropped,
coverage always travels with the numbers) plus the few per-format contracts
worth pinning — one screen for terminal, one file with no network for HTML,
primitives only for JSON.
"""

from __future__ import annotations

import json
import re
from xml.etree import ElementTree

import pytest

from task_gcal.review.render import FORMATS, render

from html import escape

from conftest import a_block, a_task, at


@pytest.fixture
def populated(review):
    """A review with something to say in every section."""
    review.tasks(
        a_task(uuid="a", status="completed", end=at(0, 9, 30), project="fraud"),
        a_task(uuid="b", status="completed", end=at(1, 16), project="fraud"),
        a_task(uuid="c", status="completed", end=at(2, 11), project="monitoring"),
        a_task(uuid="d", entry=at(0, 8)),
    )
    review.blocks(
        a_block("a", at(0, 9), 60),
        a_block("b", at(1, 9), 90),
        a_block("d", at(2, 10), 60),
    )
    review.meetings((at(0, 11), at(0, 13)), (at(1, 14), at(1, 17)))
    return review


# ---------------------------------------------------------------------------
# Properties every renderer has to hold
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", FORMATS)
def test_every_format_names_the_period(populated, fmt):
    assert "Week 37" in populated.render(fmt)


@pytest.mark.parametrize("fmt", FORMATS)
def test_every_format_carries_every_section_it_was_given(populated, fmt):
    from task_gcal.review.metrics import summary_sections

    out = populated.render(fmt)
    for section in summary_sections(populated.review().sections):
        if section.measured or fmt != "terminal":
            assert section.label in out, section.key


@pytest.mark.parametrize("fmt", FORMATS)
def test_the_default_is_the_headline_sections_not_all_of_them(populated, fmt):
    # Detailed metrics are requested by section rather than always printed —
    # otherwise the report is the dashboard we set out not to build.
    out = populated.render(fmt)
    assert "Lead time" not in out
    assert "Time" in out


@pytest.mark.parametrize("fmt", FORMATS)
def test_every_format_survives_an_empty_review(review, fmt):
    # A fresh install with no tasks, no calendar and no journal must not
    # raise; it should just have little to say.
    out = review.render(fmt)
    assert out.strip()


@pytest.mark.parametrize("fmt", FORMATS)
def test_no_format_invents_an_adjustment(review, fmt):
    assert "Look at" not in review.render(fmt)


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_section_filter_narrows_every_format(populated, fmt):
    out = populated.render(fmt, sections=("time",))
    assert "Time" in out
    assert "Lead time" not in out


def test_an_unknown_format_is_refused(populated):
    with pytest.raises(ValueError, match="unknown format"):
        render(populated.review(), fmt="pdf")


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

def test_the_default_terminal_report_fits_one_screen(populated):
    lines = populated.render("terminal").splitlines()
    assert len(lines) <= 24, "\n".join(lines)


def test_every_section_starts_a_line_of_its_own_in_the_column(populated):
    from task_gcal.review.metrics import summary_sections

    out = populated.render("terminal")
    body = out.split("\n\n")[1].splitlines()
    shown = [
        s for s in summary_sections(populated.review().sections) if s.measured
    ]
    # A summary is a sentence now, so a long one wraps under its own label —
    # but every section still begins a line, and no other line does.
    starts = [line for line in body if line[:1].strip()]
    assert len(starts) == len(shown)
    for section, line in zip(shown, starts):
        assert line.startswith(section.label)


def test_an_unmeasurable_section_is_named_rather_than_given_a_line(populated):
    # It costs a line and says nothing; the budget is one screen.
    out = populated.render("terminal")
    assert "Dates          no deadline history" not in out
    assert "Not measured" in out
    assert "Dates" in out


def test_asking_for_an_unmeasurable_section_still_shows_it(populated):
    # "There's no data for this" is the answer to the question that was asked.
    out = populated.render("terminal", sections=("dates",))
    assert "Dates" in out
    assert "not measured" in out


def test_terminal_detail_keeps_its_column_alignment(populated):
    # The metric builders pad their labels; re-wrapping would collapse the
    # padding and turn a readable column into a paragraph.
    out = populated.render("terminal", sections=("time",))
    assert "  Working hours     " in out


def test_terminal_detail_is_only_shown_when_asked_for(populated):
    assert "Working hours" not in populated.render("terminal")


def test_the_terminal_report_ends_with_the_adjustment(populated):
    populated.meetings(*[(at(day, 9), at(day, 15)) for day in range(5)])
    out = populated.render("terminal").rstrip().splitlines()
    assert "Look at:" in "\n".join(out[-3:])


def test_terminal_prose_is_wrapped_but_columns_are_not(populated):
    out = populated.render("terminal", sections=("time",))
    assert all(len(line) <= 80 for line in out.splitlines())


def test_a_wrapped_entry_keeps_the_indentation_of_its_neighbours():
    # A long entry in a nested list used to start two columns to the left of
    # the short ones, which reads as a different list rather than a longer item.
    from task_gcal.review.render import terminal

    short, long = "  2x  a short one", "  3x  " + "a very long title " * 6
    lines = terminal._fit(short, indent="  ") + terminal._fit(long, indent="  ")

    entries = [line for line in lines if line.lstrip().startswith(("2x", "3x"))]
    assert {len(e) - len(e.lstrip()) for e in entries} == {4}
    # The continuation is indented past the entry it belongs to.
    assert lines[-1].startswith("      ")


def test_unmeasured_sections_are_named_rather_than_shown_as_zero(review):
    review.calendar_ok = False
    out = review.render("terminal")
    assert "Not measured" in out
    assert "Time" in out


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def test_markdown_leads_with_a_heading(populated):
    assert populated.render("markdown").startswith("# Week 37")


def test_markdown_summarizes_in_a_table(populated):
    out = populated.render("markdown")
    assert "| --- | --- |" in out
    assert "| **Time** |" in out


def test_markdown_escapes_a_pipe_so_the_table_survives(review):
    review.tasks(
        a_task(uuid="a", status="completed", end=at(0, 9), project="a|b")
    )
    out = review.render("markdown", sections=("finished",))
    assert "a\\|b" in out


def test_markdown_carries_the_coverage_section(populated):
    assert "## Coverage" in populated.render("markdown")


def test_markdown_explains_every_section_it_shows(populated):
    # A saved note is the format you hand to someone else, so it needs the
    # definitions more than a screen you read once does.
    from task_gcal.review.metrics import glossary

    out = populated.render("markdown")
    assert "## What these mean" in out
    for section in populated.review().sections:
        if f"**{section.label}**" in out:
            assert glossary()[section.key] in out


def test_the_markdown_glossary_comes_last(populated):
    out = populated.render("markdown")
    assert out.index("## What these mean") > out.index("## Coverage")


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def test_json_is_valid_json(populated):
    payload = json.loads(populated.render("json"))
    assert payload["period"]["label"] == "Week 37"


def test_json_carries_machine_readable_data_for_each_section(populated):
    payload = json.loads(populated.render("json"))
    by_key = {s["key"]: s for s in payload["sections"]}
    assert by_key["time"]["data"]["available_minutes"] > 0
    assert by_key["finished"]["data"]["completed"] == 3


def test_json_always_ships_coverage_even_unfiltered(populated):
    payload = json.loads(populated.render("json"))
    throughput = next(
        s for s in payload["sections"] if s["key"] == "finished"
    )
    assert throughput["coverage"][0]["total"] == 3


def test_json_marks_an_unmeasured_section(review):
    review.calendar_ok = False
    payload = json.loads(review.render("json"))
    capacity = next(s for s in payload["sections"] if s["key"] == "time")
    assert capacity["measured"] is False


def test_json_contains_only_primitives(populated):
    # A datetime or a dataclass leaking into `data` breaks every consumer.
    payload = json.loads(populated.render("json"))

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        else:
            assert value is None or isinstance(value, (str, int, float, bool))

    walk(payload)


def test_json_timestamps_are_utc_with_a_z(populated):
    payload = json.loads(populated.render("json"))
    assert payload["period"]["start"].endswith("Z")
    assert payload["generated_at"].endswith("Z")


def test_json_omits_prose_detail_unless_asked(populated):
    plain = json.loads(populated.render("json"))
    assert "detail" not in plain["sections"][0]
    filtered = json.loads(populated.render("json", sections=("time",)))
    assert filtered["sections"][0]["detail"]


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def test_html_is_one_self_contained_file(populated):
    out = populated.render("html")
    assert out.startswith("<!doctype html>")
    assert "<style>" in out


def test_html_loads_nothing_from_the_network(populated):
    # Task titles are in this file; it must work offline and leak nothing.
    out = populated.render("html")
    assert "http://" not in out
    assert "https://" not in out
    assert "<script" not in out


def test_html_charts_are_inline_svg(populated):
    out = populated.render("html")
    assert "<svg" in out
    assert out.count("<figure") >= 2  # capacity stack + project bars


def test_the_html_charts_are_well_formed_xml(populated):
    out = populated.render("html")
    for svg in re.findall(r"<svg.*?</svg>", out, re.S):
        ElementTree.fromstring(svg)  # raises if malformed


def test_html_hover_text_needs_no_javascript(populated):
    # SVG <title> is the browser's own tooltip.
    assert "<title>" in populated.render("html")


def test_html_defines_dark_mode_as_its_own_values(populated):
    out = populated.render("html")
    assert "prefers-color-scheme: dark" in out
    # Not a filter or an inversion of the light values.
    assert "invert(" not in out


def test_html_always_offers_a_table_of_every_value(populated):
    out = populated.render("html")
    assert "All values" in out
    assert "<table>" in out


def test_the_table_never_shows_a_python_repr(populated):
    # Several sections hold lists of records — the promise ledger, the
    # stagnation queue — and a `str()` of those defeats the point of the
    # table being the readable way to reach a value.
    populated.blocks(
        a_block("stuck", at(0, 9), 60), a_block("stuck", at(1, 9), 60)
    )
    populated.tasks(a_task(uuid="stuck", id=40, description="Analyze peakon"))
    out = populated.render("html", sections=("stuck",))

    assert "{&#x27;" not in out and "{'" not in out
    assert "[{" not in out
    # And the values are still reachable, named by the record they came from.
    assert "entries[Analyze peakon].passed_blocks" in out
    assert ">2<" in out


def test_a_nested_dict_is_still_flattened(populated):
    out = populated.render("html", sections=("finished",))
    assert "by_project.fraud" in out


def test_html_explains_every_section_it_shows(populated):
    # Even the plainer labels need saying once: "Backlog" and "Sittings" are
    # a metric nobody understands is a metric nobody acts on.
    from task_gcal.review.metrics import glossary

    out = populated.render("html")
    assert "What these mean" in out
    for section in populated.review().sections:
        if section.label in out:
            assert escape(glossary()[section.key]) in out


def test_the_glossary_does_not_define_sections_that_are_not_shown(populated):
    from task_gcal.review.metrics import glossary

    out = populated.render("html", sections=("time",))
    assert escape(glossary()["time"]) in out
    assert escape(glossary()["stuck"]) not in out


def test_the_glossary_comes_after_the_report(populated):
    out = populated.render("html")
    assert out.index("What these mean") > out.index("All values")


def test_the_glossary_says_grey_means_unknown_not_zero(populated):
    # The one thing a reader has to know to not misread the page.
    out = populated.render("html")
    assert "not that it was zero" in out


def test_html_escapes_a_task_title(review):
    review.tasks(
        a_task(
            uuid="a", status="completed", end=at(0, 9),
            project="<script>alert(1)</script>",
        )
    )
    out = review.render("html")
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out


def test_html_omits_a_chart_it_has_no_data_for(review):
    # An empty week's capacity stack would be one full-width bar of
    # "unplanned", which is a picture of nothing.
    out = review.render("html")
    assert "<svg" not in out


def test_the_capacity_stack_appears_once_the_week_has_a_claim_on_it(review):
    review.meetings((at(0, 9), at(0, 10)))
    assert "<svg" in review.render("html")


def test_the_capacity_stack_never_exceeds_the_working_day(populated):
    section = populated.review().section("time")
    stack = (
        section.data["meeting_minutes"]
        + section.data["planned_in_hours_minutes"]
        + section.data["free_minutes"]
    )
    assert stack == section.data["available_minutes"]
