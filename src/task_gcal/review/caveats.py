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

    # The count itself is in the title now, so this says only the part a
    # count can't: what a missing day means. Repeating "1 of 5" here made the
    # reader check whether the two numbers agreed instead of reading either.
    # With no records at all, the "seed some history" note below says
    # everything this one would, and more usefully.
    if records and len(observed) < len(days):
        missed = len(days) - len(observed)
        out.append(
            f"On {missed} of the {len(days)} days task-gcal didn't run, so "
            "block moves on those days are missing from Rescheduling. "
            "Everything else is read straight from Taskwarrior and the "
            "calendar."
        )

    note = facts.change_coverage_note()
    if note:
        out.append(note)

    boundaries = definition_boundaries(records)
    if boundaries:
        when = boundaries[0].astimezone(period.tz).strftime("%a %d %b")
        out.append(
            f"Your task-gcal settings changed on {when}, so numbers from "
            "before and after that day don't compare exactly."
        )

    if not facts.calendar_ok:
        out.append(
            "The calendar couldn't be read, so meetings, blocks and "
            "after-hours work aren't measured."
        )
    else:
        out.append(f"Calendar: {facts.settings.calendar_id}.")

    if facts.journal.unreadable_lines:
        out.append(
            f"{facts.journal.unreadable_lines} line(s) of task-gcal's run "
            "log couldn't be read and were skipped."
        )

    if period.in_progress:
        out.append(
            f"This {period.kind} isn't over yet; only time up to now is "
            "counted."
        )

    if not records:
        out.append(
            f"task-gcal didn't run during this {period.kind}, so there's no "
            "record of blocks moving."
        )

    return tuple(out)
