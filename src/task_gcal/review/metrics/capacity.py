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
from ..model import Section, Suggestion
from ..words import plural

KEY = "time"

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "How much of your working hours was left for planned work once meetings "
    "are taken out. Read the rest of the review against this: a week that "
    "was mostly meetings explains a lot."
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
            summary=f"{humanize_minutes(available)} of working hours; the "
                    "calendar couldn't be read, so meetings are unknown",
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
    share = meeting_minutes / available if available else 0.0
    summary = (
        f"{humanize_minutes(available)} of working hours, "
        f"{humanize_minutes(meeting_minutes)} of them in meetings "
        f"({share:.0%}), leaving {humanize_minutes(schedulable)} free"
    )

    outside = planned_minutes - planned_in_hours
    detail = [
        f"Working hours     {humanize_minutes(available)} over "
        f"{plural(len(windows), 'day')}",
        f"Meetings          {humanize_minutes(meeting_minutes)}, "
        f"{share:.0%} of working hours",
        f"Free for work     {humanize_minutes(schedulable)}",
        # Compared with the in-hours part only: `schedulable` is
        # working-hours time, and an evening block was never competing for
        # it. The two together would print over 100% for someone who used a
        # `work_end_hour` override without over-filling their working day.
        f"Blocks booked     {humanize_minutes(planned_in_hours)}"
        + (
            f", {planned_in_hours / schedulable:.0%} of the free time"
            if schedulable
            else ""
        ),
    ]
    if outside:
        detail.append(
            f"  after hours     {humanize_minutes(outside)} more, outside "
            "working hours"
        )
    if facts.period.in_progress:
        detail.append(
            f"This {facts.period.kind} isn't over, so only hours up to now "
            "are counted."
        )

    # Both can be true at once, and both are proposed when they are: the
    # weights decide which one the review closes on, not the order of an
    # `elif`.
    suggestions: list[Suggestion] = []
    if share >= _MEETING_HEAVY:
        suggestions.append(
            Suggestion(
                f"{share:.0%} of your working hours went to meetings. Book "
                f"less work next {facts.period.kind}, not more, or clear "
                "some meetings.",
                weight=2.0,
            )
        )
    # Judged on the in-hours part, so buying an evening deliberately isn't
    # reported as over-committing the working day. Time claimed outside
    # working hours is boundary erosion's finding, not this one's.
    if schedulable and planned_in_hours > schedulable:
        suggestions.append(
            Suggestion(
                f"You booked {humanize_minutes(planned_in_hours)} of work "
                f"into {humanize_minutes(schedulable)} of free time. Something "
                "was always going to slip; book less.",
                weight=2.5,
            )
        )

    return Section(
        key=KEY,
        label="Time",
        summary=summary,
        detail=tuple(detail),
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
