"""The metric registry.

Each entry is a pure function from `Facts` to one `Section`: no printing, no
prompting, no mutation. Ordered as the report should read — **capacity first**,
so every later number has an honest denominator, then output, then flow, then
whether the plan survived.

Adding a metric means adding an entry here. There is deliberately no plugin
framework: a list of functions is easier to read, and every metric has to
justify itself to a person rather than to a registry.

Section keys and labels are the plain words a person types and reads; the
modules behind them keep the precise internal name. `capacity.py` builds the
section called `time`, because "capacity" is the right word for the concept and
the wrong word to put in front of somebody on a Friday afternoon.

Each entry also carries what the section *means*, in words a person reading
their first review would understand. It lives next to the builder so a new
metric can't ship without one — "Scope" and "Stagnation" are meaningless
labels until somebody explains them, and the report is where that has to
happen.
"""

from __future__ import annotations

from typing import Callable, Iterable, NamedTuple

from ..model import Section
from . import (
    attempts,
    boundaries,
    capacity,
    churn,
    deadlines,
    flow,
    followthrough,
    friction,
    scope,
    stagnation_section,
    throughput,
    trends,
)

SectionBuilder = Callable[[object], Section]


class Metric(NamedTuple):
    key: str
    build: SectionBuilder
    means: str


# Ordered as the report reads: capacity, then output, then flow, then whether
# the plan survived, then the behavioural detail, then what needs deciding.
_METRICS: tuple[Metric, ...] = (
    Metric(capacity.KEY, capacity.build, capacity.MEANS),
    Metric(throughput.KEY, throughput.build, throughput.MEANS),
    Metric(flow.KEY_FLOW, flow.build_flow, flow.MEANS_FLOW),
    Metric(flow.KEY_LEAD_TIME, flow.build_lead_time, flow.MEANS_LEAD_TIME),
    Metric(followthrough.KEY, followthrough.build, followthrough.MEANS),
    Metric(deadlines.KEY, deadlines.build, deadlines.MEANS),
    Metric(churn.KEY, churn.build, churn.MEANS),
    Metric(friction.KEY, friction.build, friction.MEANS),
    Metric(attempts.KEY, attempts.build, attempts.MEANS),
    Metric(scope.KEY, scope.build, scope.MEANS),
    Metric(boundaries.KEY, boundaries.build, boundaries.MEANS),
    Metric(stagnation_section.KEY, stagnation_section.build, stagnation_section.MEANS),
    Metric(trends.KEY, trends.build, trends.MEANS),
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
    return tuple(metric.build(facts) for metric in _METRICS)


def section_keys() -> tuple[str, ...]:
    """Every `--section` name, for argparse choices and for `--help`."""
    return tuple(metric.key for metric in _METRICS)


def glossary() -> dict[str, str]:
    """What each section means, keyed by section name."""
    return {metric.key: metric.means for metric in _METRICS}


def summary_sections(
    sections: Iterable[Section], *, kind: str = ""
) -> tuple[Section, ...]:
    """The one-screen default: the headline families, in report order.

    A monthly review adds the trend, because history is the point of looking
    at a month. It adds no new *metric* — the same three numbers over time.
    """
    wanted = set(SUMMARY_KEYS)
    if trends.wanted_for(kind):
        wanted.add(trends.KEY)
    return tuple(s for s in sections if s.key in wanted)


def selected(sections: Iterable[Section], keys: Iterable[str]) -> tuple[Section, ...]:
    wanted = set(keys)
    return tuple(s for s in sections if s.key in wanted)
