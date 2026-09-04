"""How much should you trust the numbers above?

Separate from the metrics because it is about the *report*, not about any one
line of it. Three things belong here and nowhere else:

- unobserved days, so missing data can't read as a quiet week;
- a settings or metric-definition change inside the period, which makes the
  two halves incomparable and must be annotated rather than averaged;
- what we couldn't read at all.
"""

from __future__ import annotations

from ..journal import definition_boundaries


def build(facts) -> tuple[str, ...]:
    out: list[str] = []
    period = facts.period
    days = period.days()
    observed = facts.observed_days()
    records = facts.records_in_period()

    # With no records at all, the "seed some history" note below says
    # everything this one would, and more usefully.
    if records and len(observed) < len(days):
        out.append(
            f"{len(observed)} of {len(days)} day(s) observed. "
            "Anything the journal didn't see is missing, not zero."
        )

    note = facts.change_coverage_note()
    if note:
        out.append(note)

    boundaries = definition_boundaries(records)
    if boundaries:
        when = boundaries[0].astimezone(period.tz).strftime("%a %d %b")
        out.append(
            f"Settings or metric definitions changed during this period "
            f"(first on {when}), so the two halves are not strictly "
            "comparable."
        )

    if not facts.calendar_ok:
        out.append(
            "The calendar could not be read, so capacity, meeting load and "
            "follow-through are unmeasured."
        )
    else:
        out.append(f"Measured on calendar {facts.settings.calendar_id}.")

    if facts.journal.unreadable_lines:
        out.append(
            f"{facts.journal.unreadable_lines} journal line(s) were "
            "unreadable and skipped."
        )

    if period.in_progress:
        out.append(
            f"This {period.kind} is still in progress; hours that haven't "
            "happened yet are excluded."
        )

    if not records:
        out.append(
            "This period has no scheduling runs recorded, so nothing is "
            "known about how blocks moved during it."
        )

    return tuple(out)
