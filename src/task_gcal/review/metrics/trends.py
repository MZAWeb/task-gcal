"""Trends — the same metrics over time, not new ones.

A monthly review adds history rather than more measurements. Three series,
each already reported for the period itself: how much got done, how many
blocks were honoured, and how many deadlines moved.

The rule that shapes this: **do not compare incompatible periods.** A change
to settings or to a metric's definition means the same field measured two
different things on either side of it, so the trend is annotated at the
boundary and the median is computed only over the comparable tail. Averaging
across it would produce a number that describes nothing.

Everything here is derived from data the review already loaded — completions
from Taskwarrior, blocks from the calendar, pushes from the journal. Meeting
load is deliberately absent: it would need a calendar query per past week,
and a trend nobody asked for isn't worth the round trips.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Optional

from ...intervals import humanize_minutes
from ..model import Coverage, Section
from ..periods import KIND_MONTH, week_of

KEY = "trends"

# Twelve weeks: long enough for a seasonal shape to show, short enough that
# the definition is likely to have held for most of it.
WEEKS = 12

# The eight block heights, low to high. A sparkline is the whole point of
# putting a trend on one line.
_BARS = "▁▂▃▄▅▆▇█"


@dataclass(frozen=True)
class WeekPoint:
    """One week of the series, and whether it's comparable with today."""

    label: str
    completed: int
    planned_minutes: int
    blocks_ended: int
    blocks_honored: int
    observed_pushes: int
    settings_hash: Optional[str]
    comparable: bool = True

    @property
    def follow_through(self) -> Optional[float]:
        if not self.blocks_ended:
            return None
        return self.blocks_honored / self.blocks_ended


def comparable_tail(weeks: list) -> list:
    """The unbroken run of weeks since the last definition change.

    Not simply "every comparable week": an older stretch with no observations
    at all counts as comparable, so filtering would produce a series with a
    hole in the middle and then join across it. The tail is the part that is
    genuinely measured the same way throughout.
    """
    last_boundary = -1
    for index, week in enumerate(weeks):
        if not _is_comparable(week):
            last_boundary = index
    return weeks[last_boundary + 1:]


def _is_comparable(week) -> bool:
    return (
        week.comparable
        if isinstance(week, WeekPoint)
        else bool(week.get("comparable"))
    )


def series_for_display(section_data: dict) -> list[tuple[str, list, str]]:
    """`(name, values, unit)` per series, for whichever renderer wants them.

    Only the comparable tail: a line drawn across a boundary where the metric
    changed meaning is a picture of two different measurements pretending to
    be one.
    """
    weeks = comparable_tail(list(section_data.get("weeks") or []))
    if len(weeks) < 2:
        return []
    return [
        ("Completed", [float(w["completed"]) for w in weeks], ""),
        (
            "Follow-through",
            [
                round(w["blocks_honored"] / w["blocks_ended"] * 100)
                if w["blocks_ended"]
                else 0
                for w in weeks
            ],
            "%",
        ),
        ("Observed deadline pushes", [float(w["observed_pushes"]) for w in weeks], ""),
    ]


def sparkline(values: list[Optional[float]]) -> str:
    """Render a series as block characters; a gap for a missing value.

    Scaled from zero rather than from the minimum, so a flat-but-high series
    doesn't look like a flat-but-low one.
    """
    present = [v for v in values if v is not None]
    if not present:
        return ""
    peak = max(present)
    if peak <= 0:
        # Present and zero throughout, which is not the same as absent — a
        # row of blanks would read as missing data.
        return "".join(_BARS[0] if v is not None else " " for v in values)
    out = []
    for value in values:
        if value is None:
            out.append(" ")
            continue
        index = min(len(_BARS) - 1, int(value / peak * (len(_BARS) - 1) + 0.5))
        out.append(_BARS[index])
    return "".join(out)


def build(facts) -> Section:
    points = series(facts)
    if len(points) < 2:
        return Section(
            key=KEY,
            label="Trend",
            summary="not enough history to compare periods",
            measured=False,
            data={},
        )

    # The tail since the last change, not every week that happens to match:
    # "measured the same way from here onward" is the claim a trend can make.
    comparable = comparable_tail(points)
    comparable_from = (
        comparable[0].label if comparable and len(comparable) < len(points) else None
    )
    if not comparable:
        # Every week predates the current definition, which is a real answer.
        comparable = points[-1:]
        comparable_from = comparable[0].label

    completed = [float(p.completed) for p in points]

    median_completed = _median([p.completed for p in comparable])
    # The summary and detail stay numeric. Each renderer draws the series in
    # its own medium — block characters in a terminal, SVG in HTML — which is
    # a presentation choice rather than something to bake into the model.
    summary = (
        f"{len(points)}w · done {_endpoints(completed)} · "
        f"median {median_completed:g}"
    )
    if comparable_from:
        summary += " · definition changed mid-trend"

    # The per-series lines are composed by the renderer from `data`, since
    # each medium draws the series differently. What's left here is what
    # reads the same everywhere.
    detail = [
        f"Median completed  {median_completed:g} over "
        f"{len(comparable)} comparable week(s)",
        "Oldest week first. Trends use only weeks measured the same way.",
    ]
    if comparable_from:
        detail.append(
            f"Settings or definitions changed, so only {comparable_from} "
            "onward is comparable; earlier weeks are shown but excluded from "
            "the median."
        )
    planned = [p.planned_minutes for p in points]
    detail.append(
        f"Planned minutes   latest {humanize_minutes(planned[-1])}, "
        f"earliest {humanize_minutes(planned[0])}"
    )

    return Section(
        key=KEY,
        label="Trend",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="weeks comparable with the current definition",
                observed=len(comparable),
                total=len(points),
            ),
        ),
        data={
            "weeks": [
                {
                    "label": p.label,
                    "completed": p.completed,
                    "planned_minutes": p.planned_minutes,
                    "blocks_ended": p.blocks_ended,
                    "blocks_honored": p.blocks_honored,
                    "observed_pushes": p.observed_pushes,
                    "comparable": p.comparable,
                }
                for p in points
            ],
            "median_completed": median_completed,
            "comparable_from": comparable_from,
        },
    )


def _anchor(period):
    """The week the series ends on.

    Two traps, both about the period's end being *exclusive*:

    - `week_of(period.end.date())` lands a week late. Reviewing last week
      would anchor on this one, dropping the oldest week and adding a week
      that isn't in the period at all.
    - A week running past the period would be measured with inputs that stop
      at the period's end: real completions (Taskwarrior isn't date-bounded)
      but no blocks and no observed pushes, so it reads as a productive week
      with a follow-through of zero. Reviewing August must not end its trend
      on the week of 31 Aug – 6 Sep.

    The exception is a period still in progress: its own final week is
    partial by definition, and that's the week being reported on.
    """
    last_instant = period.end - timedelta(microseconds=1)
    anchor = week_of(last_instant.astimezone(period.tz).date(), period.tz)
    if anchor.end > period.end and not period.in_progress:
        anchor = anchor.shifted(-1)
    return anchor


def series(facts, *, weeks: Optional[int] = None) -> list[WeekPoint]:
    """The last `weeks` whole weeks ending with the period, oldest first."""
    weeks = WEEKS if weeks is None else weeks
    anchor = _anchor(facts.period)
    windows = [anchor.shifted(-(weeks - 1 - n)) for n in range(weeks)]

    timelines = facts.timelines
    latest_hash = _latest_hash(facts)

    points: list[WeekPoint] = []
    for window in windows:
        bounds = (window.start, window.end)
        completed = [
            t
            for t in facts.tasks
            if t.status == "completed" and window.contains(t.end)
        ]
        ended = [b for b in facts.blocks if window.contains(b.end)]
        honored = [
            b
            for b in ended
            if _task_done_by(facts, b.task_uuid, b.end)
        ]
        pushes = sum(
            len(t.pushes(within=bounds)) for t in timelines.values()
        )
        points.append(
            WeekPoint(
                label=window.label,
                completed=len(completed),
                planned_minutes=sum(
                    t.estimate_minutes or 0 for t in completed
                ),
                blocks_ended=len(ended),
                blocks_honored=len(honored),
                observed_pushes=pushes,
                settings_hash=_hash_in(facts, window),
            )
        )
    return _mark_comparable(points, latest_hash)


def _mark_comparable(
    points: list[WeekPoint], latest_hash: Optional[str]
) -> list[WeekPoint]:
    """Decide comparability once the whole series is known.

    A week whose settings hash differs from the current one measured something
    else, so a trend must not span the boundary. A week with no hash at all
    can't disagree with anything, so it stays comparable rather than being
    excluded for missing metadata — otherwise a quiet week would break the
    series for bookkeeping reasons.
    """
    return [
        replace(
            point,
            comparable=(
                point.settings_hash is None or point.settings_hash == latest_hash
            ),
        )
        for point in points
    ]


def _task_done_by(facts, uuid: Optional[str], moment) -> bool:
    task = facts.by_uuid().get(uuid or "")
    return task is not None and task.end is not None and task.end <= moment


def _latest_hash(facts) -> Optional[str]:
    for record in reversed(facts.journal.records):
        if record.settings_hash:
            return record.settings_hash
    return None


def _hash_in(facts, window) -> Optional[str]:
    for record in reversed(facts.journal.records):
        if window.contains(record.at) and record.settings_hash:
            return record.settings_hash
    return None


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def _endpoints(values: list[Optional[float]], *, suffix: str = "") -> str:
    """`first → last`, skipping gaps, for the numbers behind the sparkline."""
    present = [v for v in values if v is not None]
    if not present:
        return "no data"
    return f"{present[0]:g}{suffix} → {present[-1]:g}{suffix}"


def wanted_for(kind: str) -> bool:
    """Trends belong in a monthly summary, not a weekly one.

    A month is where history is the point. A week's own numbers are the
    story, and the one-screen budget is tight — `--section trends` still
    shows it there.
    """
    return kind == KIND_MONTH
