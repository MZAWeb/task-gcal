"""Capacity — where the week went before you started.

First in the report on purpose: it's the denominator for every completion
statistic below it. A difficult week with half its capacity consumed by
meetings is a different thing from an unexplained miss, and without this line
the two look identical.
"""

from __future__ import annotations

from ...intervals import (
    clip_to_windows,
    duration_minutes,
    humanize_minutes,
    merge,
    total_minutes,
)
from ..model import Coverage, Section, Suggestion

KEY = "time"

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "How much of the period was actually free to plan work into, once "
    "meetings and non-working hours are taken out. Read every other number "
    "here against this one."
)

# Above this share of working time in meetings, the week's throughput needs no
# further explanation. Chosen to be obviously bad rather than borderline.
_MEETING_HEAVY = 0.5


def build(facts) -> Section:
    windows = facts.work_windows()
    available = facts.working_minutes()

    if not facts.calendar_ok:
        return Section(
            key=KEY,
            label="Time",
            summary=f"{humanize_minutes(available)} of working hours — "
                    "couldn't read the calendar, so meetings are unknown",
            measured=False,
            data={"available_minutes": available},
        )

    # Merged, so two people double-booking you cost one hour of capacity,
    # and clipped to the working windows, so a 22:00 call doesn't eat into a
    # denominator it was never part of.
    meetings_in_hours = facts.meetings_in_working_hours()
    meeting_minutes = total_minutes(meetings_in_hours)

    ours = merge(
        (max(b.start, facts.period.start), min(b.end, facts.period.end))
        for b in facts.blocks_in_period()
    )
    planned_minutes = total_minutes(ours)
    # Blocks can sit outside working hours — that's what boundary erosion
    # measures — so the part inside them is tracked separately. Only that
    # part is comparable with the working-hours denominator.
    planned_in_hours = total_minutes(clip_to_windows(ours, windows))
    schedulable = max(available - meeting_minutes, 0)
    # A sentence, not three numbers separated by middots. The subtraction is
    # the finding — "two thirds of the week was already gone" is what a person
    # takes from this line — and three co-equal numbers hide it.
    summary = (
        f"{humanize_minutes(available)} working, "
        f"{humanize_minutes(meeting_minutes)} in meetings, "
        f"{humanize_minutes(schedulable)} left"
    )

    share = meeting_minutes / available if available else 0.0
    detail = [
        f"Working hours     {humanize_minutes(available)} "
        f"across {len(windows)} working day(s)",
        f"Meetings          {humanize_minutes(meeting_minutes)} "
        f"({share:.0%} of working hours)",
        f"Left to schedule  {humanize_minutes(schedulable)}",
        # Compared with the in-hours part only: `schedulable` is
        # working-hours time, and an evening block was never competing for
        # it. The two together would print over 100% for someone who used a
        # `work_end_hour` override without over-filling their working day.
        f"Blocks planned    {humanize_minutes(planned_minutes)}"
        + (
            f" ({planned_in_hours / schedulable:.0%} of what was left, "
            f"{humanize_minutes(planned_minutes - planned_in_hours)} outside "
            "hours)"
            if schedulable
            else ""
        ),
    ]
    if facts.period.in_progress:
        detail.append(
            "The period is still running, so hours that haven't happened "
            "yet are not counted."
        )

    # Both can be true at once, and both are proposed when they are: the
    # weights decide which one the review closes on, not the order of an
    # `elif`.
    suggestions: list[Suggestion] = []
    if share >= _MEETING_HEAVY:
        suggestions.append(
            Suggestion(
                f"{share:.0%} of your working hours were meetings — plan "
                f"less next {facts.period.kind}, not more.",
                weight=2.0,
            )
        )
    # Judged on the in-hours part, so buying an evening deliberately isn't
    # reported as over-committing the working day. Time claimed outside
    # working hours is boundary erosion's finding, not this one's.
    if schedulable and planned_in_hours > schedulable:
        suggestions.append(
            Suggestion(
                f"You planned {humanize_minutes(planned_in_hours)} into "
                f"{humanize_minutes(schedulable)} of free working time.",
                weight=2.5,
            )
        )

    return Section(
        key=KEY,
        label="Time",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="days of the period observed",
                observed=len(facts.observed_days()),
                total=len(facts.period.days()),
            ),
        ),
        data={
            "available_minutes": available,
            "meeting_minutes": meeting_minutes,
            "schedulable_minutes": schedulable,
            "planned_minutes": planned_minutes,
            "planned_in_hours_minutes": planned_in_hours,
            "free_minutes": max(
                available - meeting_minutes - planned_in_hours, 0
            ),
            "meeting_share": round(share, 4),
            "working_days": len(windows),
            "longest_meeting_minutes": max(
                (duration_minutes(m) for m in meetings_in_hours), default=0
            ),
        },
        suggestions=tuple(suggestions),
    )
