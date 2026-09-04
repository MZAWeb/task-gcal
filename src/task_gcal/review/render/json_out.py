"""JSON — the escape hatch.

The important renderer, even though it's the least pretty: it lets charts,
notebooks or another application read these numbers without any of that
becoming the scheduler's problem. Every section's `data` is primitives only,
so this is a dump rather than a translation.

`--section` filters the sections, but coverage and caveats always ship: a
number extracted from here without its denominator is exactly what the
coverage rule exists to prevent.
"""

from __future__ import annotations

import json

from ..model import Review, Section


def render(review: Review, *, sections: tuple[Section, ...], detailed: bool) -> str:
    payload = {
        "period": {
            "kind": review.period.kind,
            "label": review.period.label,
            "start": _iso(review.period.start),
            "end": _iso(review.period.end),
            "nominal_end": _iso(review.period.nominal_end),
            "in_progress": review.period.in_progress,
            "timezone": str(review.period.tz),
        },
        "generated_at": _iso(review.generated_at),
        "adjustment": review.adjustment,
        # [observed, total] days. A consumer plotting any of this needs to know
        # how much of the period was seen as badly as a reader does.
        "observed": list(review.observed) if review.observed else None,
        "observed_days": list(review.observed_days),
        "caveats": list(review.caveats),
        "sections": [_section(s, detailed=detailed) for s in sections],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _section(section: Section, *, detailed: bool) -> dict:
    out = {
        "key": section.key,
        "label": section.label,
        "summary": section.summary,
        "measured": section.measured,
        "data": dict(section.data),
        "coverage": [
            {
                "label": c.label,
                "observed": c.observed,
                "total": c.total,
                "complete": c.complete,
            }
            for c in section.coverage
        ],
        "suggestions": [
            {"text": s.text, "weight": s.weight} for s in section.suggestions
        ],
    }
    if detailed:
        # The prose lines are for humans; a consumer wants `data`. They're
        # included only when explicitly asked for, to keep the default
        # payload one obvious shape.
        out["detail"] = list(section.detail)
    return out


def _iso(moment) -> str:
    return moment.isoformat().replace("+00:00", "Z")
