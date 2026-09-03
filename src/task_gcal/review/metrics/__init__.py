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
from . import (
    attempts,
    boundaries,
    capacity,
    deadlines,
    flow,
    followthrough,
    friction,
    scope,
    stagnation_section,
    throughput,
)

SectionBuilder = Callable[[object], Section]

# Ordered as the report reads: capacity, then output, then flow, then whether
# the plan survived, then the behavioural detail, then what needs deciding.
_BUILDERS: tuple[tuple[str, SectionBuilder], ...] = (
    (capacity.KEY, capacity.build),
    (throughput.KEY, throughput.build),
    (flow.KEY_FLOW, flow.build_flow),
    (flow.KEY_LEAD_TIME, flow.build_lead_time),
    (followthrough.KEY, followthrough.build),
    (deadlines.KEY, deadlines.build),
    (friction.KEY, friction.build),
    (attempts.KEY, attempts.build),
    (scope.KEY, scope.build),
    (boundaries.KEY, boundaries.build),
    (stagnation_section.KEY, stagnation_section.build),
)

# The default weekly review has to fit one terminal screen, so only these
# print without being asked for. The rest are `--section` material: detailed
# metrics are requested, not always shown.
SUMMARY_KEYS: tuple[str, ...] = (
    capacity.KEY,
    throughput.KEY,
    flow.KEY_FLOW,
    followthrough.KEY,
    deadlines.KEY,
    friction.KEY,
    boundaries.KEY,
    stagnation_section.KEY,
)


def build_sections(facts) -> tuple[Section, ...]:
    return tuple(build(facts) for _key, build in _BUILDERS)


def section_keys() -> tuple[str, ...]:
    """Every `--section` name, for argparse choices and for `--help`."""
    return tuple(key for key, _build in _BUILDERS)


def summary_sections(sections: Iterable[Section]) -> tuple[Section, ...]:
    """The one-screen default: the headline families, in report order."""
    return tuple(s for s in sections if s.key in SUMMARY_KEYS)


def selected(sections: Iterable[Section], keys: Iterable[str]) -> tuple[Section, ...]:
    wanted = set(keys)
    return tuple(s for s in sections if s.key in wanted)
