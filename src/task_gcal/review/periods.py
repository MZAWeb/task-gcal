"""What "this week" and "last month" mean, decided once.

A week is an **ISO week: Monday start, in the local timezone**. Written down
here and nowhere else so no two metrics can disagree about which day a
Sunday-evening block belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Iterator, Optional

KIND_WEEK = "week"
KIND_MONTH = "month"


@dataclass(frozen=True)
class Period:
    """A half-open local-time window, expressed in UTC.

    `end` is exclusive. A period that includes today ends at `now` rather
    than at midnight, so nothing is measured against hours that haven't
    happened yet — a Tuesday review would otherwise report three days of
    unused capacity as a shortfall.
    """

    kind: str
    label: str
    start: datetime  # inclusive, UTC
    end: datetime  # exclusive, UTC
    tz: tzinfo
    # The window the period *would* have covered if it had finished. Equal to
    # `end` for a past period; later than it for the current one.
    nominal_end: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.nominal_end is None:
            object.__setattr__(self, "nominal_end", self.end)

    @property
    def in_progress(self) -> bool:
        return self.nominal_end > self.end

    def days(self) -> list[date]:
        """Local dates the period touches, in order."""
        out: list[date] = []
        day = self.start.astimezone(self.tz).date()
        last = (self.end - timedelta(microseconds=1)).astimezone(self.tz).date()
        while day <= last:
            out.append(day)
            day += timedelta(days=1)
        return out

    def contains(self, moment: Optional[datetime]) -> bool:
        return moment is not None and self.start <= moment < self.end

    def shifted(self, count: int) -> "Period":
        """The same kind of period, `count` steps earlier (or later).

        Used for previous-period comparison and for a trailing median, both
        of which have to line up exactly with the period being reported.
        """
        if self.kind == KIND_WEEK:
            anchor = self.start.astimezone(self.tz).date() + timedelta(
                weeks=count
            )
            return week_of(anchor, self.tz)
        anchor = _month_shift(self.start.astimezone(self.tz).date(), count)
        return month_of(anchor, self.tz)


def _local_midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)


def _month_shift(day: date, count: int) -> date:
    total = (day.year * 12 + day.month - 1) + count
    return date(total // 12, total % 12 + 1, 1)


def week_of(day: date, tz: tzinfo, *, now: Optional[datetime] = None) -> Period:
    """The ISO week containing `day`, Monday-based, in `tz`."""
    monday = day - timedelta(days=day.weekday())
    start = _local_midnight(monday, tz)
    nominal_end = _local_midnight(monday + timedelta(days=7), tz)
    iso_year, iso_week, _ = monday.isocalendar()
    label = f"Week {iso_week}"
    # Only spell out the year when it isn't the one the week's own days fall
    # in — an ISO week can belong to the previous or next calendar year.
    if iso_year != monday.year:
        label = f"Week {iso_week} ({iso_year})"
    return _clamped(KIND_WEEK, label, start, nominal_end, tz, now)


def month_of(day: date, tz: tzinfo, *, now: Optional[datetime] = None) -> Period:
    first = day.replace(day=1)
    start = _local_midnight(first, tz)
    nominal_end = _local_midnight(_month_shift(first, 1), tz)
    label = f"{first:%B %Y}"
    return _clamped(KIND_MONTH, label, start, nominal_end, tz, now)


def _clamped(
    kind: str,
    label: str,
    start: datetime,
    nominal_end: datetime,
    tz: tzinfo,
    now: Optional[datetime],
) -> Period:
    end = nominal_end
    if now is not None and now < nominal_end:
        end = max(now, start)
    return Period(
        kind=kind,
        label=label,
        start=start,
        end=end,
        tz=tz,
        nominal_end=nominal_end,
    )


def resolve(
    kind: str, tz: tzinfo, *, now: datetime, offset: int = 0
) -> Period:
    """The week or month containing `now`, `offset` periods back."""
    today = now.astimezone(tz).date()
    period = (
        week_of(today, tz, now=now)
        if kind == KIND_WEEK
        else month_of(today, tz, now=now)
    )
    if offset:
        period = period.shifted(-offset)
        # A past period is complete, so it shouldn't be clamped to `now`;
        # `shifted` rebuilds it without a clock, which already does that.
    return period


def work_windows(period: Period, settings) -> Iterator[tuple[datetime, datetime]]:
    """Working-hours windows inside the period, one per working day.

    Clipped to the period, so a review run on Tuesday counts Monday and the
    part of Tuesday that has happened — not the whole week.
    """
    for day in period.days():
        if day.weekday() not in settings.work_days:
            continue
        midnight = datetime.combine(day, time(0, 0), tzinfo=period.tz)
        start = (midnight + timedelta(hours=settings.work_start_hour)).astimezone(
            timezone.utc
        )
        end = (midnight + timedelta(hours=settings.work_end_hour)).astimezone(
            timezone.utc
        )
        lo, hi = max(start, period.start), min(end, period.end)
        if hi > lo:
            yield lo, hi
