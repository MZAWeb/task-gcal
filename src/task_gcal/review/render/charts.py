"""Inline SVG for the HTML report. Geometry only — no styling decisions.

Every colour is a CSS custom property defined once in `html.py`, so light and
dark are two sets of values in one place rather than two code paths here. Every
chart is handed the *display string* for each value alongside the number, so
this module never formats a quantity either: it decides where ink goes and
nothing else.

House style, applied by all four chart types:

- bars grow from a baseline and are rounded only on their outer ends, so the
  eye reads them as measured from somewhere rather than floating;
- touching segments are separated by a gap of negative space rather than a
  stroke, because a border is ink that isn't data;
- gridlines are hairlines and there are at most two of them (zero, and the
  peak the marks are measured against);
- **no value is legible only as a mark.** Every number a chart draws is also
  printed as text in the same figure — legend, tip label or caption — so the
  picture is an aid and never the only route to a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Optional, Sequence

# One consistent surface gap between touching marks, as negative space rather
# than a stroke.
GAP = 2.0
CHART_WIDTH = 640.0
CORNER = 4.0

STACK_HEIGHT = 30.0
ROW_BAR_HEIGHT = 15.0
ROW_PITCH = 25.0
COLUMN_HEIGHT = 96.0
SPARK_HEIGHT = 60.0


@dataclass(frozen=True)
class Slice:
    """One segment of a stacked bar.

    `display` is the value as the reader should see it — `30h35`, `14 pushes` —
    because turning a quantity into words is the caller's job, not geometry's.
    """

    label: str
    value: float
    series: str  # a CSS custom-property name, e.g. "--series-1"
    display: str


def _rounded(
    x: float,
    y: float,
    w: float,
    h: float,
    r: float,
    *,
    left: bool = False,
    right: bool = True,
) -> str:
    """A horizontal bar with either end independently rounded.

    Written in absolute coordinates on purpose: the same path with relative
    moves has to subtract each corner radius from the side it *doesn't* round,
    and getting that wrong produces a bar that is quietly the wrong height.
    """
    limit = max(0.0, min(r, w / 2, h / 2))
    rl = limit if left else 0.0
    rr = limit if right else 0.0
    if not (rl or rr):
        return f"M{x:.1f},{y:.1f} h{w:.1f} v{h:.1f} h{-w:.1f} Z"
    right_x, bottom_y = x + w, y + h
    parts = [f"M{x + rl:.1f},{y:.1f}", f"L{right_x - rr:.1f},{y:.1f}"]
    if rr:
        parts.append(f"A{rr:.1f},{rr:.1f} 0 0 1 {right_x:.1f},{y + rr:.1f}")
    parts.append(f"L{right_x:.1f},{bottom_y - rr:.1f}")
    if rr:
        parts.append(
            f"A{rr:.1f},{rr:.1f} 0 0 1 {right_x - rr:.1f},{bottom_y:.1f}"
        )
    parts.append(f"L{x + rl:.1f},{bottom_y:.1f}")
    if rl:
        parts.append(f"A{rl:.1f},{rl:.1f} 0 0 1 {x:.1f},{bottom_y - rl:.1f}")
    parts.append(f"L{x:.1f},{y + rl:.1f}")
    if rl:
        parts.append(f"A{rl:.1f},{rl:.1f} 0 0 1 {x + rl:.1f},{y:.1f}")
    return " ".join(parts) + " Z"


def _rounded_top(x: float, y: float, w: float, h: float, r: float) -> str:
    """A column with its top corners rounded and its baseline square."""
    r = max(0.0, min(r, w / 2, h / 2))
    if r == 0:
        return f"M{x:.1f},{y:.1f} h{w:.1f} v{h:.1f} h{-w:.1f} Z"
    return (
        f"M{x:.1f},{y + h:.1f} "
        f"v{-(h - r):.1f} a{r:.1f},{r:.1f} 0 0 1 {r:.1f},{-r:.1f} "
        f"h{w - 2 * r:.1f} a{r:.1f},{r:.1f} 0 0 1 {r:.1f},{r:.1f} "
        f"v{h - r:.1f} Z"
    )


def _figure(
    body: str,
    *,
    title: str,
    height: float,
    legend: str = "",
    note: str = "",
    width: float = CHART_WIDTH,
) -> str:
    """One chart, captioned, with an accessible name and an optional legend."""
    return (
        '<figure class="chart">'
        f'<figcaption>{escape(title)}</figcaption>'
        f'<svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
        f'aria-label="{escape(title)}">{body}</svg>'
        f"{legend}"
        + (f'<p class="chart-note">{escape(note)}</p>' if note else "")
        + "</figure>"
    )


def stacked_bar(
    slices: Sequence[Slice], *, title: str, note: str = ""
) -> str:
    """One horizontal bar split by category, with a value legend beneath.

    For parts of one known whole, where the comparison that matters is segment
    against segment. Values ride the legend rather than the segments, so a thin
    slice can never clip its own label.
    """
    present = [s for s in slices if s.value > 0]
    total = sum(s.value for s in present)
    if total <= 0:
        return ""

    height = STACK_HEIGHT + 2
    x = 0.0
    marks: list[str] = []
    for index, piece in enumerate(present):
        raw = CHART_WIDTH * piece.value / total
        last = index == len(present) - 1
        # The gap comes out of every segment but the last, so the bar still
        # spans the full width and the shares stay honest.
        width = raw - (GAP if not last else 0)
        if width <= 0:
            x += raw
            continue
        marks.append(
            f'<path class="mark" d="'
            f"{_rounded(x, 1, width, STACK_HEIGHT, CORNER, left=index == 0, right=last)}"
            f'" fill="var({piece.series})">'
            f"<title>{escape(piece.label)}: {escape(piece.display)}</title>"
            "</path>"
        )
        x += raw

    legend = '<ul class="legend">' + "".join(
        f'<li><span class="swatch" style="background:var({p.series})"></span>'
        f"<span class=\"legend-label\">{escape(p.label)}</span>"
        f'<b class="legend-value">{escape(p.display)}</b></li>'
        for p in present
    ) + "</ul>"
    return _figure(
        "".join(marks), title=title, height=height, legend=legend, note=note
    )


def bar_rows(
    rows: Sequence[tuple[str, float, str]],
    *,
    title: str,
    note: str = "",
    min_rows: int = 2,
) -> str:
    """A horizontal bar per category, largest first, value printed at the tip.

    One series, so no legend: the caption says what is plotted and the tip
    labels carry the numbers.

    Below `min_rows` bars there is nothing to compare, and a chart of one bar is
    a decoration around a number that the sentence above it already carries.
    """
    present = [row for row in rows if row[1] > 0]
    if len(present) < min_rows:
        return ""
    present = sorted(present, key=lambda row: row[1], reverse=True)
    peak = max(row[1] for row in present)

    label_width = 168.0
    value_width = 64.0
    plot_width = CHART_WIDTH - label_width - value_width
    height = ROW_PITCH * len(present)

    parts: list[str] = []
    for index, (label, value, display) in enumerate(present):
        y = index * ROW_PITCH + (ROW_PITCH - ROW_BAR_HEIGHT) / 2
        width = max(plot_width * value / peak, 2.0)
        baseline = y + ROW_BAR_HEIGHT * 0.8
        parts.append(
            f'<text class="axis" x="{label_width - 10:.1f}" y="{baseline:.1f}" '
            f'text-anchor="end">{escape(_ellipsize(label, 26))}</text>'
        )
        parts.append(
            f'<path class="mark" d="'
            f"{_rounded(label_width, y, width, ROW_BAR_HEIGHT, CORNER)}"
            f'" fill="var(--series-1)">'
            f"<title>{escape(label)}: {escape(display)}</title></path>"
        )
        parts.append(
            f'<text class="value" x="{label_width + width + 10:.1f}" '
            f'y="{baseline:.1f}">{escape(display)}</text>'
        )

    return _figure("".join(parts), title=title, height=height, note=note)


def columns(
    rows: Sequence[tuple[str, float, str]],
    *,
    title: str,
    note: str = "",
    axis_label: str = "",
) -> str:
    """A column per bucket, in the order given — for a distribution.

    Order is the caller's, because the x axis of a distribution means
    something: hours run 09:00 to 17:00 whether or not every hour has a
    column, and sorting by size would destroy the only axis it has.
    """
    present = list(rows)
    if not present or max(row[1] for row in present) <= 0:
        return ""
    peak = max(row[1] for row in present)

    top = 14.0  # room for the value printed above each column
    bottom = 18.0  # room for the bucket label
    plot = COLUMN_HEIGHT - top - bottom
    height = COLUMN_HEIGHT
    pitch = CHART_WIDTH / len(present)
    bar = min(pitch - 8.0, 46.0)

    parts: list[str] = [
        f'<line class="grid" x1="0" y1="{top + plot:.1f}" '
        f'x2="{CHART_WIDTH:.0f}" y2="{top + plot:.1f}" />'
    ]
    for index, (label, value, display) in enumerate(present):
        centre = pitch * (index + 0.5)
        column = plot * value / peak
        if value > 0:
            parts.append(
                f'<path class="mark" d="'
                f"{_rounded_top(centre - bar / 2, top + plot - column, bar, column, CORNER)}"
                f'" fill="var(--series-1)">'
                f"<title>{escape(label)}: {escape(display)}</title></path>"
            )
            parts.append(
                f'<text class="value" x="{centre:.1f}" '
                f'y="{top + plot - column - 5:.1f}" text-anchor="middle">'
                f"{escape(display)}</text>"
            )
        parts.append(
            f'<text class="axis" x="{centre:.1f}" y="{height - 5:.1f}" '
            f'text-anchor="middle">{escape(label)}</text>'
        )

    return _figure(
        "".join(parts),
        title=title,
        height=height,
        note=note or axis_label,
    )


def sparklines(
    rows: Sequence[tuple[str, Sequence[float], str]],
    *,
    labels: Sequence[str],
    formatter=None,
) -> str:
    """One small chart per series, sharing an x axis of period labels.

    Small multiples rather than one chart with several y scales: the series are
    counts, a rate and hours, and putting two scales on one chart is the single
    worst thing a chart can do. Every series is scaled from zero, so a
    flat-but-high line can't be mistaken for a flat-but-low one.
    """
    present = [
        (name, [float(v) for v in values], unit)
        for name, values, unit in rows
        if values
    ]
    if not present:
        return ""

    def show(value: float, unit: str) -> str:
        return formatter(value, unit) if formatter else f"{value:g}{unit}"

    width = CHART_WIDTH
    height = SPARK_HEIGHT
    # Half the end marker plus its ring, so the marker at a peak or a zero sits
    # inside the box instead of being clipped by it.
    inset = 6.0
    charts: list[str] = []
    for name, values, unit in present:
        peak = max(values)
        scale = peak or 1.0
        step = (width - inset) / max(len(values) - 1, 1)

        def y_for(value: float, scale: float = scale) -> float:
            return height - inset - (value / scale) * (height - 2 * inset)

        points = " ".join(
            f"{index * step:.1f},{y_for(value):.1f}"
            for index, value in enumerate(values)
        )
        last_x = (len(values) - 1) * step
        # The area under the line is a path rather than a polygon with its own
        # `points`, so the only list of points in the figure is the line's own —
        # one coordinate per period, and nothing else to confuse for it.
        area = (
            f"M0,{y_for(0):.1f} L"
            + " L".join(
                f"{index * step:.1f},{y_for(value):.1f}"
                for index, value in enumerate(values)
            )
            + f" L{last_x:.1f},{y_for(0):.1f} Z"
        )
        charts.append(
            _figure(
                # Two hairlines give the line a scale: the peak it is measured
                # against, and the zero it is measured from.
                f'<line class="grid" x1="0" y1="{y_for(peak):.1f}" '
                f'x2="{width:.0f}" y2="{y_for(peak):.1f}" />'
                f'<line class="grid" x1="0" y1="{y_for(0):.1f}" '
                f'x2="{width:.0f}" y2="{y_for(0):.1f}" />'
                f'<path class="spark-area" d="{area}" />'
                f'<polyline class="spark" points="{points}" />'
                # A ring in the surface colour keeps the end marker legible
                # where it crosses the line, and makes it a real hover target.
                f'<circle class="spark-end" cx="{last_x:.1f}" '
                f'cy="{y_for(values[-1]):.1f}" r="4">'
                f"<title>{escape(name)}: {escape(show(values[-1], unit))} "
                f"most recently</title></circle>",
                title=(
                    f"{name} — {show(values[0], unit)} to "
                    f"{show(values[-1], unit)}, peak {show(peak, unit)}"
                ),
                height=height,
            )
        )

    axis = " · ".join(escape(label) for label in labels[-4:])
    return (
        f'<div class="multiples">{"".join(charts)}</div>'
        f'<p class="axis-note">Oldest to newest, ending {axis}</p>'
    )


def _ellipsize(text: str, limit: int) -> str:
    """Shorten rather than let a long label collide with its bar.

    A label that doesn't fit is never clipped by its mark — the full name is in
    the tooltip and in the table of every value.
    """
    return text if len(text) <= limit else text[: limit - 1] + "…"


def hour_label(hour: int) -> str:
    return f"{hour:02d}"


def fill_hour_range(counts: dict) -> Optional[list[tuple[int, int]]]:
    """A continuous run of hours from the earliest to the latest with a count.

    Gaps are filled with zeros rather than closed up: a quiet hour between two
    busy ones is part of the shape, and dropping it would redraw the day.
    """
    hours = sorted(int(hour) for hour in counts)
    if not hours:
        return None
    return [
        (hour, int(counts.get(str(hour), counts.get(hour, 0)) or 0))
        for hour in range(hours[0], hours[-1] + 1)
    ]
