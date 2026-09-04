"""`task-gcal review` — the read-only report path.

    collect facts -> compute report -> render report

Kept strictly apart from scheduling, in both directions. Nothing here writes
to Taskwarrior or to the calendar, nothing here participates in reconciliation,
and none of it is imported by the scheduling path — so the fast action path
never pays for analytics it doesn't run.
"""

from __future__ import annotations

import os
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import Settings
from . import caveats as caveats_mod
from . import metrics
from .facts import Facts, collect, utc_now
from .model import Review
from .periods import KIND_MONTH, KIND_WEEK, Period, resolve
from .render import FORMAT_TERMINAL, FORMATS, render


@dataclass(frozen=True)
class ReviewRequest:
    """What the user asked for, separated from how it gets answered."""

    kind: str = KIND_WEEK
    offset: int = 0  # periods back from the current one
    sections: tuple[str, ...] = ()  # empty means the one-screen summary
    all_sections: bool = False  # every section, in full
    fmt: str = FORMAT_TERMINAL
    open_in_browser: bool = False
    output: Optional[Path] = None
    # Print the stagnation queue as pasteable commands instead of a report.
    triage: bool = False


def build(facts: Facts) -> Review:
    """Turn collected facts into a report. Pure: no I/O, no printing."""
    return Review(
        period=facts.period,
        generated_at=facts.now,
        sections=metrics.build_sections(facts),
        caveats=caveats_mod.build(facts),
    )


def run(
    settings: Settings,
    request: ReviewRequest,
    *,
    now: Optional[datetime] = None,
    gcal=None,
) -> int:
    """Collect, compute, render, emit. Returns a process exit code."""
    now = now or utc_now()
    tz = settings.resolve_timezone()
    period = resolve(request.kind, tz, now=now, offset=request.offset)

    facts = collect(settings, period, now=now, gcal=gcal)

    if request.triage:
        from .triage import render as render_triage

        print(render_triage(facts), end="")
        return 0

    review = build(facts)

    # The default is the one-screen summary. Detailed sections are requested
    # rather than always printed — the whole point of not being a dashboard.
    chosen = metrics.summary_sections(review.sections, kind=period.kind)
    if request.sections:
        chosen = metrics.selected(review.sections, request.sections)
        if not chosen:
            print(
                f"No such section(s): {', '.join(request.sections)}. "
                f"Available: {', '.join(metrics.section_keys())}",
                file=sys.stderr,
            )
            return 2
    elif request.all_sections:
        chosen = review.sections

    text = render(
        review,
        fmt=request.fmt,
        sections=chosen,
        # Asking for sections means "show me these properly", so the detail
        # comes with the request rather than needing a second flag. `--all` is
        # the same request for all of them: nobody wants thirteen summary
        # lines they then have to re-run one at a time to read.
        detailed=bool(request.sections) or request.all_sections,
    )

    _emit(text, request, period)
    return 0


def _emit(text: str, request: ReviewRequest, period: Period) -> None:
    if request.output is not None:
        request.output.write_text(text, encoding="utf-8")
        print(f"Wrote {request.output}")
        if request.open_in_browser:
            _open(request.output)
        return

    if request.open_in_browser:
        # A browser needs a file, so one is made even without `--output`;
        # it goes to a temp dir rather than cluttering the working directory.
        suffix = ".html" if request.fmt == "html" else ".txt"
        handle, name = tempfile.mkstemp(
            prefix=f"task-gcal-{period.label.replace(' ', '-').lower()}-",
            suffix=suffix,
        )
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
        path = Path(name)
        # Task titles are in here, so keep it to the owner even in /tmp.
        os.chmod(path, 0o600)
        _open(path)
        print(f"Wrote {path}")
        return

    print(text, end="")


def _open(path: Path) -> None:
    webbrowser.open(path.resolve().as_uri())


def section_keys() -> tuple[str, ...]:
    return metrics.section_keys()


__all__ = [
    "FORMATS",
    "FORMAT_TERMINAL",
    "KIND_MONTH",
    "KIND_WEEK",
    "Review",
    "ReviewRequest",
    "build",
    "run",
    "section_keys",
]
