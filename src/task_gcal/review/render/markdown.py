"""Markdown — the same content, for a durable weekly or monthly note.

Deliberately not a *richer* version of the terminal report. A terminal review
leaves no trail, so you can't reread what you concluded in week 34; this
exists to fix that, not to say more.
"""

from __future__ import annotations

from ..metrics import glossary
from ..model import Review, Section


def render(review: Review, *, sections: tuple[Section, ...], detailed: bool) -> str:
    title = review.period.label
    if review.observed is not None:
        seen, total = review.observed
        title += f" · {seen} of {total} days seen"
    lines: list[str] = [f"# {title}", ""]
    stamp = review.generated_at.astimezone(review.period.tz)
    lines.append(f"*Generated {stamp:%Y-%m-%d %H:%M %Z}*")
    lines.append("")

    lines.append("| | |")
    lines.append("| --- | --- |")
    for section in sections:
        lines.append(f"| **{section.label}** | {_escape(section.summary)} |")
    lines.append("")

    if review.adjustment:
        lines.append(f"> **Look at:** {_escape(review.adjustment)}")
        lines.append("")

    if detailed:
        for section in sections:
            lines.extend(_detail(section))

    if review.caveats:
        lines.append("## Coverage")
        lines.append("")
        for caveat in review.caveats:
            lines.append(f"- {_escape(caveat)}")
        lines.append("")

    lines.extend(_glossary(sections))

    return "\n".join(lines).rstrip("\n") + "\n"


def _glossary(sections: tuple[Section, ...]) -> list[str]:
    """Plain-language definitions, at the foot of the note.

    A saved note outlives the context you wrote it in, and it's the format you
    hand to somebody else — so it needs the definitions even more than the
    screen you read once does.
    """
    meanings = glossary()
    defined = [(s.label, meanings[s.key]) for s in sections if s.key in meanings]
    if not defined:
        return []
    out = ["## What these mean", ""]
    for label, means in defined:
        out.append(f"- **{label}** — {_escape(means)}")
    out.append("")
    out.append(
        "*Anything marked not measured isn't known for this period, rather "
        "than zero.*"
    )
    out.append("")
    return out


def _detail(section: Section) -> list[str]:
    out = [f"## {section.label}", ""]
    if not section.measured:
        out.append("*Not measured — see Coverage.*")
        out.append("")
    for line in section.detail:
        out.append(f"- {_escape(' '.join(line.split()))}")
    if section.detail:
        out.append("")
    for coverage in section.coverage:
        out.append(f"*{_escape(coverage.text)}*")
    if section.coverage:
        out.append("")
    return out


def _escape(text: str) -> str:
    """Keep a task title with a pipe in it from breaking the summary table."""
    return text.replace("|", "\\|")
