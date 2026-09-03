"""Markdown — the same content, for a durable weekly or monthly note.

Deliberately not a *richer* version of the terminal report. A terminal review
leaves no trail, so you can't reread what you concluded in week 34; this
exists to fix that, not to say more.
"""

from __future__ import annotations

from ..model import Review, Section


def render(review: Review, *, sections: tuple[Section, ...], detailed: bool) -> str:
    lines: list[str] = [f"# {review.period.label}", ""]
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

    return "\n".join(lines).rstrip("\n") + "\n"


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
