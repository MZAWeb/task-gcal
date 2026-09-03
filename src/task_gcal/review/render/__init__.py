"""Renderers: one `Review` in, one string out.

Terminal ships first and is the default. The others exist because the report
is built as data and rendered at the end, so a new output format is a
function rather than a rewrite — which is also why a TUI could be added later
without any of this work being wasted.
"""

from __future__ import annotations

from typing import Callable

from ..model import Review, Section
from . import html, json_out, markdown, terminal

FORMAT_TERMINAL = "terminal"
FORMAT_MARKDOWN = "markdown"
FORMAT_JSON = "json"
FORMAT_HTML = "html"

FORMATS = (FORMAT_TERMINAL, FORMAT_MARKDOWN, FORMAT_JSON, FORMAT_HTML)

Renderer = Callable[..., str]

_RENDERERS: dict[str, Renderer] = {
    FORMAT_TERMINAL: terminal.render,
    FORMAT_MARKDOWN: markdown.render,
    FORMAT_JSON: json_out.render,
    FORMAT_HTML: html.render,
}


def render(
    review: Review,
    *,
    fmt: str = FORMAT_TERMINAL,
    sections: tuple[Section, ...] = None,
    detailed: bool = False,
) -> str:
    if fmt not in _RENDERERS:
        raise ValueError(f"unknown format {fmt!r}; expected one of {FORMATS}")
    chosen = review.sections if sections is None else sections
    return _RENDERERS[fmt](review, sections=chosen, detailed=detailed)
