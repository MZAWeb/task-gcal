"""Boundary erosion — what this cost you outside working hours.

The one cost that never shows up as a missed deadline, and the metric with the
most obvious wrong way to build it. A naive count of per-task overrides would
report every `gcal:` value as a boundary violation, when in this data the
largest single group narrows the window rather than widening it
(`work_days=4`, Friday only) — so the first rule is **classify intent, don't
count overrides.**

Three numbers, in increasing order of honesty:

1. **Widening overrides**, kept apart from narrowing and density ones. This
   measures what you asked for.
2. **Capacity bought** — the extra minutes of window each widening opened.
   This measures intent.
3. **Evenings and weekend days actually claimed** — minutes of blocks placed
   outside the default window, and how many distinct evenings and weekend days
   they touched. This is the number that matters: an override that opened
   three hours and then placed a 30-minute block cost you half an hour, not an
   evening.

This is never a badge. There is no streak for working weekends, and the
gamified form would be the inverse — consecutive days with nothing placed
outside the window.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from ...config import parse_task_overrides
from ...intervals import humanize_minutes, merge, subtract, total_minutes
from ..model import Coverage, Section, Suggestion
from ..periods import work_windows

KEY = "boundaries"

# Keys that buy time outside the default window, as opposed to narrowing it
# (`work_days=4`) or just packing it tighter (`buffer_minutes=0`).
_WIDENING_KEYS = ("work_start_hour", "work_end_hour", "work_days")

# `overdue_horizon_days` belongs to deadline integrity, not here: extending a
# deadline's tolerance is deferral, not boundary loss.
_ELSEWHERE = ("overdue_horizon_days", "attendees")

_WEEKEND = (5, 6)


def _classify(raw: str, settings) -> str:
    """`widening`, `narrowing`, `density`, or `other` for one override string."""
    try:
        overrides = parse_task_overrides(raw)
    except ValueError:
        return "other"
    verdict = "other"
    for key, value in overrides.items():
        if key in _ELSEWHERE:
            continue
        if key == "work_days":
            added = set(value) - set(settings.work_days)
            removed = set(settings.work_days) - set(value)
            if added:
                return "widening"
            if removed:
                verdict = "narrowing"
        elif key == "work_end_hour":
            if value > settings.work_end_hour:
                return "widening"
            verdict = "narrowing"
        elif key == "work_start_hour":
            if value < settings.work_start_hour:
                return "widening"
            verdict = "narrowing"
        elif key == "buffer_minutes" and verdict == "other":
            verdict = "density"
    return verdict


def _capacity_bought(raw: str, settings) -> int:
    """Extra minutes of window one widening override opened, per working day."""
    try:
        overrides = parse_task_overrides(raw)
    except ValueError:
        return 0
    minutes = 0
    end = overrides.get("work_end_hour")
    if isinstance(end, int) and end > settings.work_end_hour:
        minutes += (end - settings.work_end_hour) * 60
    start = overrides.get("work_start_hour")
    if isinstance(start, int) and start < settings.work_start_hour:
        minutes += (settings.work_start_hour - start) * 60
    days = overrides.get("work_days")
    if days is not None:
        extra_days = set(days) - set(settings.work_days)
        minutes += len(extra_days) * (
            settings.work_end_hour - settings.work_start_hour
        ) * 60
    return minutes


def _in_play(facts) -> list:
    """Tasks this period actually involved.

    The override UDA carries no date of its own, so counting it across every
    task Taskwarrior remembers would put an all-time total inside a
    per-period section — `review --week` and `review --week --last 8` would
    print identical override counts, and a project that bought an evening a
    year ago could become this week's closing recommendation.

    "In play" is the closest honest approximation: a task with a block in the
    period, or one created or closed inside it.
    """
    with_blocks = {
        b.task_uuid for b in facts.blocks_in_period() if b.task_uuid
    }
    return [
        t
        for t in facts.tasks
        if t.uuid in with_blocks
        or facts.period.contains(t.entry)
        or facts.period.contains(t.end)
    ]


def build(facts) -> Section:
    if not facts.calendar_ok:
        return Section(
            key=KEY,
            label="Boundaries",
            summary="calendar not read",
            measured=False,
        )

    settings = facts.settings
    period = facts.period
    default_windows = list(work_windows(period, settings))
    blocks = merge(
        (max(b.start, period.start), min(b.end, period.end))
        for b in facts.blocks_in_period()
    )

    # Everything we placed that the default window doesn't cover. Computed by
    # subtraction rather than by inspecting overrides: what was actually
    # claimed is the number that matters, not what was asked for.
    outside: list[tuple[datetime, datetime]] = []
    for block in blocks:
        outside.extend(subtract(block, default_windows))
    outside = merge(outside)
    outside_minutes = total_minutes(outside)

    evenings, weekend_days, weekend_minutes, evening_minutes = _split(
        outside, settings, period
    )

    overrides = Counter()
    bought = 0
    payers: Counter = Counter()
    for task in _in_play(facts):
        if not task.overrides_raw:
            continue
        kind = _classify(task.overrides_raw, settings)
        overrides[kind] += 1
        if kind == "widening":
            bought += _capacity_bought(task.overrides_raw, settings)
            payers[task.project or "(no project)"] += 1

    # One shape whatever the week held, so a JSON consumer doesn't have to
    # guess which keys exist.
    measurements = {
        "outside_minutes": outside_minutes,
        "evening_minutes": evening_minutes,
        "weekend_minutes": weekend_minutes,
        "evenings": len(evenings),
        "weekend_days": len(weekend_days),
        "widening_overrides": overrides["widening"],
        "narrowing_overrides": overrides["narrowing"],
        "density_overrides": overrides["density"],
        "capacity_bought_minutes": bought,
        "paid_for_by": dict(payers.most_common()),
    }

    if not outside_minutes and not overrides:
        return Section(
            key=KEY,
            label="Boundaries",
            summary="nothing placed outside working hours",
            data=measurements,
        )

    pieces = []
    if evenings:
        pieces.append(
            f"{len(evenings)} evening(s) ({humanize_minutes(evening_minutes)})"
        )
    if weekend_days:
        pieces.append(
            f"{len(weekend_days)} weekend day(s) "
            f"({humanize_minutes(weekend_minutes)})"
        )
    if not pieces and outside_minutes:
        pieces.append(f"{humanize_minutes(outside_minutes)} outside hours")
    summary = " · ".join(pieces) or "no time claimed outside working hours"

    detail = [
        f"Claimed outside hours  {humanize_minutes(outside_minutes)}",
        f"  evenings             {len(evenings)} day(s), "
        f"{humanize_minutes(evening_minutes)}",
        f"  weekend days         {len(weekend_days)} day(s), "
        f"{humanize_minutes(weekend_minutes)}",
        f"Overrides              {overrides['widening']} widening, "
        f"{overrides['narrowing']} narrowing, {overrides['density']} density",
        "  counted on tasks this period involved; the UDA carries no date "
        "of its own",
        f"Capacity bought        {humanize_minutes(bought)} of extra window "
        "(intent, not time spent)",
    ]
    if payers:
        detail.append("Paid for by:")
        for project, count in payers.most_common(5):
            detail.append(f"  {count}x  {project}")
    detail.append(
        "Narrowing overrides are the opposite of erosion and are counted "
        "separately on purpose."
    )

    suggestions: tuple[Suggestion, ...] = ()
    if payers and weekend_days:
        project, count = payers.most_common(1)[0]
        if count >= 2:
            suggestions = (
                Suggestion(
                    f"{project} keeps taking time outside working hours "
                    f"({count} task(s)) — the estimate or the commitment is "
                    "wrong, not your weekend.",
                    weight=2.8,
                ),
            )

    return Section(
        key=KEY,
        label="Boundaries",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="days of the period the calendar covered",
                observed=len(period.days()),
                total=len(period.days()),
            ),
        ),
        data=measurements,
        suggestions=suggestions,
    )


def _split(outside, settings, period):
    """Separate out-of-hours time into evenings and weekend days."""
    evenings: set = set()
    weekend: set = set()
    evening_minutes = 0
    weekend_minutes = 0
    for start, end in outside:
        local_day = start.astimezone(period.tz).date()
        minutes = total_minutes([(start, end)])
        if local_day.weekday() in _WEEKEND or (
            local_day.weekday() not in settings.work_days
        ):
            weekend.add(local_day)
            weekend_minutes += minutes
        else:
            evenings.add(local_day)
            evening_minutes += minutes
    return evenings, weekend, weekend_minutes, evening_minutes
