"""Tiny zero-dependency progress indicator for terminal output.

Writes to stderr (so it never pollutes the report on stdout) and becomes a
no-op when stderr isn't a TTY -- e.g. when output is piped or redirected --
or when explicitly disabled (such as a dry run with no network calls).
"""

from __future__ import annotations

import itertools
import sys
from typing import IO, Optional

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_BAR_WIDTH = 24


class Progress:
    """A spinner / progress bar that redraws a single stderr line.

    Pass ``total`` for a determinate bar; omit it for an indeterminate
    spinner with a running count. Call :meth:`advance` as work completes and
    :meth:`close` when done (clearing the line).
    """

    def __init__(
        self,
        total: Optional[int] = None,
        *,
        label: str = "",
        enabled: bool = True,
        stream: Optional[IO[str]] = None,
    ) -> None:
        self.total = total
        self.label = label
        self.n = 0
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled and self.stream.isatty()
        self._spinner = itertools.cycle(_SPINNER_FRAMES)
        self._last_len = 0

    def _write(self, text: str) -> None:
        # Pad over any leftover characters from a previous, longer line.
        pad = max(self._last_len - len(text), 0)
        self.stream.write("\r" + text + " " * pad)
        self.stream.flush()
        self._last_len = len(text)

    def render(self, suffix: str = "") -> None:
        if not self.enabled:
            return
        frame = next(self._spinner)
        if self.total:
            filled = int(_BAR_WIDTH * self.n / self.total)
            bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
            text = f"{frame} {self.label} [{bar}] {self.n}/{self.total}"
        else:
            count = f" {self.n}" if self.n else ""
            text = f"{frame} {self.label}{count}"
        if suffix:
            text += f"  {suffix}"
        self._write(text)

    def advance(self, suffix: str = "") -> None:
        self.n += 1
        self.render(suffix)

    def close(self) -> None:
        if self.enabled and self._last_len:
            self.stream.write("\r" + " " * self._last_len + "\r")
            self.stream.flush()
        self._last_len = 0

    def __enter__(self) -> "Progress":
        self.render()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
