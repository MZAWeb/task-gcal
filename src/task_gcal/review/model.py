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


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


@dataclass(frozen=True)
class Coverage:
    """How much of what a number claims to measure was actually observed.

    `38 of 50 completed tasks had estimates` is the difference between a
    finding and a guess.
    """

    label: str
    observed: int
    total: int
    # The label of the detail row this denominator belongs to, when it belongs
    # to one figure rather than to the whole section. "38 of 50 had estimates"
    # qualifies the estimate total and nothing else, and a renderer that knows
    # that can print the two together instead of putting the denominator in a
    # footnote several inches away. Naming the row rather than the `data` key
    # because the row is what a reader sees; a renderer that can't find the row
    # falls back to the footnote, so drift costs placement and never content.
    qualifies: str = ""

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
    # How many whole periods back this same finding was already true, when a
    # section can work that out. The closing line is the only part of the
    # report with any authority, and the heaviest finding wins every week —
    # so left alone it becomes furniture. Saying "this is the fourth week I've
    # closed with this" is the tool noticing it's repeating itself, which is
    # both more honest and more likely to force the decision than saying the
    # same sentence again in the same tone.
    #
    # Worth watching on real data: at a high enough count this line can become
    # furniture in its own right. Week 12 of "the 12th week running" is a fact
    # about the tool rather than about the task, and at that point the count
    # has *become* the finding — same words, different subject. Not fixed
    # pre-emptively, because nobody knows yet where that line is.
    already_true_for: int = 0


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
    # *Which* local days were observed, ISO, oldest first. A proportion can only
    # say how much is missing; the days say which — and "the journal stopped
    # running on Tuesday" is a different problem from "I only ran it once".
    observed_days: tuple[str, ...] = ()

    def section(self, key: str) -> Optional[Section]:
        for s in self.sections:
            if s.key == key:
                return s
        return None

    @property
    def closing(self) -> Optional[tuple[str, Suggestion]]:
        """The heaviest suggestion, and the key of the section that raised it.

        The selection rule lives here and nowhere else. `adjustment` is this
        same finding flattened into a sentence, for the renderers that want a
        sentence; this is for the ones that want to link the finding to its own
        evidence, or to set the repetition clause in a different voice from the
        finding itself. Neither should have to recover from the other's prose.
        """
        best_key: Optional[str] = None
        best: Optional[Suggestion] = None
        for section in self.sections:
            for suggestion in section.suggestions:
                if best is None or suggestion.weight > best.weight:
                    best_key, best = section.key, suggestion
        if best is None or best_key is None:
            return None
        return best_key, best

    def repetition_note(self) -> str:
        """"This is the 4th week running that I've closed with this", or "".

        One wording, in one place. The closing line is the only part of the
        report with any authority and the heaviest finding wins every week, so
        left alone it becomes furniture; saying that it is repeating itself is
        the honest alternative to quietly picking something less true. A
        renderer that wants to set this clause apart from the finding asks for
        it, rather than splitting a sentence back up on a substring.
        """
        found = self.closing
        if found is None:
            return ""
        _key, best = found
        if not best.already_true_for:
            return ""
        running = best.already_true_for + 1
        return (
            f"This is the {_ordinal(running)} {self.period.kind} running "
            "that I've closed with this."
        )

    @property
    def adjustment(self) -> Optional[str]:
        """The one thing to look at, as a sentence, or None if nothing stands out.

        If a review can't end with a single concrete thing to inspect, it's a
        dashboard — and a dashboard was the thing we set out not to build.
        """
        found = self.closing
        if found is None:
            return None
        _key, best = found
        note = self.repetition_note()
        return f"{best.text} {note}" if note else best.text

    @property
    def incomplete_sections(self) -> tuple[str, ...]:
        return tuple(
            s.label for s in self.sections if not s.measured and not s.optional
        )
