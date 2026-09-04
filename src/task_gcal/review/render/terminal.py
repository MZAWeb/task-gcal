"""Concise human output — the default, and the one that ships first.

A review is a document, not an application. The default is one screen: a
label column, one summary line each, and one closing thing to look at.
Everything else waits behind `--section`, because a report that prints
thirteen metrics every time is a dashboard.
"""

from __future__ import annotations

from ..metrics import trends as trends_metric
from ..model import Review, Section

# Wide enough for the longest label, so the summaries line up into a column
# you can read down.
_LABEL_WIDTH = 15


def render(review: Review, *, sections: tuple[Section, ...], detailed: bool) -> str:
    lines: list[str] = [review.period.label, ""]

    # A section with nothing to measure costs a line and says nothing, and the
    # one-screen budget is tight. They're named together below instead — but
    # only in the summary: asking for a section explicitly should show it even
    # if the answer is "there's no data".
    shown = (
        sections if detailed else tuple(s for s in sections if s.measured)
    )
    for section in shown:
        lines.extend(_summary_lines(section))
        if detailed:
            lines.extend(_detail_block(section))

    if not detailed:
        unmeasured = tuple(s.label for s in sections if not s.measured)
        if unmeasured:
            lines.append("")
            lines.append(f"Not measured   {', '.join(unmeasured)}")

    if review.caveats:
        lines.append("")
        lines.append("Coverage")
        for caveat in review.caveats:
            lines.extend(_wrap(caveat, indent="  "))

    if review.adjustment:
        lines.append("")
        lines.extend(_wrap(f"Look at: {review.adjustment}"))

    return "\n".join(lines) + "\n"


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
