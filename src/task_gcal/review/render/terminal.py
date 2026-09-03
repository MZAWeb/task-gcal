"""Concise human output — the default, and the one that ships first.

A review is a document, not an application. The default is one screen: a
label column, one summary line each, and one closing thing to look at.
Everything else waits behind `--section`, because a report that prints
thirteen metrics every time is a dashboard.
"""

from __future__ import annotations

from ..model import Review, Section

# Wide enough for the longest label, so the summaries line up into a column
# you can read down.
_LABEL_WIDTH = 15


def render(review: Review, *, sections: tuple[Section, ...], detailed: bool) -> str:
    lines: list[str] = [review.period.label, ""]

    for section in sections:
        lines.append(_summary_line(section))
        if detailed:
            lines.extend(_detail_block(section))

    if not detailed:
        unmeasured = review.incomplete_sections
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


def _summary_line(section: Section) -> str:
    label = section.label[:_LABEL_WIDTH].ljust(_LABEL_WIDTH)
    return f"{label}{section.summary}"


def _detail_block(section: Section) -> list[str]:
    out: list[str] = []
    for line in section.detail:
        out.extend(_fit(line, indent="  ", subsequent="    "))
    for coverage in section.coverage:
        out.append(f"  ({coverage.text})")
    if not section.measured:
        out.append("  (not measured — see Coverage below)")
    out.append("")
    return out


def _fit(
    text: str, *, indent: str = "", subsequent: str = None, width: int = 78
) -> list[str]:
    """Emit a detail line, wrapping only if it doesn't fit.

    Detail lines are label-padded columns, so the common case must go out
    byte for byte — reflowing them would collapse the padding the metric
    builders put there and turn a readable column into a paragraph.
    """
    if len(indent) + len(text) <= width:
        return [indent + text]
    return _wrap(text, indent=indent, subsequent=subsequent, width=width)


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
