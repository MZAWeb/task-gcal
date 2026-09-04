"""A single self-contained HTML file — the medium for a review you sit and read.

Chosen over a TUI when reviews outgrow the terminal, because it costs nothing
that a TUI costs: no terminal framework, no keyboard navigation, no resize or
colour handling, no persistent interaction state, and no second application
living inside task-gcal. It is a *renderer* — one function of the same `Review`
model the terminal reads.

Deliberate constraints, all of them load-bearing:

- **One file, no network.** Styles and charts are inline; there is no CDN, no
  script tag and no font download, so nothing about your task titles leaves the
  machine when you open it. That rules out a webfont, so the type is a chosen
  stack of fonts you already have — a serif for reading, a sans for structure.
- **No JavaScript at all.** Hover text is SVG `<title>`, disclosure is
  `<details>`, and the section you jumped to highlights itself with `:target`.
  Everything works with scripting off, from a temp directory, offline.
- **Light and dark are two chosen palettes**, not one flipped. Dark text is a
  warm off-white rather than #fff, because pure white on a dark surface haloes.
- **Nothing is scored.** No grades, no deltas dressed as progress, no red and
  green. Colour is used to separate *categories* in charts and for nothing else,
  and the one place the report raises its voice is the single decision at the
  top.
- **Missing data is never a zero.** An unmeasured section keeps its place on the
  page and says what isn't known, in a visibly different treatment from a
  measured one.
- **Every number is reachable as text.** Charts print their own values in a
  legend or at the tip of the mark, and the appendix carries every value in the
  model as a table. No figure is legible only as a picture.

The presentation layer is deliberately thicker than the other renderers'. Three
things happen here that don't happen in the terminal:

- each section carries its own plain-language definition, at the point of use
  rather than only in an appendix, because a metric nobody understands is a
  metric nobody acts on;
- the detail lines the metrics format as padded columns are parsed back into
  structure (label/value rows, grouped lists) so they can be typeset rather
  than printed;
- `N task(s)` is resolved against its own number, because "1 tasks" is the
  sort of thing that tells a reader no one was looking.

None of that computes a new number, and nothing here reaches past the `Review`
model. Where a section's `data` carries a richer record than its prose — the
promise ledger, the stuck queue — the table is built from the data and the
prose is dropped, so the page never says the same thing twice.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from html import escape
from typing import Optional

from ...intervals import humanize_minutes
from ..metrics import (
    attempts as attempts_metric,
    capacity as capacity_metric,
    churn as churn_metric,
    deadlines as deadlines_metric,
    followthrough as followthrough_metric,
    glossary,
    groups,
    scope as scope_metric,
    stagnation_section as stuck_metric,
    throughput as throughput_metric,
    trends as trends_metric,
)
from ..model import Review, Section
from .charts import Slice, bar_rows, columns, fill_hour_range, hour_label, sparklines, stacked_bar

# Two palettes, each stepped for its own surface. The series hues are the
# validated pair (worst adjacent CVD ΔE 24.7 light / 26.8 dark, normal-vision
# 33.6 / 31.8) and carry no meaning beyond "a different category from the one
# next to it" — there is no good colour and no bad colour on this page.
_STYLE = """
:root {
  color-scheme: light;
  --page: #f6f5f1;
  --surface: #fffefb;
  --sunken: #efeee8;
  --ink: #17170f;
  --ink-2: #56544a;
  --ink-3: #6f6d60;
  --hair: rgba(23, 23, 15, 0.09);
  --rule: rgba(23, 23, 15, 0.18);
  --series-1: #2a78d6;
  --series-2: #eb6834;
  --track: #e2e0d6;
  --lift: 0 1px 1px rgba(23, 23, 15, 0.03), 0 10px 26px -18px rgba(23, 23, 15, 0.22);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0e0e0d;
    --surface: #181816;
    --sunken: #121211;
    --ink: #f4f2ec;
    --ink-2: #b6b3a7;
    --ink-3: #8e8b7a;
    --hair: rgba(244, 242, 236, 0.10);
    --rule: rgba(244, 242, 236, 0.20);
    --series-1: #3987e5;
    --series-2: #d95926;
    --track: #34342f;
    --lift: none;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  padding: 3.5rem 1.5rem 6rem;
  background: var(--page);
  color: var(--ink);
  font: 400 15.5px/1.6 var(--sans);
  font-synthesis: none;
  -webkit-font-smoothing: antialiased;
}
:root {
  --serif: ui-serif, "Iowan Old Style", Charter, "Palatino Linotype", Palatino,
    "Book Antiqua", Georgia, serif;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue",
    Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}

/* ---------------------------------------------------------------- layout */
.page {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 3rem;
  max-width: 46rem;
  margin: 0 auto;
}
.page > * { min-width: 0; }
.page.has-rail { max-width: 66rem; grid-template-columns: 11.5rem minmax(0, 1fr); }
main { min-width: 0; max-width: 44rem; overflow-wrap: break-word; }
/* ------------------------------------------------------------------ rail */
.rail {
  position: sticky;
  top: 3.5rem;
  align-self: start;
  font-size: 0.82rem;
  line-height: 1.5;
}
.rail h2 {
  font: 600 0.68rem/1 var(--sans);
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin: 0 0 0.9rem;
}
.rail ol { list-style: none; margin: 0; padding: 0; }
.rail .rail-group {
  color: var(--ink-3);
  margin: 1.1rem 0 0.35rem;
  font-size: 0.72rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.rail .rail-group:first-child { margin-top: 0; }
.rail a {
  color: var(--ink-2);
  text-decoration: none;
  display: block;
  padding: 0.12rem 0;
  border-bottom: 0;
}
.rail a:hover { color: var(--ink); }

/* ---------------------------------------------------------------- header */
.eyebrow {
  font: 600 0.7rem/1 var(--sans);
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin: 0 0 0.9rem;
}
h1 {
  font: 400 clamp(2.4rem, 6vw, 3.4rem)/1.02 var(--serif);
  letter-spacing: -0.02em;
  margin: 0;
}
.dateline {
  font-family: var(--serif);
  font-size: 1.05rem;
  color: var(--ink-2);
  margin: 0.5rem 0 0;
}
.basis-strip {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.4rem 0;
  margin: 1.6rem 0 0;
  padding-top: 1.1rem;
  border-top: 1px solid var(--hair);
  font-size: 0.85rem;
  color: var(--ink-3);
}
.basis-strip b { color: var(--ink-2); font-weight: 600; }
.basis-strip > span { padding: 0 0.85rem; }
.basis-strip > span:first-of-type { padding-left: 0.6rem; }
.basis-strip > span + span { border-left: 1px solid var(--hair); }
.meter {
  display: block;
  width: 3.4rem;
  height: 6px;
  border-radius: 3px;
  background: var(--track);
  overflow: hidden;
  flex: none;
}
.meter span { display: block; height: 100%; border-radius: 3px; background: var(--series-1); }
/* One cell per day of the period, filled where a run observed it. Deliberately
   plain: no intensity ramp and no ordering, because this is a record of what
   the tool saw and not a picture of how the week went. */
.days { display: flex; gap: 3px; flex: none; align-items: flex-start; }
.day { display: flex; flex-direction: column; align-items: center; gap: 2px; }
.day .box {
  display: block;
  width: 0.85rem;
  height: 0.85rem;
  border-radius: 2px;
  box-shadow: inset 0 0 0 1px var(--rule);
}
.day.seen .box { background: var(--series-1); box-shadow: none; }
.day-letter { font-size: 0.6rem; line-height: 1; color: var(--ink-3); }

/* -------------------------------------------------------------- decision */
.decision {
  margin: 2.5rem 0 0;
  padding: 1.5rem 1.6rem;
  background: var(--surface);
  border: 1px solid var(--hair);
  border-left: 3px solid var(--ink);
  border-radius: 4px;
  box-shadow: var(--lift);
}
.decision .eyebrow { margin-bottom: 0.7rem; }
.decision p {
  font-family: var(--serif);
  font-size: 1.28rem;
  line-height: 1.45;
  margin: 0;
  text-wrap: pretty;
}
.decision .again {
  font-family: var(--sans);
  font-size: 0.85rem;
  color: var(--ink-3);
  margin: 0.8rem 0 0;
}
.decision a { color: inherit; }

/* --------------------------------------------------------------- section */
.group { margin: 3.5rem 0 0; }
.group > h2 {
  font: 600 0.72rem/1 var(--sans);
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin: 0 0 1rem;
  padding-bottom: 0.7rem;
  border-bottom: 1px solid var(--rule);
}
.card {
  padding: 1.4rem 0 1.6rem;
  border-bottom: 1px solid var(--hair);
  scroll-margin-top: 2rem;
}
.card:last-child { border-bottom: 0; }
.card > h3 {
  font: 600 0.95rem/1.3 var(--sans);
  letter-spacing: 0.01em;
  margin: 0;
}
.card > h3 a { color: inherit; text-decoration: none; }
.card > h3 a:hover { color: var(--ink-3); }
.means {
  margin: 0.35rem 0 0;
  font-size: 0.85rem;
  line-height: 1.5;
  color: var(--ink-3);
  max-width: 34rem;
}
.summary {
  font-family: var(--serif);
  font-size: 1.22rem;
  line-height: 1.5;
  margin: 0.9rem 0 0;
  text-wrap: pretty;
}
.summary .sep { color: var(--ink-3); padding: 0 0.1rem; }
.card:target { position: relative; }
.card:target > h3::before {
  content: "";
  position: absolute;
  left: -1.1rem;
  top: 1.55rem;
  width: 3px;
  height: 1.1rem;
  border-radius: 2px;
  background: var(--series-1);
}

/* ----------------------------------------------------------------- facts */
.facts { margin: 1.1rem 0 0; }
.fact {
  display: grid;
  grid-template-columns: minmax(9rem, 12rem) minmax(0, 1fr);
  gap: 0 1.2rem;
  padding: 0.4rem 0;
  border-top: 1px solid var(--hair);
}
.fact:first-child { border-top: 0; padding-top: 0; }
.fact dt { color: var(--ink-2); font-size: 0.88rem; }
.fact dd {
  margin: 0;
  font-size: 0.95rem;
  font-variant-numeric: tabular-nums;
}
.fact.sub dt { padding-left: 0.9rem; color: var(--ink-3); font-size: 0.85rem; }
.fact.sub dd { color: var(--ink-2); font-size: 0.88rem; }
.note {
  margin: 0.9rem 0 0;
  font-size: 0.88rem;
  line-height: 1.55;
  color: var(--ink-3);
  max-width: 34rem;
}
.note code {
  font: 0.85em var(--mono);
  background: var(--sunken);
  padding: 0.1em 0.35em;
  border-radius: 3px;
  color: var(--ink-2);
}
h4.sublist {
  font: 600 0.78rem/1 var(--sans);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin: 1.3rem 0 0.5rem;
}
table.mini { border-collapse: collapse; width: 100%; font-size: 0.9rem; table-layout: auto; }
table.mini td {
  padding: 0.3rem 0.7rem 0.3rem 0;
  border-top: 1px solid var(--hair);
  vertical-align: baseline;
  white-space: nowrap;
}
table.mini tr:first-child td { border-top: 0; }
table.mini td:first-child {
  font-variant-numeric: tabular-nums;
  color: var(--ink-2);
  width: 1%;
}
table.mini td:last-child { white-space: normal; width: auto; }
table.mini td.wide { color: var(--ink-3); white-space: normal; }

/* ---------------------------------------------------------------- charts */
.chart { margin: 1.3rem 0 0; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; }
.chart figcaption {
  font-size: 0.8rem;
  color: var(--ink-3);
  margin-bottom: 0.6rem;
}
.chart-note, .axis-note {
  font-size: 0.78rem;
  color: var(--ink-3);
  margin: 0.6rem 0 0;
  max-width: 34rem;
}
.multiples { display: grid; gap: 0.4rem; }
.legend {
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem 1.4rem;
  margin: 0.7rem 0 0;
  padding: 0;
  font-size: 0.85rem;
  color: var(--ink-2);
}
.legend .legend-value {
  font-weight: 600;
  color: var(--ink);
  font-variant-numeric: tabular-nums;
  margin-left: 0.35rem;
}
.swatch {
  display: inline-block;
  width: 0.65rem;
  height: 0.65rem;
  border-radius: 2px;
  margin-right: 0.45rem;
  vertical-align: 0;
  box-shadow: inset 0 0 0 1px var(--hair);
}
text.axis, text.value {
  font: 11.5px var(--sans);
  fill: var(--ink-3);
}
text.value { fill: var(--ink-2); font-variant-numeric: tabular-nums; font-weight: 600; }
line.grid { stroke: var(--hair); stroke-width: 1; }
polyline.spark {
  fill: none;
  stroke: var(--series-1);
  stroke-width: 1.75;
  stroke-linejoin: round;
  stroke-linecap: round;
}
path.spark-area { fill: var(--series-1); opacity: 0.10; }
circle.spark-end { fill: var(--series-1); stroke: var(--surface); stroke-width: 2; }

/* ------------------------------------------------------------- entries */
ol.entries { list-style: none; margin: 1.2rem 0 0; padding: 0; }
ol.entries > li {
  display: grid;
  grid-template-columns: 3.1rem minmax(0, 1fr);
  gap: 0 0.9rem;
  padding: 0.7rem 0;
  border-top: 1px solid var(--hair);
}
ol.entries > li:first-child { border-top: 0; padding-top: 0; }
.ref {
  font: 0.8rem/1.7 var(--mono);
  color: var(--ink-3);
  font-variant-numeric: tabular-nums;
}
.entry-title { font-weight: 600; font-size: 0.95rem; }
.reasons {
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem 0.5rem;
  margin: 0.4rem 0 0;
  padding: 0;
}
.reasons li {
  display: block;
  flex: 0 0 auto;
  max-width: 100%;
  padding: 0.1rem 0.45rem;
  border: 1px solid var(--hair);
  border-radius: 3px;
  background: var(--sunken);
  color: var(--ink-2);
  font-size: 0.78rem;
  line-height: 1.5;
}

/* ------------------------------------------------------- unmeasured/etc */
.card.unmeasured > h3 { color: var(--ink-2); }
.unknown {
  margin: 0.9rem 0 0;
  padding: 0.9rem 1.1rem;
  border: 1px dashed var(--rule);
  border-radius: 4px;
  max-width: 36rem;
}
.unknown .summary { margin: 0; color: var(--ink-3); font-style: italic; }
.unknown p.why {
  margin: 0.5rem 0 0;
  font-size: 0.85rem;
  line-height: 1.55;
  color: var(--ink-3);
}
.unknown a { color: inherit; }
.coverage {
  margin: 1.1rem 0 0;
  font-size: 0.8rem;
  color: var(--ink-3);
  display: flex;
  flex-wrap: wrap;
  gap: 0.15rem 1.1rem;
}
.coverage b { color: var(--ink-2); font-variant-numeric: tabular-nums; font-weight: 600; }

/* --------------------------------------------------------------- details */
details.more { margin: 1.1rem 0 0; }
details.more > summary {
  cursor: pointer;
  font-size: 0.85rem;
  color: var(--ink-2);
  list-style: none;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.15rem 0;
}
details.more > summary::-webkit-details-marker { display: none; }
details.more > summary::before {
  content: "＋";
  font-size: 0.8em;
  color: var(--ink-3);
}
details.more[open] > summary::before { content: "－"; }
details.more > summary:hover { color: var(--ink); }

/* ------------------------------------------------------------- appendix */
.appendix { margin-top: 3.5rem; }
.appendix h2, .glossary h2 {
  font: 600 0.72rem/1 var(--sans);
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin: 0 0 1rem;
  padding-bottom: 0.7rem;
  border-bottom: 1px solid var(--rule);
}
.appendix ul { margin: 0; padding-left: 1.1rem; }
.appendix li { font-size: 0.9rem; color: var(--ink-2); margin-bottom: 0.35rem; }
/* A table too wide for the column scrolls inside its own box rather than
   widening the page. Two reasons it has to be handled rather than hoped about:
   the promise ledger is six columns of task titles, and a closed <details> is
   still laid out by current browsers, so an unreachable table can still push
   the whole document sideways. */
.scroll { overflow-x: auto; max-width: 100%; }
table.values {
  border-collapse: collapse;
  width: 100%;
  font-size: 0.82rem;
  margin-top: 0.9rem;
}
table.values th {
  text-align: left;
  font: 600 0.7rem/1 var(--sans);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-3);
  padding: 0 0.7rem 0.5rem 0;
  border-bottom: 1px solid var(--rule);
}
table.values td {
  padding: 0.28rem 0.7rem 0.28rem 0;
  border-bottom: 1px solid var(--hair);
  color: var(--ink-2);
}
table.values td.measure { font-family: var(--mono); font-size: 0.95em; }
table.values td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--ink);
  white-space: nowrap;
}
table.values td.sec { color: var(--ink-3); white-space: nowrap; }

/* ------------------------------------------------------------- glossary */
.glossary { margin-top: 3.5rem; }
.glossary dl { margin: 0; }
.glossary .fact { grid-template-columns: minmax(8rem, 10rem) minmax(0, 1fr); }
.glossary dd { color: var(--ink-2); font-size: 0.88rem; line-height: 1.55; }
footer {
  margin-top: 3rem;
  padding-top: 1.2rem;
  border-top: 1px solid var(--hair);
  font-size: 0.8rem;
  color: var(--ink-3);
}
footer p { margin: 0 0 0.4rem; max-width: 34rem; }

/* -------------------------------------------------------------- responsive
   Last in the sheet on purpose. A media query adds no specificity, so an
   override that sits above the rule it overrides silently loses to it. */
@media (max-width: 62rem) {
  .page.has-rail { grid-template-columns: minmax(0, 1fr); max-width: 46rem; gap: 2rem; }
  .rail { position: static !important; }
  .rail ol { display: flex; flex-wrap: wrap; gap: 0.3rem 0.9rem; }
  .rail .rail-group { width: 100%; margin-top: 0.6rem; }
}
/* Phone width. Nothing here is a different design — the same document, with
   the two-column rows stacked, because a 9rem label column beside a value
   leaves too little room for either. */
@media (max-width: 34rem) {
  body { padding: 2rem 1.1rem 4rem; }
  .fact { grid-template-columns: minmax(0, 1fr); gap: 0.1rem; padding: 0.5rem 0; }
  .fact dt { font-size: 0.8rem; }
  .fact.sub dt { padding-left: 0; }
  .fact.sub dd { padding-left: 0.9rem; }
  .glossary .fact { grid-template-columns: minmax(0, 1fr); }
  table.mini td { white-space: normal; }
  table.mini td:first-child { width: auto; }
  ol.entries > li { grid-template-columns: minmax(0, 1fr); gap: 0.2rem; }
  .basis-strip > span, .basis-strip > span:first-of-type { padding: 0 0.7rem 0 0; }
  .basis-strip > span + span { border-left: 0; }
  h1 { font-size: 2.1rem; }
  .decision { padding: 1.2rem 1.1rem; }
  .decision p { font-size: 1.12rem; }
  .summary { font-size: 1.1rem; }
}

@media print {
  :root {
    color-scheme: light;
    --page: #fff; --surface: #fff; --sunken: #f4f3ee;
    --ink: #000; --ink-2: #333; --ink-3: #555;
    --hair: rgba(0, 0, 0, 0.14); --rule: rgba(0, 0, 0, 0.3); --lift: none;
  }
  body { padding: 0; font-size: 10.5pt; }
  .rail { display: none; }
  .page.has-rail { grid-template-columns: minmax(0, 1fr); max-width: none; }
  .card, .decision, figure, ol.entries > li { break-inside: avoid; }
  details.more, details.values { display: none; }
}
"""

# Sections whose prose detail is dropped in favour of a structure built from
# `data`. Keeping both would print the same list twice, and the data version
# is the complete one — the prose is truncated for a terminal screen.
_DATA_DRIVEN = {stuck_metric.KEY}

# Detail groups a chart in the same section already draws, keyed by section.
# A picture and a list of the same eight numbers, one under the other, is how a
# page starts feeling like a database dump: the chart wins because it carries
# its own values as text anyway. Matched loosely, so a metric rewording its
# heading loses the de-duplication rather than the content.
_CHARTED_GROUPS = {
    throughput_metric.KEY: ("by project",),
    attempts_metric.KEY: ("distribution",),
    churn_metric.KEY: ("why",),
}

_PLURAL = re.compile(
    r"(?P<count>\d+(?:\.\d+)?)"
    r"(?P<mid>(?: [A-Za-z][A-Za-z-]*){0,2}? )"
    r"(?P<word>[A-Za-z][\w'-]*)\((?P<suffix>s|es)\)"
)
_CODE = re.compile(r"`([^`]+)`")

# Cell separator while a detail line is being taken apart. A control character
# because it cannot occur in a task title, a project name or any metric's prose,
# so splitting on it can never split something that was one field.
_CELL = "\x00"


def render(review: Review, *, sections: tuple[Section, ...], detailed: bool) -> str:
    where = groups(review.period.kind)
    rail = _rail(sections, where) if len(sections) >= 4 else ""
    body = [
        _header(review),
        _decision(review, sections),
    ]

    group = None
    open_group = False
    for section in sections:
        this = where.get(section.key)
        if this != group:
            if open_group:
                body.append("</section>")
            group = this
            body.append(
                f'<section class="group"><h2>{escape(group or "Sections")}</h2>'
            )
            open_group = True
        body.append(_card(section, detailed=detailed, has_basis=bool(review.caveats)))
    if open_group:
        body.append("</section>")

    body.append(_basis(review))
    body.append(_appendix(sections))
    body.append(_glossary(sections))
    body.append(_footer())

    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta name="color-scheme" content="light dark">',
            f"<title>{escape(review.period.label)} — task-gcal review</title>",
            f"<style>{_STYLE}</style></head><body>",
            f'<div class="page{" has-rail" if rail else ""}">',
            rail,
            "<main>",
            *body,
            "</main></div></body></html>",
        ]
    ) + "\n"


# ---------------------------------------------------------------------------
# Page furniture
# ---------------------------------------------------------------------------

def _rail(sections: tuple[Section, ...], where: dict) -> str:
    """A sticky table of contents.

    Present because `--all` is a long document and a reader who can't see the
    shape of it has to scroll to find out what it contains. Only for reports
    with enough sections to get lost in.
    """
    out = ['<aside class="rail"><h2>Contents</h2><ol>']
    group = None
    for section in sections:
        this = where.get(section.key)
        if this != group:
            group = this
            out.append(f'<li class="rail-group">{escape(group or "Sections")}</li>')
        out.append(
            f'<li><a href="#{escape(section.key)}">{escape(section.label)}</a></li>'
        )
    out.append("</ol></aside>")
    return "".join(out)


def _header(review: Review) -> str:
    period = review.period
    stamp = review.generated_at.astimezone(period.tz)
    kind = "Weekly review" if period.kind == "week" else "Monthly review"

    out = [
        f'<header><p class="eyebrow">{escape(kind)}</p>',
        f"<h1>{escape(period.label)}</h1>",
        f'<p class="dateline">{escape(_date_range(review))}</p>',
        '<div class="basis-strip">',
    ]

    if review.observed is not None:
        seen, total = review.observed
        out.append(_observed_mark(review, seen, total))
        if seen >= total:
            out.append(f"<span>Built on all <b>{total}</b> days of the period</span>")
        else:
            out.append(f"<span>Built on <b>{seen} of {total} days</b> observed</span>")
    if period.in_progress:
        out.append("<span>Still in progress</span>")
    out.append(f'<span>Generated {escape(_stamp(stamp))}</span>')
    out.append("</div></header>")
    return "".join(out)


def _observed_mark(review: Review, seen: int, total: int) -> str:
    """Which days the report is built on — a cell per day, filled if observed.

    A proportion can only say *how much* is missing. Which days are missing is a
    different fact and a more useful one: a run that stopped on Tuesday is not
    the same problem as a run that only ever happened once, and the bar cannot
    tell those apart.

    Two things this is careful not to become. It is not a streak: filled cells
    are days *a run observed*, which is a fact about the tool's own record and
    not about the person, so there is no intensity, no ordering claim and
    nothing to keep going. And it is not the only route to the information — the
    days are listed in words under "what this is based on", because a strip of
    cells is a glance, not a reading.
    """
    days = review.period.days()
    if not review.observed_days or not days:
        # No day list to draw, so say the proportion and nothing more. Zero
        # observed draws an empty track: a sliver of ink for nothing is a lie.
        share = 0 if not (total and seen) else max(4, round(100 * seen / total))
        return (
            f'<span class="meter" role="img" aria-label="{seen} of {total} '
            f'days observed"><span style="width:{share}%"></span></span>'
        )

    observed = set(review.observed_days)
    # Weekday initials only when there are few enough for them to be read. A
    # month is a strip of thirty cells and thirty letters is a texture.
    letters = len(days) <= 7
    cells = []
    for day in days:
        was_seen = day.isoformat() in observed
        klass = "day seen" if was_seen else "day"
        title = (
            f"{day.strftime('%A')} {day.day} {day.strftime('%b')}: "
            + ("a run observed this day" if was_seen else "not observed")
        )
        cells.append(
            f'<span class="{klass}" title="{escape(title)}">'
            '<span class="box"></span>'
            + (
                f'<span class="day-letter">{escape(day.strftime("%a")[:1])}</span>'
                if letters
                else ""
            )
            + "</span>"
        )
    return (
        f'<span class="days" role="img" aria-label="{seen} of {total} days '
        f'observed">{"".join(cells)}</span>'
    )


def _stamp(moment: datetime) -> str:
    """`4 Sep 2026, 18:56 CEST`, without a platform-specific format string.

    `%-d` is a glibc and BSD extension. It is not portable, and a renderer that
    raises on someone else's C library is a renderer that doesn't work.
    """
    return f"{moment.day} {moment.strftime('%b %Y, %H:%M %Z').strip()}"


def _date_range(review: Review) -> str:
    """The actual days the period covers, in words.

    "Week 36" is an index, not a date. Anyone reading this in three months
    needs to know which week that was without doing ISO arithmetic.
    """
    period = review.period
    first = period.start.astimezone(period.tz)
    last = (period.nominal_end - timedelta(microseconds=1)).astimezone(period.tz)
    if first.year != last.year:
        return (
            f"{first.day} {first.strftime('%b %Y')} – "
            f"{last.day} {last.strftime('%b %Y')}"
        )
    if first.month == last.month:
        return f"{first.day} – {last.day} {last.strftime('%B %Y')}"
    return f"{first.day} {first.strftime('%b')} – {last.day} {last.strftime('%b %Y')}"


def _decision(review: Review, sections: tuple[Section, ...]) -> str:
    """The one thing to decide, at the top and in the largest type on the page.

    First rather than last, unlike the terminal: a page you scroll can't rely
    on the reader reaching the end, and this is the only part of the report
    that asks for anything. The "nth week running" clause is pulled out of the
    sentence and set quietly — it's context for the ask, not part of it.
    """
    if not review.adjustment:
        return ""
    text, again = _split_repetition(review.adjustment)
    stuck = review.section(stuck_metric.KEY)
    shown = {s.key for s in sections}
    body = _link_sections(_prose(text), shown)
    # Only when the finding doesn't already account for the others: the stuck
    # section's own suggestion names them, and two sentences saying "9 other
    # tasks look like this" is worse than neither.
    if (
        stuck is not None
        and stuck.key in shown
        and (stuck.data.get("stagnant") or 0) > 1
        and "other task" not in review.adjustment
    ):
        others = int(stuck.data["stagnant"]) - 1
        again = (again + " " if again else "") + (
            f'{others} other task{"s" if others != 1 else ""} '
            f'<a href="#{escape(stuck.key)}">show the same pattern</a>.'
        )
    return (
        '<section class="decision"><p class="eyebrow">One thing to decide</p>'
        f"<p>{body}</p>"
        + (f'<p class="again">{again}</p>' if again else "")
        + "</section>"
    )


_SECTION_FLAG = re.compile(r"<code>--section (\w+)</code>")


def _link_sections(html: str, shown: set[str]) -> str:
    """Turn `--section stuck` into a link to the section, when it's on the page.

    A finding written for the terminal tells you which command to run next. In a
    document where that section is a click away, the command is the wrong
    affordance — so it becomes the link instead of being repeated as one.
    """
    return _SECTION_FLAG.sub(
        lambda m: (
            f'<a href="#{m.group(1)}">{m.group(0)}</a>'
            if m.group(1) in shown
            else m.group(0)
        ),
        html,
    )


def _split_repetition(adjustment: str) -> tuple[str, str]:
    marker = " This is the "
    if marker in adjustment and adjustment.rstrip().endswith("with this."):
        head, tail = adjustment.split(marker, 1)
        return head.strip(), escape("This is the " + tail.strip())
    return adjustment, ""


# ---------------------------------------------------------------------------
# One section
# ---------------------------------------------------------------------------

def _card(section: Section, *, detailed: bool, has_basis: bool = False) -> str:
    meanings = glossary()
    classes = "card" + ("" if section.measured else " unmeasured")
    out = [
        f'<article class="{classes}" id="{escape(section.key)}">',
        f'<h3><a href="#{escape(section.key)}">{escape(section.label)}</a></h3>',
    ]
    if section.key in meanings:
        out.append(f'<p class="means">{_prose(meanings[section.key])}</p>')

    # An unmeasured section keeps its place on the page and its headline, inside
    # a treatment that can't be mistaken for a measurement. The summary goes
    # *inside* the box rather than above it, so there is exactly one statement
    # about the blank and it is impossible to read as a zero.
    if section.measured:
        out.append(f'<p class="summary">{_summary(section.summary)}</p>')
    else:
        out.append(
            '<div class="unknown">'
            f'<p class="summary">{_summary(section.summary)}</p>'
            f'<p class="why">{_why_blank(section, has_basis=has_basis)}</p></div>'
        )

    chart = _chart_for(section)
    out.append(chart)
    out.append(_entries(section))

    facts = _facts(section, charted=bool(chart))
    if facts:
        if detailed:
            out.append(facts)
        else:
            out.append(
                '<details class="more"><summary>The detail</summary>'
                f"{facts}</details>"
            )
    out.append(_ledger(section))
    out.append(_coverage(section))
    out.append("</article>")
    return "".join(part for part in out if part)


def _why_blank(section: Section, *, has_basis: bool) -> str:
    """Why a section is blank — and that blank is not zero.

    The two reasons a section can have nothing in it are not the same reason: a
    calendar we couldn't read is a gap in the record, while a check-in feature
    nobody turned on is a gap in the setup. Saying "not measured" for both
    wears the phrase out on the one that doesn't matter, and then it can't do
    its job on the week the calendar fails.
    """
    if section.optional:
        return (
            "Nothing to read here, which is not the same as zero — this one "
            "only fills in when you use it."
        )
    unknown = (
        "Not measured this period: the inputs weren't there. That means "
        "<b>unknown</b>, not zero"
    )
    if not has_basis:
        return unknown + "."
    return unknown + ' — see <a href="#basis">what this is based on</a>.'


def _coverage(section: Section) -> str:
    if not section.coverage:
        return ""
    items = "".join(
        f"<span><b>{cover.observed} of {cover.total}</b> "
        f"{escape(_depluralize(cover.label))}</span>"
        for cover in section.coverage
    )
    return f'<p class="coverage">{items}</p>'


# ---------------------------------------------------------------------------
# Detail lines, parsed back into structure
# ---------------------------------------------------------------------------

def _facts(section: Section, *, charted: bool = False) -> str:
    """Typeset the metric's detail lines.

    The metrics format detail as padded columns because the terminal reads them
    as a column. Printing those strings into HTML as a bullet list — which is
    what this renderer used to do — throws away the structure that the padding
    *is*. So it's parsed back: `Label   value` becomes a row, a line ending in a
    colon opens a list, an indented line belongs to whatever is above it, and
    anything else is prose.

    Nothing here knows what any particular metric measures. A line whose shape
    isn't recognised still gets printed, as a note, so no detail is ever lost.
    """
    if section.key in _DATA_DRIVEN:
        lines = [line for line in section.detail if not line.startswith(" ")]
    else:
        lines = _drop_charted_groups(section, charted=charted)
    if not lines:
        return ""

    chunks: list[str] = []
    pairs: list[str] = []
    items: list[str] = []
    heading: Optional[str] = None

    def flush_pairs() -> None:
        if pairs:
            chunks.append(f'<dl class="facts">{"".join(pairs)}</dl>')
            pairs.clear()

    def flush_items() -> None:
        nonlocal heading
        if items:
            width = max(item.count(_CELL) + 1 for item in items)
            rows = "".join(_mini_row(item, width) for item in items)
            chunks.append(f'<table class="mini">{rows}</table>')
            items.clear()
        heading = None

    for raw in lines:
        text = _depluralize(raw.rstrip())
        body = text.strip()
        if not body:
            continue
        indented = text[:1] == " "

        if body.endswith(":"):
            flush_pairs()
            flush_items()
            heading = body[:-1]
            chunks.append(f'<h4 class="sublist">{escape(heading)}</h4>')
            continue

        split = re.split(r"\s{2,}", body, maxsplit=1)
        if heading is not None and indented:
            items.append(_CELL.join(re.split(r"\s{2,}", body)))
            continue
        if len(split) == 1:
            # A line the metric didn't pad, because it was already short enough
            # to read: `Met the final date 1 of 7 completed tasks`. It is still
            # a label and a value, and leaving it as prose next to rows that
            # *were* padded makes two siblings look like different kinds of
            # thing. Split at the number, which is where the value starts.
            at_number = _split_at_number(body)
            if at_number:
                split = list(at_number)
        if len(split) == 2 and not _looks_like_a_list_row(split[0]):
            flush_items()
            klass = "fact sub" if indented else "fact"
            pairs.append(
                f'<div class="{klass}"><dt>{escape(split[0])}</dt>'
                f"<dd>{_prose(split[1])}</dd></div>"
            )
            continue
        if indented:
            flush_pairs()
            items.append(_CELL.join(re.split(r"\s{2,}", body)))
            continue
        flush_pairs()
        flush_items()
        chunks.append(f'<p class="note">{_prose(body)}</p>')

    flush_pairs()
    flush_items()
    return "".join(chunks)


def _drop_charted_groups(section: Section, *, charted: bool) -> list[str]:
    """The detail lines, minus any group a chart in this section already draws."""
    charted_groups = _CHARTED_GROUPS.get(section.key)
    if not charted_groups or not charted:
        return list(section.detail)
    out: list[str] = []
    skipping = False
    for line in section.detail:
        if line.endswith(":") and not line.startswith(" "):
            skipping = line.rstrip(":").strip().lower() in charted_groups
            if skipping:
                continue
        elif skipping and line.startswith(" "):
            continue
        else:
            skipping = False
        out.append(line)
    return out


_LABEL_THEN_NUMBER = re.compile(r"^([A-Za-z][A-Za-z/'’ -]{2,33}?) (\d.*)$")


def _split_at_number(body: str) -> Optional[tuple[str, str]]:
    """`Passed still open 15` -> `("Passed still open", "15")`, or None.

    Deliberately narrow. The label has to be plain words — no comma, no colon,
    no full stop — and short, so a sentence that happens to contain a number
    ("Of those, 1 were recurring") stays a sentence instead of being torn into
    a row that reads as a measurement.
    """
    match = _LABEL_THEN_NUMBER.match(body)
    if not match:
        return None
    return match.group(1), match.group(2)


def _looks_like_a_list_row(first: str) -> bool:
    """True for `2x`, `4`, `#31` — a count leading a list row, not a label.

    A label names the thing being measured and a list row starts with how many
    of it there are. Telling them apart is what keeps "Blocks ended 17" a row
    of the fact table and "2x +3d Update cost tracker" a row of a list.
    """
    return bool(re.fullmatch(r"[#+]?\d+(\.\d+)?[a-zA-Z%]{0,2}", first))


def _mini_row(item: str, width: int) -> str:
    cells = item.split(_CELL)
    if len(cells) == 1:
        return f'<tr><td class="wide" colspan="{width}">{_prose(cells[0])}</td></tr>'
    padded = cells + [""] * (width - len(cells))
    return "<tr>" + "".join(f"<td>{_prose(cell)}</td>" for cell in padded) + "</tr>"


# ---------------------------------------------------------------------------
# Structures built from `data` rather than from prose
# ---------------------------------------------------------------------------

def _entries(section: Section) -> str:
    """The stuck queue, as named tasks with their evidence.

    Built from `data` because the prose version is cut to five for a terminal
    screen and this page has no such budget: the whole point of the section is
    that it names things, and a list that stops before the end can't be worked
    through. The reasons are the section's own words, one chip each, because
    "why is this here" is the question a named task raises.
    """
    if section.key != stuck_metric.KEY:
        return ""
    entries = section.data.get("entries") or []
    if not entries:
        return ""
    out = ['<ol class="entries">']
    for entry in entries:
        reasons = "".join(
            f"<li>{escape(_depluralize(str(reason)))}</li>"
            for reason in entry.get("reasons") or []
        )
        ref = str(entry.get("ref") or "")
        out.append(
            "<li>"
            f'<span class="ref">{escape(ref)}</span>'
            "<span>"
            f'<span class="entry-title">{escape(str(entry.get("label") or ""))}</span>'
            + (f'<ul class="reasons">{reasons}</ul>' if reasons else "")
            + "</span></li>"
        )
    out.append("</ol>")
    return "".join(out)


def _ledger(section: Section) -> str:
    """The promise ledger: what a due date was first, and what it became.

    Behind a disclosure because it's per-task reference material rather than a
    finding. The outcome column is words rather than a tick and a cross, and
    there is no colour in it: "late" is a fact about a date, and the moment it
    turns red the report is grading somebody.

    The two count columns say "all time" because they are a task's whole history,
    while the most-moved list above them counts only this period. Both are
    right, and the same task appearing as `2x` in one and `5` in the other is
    exactly the sort of thing that makes a reader stop trusting the page.
    """
    if section.key != deadlines_metric.KEY:
        return ""
    ledger = [row for row in (section.data.get("ledger") or []) if row.get("final_due")]
    if not ledger:
        return ""
    ledger = sorted(ledger, key=lambda row: -(row.get("pushes") or 0))
    rows = []
    for row in ledger:
        moved = row.get("pushes") or 0
        days = row.get("days_pushed") or 0
        if row.get("met_final") and row.get("met_original"):
            outcome = "on the first date"
        elif row.get("met_final"):
            outcome = "on the moved date"
        else:
            outcome = "not by either date"
        rows.append(
            "<tr>"
            f'<td>{escape(str(row.get("label") or ""))}</td>'
            f'<td class="num">{escape(_day(row.get("original_due")))}</td>'
            f'<td class="num">{escape(_day(row.get("final_due")))}</td>'
            f'<td class="num">{moved or "—"}</td>'
            f'<td class="num">{escape(f"+{days:g}d") if days else "—"}</td>'
            f'<td class="sec">{escape(outcome)}</td>'
            "</tr>"
        )
    return (
        '<details class="more"><summary>Every date that moved or was missed'
        "</summary>"
        '<div class="scroll"><table class="values"><thead><tr><th>Task</th>'
        '<th class="num">First seen</th><th class="num">Ended as</th>'
        '<th class="num">Moves, all time</th>'
        '<th class="num">Days, all time</th>'
        "<th>Finished</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table></div></details>'
    )


def _day(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    return f"{moment.day} {moment.strftime('%b')}"


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def _chart_for(section: Section) -> str:
    """The chart that belongs to this section, drawn inside it.

    Charts live in the section they illustrate rather than in a band of their
    own: a picture parked away from the sentence it belongs to is decoration.
    Choosing a visual form for known data is the renderer's job, which is why
    these live here and the metrics stay printable as text.

    A section with no data draws nothing at all. An empty chart is a picture of
    nothing, and a full-width bar of "unclaimed" would be a picture that lies.
    """
    if not section.measured:
        return ""
    data = section.data
    builder = _CHARTS.get(section.key)
    return builder(data) if builder else ""


def _capacity_chart(data) -> str:
    meetings = data.get("meeting_minutes", 0) or 0
    planned = data.get("planned_in_hours_minutes", 0) or 0
    if not (meetings or planned):
        return ""
    outside = (data.get("planned_minutes", 0) or 0) - planned
    return stacked_bar(
        [
            Slice("Meetings", meetings, "--series-2", humanize_minutes(meetings)),
            Slice("Blocks you planned", planned, "--series-1", humanize_minutes(planned)),
            Slice(
                "Left unclaimed",
                data.get("free_minutes", 0) or 0,
                "--track",
                humanize_minutes(data.get("free_minutes", 0) or 0),
            ),
        ],
        title=(
            "Your working hours, all "
            f"{humanize_minutes(data.get('available_minutes', 0) or 0)} of them"
        ),
        note=(
            f"{humanize_minutes(outside)} of planned work sat outside working "
            "hours and is counted under After hours, not here."
            if outside > 0
            else ""
        ),
    )


def _project_chart(data) -> str:
    return bar_rows(
        [
            (str(name), int(count), str(count))
            for name, count in (data.get("by_project") or {}).items()
        ],
        title="Finished tasks, by project",
    )


def _blocks_chart(data) -> str:
    hours = fill_hour_range(data.get("passed_open_by_hour") or {})
    if not hours:
        return ""
    return columns(
        [(hour_label(hour), count, str(count)) for hour, count in hours],
        title="Blocks that passed with the task still open, by hour of day",
        note="Hour the block started. A run of them at one hour is about the hour.",
    )


def _sittings_chart(data) -> str:
    distribution = data.get("distribution") or {}
    if not distribution:
        return ""
    ordered = sorted(distribution.items(), key=lambda pair: int(pair[0]))
    return columns(
        [(str(sittings), int(count), str(count)) for sittings, count in ordered],
        title="Finished tasks, by how many sittings they took",
        note="Sittings per task along the bottom, tasks up the side.",
    )


def _rescheduling_chart(data) -> str:
    return bar_rows(
        [
            (str(cause), int(count), str(count))
            for cause, count in (data.get("causes") or {}).items()
        ],
        title="Why blocks moved",
    )


def _dates_chart(data) -> str:
    reactive = data.get("reactive_pushes", 0) or 0
    proactive = data.get("proactive_pushes", 0) or 0
    if not (reactive or proactive):
        return ""
    return stacked_bar(
        [
            Slice("Made after the old date had passed", reactive, "--series-2", str(reactive)),
            Slice("Made before it", proactive, "--series-1", str(proactive)),
        ],
        title=f"When the {reactive + proactive} date changes were made",
        note=(
            "A date moved before it arrives renegotiates a commitment; moved "
            "after, it reports a miss. Neither is automatically the wrong move."
        ),
    )


def _growth_chart(data) -> str:
    return bar_rows(
        [
            ("Estimate raised", data.get("estimates_up", 0) or 0, str(data.get("estimates_up", 0))),
            ("Retitled", data.get("retitled", 0) or 0, str(data.get("retitled", 0))),
            ("Start pushed out", data.get("deferred", 0) or 0, str(data.get("deferred", 0))),
        ],
        title="How tasks grew",
    )


def _trend_chart(data) -> str:
    rows = trends_metric.series_for_display(data)
    if not rows:
        return ""
    labels = [
        week["label"] for week in (data.get("weeks") or []) if week.get("comparable")
    ]
    return sparklines(
        rows,
        labels=labels,
        formatter=lambda value, unit: (
            humanize_minutes(int(value)) if unit == "m" else f"{value:g}{unit}"
        ),
    )


_CHARTS = {
    capacity_metric.KEY: _capacity_chart,
    throughput_metric.KEY: _project_chart,
    followthrough_metric.KEY: _blocks_chart,
    attempts_metric.KEY: _sittings_chart,
    churn_metric.KEY: _rescheduling_chart,
    deadlines_metric.KEY: _dates_chart,
    scope_metric.KEY: _growth_chart,
    trends_metric.KEY: _trend_chart,
}


# ---------------------------------------------------------------------------
# Back matter
# ---------------------------------------------------------------------------

def _basis(review: Review) -> str:
    """What the report is built on, and what it couldn't see.

    Named "what this is based on" rather than "coverage", and linked to from
    every unmeasured section, because it's the answer to the only question that
    can make the rest of the page misleading.
    """
    items = [_prose(_depluralize(caveat)) for caveat in review.caveats]
    named = _observed_in_words(review)
    if named:
        items.append(named)
    if not items:
        return ""
    body = "".join(f"<li>{item}</li>" for item in items)
    return (
        '<section class="appendix" id="basis">'
        "<h2>What this is based on</h2>"
        f"<ul>{body}</ul></section>"
    )


def _observed_in_words(review: Review) -> str:
    """The observed days named, so the strip beside the title isn't the only copy.

    The rule that no number is reachable only as a picture applies to the day
    cells too: they are a glance, and this is the reading. Only when some days
    are missing — naming all five days of a fully observed week says nothing.
    """
    days = review.period.days()
    if not review.observed_days or not days:
        # Nothing observed at all is already said, twice: the header prints
        # "0 of 5 days" and the caveats explain that no run was recorded. A
        # third sentence here would be the repetition this page went to some
        # trouble to remove.
        return ""
    if len(review.observed_days) >= len(days):
        return ""
    observed = set(review.observed_days)
    names = [
        f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"
        for day in days
        if day.isoformat() in observed
    ]
    if not names:
        return ""
    if len(names) == 1:
        listed = names[0]
    else:
        listed = ", ".join(names[:-1]) + f" and {names[-1]}"
    return escape(f"A run observed {listed}. The other days are not in the record.")


def _appendix(sections: tuple[Section, ...]) -> str:
    """Every number in the model, as text.

    Not an afterthought: it's what makes the charts optional rather than the
    only way to reach a value, and it's the check on the prose above — if a
    figure in a sentence disagrees with this table, the sentence is wrong.
    """
    rows = [
        _row(section.label, measure, value)
        for section in sections
        for key, raw in section.data.items()
        for measure, value in _flatten(key, raw)
    ]
    if not rows:
        return ""
    return (
        '<section class="appendix"><h2>Every value</h2>'
        '<details class="more values"><summary>All values, as text</summary>'
        '<div class="scroll"><table class="values">'
        '<thead><tr><th>Section</th><th>Measure</th>'
        '<th class="num">Value</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div></details></section>'
    )


def _row(label: str, measure: str, value: str) -> str:
    return (
        f'<tr><td class="sec">{escape(label)}</td>'
        f'<td class="measure">{escape(measure)}</td>'
        f'<td class="num">{escape(value)}</td></tr>'
    )


def _flatten(measure: str, value) -> list[tuple[str, str]]:
    """`(measure, value)` pairs for one `data` entry, however nested.

    Several sections hold lists of records — the promise ledger, the tasks whose
    estimates doubled, the stuck queue — and a `str()` of those puts a Python
    repr in a cell that's meant to be the readable way to reach a value.
    """
    if isinstance(value, dict):
        return [(f"{measure}.{key}", _as_text(item)) for key, item in value.items()]
    if isinstance(value, (list, tuple)):
        out: list[tuple[str, str]] = []
        for index, item in enumerate(value):
            if isinstance(item, dict):
                # One row per field, named by whichever key identifies the
                # record, so the rows read as "ledger[Prepare PIR].pushes".
                name = item.get("label") or item.get("ref") or index
                out.extend(
                    (f"{measure}[{name}].{key}", _as_text(sub))
                    for key, sub in item.items()
                    if key not in ("label", "uuid")
                )
            else:
                out.append((f"{measure}[{index}]", _as_text(item)))
        return out
    return [(measure, _as_text(value))]


def _as_text(value) -> str:
    """One value as a reader should see it, with no Python showing through.

    `None` is the one that matters. It means the value isn't known, and printing
    the literal `None` in a table on a page whose central rule is that missing
    data is never a zero would be the same mistake in a different costume — an
    em dash says "nothing here" without naming a language.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _glossary(sections: tuple[Section, ...]) -> str:
    """Plain-language definitions, at the foot of the report.

    Each section already carries its own definition where it's used, which is
    where a first-time reader actually needs it. This is the same text collected
    in one place, for the second reading — and only for the sections on this
    page, because a glossary of things that aren't here is noise.
    """
    meanings = glossary()
    defined = [(s.label, meanings[s.key]) for s in sections if s.key in meanings]
    if not defined:
        return ""
    rows = "".join(
        f'<div class="fact"><dt>{escape(label)}</dt><dd>{_prose(means)}</dd></div>'
        for label, means in defined
    )
    return (
        '<section class="glossary"><h2>What these mean</h2>'
        f'<dl class="facts">{rows}</dl></section>'
    )


def _footer() -> str:
    """The two rules the page is built on, stated on the page.

    Not decoration. A report that never says "this is not a score" gets read as
    one, and a reader who doesn't know that a blank means unknown will read it
    as zero — which is the single way this page can mislead.
    """
    return (
        "<footer>"
        "<p>Nothing here is a score. There is no target, no streak and no "
        "grade — every line is a description of what happened, and the only "
        "thing the report asks for is one decision.</p>"
        "<p>Where a section says something isn't measured, it means unknown. "
        "It never means zero.</p>"
        "</footer>"
    )


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def _summary(text: str) -> str:
    """A section's headline sentence, with its separators set quietly."""
    parts = _depluralize(text).split(" · ")
    joined = '<span class="sep"> · </span>'.join(escape(part) for part in parts)
    return joined


def _prose(text: str) -> str:
    """Escaped text, with `backticks` becoming code and nothing else allowed.

    The only markup any metric uses is a backtick around a command, and a
    command a reader is meant to type deserves to look like one. Everything
    else — every task title, project and description in this file — is escaped
    on the way in.
    """
    return _CODE.sub(
        lambda m: f"<code>{escape(m.group(1))}</code>", escape(text)
    )


def _depluralize(text: str) -> str:
    """`1 task(s)` -> `1 task`, `7 task(s)` -> `7 tasks`.

    The metrics write `(s)` because a metric can't know its own count is one
    without saying it twice. A renderer does know, and "1 tasks" on a page
    someone is meant to trust is the kind of detail that says nobody was
    looking.

    The count has to be *next to* the word -- at most two plain words before it
    -- and that narrowness is the whole point. A task really called
    "H2 Guidance - Daniel(s)" turns up in a most-moved list, and a looser rule
    that took the nearest number anywhere to its left would rewrite it to
    "Daniels". A report that quietly edits the name of your task has done
    something far worse than print "1 tasks": every other number on the page is
    now a question. So a `(s)` with no count beside it is left exactly as its
    owner typed it.
    """

    def fix(match: re.Match) -> str:
        count, mid, word = match.group("count", "mid", "word")
        plural = float(count) != 1
        return f"{count}{mid}{word}{match.group('suffix') if plural else ''}"

    return _PLURAL.sub(fix, text)
