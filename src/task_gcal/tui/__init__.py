"""Full-screen front ends for `checkin` and `review`.

Front ends and nothing else. Every number, episode, answer and command shown
here comes from the same code the plain output uses — `review.build`,
`checkin.open_episodes`, `checkin.reflection_for`, `stagnation.prescribe` — so
the TUI can't disagree with the report, and choosing it changes how things
look, never what they say.

The rules the plain paths keep, kept here too:

- **Nothing here writes to Taskwarrior or the calendar.** The check-in appends
  reflections; review writes nothing. Triage commands are copied, never run.
- **Unknown stays unknown.** Skipping leaves an episode open; minutes are never
  defaulted to the estimate.

Opened only on a real terminal (see `wanted`). Pipes, `--plain`, and every
option that asks for a document rather than a screen keep the plain output, so
scripts never meet a full-screen app. Textual is imported by the modules below,
never by this one, so deciding costs nothing.
"""

from __future__ import annotations

import os
import sys


def wanted(*, plain: bool = False) -> bool:
    """True if a full-screen UI is appropriate for this invocation."""
    if plain:
        return False
    if os.environ.get("TERM", "") in ("", "dumb"):
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):  # closed or replaced streams
        return False
