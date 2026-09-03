"""Inline SVG for the HTML report. Geometry only — no styling decisions.

Every colour is a CSS custom property defined once in `html.py`, so light and
dark are two sets of values in one place rather than two code paths here.
Marks follow one fixed spec: bars no thicker than 24px, a 4px rounded
data-end square at the baseline, a 2px surface-coloured gap doing the
separating between touching segments, and hairline recessive gridlines.

Values are never printed on the marks themselves — they ride the legend and
the table, so a short segment can't clip its own label.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Sequence

from ...intervals import humanize_minutes

# One consistent surface gap between touching marks, as negative space rather
# than a stroke: a border would add ink that isn't data.
GAP = 2.0
BAR_HEIGHT = 24.0
ROW_BAR_HEIGHT = 16.0
ROW_PITCH = 26.0
CORNER = 4.0
CHART_WIDTH = 640.0


@dataclass(frozen=True)
class Slice:
    """One segment of a stacked bar."""

    label: str
    minutes: int
    series: str  # a CSS custom-property name, e.g. "--series-1"


def _rounded_right(x: float, y: float, w: float, h: float, r: float) -> str:
    """A rect with its right end rounded and its left square.

    The data-end is rounded and the baseline end is square, so the eye reads
    the bar as growing from the axis rather than floating.
    """
    r = max(0.0, min(r, w / 2, h / 2))
    if r == 0:
        return f"M{x:.1f},{y:.1f} h{w:.1f} v{h:.1f} h{-w:.1f} Z"
    return (
        f"M{x:.1f},{y:.1f} "
        f"h{w - r:.1f} a{r:.1f},{r:.1f} 0 0 1 {r:.1f},{r:.1f} "
        f"v{h - 2 * r:.1f} a{r:.1f},{r:.1f} 0 0 1 {-r:.1f},{r:.1f} "
        f"h{-(w - r):.1f} Z"
    )


def stacked_bar(slices: Sequence[Slice], *, title: str) -> str:
    """One horizontal bar split by category, with a value legend beneath.

    Used for capacity, where the parts are shares of one known whole and the
    comparison that matters is segment against segment.
    """
    present = [s for s in slices if s.minutes > 0]
    total = sum(s.minutes for s in present)
    if total <= 0:
        return ""

    height = BAR_HEIGHT + 8
    x = 0.0
    parts: list[str] = []
    for index, piece in enumerate(present):
        raw = CHART_WIDTH * piece.minutes / total
        # The gap comes out of every segment but the last, so the bar still
        # spans the full width and the shares stay honest.
        width = raw - (GAP if index < len(present) - 1 else 0)
        if width <= 0:
            x += raw
            continue
        last = index == len(present) - 1
        parts.append(
            f'<path class="mark" d="'
            f'{_rounded_right(x, 4, width, BAR_HEIGHT, CORNER if last else 0)}"'
            f' fill="var({piece.series})">'
            f"<title>{escape(piece.label)}: "
            f"{escape(humanize_minutes(piece.minutes))}</title></path>"
        )
        x += raw

    legend = "".join(
        f'<li><span class="swatch" style="background:var({p.series})"></span>'
        f"{escape(p.label)} <b>{escape(humanize_minutes(p.minutes))}</b></li>"
        for p in present
    )
    return (
        f'<figure class="chart">'
        f"<figcaption>{escape(title)}</figcaption>"
        f'<svg viewBox="0 0 {CHART_WIDTH:.0f} {height:.0f}" '
        f'width="100%" height="{height:.0f}" role="img" '
        f'aria-label="{escape(title)}">{"".join(parts)}</svg>'
        f'<ul class="legend">{legend}</ul>'
        f"</figure>"
    )


def bar_rows(
    rows: Sequence[tuple[str, int]], *, title: str, unit: str = ""
) -> str:
    """A horizontal bar per category, largest first, value at the tip.

    One series, so no legend: the caption already says what is plotted.
    """
    present = [(label, value) for label, value in rows if value > 0]
    if not present:
        return ""
    present = sorted(present, key=lambda row: row[1], reverse=True)
    peak = max(value for _label, value in present)

    label_width = 150.0
    value_width = 56.0
    plot_width = CHART_WIDTH - label_width - value_width
    height = ROW_PITCH * len(present)

    parts: list[str] = [
        # One recessive hairline at the peak, so the bars have a scale
        # without a full grid competing with them.
        f'<line class="grid" x1="{label_width + plot_width:.1f}" y1="0" '
        f'x2="{label_width + plot_width:.1f}" y2="{height:.1f}" />'
    ]
    for index, (label, value) in enumerate(present):
        y = index * ROW_PITCH + (ROW_PITCH - ROW_BAR_HEIGHT) / 2
        width = plot_width * value / peak
        parts.append(
            f'<text class="axis" x="{label_width - 8:.1f}" '
            f'y="{y + ROW_BAR_HEIGHT * 0.75:.1f}" text-anchor="end">'
            f"{escape(_ellipsize(label, 24))}</text>"
        )
        parts.append(
            f'<path class="mark" d="'
            f"{_rounded_right(label_width, y, width, ROW_BAR_HEIGHT, CORNER)}"
            f'" fill="var(--series-1)"><title>{escape(label)}: {value}'
            f"{escape(' ' + unit if unit else '')}</title></path>"
        )
        parts.append(
            f'<text class="value" x="{label_width + width + 8:.1f}" '
            f'y="{y + ROW_BAR_HEIGHT * 0.75:.1f}">{value}</text>'
        )

    return (
        f'<figure class="chart">'
        f"<figcaption>{escape(title)}</figcaption>"
        f'<svg viewBox="0 0 {CHART_WIDTH:.0f} {height:.0f}" width="100%" '
        f'height="{height:.0f}" role="img" aria-label="{escape(title)}">'
        f'{"".join(parts)}</svg></figure>'
    )


def sparklines(
    rows: Sequence[tuple[str, Sequence[float], str]], *, labels: Sequence[str]
) -> str:
    """One small line chart per series, sharing an x axis of period labels.

    Small multiples rather than one chart with several y scales: the series
    are counts, rates and hours, and putting two scales on one chart is the
    single worst thing a chart can do.
    """
    present = [(name, list(values), unit) for name, values, unit in rows if values]
    if not present:
        return ""

    width = CHART_WIDTH
    height = 56.0
    inset = 6.0  # room for the end marker and its surface ring
    charts: list[str] = []
    for name, values, unit in present:
        peak = max(values)
        # Only the geometry needs a non-zero divisor; the caption reports the
        # real peak, so an all-zero series doesn't claim a peak of one.
        scale = peak or 1.0
        step = (width - inset) / max(len(values) - 1, 1)

        def y_for(value: float, scale: float = scale) -> float:
            # Scaled from zero, so a flat-but-high series can't be mistaken
            # for a flat-but-low one.
            return height - inset - (value / scale) * (height - 2 * inset)

        points = " ".join(
            f"{index * step:.1f},{y_for(value):.1f}"
            for index, value in enumerate(values)
        )
        last_x = (len(values) - 1) * step
        charts.append(
            f'<figure class="chart">'
            f"<figcaption>{escape(name)} — "
            f"{escape(f'{values[0]:g}')} to {escape(f'{values[-1]:g}')}"
            f"{escape(unit)}, peak {escape(f'{peak:g}')}{escape(unit)}"
            f"</figcaption>"
            f'<svg viewBox="0 0 {width:.0f} {height:.0f}" width="100%" '
            f'height="{height:.0f}" role="img" '
            f'aria-label="{escape(name)} over {len(values)} periods">'
            # Two hairlines give the line a scale: the peak it is measured
            # against, and the zero it is measured from.
            f'<line class="grid" x1="0" y1="{y_for(peak):.1f}" '
            f'x2="{width:.0f}" y2="{y_for(peak):.1f}" />'
            f'<line class="grid" x1="0" y1="{y_for(0):.1f}" '
            f'x2="{width:.0f}" y2="{y_for(0):.1f}" />'
            f'<polyline class="spark" points="{points}" />'
            # A ring in the surface colour keeps the end marker legible where
            # it crosses the line, and makes it a real hover target.
            f'<circle class="spark-end" cx="{last_x:.1f}" '
            f'cy="{y_for(values[-1]):.1f}" r="4" />'
            f"<title>{escape(name)}: {escape(f'{values[-1]:g}')}"
            f"{escape(unit)} most recently</title>"
            f"</svg></figure>"
        )

    axis = " · ".join(escape(label) for label in labels[-4:])
    return (
        f'{"".join(charts)}'
        f'<p class="axis-note">Oldest to newest, ending {axis}</p>'
    )


def _ellipsize(text: str, limit: int) -> str:
    """Shorten rather than let a long project name collide with its bar.

    A label that doesn't fit is never clipped by its mark — the full name is
    in the tooltip and the table.
    """
    return text if len(text) <= limit else text[: limit - 1] + "…"
