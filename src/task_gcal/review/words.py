"""Small wording helpers shared by the metrics, so no line says `task(s)`."""

from __future__ import annotations

from typing import Optional


def plural(count: int, word: str, many: Optional[str] = None) -> str:
    """`1 task`, `3 tasks`, `2 days`; pass `many` for irregular plurals."""
    return f"{count} {word if count == 1 else (many or word + 's')}"


def times(count: int) -> str:
    """`once`, `twice`, `3 times`."""
    return {1: "once", 2: "twice"}.get(count, f"{count} times")
