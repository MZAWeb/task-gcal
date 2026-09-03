"""The metric registry.

Each entry is a pure function from `Facts` to one `Section`: no printing, no
prompting, no mutation. Ordered as the report should read — **capacity first**,
so every later number has an honest denominator, then output, then flow, then
whether the plan survived.

Adding a metric means adding a builder here. There is deliberately no plugin
framework: a list of functions is easier to read, and every metric has to
justify itself to a person rather than to a registry.
"""

from __future__ import annotations

from typing import Callable, Iterable

from ..model import Section
from . import capacity, flow, followthrough, throughput

SectionBuilder = Callable[[object], Section]

_BUILDERS: tuple[SectionBuilder, ...] = (
    capacity.build,
    throughput.build,
    flow.build_flow,
    flow.build_lead_time,
    followthrough.build,
)


def build_sections(facts) -> tuple[Section, ...]:
    return tuple(build(facts) for build in _BUILDERS)


def section_keys() -> tuple[str, ...]:
    """Every `--section` name, for argparse choices and for `--help`."""
    return tuple(
        key
        for key in (
            capacity.KEY,
            throughput.KEY,
            flow.KEY_FLOW,
            flow.KEY_LEAD_TIME,
            followthrough.KEY,
        )
    )


def selected(sections: Iterable[Section], keys: Iterable[str]) -> tuple[Section, ...]:
    wanted = set(keys)
    return tuple(s for s in sections if s.key in wanted)
