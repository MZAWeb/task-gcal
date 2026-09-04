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
    # 26 rather than 24 because this fixture is a *first run*: it carries two
    # caveats a configured setup never shows together ("no change history yet"
    # and "no scheduling runs recorded"). The real steady-state report is 22.
    lines = populated.render("terminal").splitlines()
    assert len(lines) <= 26, "\n".join(lines)


def test_the_summary_block_itself_stays_short(populated):
    # The part that grows when somebody adds a metric. Kept separate from the
    # whole-page budget so caveats can't disguise creep here.
    body = populated.render("terminal").split("\n\n")[1].splitlines()
    assert len(body) <= 10, "\n".join(body)


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


def test_the_detailed_view_groups_sections_by_what_they_are_about(populated):
    # Blocks together, dates together — grouped by the object measured, not by
    # a narrative. Four sections were about "did the plan hold" because there
    # are four data sources, not because a reader has four questions.
    from task_gcal.review.metrics import GROUP_BLOCKS, GROUP_DATES

    lines = populated.render("terminal", sections=("blocks", "dates")).splitlines()
    headings = [i for i, line in enumerate(lines) if line.startswith("──")]
    labelled = {
        line.split()[0]: i for i, line in enumerate(lines) if line[:1].strip()
    }
    assert [lines[i] for i in headings] == [
        f"── {GROUP_BLOCKS} ".ljust(78, "─"),
        f"── {GROUP_DATES} ".ljust(78, "─"),
    ]
    # Each section sits under its own group's heading, not above it.
    assert headings[0] < labelled["Blocks"] < headings[1] < labelled["Dates"]


def test_the_summary_has_no_group_headings(populated):
    # They'd cost a third of a one-screen budget to organise seven lines that
    # already read in order.
    from task_gcal.review.metrics import GROUP_BLOCKS

    assert GROUP_BLOCKS not in populated.render("terminal")


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
    assert "<table" in out


def test_html_says_unmeasured_means_unknown_rather_than_zero(review):
    # The rule the whole report rests on, and the one thing a reader has to
    # know to not misread the page. Said where the blank is, not only in an
    # appendix the reader may never reach.
    review.calendar_ok = False
    out = review.render("html", sections=("time",))
    card = out[out.index('id="time"') : out.index("</article>")]
    assert "not zero" in card
    assert "unknown" in card.lower()
    # And it keeps its place on the page rather than being dropped, so a
    # missing measurement can't read as a week with nothing in it.
    assert "Time" in card


def test_an_unmeasured_section_says_it_once_rather_than_four_times(review):
    # It used to say it in the label, the summary, a box and a note. A page
    # that repeats itself is a page that gets skimmed.
    review.calendar_ok = False
    out = review.render("html", sections=("time",))
    assert out.count("not zero") == 1


def test_an_optional_section_is_not_treated_as_a_failed_measurement(populated):
    # A check-in feature nobody turned on is a gap in the setup, not a gap in
    # the record. Spending "not measured" on it wears the phrase out.
    out = populated.render("html", sections=("reasons",))
    assert "only fills in when you use it" in out
    assert "inputs weren't there" not in out


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


def test_every_section_is_defined_where_it_is_used_not_only_in_the_glossary(
    populated,
):
    # "WTF is Scope" is answered at the point of use or it isn't answered: a
    # definition in an appendix is a definition the reader has to go and find.
    from task_gcal.review.metrics import glossary

    out = populated.render("html")
    means = escape(glossary()["blocks"])
    card = out[out.index('id="blocks"') :]
    card = card[: card.index("</article>")]
    assert means in card


def test_the_glossary_says_unmeasured_is_not_zero(populated):
    # The one thing a reader has to know to not misread the page, kept on it
    # even when every section happens to have data this week.
    out = populated.render("html")
    assert "never means zero" in out


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


# ---------------------------------------------------------------------------
# HTML: the three rails that matter
#
# Escaping, because every task title in this file came from somewhere else.
# No network, because the file has task titles in it and gets opened from a
# temp directory. And every number reachable as text, because a value that is
# only a picture is a value you can't check, copy or read aloud.
# ---------------------------------------------------------------------------

def a_hostile_title() -> str:
    """Every character that could break out of a context this renderer writes."""
    return "<script>alert(1)</script> & \"quoted\" 'single' </title> --> ]]>"


@pytest.fixture
def hostile(review):
    """A review whose task titles are all trying to get out of their context.

    Titles reach the page through several different paths — a chart's tooltip,
    an SVG text label, a table cell, a stuck entry, the promise ledger — and
    each is a separate chance to forget to escape.
    """
    from conftest import due_moved

    nasty = a_hostile_title()
    review.tasks(
        a_task(
            uuid="a",
            id=41,
            description=nasty,
            status="completed",
            end=at(1, 10),
            due=at(0, 9),
            project=nasty,
        ),
        a_task(
            uuid="c",
            id=43,
            description=nasty + " two",
            status="completed",
            end=at(2, 10),
            project=nasty + " two",
        ),
        a_task(uuid="b", id=42, description=nasty, due=at(4, 9)),
    )
    review.blocks(
        a_block("a", at(0, 9), 60, summary=nasty),
        a_block("b", at(1, 9), 60, summary=nasty),
        a_block("b", at(2, 9), 60, summary=nasty),
    )
    review.changes(
        due_moved("b", at(0, 12), at(0, 9), at(1, 9)),
        due_moved("b", at(1, 12), at(1, 9), at(2, 9)),
        due_moved("b", at(2, 12), at(2, 9), at(4, 9)),
    )
    return review


def test_no_task_title_escapes_its_context_anywhere_on_the_page(hostile):
    out = hostile.render(
        "html", sections=("time", "finished", "blocks", "dates", "stuck")
    )
    # Not one character of it survives as markup, in any of the contexts a
    # title reaches: a table cell, a chart tooltip, an SVG text label, a stuck
    # entry, the promise ledger.
    assert a_hostile_title() not in out
    assert "<script" not in out
    assert "]]>" not in out
    # And it is on the page, escaped, rather than silently dropped.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert out.count("&lt;script&gt;") >= 2


def test_a_hostile_title_survives_inside_the_svg_it_labels(hostile):
    # SVG is XML: a raw & or < inside a <title> or <text> is not merely ugly,
    # it stops the whole chart parsing.
    out = hostile.render("html", sections=("finished",))
    svgs = re.findall(r"<svg.*?</svg>", out, re.S)
    assert svgs
    for svg in svgs:
        ElementTree.fromstring(svg)


def test_every_chart_on_a_full_report_is_well_formed_xml(populated):
    populated.blocks(
        a_block("a", at(0, 9), 60),
        a_block("b", at(1, 9), 90),
        a_block("b", at(1, 11), 90),
        a_block("d", at(2, 10), 60),
    )
    out = populated.render("html", detailed=True)
    for svg in re.findall(r"<svg.*?</svg>", out, re.S):
        ElementTree.fromstring(svg)


def test_the_page_asks_the_network_for_nothing_at_all(populated):
    out = populated.render("html")
    for forbidden in (
        "http://",
        "https://",
        "<script",
        "<link",
        "<img",
        "<iframe",
        "@import",
        "url(",  # a background image, a webfont, anything fetched
        "srcset",
        "@font-face",
    ):
        assert forbidden not in out, forbidden


def test_the_page_works_from_a_temp_directory(populated, tmp_path):
    # No <base>, and every link is a fragment: nothing resolves against a path,
    # so the file behaves the same wherever it is written.
    out = populated.render("html")
    assert "<base" not in out
    for href in re.findall(r'href="([^"]*)"', out):
        assert href.startswith("#"), href
    (tmp_path / "r.html").write_text(out, encoding="utf-8")
    assert (tmp_path / "r.html").read_text(encoding="utf-8") == out


def flatten(measure, value):
    """Every scalar in a section's data, with the path that reaches it."""
    if isinstance(value, dict):
        return [
            pair
            for key, item in value.items()
            for pair in flatten(f"{measure}.{key}", item)
        ]
    if isinstance(value, (list, tuple)):
        out = []
        for index, item in enumerate(value):
            out.extend(flatten(f"{measure}[{index}]", item))
        return out
    return [(measure, value)]


def test_no_number_is_reachable_only_as_a_picture(populated):
    # The rule that makes the charts optional. Strip every chart out of the
    # page and all of it must still be readable as text.
    populated.blocks(
        a_block("a", at(0, 9), 60),
        a_block("b", at(1, 9), 90),
        a_block("b", at(1, 11), 90),
    )
    from task_gcal.review.metrics import summary_sections

    out = populated.render("html", detailed=True)
    without_charts = re.sub(r"<svg.*?</svg>", "", out, flags=re.S)

    for section in summary_sections(populated.review().sections):
        for measure, value in [
            pair
            for key, raw in section.data.items()
            for pair in flatten(key, raw)
        ]:
            if value is None or value == "":
                continue
            assert escape(str(value)) in without_charts, f"{section.key}.{measure}"


def test_a_chart_prints_its_own_values_as_text_beside_it(populated):
    # Belt as well as braces: the legend and the tip labels carry the numbers,
    # so a reader doesn't have to open the appendix to read a chart.
    out = populated.render("html", sections=("time",))
    figure = out[out.index("<figure") : out.index("</figure>")]
    section = populated.review().section("time")
    from task_gcal.intervals import humanize_minutes

    assert humanize_minutes(section.data["meeting_minutes"]) in figure


def test_a_chart_of_one_bar_is_not_drawn(review):
    # One bar compares nothing, and the sentence above it already says the
    # number. A picture that adds nothing is decoration.
    review.tasks(
        a_task(uuid="a", status="completed", end=at(0, 9), project="only"),
        a_task(uuid="b", status="completed", end=at(1, 9), project="only"),
    )
    out = review.render("html", sections=("finished",))
    assert "<svg" not in out


def test_an_unmeasured_section_draws_no_chart(review):
    review.calendar_ok = False
    assert "<svg" not in review.render("html", sections=("time",))


# ---------------------------------------------------------------------------
# HTML: the reading experience
# ---------------------------------------------------------------------------

def test_the_page_opens_with_the_one_decision(populated):
    populated.meetings(*[(at(day, 9), at(day, 15)) for day in range(5)])
    # Measured inside <main>: the contents rail names the groups too, and it
    # sits before everything by design.
    body = populated.render("html")
    body = body[body.index("<main>") :]
    assert "One thing to decide" in body
    assert body.index("One thing to decide") < body.index("The week you had")


def test_the_decision_points_at_the_tasks_behind_it(populated):
    # The closing line names one task; the section names the rest. Linking the
    # two beats repeating either.
    populated.blocks(
        a_block("stuck", at(0, 9), 60),
        a_block("stuck", at(1, 9), 60),
        a_block("other", at(1, 11), 60),
        a_block("other", at(2, 11), 60),
    )
    populated.tasks(
        a_task(uuid="stuck", id=40, description="Analyze peakon"),
        a_task(uuid="other", id=41, description="Something else"),
    )
    out = populated.render("html", sections=("dates", "stuck"))
    assert "One thing to decide" in out
    decision = out[out.index("One thing to decide") :]
    decision = decision[: decision.index("</section>")]
    assert 'href="#stuck"' in decision
    # And it is a link rather than a repetition of the list.
    assert "Something else" not in decision


def test_the_header_says_which_days_the_report_covers(populated):
    # "Week 36" is an index, not a date. In three months it means nothing on
    # its own.
    out = populated.render("html")
    assert "Sep 2026" in out


def test_the_header_says_how_much_of_the_period_was_observed(populated):
    out = populated.render("html")
    assert "of 5 days" in out


def test_a_long_report_carries_a_table_of_contents(populated):
    out = populated.render("html", detailed=True)
    assert "Contents" in out
    # Every link in it resolves to a section that is actually on the page.
    rail = out[out.index('class="rail"') : out.index("</aside>")]
    for target in re.findall(r'href="#([^"]+)"', rail):
        assert f'id="{target}"' in out


def test_a_single_section_report_has_no_table_of_contents(populated):
    # Navigation for one card is furniture.
    assert "Contents" not in populated.render("html", sections=("time",))


def test_the_detail_is_typeset_as_rows_rather_than_printed_as_bullets(populated):
    # The metrics pad their detail into columns for the terminal. Printing
    # those strings into HTML throws away the structure the padding *is*.
    out = populated.render("html", sections=("time",))
    assert "<dt>Working hours</dt>" in out
    assert re.search(r"<dt>Working hours</dt><dd>\d+h[\d]* across", out)
    # And not as the bullet list of pre-padded strings this used to be.
    assert "<li>Working hours" not in out


def test_a_line_the_metric_did_not_pad_is_still_a_row(populated):
    # Two sibling facts, one padded and one not, used to render as a row and a
    # grey sentence — which reads as two different kinds of thing.
    populated.blocks(a_block("a", at(0, 9), 60), a_block("b", at(1, 9), 90))
    out = populated.render("html", sections=("blocks",))
    assert "<dt>Passed still open</dt>" in out


def test_prose_that_happens_to_contain_a_number_stays_prose(populated):
    # A sentence that happens to contain a number is not a measurement, and
    # tearing it into a label and a value would state it as one.
    out = populated.render("html", sections=("time",))
    sentence = "The period is still running"
    assert f'<p class="note">{sentence}' in out
    assert f"<dt>{sentence}" not in out


def test_the_report_never_says_one_tasks(populated):
    # `N task(s)` is what a metric writes when it can't know its own count.
    # A renderer does know, and "1 tasks" is how a page tells you nobody looked.
    out = populated.render("html", detailed=True)
    assert "(s)" not in out
    assert "(es)" not in out
    assert not re.search(r"\b1 [a-z]+s\b", strip_tags(out).replace("1 seconds", ""))


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def test_a_detail_list_becomes_a_table_rather_than_a_paragraph(populated):
    out = populated.render("html", sections=("dates",), detailed=True)
    assert "<table" in out or "no deadline history" in out


def test_light_and_dark_are_two_chosen_palettes(populated):
    out = populated.render("html")
    assert "prefers-color-scheme: dark" in out
    # Not a filter, not an inversion, and dark text is not pure white.
    assert "invert(" not in out
    assert "filter:" not in out
    dark = out[out.index("prefers-color-scheme: dark") :]
    dark = dark[: dark.index("}\n}")]
    assert "--ink: #ffffff" not in dark
    assert "--page:" in dark and "--surface:" in dark


def test_the_report_prints(populated):
    # A review is a document. Some people print documents.
    out = populated.render("html")
    assert "@media print" in out


def test_no_colour_on_the_page_means_good_or_bad(populated):
    # The palette separates categories and does nothing else. The moment a
    # number can be green or red the report is grading somebody, and a metric
    # that can shame you stops being told the truth.
    out = populated.render("html")
    style = out[out.index("<style>") : out.index("</style>")]
    for token in (
        "--good",
        "--bad",
        "--success",
        "--danger",
        "--warning",
        "--error",
        "--positive",
        "--negative",
    ):
        assert token not in style, token


def test_a_wide_table_scrolls_itself_rather_than_the_page(populated):
    # The promise ledger is six columns of task titles, and a closed <details>
    # is still laid out by current browsers — so a table nobody has opened can
    # still drag the whole document sideways if it isn't contained.
    from conftest import due_moved

    populated.tasks(
        a_task(uuid="a", status="completed", end=at(1, 10), due=at(0, 9)),
        a_task(uuid="e", description="x" * 90, due=at(4, 9)),
    )
    populated.changes(due_moved("e", at(0, 12), at(0, 9), at(4, 9)))
    out = populated.render("html", sections=("dates",), detailed=True)
    assert out.count('<table class="values"') == out.count('<div class="scroll">')
    assert '<div class="scroll"><table class="values"' in out


def test_the_page_never_scrolls_sideways_on_a_phone(populated):
    # Everything that could be wider than a phone is either wrapped in a
    # scroller or allowed to wrap; nothing sets a fixed pixel width.
    out = populated.render("html")
    style = out[out.index("<style>") : out.index("</style>")]
    assert "width: 100%" in style
    assert not re.search(r"(?<!max-)width:\s*\d{3,}px", style)
    assert "@media (max-width: 34rem)" in style


def test_the_dates_are_formatted_without_a_platform_specific_directive(populated):
    # `%-d` is a glibc and BSD extension: it raises on other C libraries. A
    # renderer that only works on the developer's laptop isn't a renderer.
    import inspect

    from task_gcal.review.render import html as html_render

    source = inspect.getsource(html_render)
    assert not re.search(r"""strftime\(\s*['"][^'"]*%-""", source)
    assert "Sep 2026" in populated.render("html")


def test_the_decision_does_not_repeat_what_the_finding_already_says(populated):
    # The stuck section's own suggestion names the other tasks. Adding a second
    # sentence that also names them is worse than adding neither.
    populated.blocks(
        *[a_block(u, at(d, 9), 60) for u in ("s1", "s2") for d in range(3)]
    )
    populated.tasks(
        a_task(uuid="s1", id=51, description="One"),
        a_task(uuid="s2", id=52, description="Two"),
    )
    out = populated.render("html", sections=("stuck",))
    decision = out[out.index("<main>") :]
    decision = decision[: decision.index("<section class=\"group\"")]
    assert decision.count("other task") <= 1


def test_a_command_in_a_finding_becomes_a_link_to_the_section(populated):
    # A finding written for the terminal ends in "run --section stuck". On a
    # page where that section is a click away, the command is the wrong thing
    # to offer.
    populated.blocks(
        *[a_block(u, at(d, 9), 60) for u in ("s1", "s2") for d in range(3)]
    )
    populated.tasks(
        a_task(uuid="s1", id=51, description="One"),
        a_task(uuid="s2", id=52, description="Two"),
    )
    out = populated.render("html", sections=("stuck",))
    assert '<a href="#stuck"><code>--section stuck</code></a>' in out


def test_a_command_for_a_section_that_is_not_shown_stays_a_command(populated):
    out = populated.render("html", sections=("time",))
    assert '<a href="#stuck">' not in out


@pytest.mark.parametrize("kwargs", [{}, {"detailed": True}, {"sections": ("dates",)}])
def test_every_link_on_the_page_goes_somewhere_on_the_page(populated, kwargs):
    # There is nowhere else for a link to go: no network, one file. A fragment
    # that resolves to nothing is a dead end in a document with no back button.
    out = populated.render("html", **kwargs)
    ids = set(re.findall(r'id="([^"]+)"', out))
    for href in re.findall(r'href="#([^"]+)"', out):
        assert href in ids, href


def test_an_unmeasured_section_reads_as_present_but_quiet(review):
    # Present, because dropping it would let a missing measurement read as a
    # week with nothing in it. Quiet, because it has nothing to say.
    review.calendar_ok = False
    out = review.render("html", sections=("time",))
    assert 'class="card unmeasured"' in out
