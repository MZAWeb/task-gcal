"""The report model: what a review *is*, before anything renders it.

The boundary this file defines is the whole architecture:

    collect facts -> compute report -> render report

Metrics build a `Review` and never print. Renderers read a `Review` and never
compute. That's why adding Markdown, JSON or HTML output is a renderer rather
than a rewrite — and why a future TUI could consume exactly the same model,
so deferring one closes no doors.

Two rules are encoded in the types rather than left to good intentions:

- **Every number carries its coverage.** A number without a denominator is a
  rumour, so `Coverage` exists and sections are expected to carry some.
- **Missing data is not zero.** A section that couldn't be measured says so
  (`measured=False`) instead of reporting a confident zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from .periods import Period


@dataclass(frozen=True)
class Coverage:
    """How much of what a number claims to measure was actually observed.

    `38 of 50 completed tasks had estimates` is the difference between a
    finding and a guess.
    """

    label: str
    observed: int
    total: int

    @property
    def complete(self) -> bool:
        return self.observed >= self.total

    @property
    def text(self) -> str:
        return f"{self.observed}/{self.total} {self.label}"


@dataclass(frozen=True)
class Suggestion:
    """A candidate for the review's single closing recommendation.

    Sections propose; the review picks one. Heaviest wins, so a repeatedly
    deferred task outranks a mild observation about capacity.
    """

    text: str
    weight: float = 1.0


@dataclass(frozen=True)
class Section:
    """One family of metrics: a summary line, and detail behind `--section`.

    The default review is one screen, so `summary` is the only part that
    always prints. `detail` is what `--section` reveals, and `data` is the
    same content as primitives for JSON and HTML.
    """

    key: str
    label: str
    summary: str
    detail: tuple[str, ...] = ()
    coverage: tuple[Coverage, ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)
    suggestions: tuple[Suggestion, ...] = ()
    # False when the inputs weren't there. A renderer must then say "not
    # measured" rather than print whatever zero the arithmetic produced.
    measured: bool = True
    # True when what's missing is a feature the owner hasn't turned on, rather
    # than data that should have been there. "Not measured" has to keep meaning
    # something: printing it every week for an optional feature nobody enabled
    # teaches the reader that the phrase is furniture, and then it can't do its
    # job on the week the calendar genuinely failed.
    optional: bool = False


@dataclass(frozen=True)
class Review:
    """A whole report, renderer-agnostic."""

    period: Period
    generated_at: datetime
    sections: tuple[Section, ...] = ()
    # Things that make the numbers above less trustworthy: unobserved days, a
    # settings change mid-period, a calendar we couldn't read.
    caveats: tuple[str, ...] = ()
    # (days a run observed, days in the period). In the model rather than only
    # in the caveats because it belongs beside the title: a reader should know
    # how much of the week this is built on before reading any of it, not
    # after.
    observed: Optional[tuple[int, int]] = None

    def section(self, key: str) -> Optional[Section]:
        for s in self.sections:
            if s.key == key:
                return s
        return None

    @property
    def adjustment(self) -> Optional[str]:
        """The one thing to look at, or None if nothing stands out.

        If a review can't end with a single concrete thing to inspect, it's a
        dashboard — and a dashboard was the thing we set out not to build.
        """
        best: Optional[Suggestion] = None
        for section in self.sections:
            for suggestion in section.suggestions:
                if best is None or suggestion.weight > best.weight:
                    best = suggestion
        return best.text if best is not None else None

    @property
    def incomplete_sections(self) -> tuple[str, ...]:
        return tuple(
            s.label for s in self.sections if not s.measured and not s.optional
        )
