"""Concise human output — the default, and the one that ships first.

A review is a document, not an application. The default is one screen: a
label column, one summary line each, and one closing thing to look at.
Everything else waits behind `--section`, because a report that prints
thirteen metrics every time is a dashboard.
"""

from __future__ import annotations

from ..metrics import groups, section_keys
from ..metrics import trends as trends_metric
from ..model import Review, Section

# Wide enough for the longest label, so the summaries line up into a column
# you can read down.
_LABEL_WIDTH = 15


def render(review: Review, *, sections: tuple[Section, ...], detailed: bool) -> str:
    lines: list[str] = [_title(review), ""]

    # A section with nothing to measure costs a line and says nothing, and the
    # one-screen budget is tight. They're named together below instead — but
    # only in the summary: asking for a section explicitly should show it even
    # if the answer is "there's no data".
    shown = (
        sections if detailed else tuple(s for s in sections if s.measured)
    )
    # Headings only in the detailed views. On the one-screen summary they'd
    # cost a third of the budget to organise seven lines that already read in
    # order.
    where = groups(review.period.kind) if detailed else {}
    group = None
    for section in shown:
        if detailed and where.get(section.key) != group:
            group = where.get(section.key)
            if group:
                lines.append(f"── {group} ".ljust(78, "─"))
                lines.append("")
        lines.extend(_summary_lines(section))
        if detailed:
            lines.extend(_detail_block(section))

    if not detailed:
        unmeasured = tuple(
            s.label for s in sections if not s.measured and not s.optional
        )
        if unmeasured:
            lines.append("")
            lines.append(f"Not measured   {', '.join(unmeasured)}")

    if review.caveats:
        lines.append("")
        lines.append("Coverage")
        for caveat in review.caveats:
            lines.extend(_wrap(caveat, indent="  "))

    # The menu goes above the closing line, not below it. Ending on "Look at"
    # is the point of the whole report; a list of flags after it would be the
    # last thing you read.
    if not detailed:
        rest = _more(tuple(s.key for s in sections))
        if rest:
            lines.append("")
            lines.extend(rest)

    if review.adjustment:
        lines.append("")
        lines.extend(_wrap(f"Look at: {review.adjustment}"))

    return "\n".join(lines) + "\n"


def _title(review: Review) -> str:
    """The period, and how much of it was actually seen.

    Coverage belongs here as well as in the footer: "1 of 5 days" changes how
    every number below it should be read, and a reader who meets it at the
    bottom has already believed the top.
    """
    if review.observed is None:
        return review.period.label
    seen, total = review.observed
    return f"{review.period.label} · {seen} of {total} days seen"


def _more(shown: tuple[str, ...]) -> list[str]:
    """Name the sections that aren't on the screen.

    The complaint that produced this was exact: `--section` is no use if you
    have to know the names already. Naming the ones you *have* just read would
    be noise, so this is the rest — the list you'd otherwise have to remember
    exists.
    """
    rest = [key for key in section_keys() if key not in shown]
    if not rest:
        return []
    return _wrap(
        " · ".join(rest),
        indent="Also: task-gcal review --section ",
        subsequent="  ",
    )


def _summary_lines(section: Section) -> list[str]:
    """The label column, then a sentence that wraps under itself.

    Summaries are sentences now rather than middot-separated numbers, so they
    can run past the terminal. Wrapping into the column keeps the label
    scannable while letting the line read like something a person wrote.
    """
    label = section.label[:_LABEL_WIDTH].ljust(_LABEL_WIDTH)
    return _wrap(
        section.summary, indent=label, subsequent=" " * _LABEL_WIDTH
    )


def _sparklines(section: Section) -> list[str]:
    """One line per trend series: name, block-character shape, endpoints.

    The choice of medium belongs to the renderer. HTML draws real SVG for the
    same data; these characters would render there as a row of solid boxes.
    """
    if section.key != trends_metric.KEY:
        return []
    rows = trends_metric.series_for_display(section.data)
    if not rows:
        return []
    width = max(len(name) for name, _values, _unit in rows)
    out = []
    for name, values, unit in rows:
        bars = trends_metric.sparkline(list(values))
        if not bars:
            continue
        first, last = values[0], values[-1]
        out.append(
            f"  {name.ljust(width)}  {bars}  "
            f"{first:g}{unit} → {last:g}{unit}"
        )
    return out


def _detail_block(section: Section) -> list[str]:
    out: list[str] = _sparklines(section)
    for line in section.detail:
        out.extend(_fit(line, indent="  "))
    for coverage in section.coverage:
        out.append(f"  ({coverage.text})")
    if not section.measured:
        out.append("  (not measured — see Coverage below)")
    out.append("")
    return out


def _fit(text: str, *, indent: str = "", width: int = 78) -> list[str]:
    """Emit a detail line, wrapping only if it doesn't fit.

    Detail lines are label-padded columns, so the common case must go out
    byte for byte — reflowing them would collapse the padding the metric
    builders put there and turn a readable column into a paragraph.

    A line that does need wrapping keeps its own leading indentation on every
    line it becomes. Otherwise a long entry in a nested list would start two
    columns to the left of its short neighbours, which reads as a different
    list rather than a longer item.
    """
    own = text[: len(text) - len(text.lstrip())]
    body = text.strip()
    if len(indent) + len(text.rstrip()) <= width:
        return [indent + text.rstrip()]
    lead = indent + own
    return _wrap(body, indent=lead, subsequent=lead + "  ", width=width)


def _wrap(
    text: str, *, indent: str = "", subsequent: str = None, width: int = 78
) -> list[str]:
    """Wrap prose without importing textwrap for four lines of output."""
    if subsequent is None:
        subsequent = indent
    words = text.split()
    if not words:
        return [indent.rstrip()]
    lines: list[str] = []
    current = indent + words[0]
    prefix = subsequent
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = prefix + word
    lines.append(current)
    return lines
