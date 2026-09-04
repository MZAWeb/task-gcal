"""A single self-contained HTML file — the medium for monthly trends.

Chosen over a TUI when reviews outgrow the terminal, because it costs nothing
that a TUI costs: no terminal framework, no keyboard navigation, no resize or
colour handling, no persistent interaction state, and no second application
living inside task-gcal. It is a *renderer* — one function of the same
`Review` model the terminal reads.

Deliberate constraints:

- **One file, no network.** Styles and charts are inline; there is no CDN, no
  script tag and no font download, so nothing about your task titles leaves the
  machine when you open it.
- **No JavaScript at all.** Hover text is SVG `<title>` (the browser's own
  tooltip) and disclosure is `<details>`, so the page works with scripting off.
- **Dark mode is chosen, not flipped.** Its colours are their own steps,
  validated against the dark surface.
- **A table view always exists**, so no number is reachable only by reading a
  bar.
"""

from __future__ import annotations

from html import escape

from ...intervals import humanize_minutes
from ..metrics import capacity as capacity_metric
from ..metrics import glossary, groups
from ..metrics import throughput as throughput_metric
from ..metrics import trends as trends_metric
from ..model import Review, Section
from .charts import Slice, bar_rows, sparklines, stacked_bar

# Values from the validated reference palette. Both modes are selected sets
# stepped for their own surface — the dark column is not a lightened flip —
# and the pair passes every gate in both (worst adjacent CVD ΔE 24.7 light /
# 26.8 dark, normal-vision 33.6 / 31.8).
_STYLE = """
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface-1: #fcfcfb;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --grid: #e1e0d9;
  --border: rgba(11, 11, 11, 0.10);
  --series-1: #2a78d6;
  --series-2: #eb6834;
  --track: #e1e0d9;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface-1: #1a1a19;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --border: rgba(255, 255, 255, 0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
    --track: #383835;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 2.5rem 1.5rem 4rem;
  background: var(--page);
  color: var(--text-primary);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 46rem; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 0.25rem; }
h2 { font-size: 1rem; margin: 2rem 0 0.5rem; }
.stamp { color: var(--text-muted); margin: 0 0 2rem; font-size: 0.85rem; }
.card {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1.25rem 1.5rem;
  margin-bottom: 1rem;
}
.rows { display: grid; grid-template-columns: 10rem 1fr; gap: 0.4rem 1rem; }
.rows dt { color: var(--text-secondary); }
.rows dd { margin: 0; }
.adjust {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-left: 4px solid var(--series-2);
  border-radius: 10px;
  padding: 1rem 1.25rem;
  margin: 0 0 2rem;
}
.adjust b { display: block; color: var(--text-secondary); font-weight: 600; }
.chart { margin: 0 0 1.5rem; }
.chart figcaption {
  color: var(--text-secondary);
  font-size: 0.85rem;
  margin-bottom: 0.5rem;
}
.legend {
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem 1.25rem;
  margin: 0.5rem 0 0;
  padding: 0;
  color: var(--text-secondary);
  font-size: 0.85rem;
}
.swatch {
  display: inline-block;
  width: 0.7rem;
  height: 0.7rem;
  border-radius: 2px;
  margin-right: 0.4rem;
  vertical-align: -1px;
}
text.axis, text.value {
  font: 12px system-ui, -apple-system, "Segoe UI", sans-serif;
  fill: var(--text-muted);
}
text.value { fill: var(--text-secondary); font-variant-numeric: tabular-nums; }
line.grid { stroke: var(--grid); stroke-width: 1; }
polyline.spark {
  fill: none;
  stroke: var(--series-1);
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}
circle.spark-end {
  fill: var(--series-1);
  stroke: var(--surface-1);
  stroke-width: 2;
}
.axis-note { color: var(--text-muted); font-size: 0.8rem; margin: 0; }
details { margin-bottom: 0.75rem; }
summary { cursor: pointer; color: var(--text-secondary); }
ul.detail { margin: 0.5rem 0 0; padding-left: 1.25rem; }
ul.detail li { white-space: pre-wrap; }
.coverage { color: var(--text-muted); font-size: 0.85rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { text-align: left; padding: 0.3rem 0.6rem; border-bottom: 1px solid var(--grid); }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.unmeasured { color: var(--text-muted); font-style: italic; }
.glossary { margin-top: 3rem; }
.glossary .rows { grid-template-columns: 9rem 1fr; row-gap: 0.7rem; }
.glossary dd { color: var(--text-secondary); }
.glossary p { color: var(--text-muted); font-size: 0.85rem; margin: 1.25rem 0 0; }
"""


def render(review: Review, *, sections: tuple[Section, ...], detailed: bool) -> str:
    stamp = review.generated_at.astimezone(review.period.tz)
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>task-gcal — {escape(review.period.label)}</title>",
        f"<style>{_STYLE}</style></head><body><main>",
        f"<h1>{escape(review.period.label)}</h1>",
        f'<p class="stamp">Generated {escape(stamp.strftime("%Y-%m-%d %H:%M %Z"))}'
        + (
            f" · {review.observed[0]} of {review.observed[1]} days seen"
            if review.observed is not None
            else ""
        )
        + (" · period still in progress" if review.period.in_progress else "")
        + "</p>",
    ]

    if review.adjustment:
        parts.append(
            f'<div class="adjust"><b>Look at</b>'
            f"{escape(review.adjustment)}</div>"
        )

    parts.append('<div class="card"><dl class="rows">')
    for section in sections:
        value = escape(section.summary)
        if not section.measured:
            value = f'<span class="unmeasured">{value}</span>'
        parts.append(f"<dt>{escape(section.label)}</dt><dd>{value}</dd>")
    parts.append("</dl></div>")

    parts.extend(_charts(review, sections))

    where = groups()
    group = None
    for section in sections:
        if where.get(section.key) != group:
            group = where.get(section.key)
            if group:
                parts.append(f"<h2>{escape(group)}</h2>")
        parts.append(_section_details(section, open_by_default=detailed))

    parts.append(_table(sections))

    if review.caveats:
        parts.append("<h2>Coverage</h2><ul>")
        for caveat in review.caveats:
            parts.append(f"<li>{escape(caveat)}</li>")
        parts.append("</ul>")

    parts.extend(_glossary(sections))

    parts.append("</main></body></html>")
    return "\n".join(parts) + "\n"


def _glossary(sections: tuple[Section, ...]) -> list[str]:
    """Plain-language definitions, at the foot of the report.

    At the bottom because it's reference material, not the report — but present
    every time, because "Scope" and "Stagnation" are labels a first-time reader
    has no way to guess, and a metric nobody understands is a metric nobody
    acts on. Only the sections actually in this report are defined; a glossary
    of things that aren't on the page is noise.
    """
    meanings = glossary()
    defined = [(s.label, meanings[s.key]) for s in sections if s.key in meanings]
    if not defined:
        return []
    out = [
        '<section class="glossary"><h2>What these mean</h2>',
        '<div class="card"><dl class="rows">',
    ]
    for label, means in defined:
        out.append(f"<dt>{escape(label)}</dt><dd>{escape(means)}</dd>")
    out.append("</dl>")
    out.append(
        "<p>Anything shown in grey wasn't measured for this period. That "
        "means it isn't known, not that it was zero — see Coverage above "
        "for what was missing.</p>"
    )
    out.append("</div></section>")
    return out


def _charts(review: Review, sections: tuple[Section, ...]) -> list[str]:
    """Charts for the sections whose shape has an obvious visual form.

    The renderer, not the model, decides that a stacked bar suits capacity:
    choosing a visual form for known data is exactly a renderer's job, and
    keeping it here means the metrics stay printable as text.
    """
    keys = {s.key for s in sections}
    out: list[str] = []

    cap = review.section(capacity_metric.KEY)
    # A stack whose only segment is the unclaimed remainder is a full-width
    # bar saying nothing, so it needs at least one real claim on the week.
    claimed = cap is not None and (
        cap.data.get("meeting_minutes", 0)
        or cap.data.get("planned_in_hours_minutes", 0)
    )
    if cap is not None and cap.key in keys and cap.measured and claimed:
        chart = stacked_bar(
            [
                Slice("Meetings", cap.data.get("meeting_minutes", 0), "--series-2"),
                Slice(
                    "Planned work",
                    cap.data.get("planned_in_hours_minutes", 0),
                    "--series-1",
                ),
                Slice("Unplanned", cap.data.get("free_minutes", 0), "--track"),
            ],
            title=(
                "Working hours, "
                f"{humanize_minutes(cap.data.get('available_minutes', 0))} total"
            ),
        )
        if chart:
            out.append(f'<div class="card">{chart}</div>')

    through = review.section(throughput_metric.KEY)
    if through is not None and through.key in keys:
        by_project = through.data.get("by_project") or {}
        chart = bar_rows(
            list(by_project.items()),
            title="Completed tasks by project",
            unit="tasks",
        )
        if chart:
            out.append(f'<div class="card">{chart}</div>')

    trend = review.section(trends_metric.KEY)
    if trend is not None and trend.key in keys and trend.measured:
        chart = _trend_charts(trend)
        if chart:
            out.append(f'<div class="card">{chart}</div>')

    return out


def _trend_charts(section: Section) -> str:
    """Small multiples of the weekly series, one chart per measure.

    Small multiples rather than one chart with three y scales — counts, a
    percentage and hours share no axis, and two scales on one chart is the
    single worst thing a chart can do.
    """
    rows = trends_metric.series_for_display(section.data)
    if not rows:
        return ""
    labels = [
        w["label"]
        for w in (section.data.get("weeks") or [])
        if w.get("comparable")
    ]
    return sparklines(rows, labels=labels)


def _section_details(section: Section, *, open_by_default: bool) -> str:
    if not (section.detail or section.coverage or not section.measured):
        return ""
    body: list[str] = []
    if not section.measured:
        body.append('<p class="unmeasured">Not measured — see Coverage.</p>')
    if section.detail:
        body.append('<ul class="detail">')
        body.extend(
            f"<li>{escape(' '.join(line.split()))}</li>" for line in section.detail
        )
        body.append("</ul>")
    for coverage in section.coverage:
        body.append(f'<p class="coverage">{escape(coverage.text)}</p>')
    is_open = " open" if open_by_default else ""
    return (
        f'<details{is_open}><summary>{escape(section.label)}</summary>'
        f'{"".join(body)}</details>'
    )


def _row(label: str, measure: str, value: str) -> str:
    return (
        f"<tr><td>{escape(label)}</td><td>{escape(measure)}</td>"
        f'<td class="num">{escape(value)}</td></tr>'
    )


def _flatten(measure: str, value) -> list[tuple[str, str]]:
    """`(measure, value)` pairs for one `data` entry, however nested.

    Several sections hold lists of records — the promise ledger, the tasks
    whose estimates doubled, the stagnation queue — and a `str()` of those
    puts a Python repr in a cell that's meant to be the readable way to
    reach a value.
    """
    if isinstance(value, dict):
        return [
            (f"{measure}.{key}", str(item)) for key, item in value.items()
        ]
    if isinstance(value, (list, tuple)):
        out: list[tuple[str, str]] = []
        for index, item in enumerate(value):
            if isinstance(item, dict):
                # One row per field, named by whichever key identifies the
                # record, so the rows read as "ledger[Prepare PIR].pushes".
                name = item.get("label") or item.get("ref") or index
                out.extend(
                    (f"{measure}[{name}].{key}", str(sub))
                    for key, sub in item.items()
                    if key not in ("label", "uuid")
                )
            else:
                out.append((f"{measure}[{index}]", str(item)))
        return out
    return [(measure, str(value))]


def _table(sections: tuple[Section, ...]) -> str:
    """Every number as text.

    Not an afterthought: it's what makes the charts optional rather than the
    only way to reach a value.
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
        "<details><summary>All values</summary>"
        "<table><thead><tr><th>Section</th><th>Measure</th>"
        '<th class="num">Value</th></tr></thead><tbody>'
        f'{"".join(rows)}</tbody></table></details>'
    )
