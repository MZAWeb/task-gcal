"""Friction — the distribution of *confirmed* reasons, with its coverage.

`capacity 4 · avoided 3 · estimate 1 · blocked 1` is more actionable than any
follow-through score, because each bucket implies a different fix. But only
because every entry was confirmed by a person: nothing here is inferred, and
`unknown` is reported as missing classification rather than filled in.

Three rules that keep this honest:

- **Actual-time metrics use only checked-in blocks, and always state their
  coverage.** Estimate-minutes are planned minutes; blocks-to-completion is
  the fallback signal when no check-in exists.
- **No answer is ever rewarded or penalised** — least of all `avoided`. A
  metric that costs you something for admitting avoidance trains you to lie
  to it, and then the whole thing is worthless.
- **Patterns need a sample.** Breakdowns by project or hour only appear once
  there are enough confirmed answers to mean anything, and they're presented
  as correlations rather than diagnoses.
"""

from __future__ import annotations

from collections import Counter

from ...intervals import humanize_minutes
from ...reflections import (
    OUTCOME_DONE,
    OUTCOME_LABELS,
    OUTCOME_NOT_STARTED,
    OUTCOME_PARTIAL,
    OUTCOME_UNKNOWN,
    REASON_UNKNOWN,
    load,
)
from ..model import Coverage, Section, Suggestion

KEY = "friction"

# Outcomes that represent a commitment actually missed, and so have a "why"
# worth counting. `done` and `cancelled` had no miss to explain.
_MISSES = (OUTCOME_PARTIAL, OUTCOME_NOT_STARTED, OUTCOME_UNKNOWN)

# Below this many confirmed reasons, a breakdown is noise dressed as insight.
_PATTERN_SAMPLE = 8

# Where estimate error is worth mentioning at all.
_ESTIMATE_DRIFT = 1.3


def build(facts) -> Section:
    stored = load().values()
    answers = [r for r in stored if facts.period.contains(r.covers_until)]
    previous_period = facts.period.shifted(-1)
    previous = [r for r in stored if previous_period.contains(r.covers_until)]
    if not answers:
        return Section(
            key=KEY,
            label="Friction",
            summary="no check-ins for this period",
            measured=False,
            detail=(
                "`task-gcal checkin` records what happened and why. Without "
                "it, follow-through and attempts are still measured — they "
                "just can't say the cause.",
            ),
            data={"answers": 0},
        )

    outcomes = Counter(r.outcome for r in answers)
    misses = [r for r in answers if r.outcome in _MISSES]
    reasons = Counter(r.reason for r in misses if r.classified)
    unclassified = sum(1 for r in misses if not r.classified)

    mix = " · ".join(f"{reason} {count}" for reason, count in reasons.most_common())
    summary = mix or f"{len(answers)} check-in(s), no reason given"
    if unclassified:
        summary += f" · {unclassified} unclassified"

    detail = [
        f"Check-ins          {len(answers)}",
        "Outcomes:",
    ]
    for outcome, count in outcomes.most_common():
        detail.append(f"  {count:>3}  {OUTCOME_LABELS.get(outcome, outcome)}")
    if reasons:
        detail.append("Reasons (confirmed misses only):")
        for reason, count in reasons.most_common():
            detail.append(f"  {count:>3}  {reason}")
    if unclassified:
        detail.append(
            f"  {unclassified:>3}  unknown — reported as missing, not "
            "assigned a cause"
        )

    detail.extend(_shift(reasons, previous, facts))
    detail.extend(_actual_time(answers, facts))
    detail.extend(_patterns(misses, facts))
    detail.append(
        "No answer is scored. `avoided` costs nothing to admit, on purpose."
    )

    suggestions = _suggest(reasons, facts)

    return Section(
        key=KEY,
        label="Friction",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="confirmed misses carry a reason",
                observed=len(misses) - unclassified,
                total=len(misses),
            ),
            Coverage(
                label="check-ins recorded actual minutes",
                observed=sum(1 for r in answers if r.actual_minutes),
                total=len(answers),
            ),
        ),
        data={
            "answers": len(answers),
            "outcomes": dict(outcomes.most_common()),
            "reasons": dict(reasons.most_common()),
            "unclassified": unclassified,
            "with_actuals": sum(1 for r in answers if r.actual_minutes),
            "previous_reasons": dict(
                Counter(
                    r.reason for r in previous if r.outcome in _MISSES and r.classified
                ).most_common()
            ),
        },
        suggestions=suggestions,
    )


def _shift(reasons: Counter, previous: list, facts) -> list[str]:
    """How the mix moved since the previous period.

    Period-over-period rather than a 12-week series per reason: at realistic
    check-in volumes a weekly series for each of five reasons is almost all
    zeros, and a chart of zeros is not a trend.

    Movements only. There is no "improved" or "worsened" here, because that
    would make one answer better than another to give.
    """
    before = Counter(
        r.reason for r in previous if r.outcome in _MISSES and r.classified
    )
    if not before:
        return []
    moves = []
    for reason in sorted(set(reasons) | set(before)):
        change = reasons[reason] - before[reason]
        if change:
            moves.append(f"{reason} {change:+d}")
    if not moves:
        return [
            f"Since last {facts.period.kind}  the mix is unchanged",
        ]
    return [f"Since last {facts.period.kind}  {' · '.join(moves)}"]


def _actual_time(answers, facts) -> list[str]:
    """Estimate-vs-actual, over finished checked-in work only.

    Restricted to `done`, and that restriction is the whole point. The 20
    minutes you spent on something you didn't finish says nothing about
    whether the estimate was right — averaging partials in would make every
    estimate look generous.
    """
    tasks = facts.by_uuid()
    pairs = [
        (tasks[r.task_uuid].estimate_minutes, r.actual_minutes)
        for r in answers
        if r.outcome == OUTCOME_DONE
        and r.actual_minutes
        and r.task_uuid in tasks
        and tasks[r.task_uuid].estimate_minutes
    ]
    if not pairs:
        return [
            "Actual time        no finished work with an actual — "
            "blocks-to-completion is the fallback",
        ]
    planned = sum(estimate for estimate, _actual in pairs)
    actual = sum(a for _estimate, a in pairs)
    ratio = actual / planned if planned else 0
    return [
        f"Actual time        {humanize_minutes(actual)} against "
        f"{humanize_minutes(planned)} planned",
        f"  over             {len(pairs)} finished checked-in task(s)",
        f"  ratio            {ratio:.2f}x — these tasks only",
    ]


# Where "large" starts, for the size breakdown. Round, and named, because it
# is a reporting bucket rather than a measurement.
_LARGE_TASK_MINUTES = 120


def _size_bucket(task) -> str:
    if task is None or task.estimate_minutes is None:
        return "no estimate"
    return (
        "large" if task.estimate_minutes >= _LARGE_TASK_MINUTES else "small"
    )


def _time_bucket(moment, tz) -> str:
    hour = moment.astimezone(tz).hour
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    return "evening"


def _patterns(misses, facts) -> list[str]:
    """Correlations, gated on sample size and labelled as correlations.

    Four cuts, because each implies a different response: a project (the
    commitment is wrong), task size (large ambiguous work needs
    decomposition), time of day (the slot is wrong), and how busy the week
    was (you planned more than the week had room for).

    Presented as correlations rather than diagnoses, and only once there are
    enough confirmed answers for a breakdown to mean anything — below that,
    coverage is the finding.
    """
    if len(misses) < _PATTERN_SAMPLE:
        return [
            f"Patterns           need {_PATTERN_SAMPLE} confirmed misses; "
            f"have {len(misses)}",
        ]

    tasks = facts.by_uuid()
    tz = facts.period.tz
    classified = [
        (r, tasks.get(r.task_uuid)) for r in misses if r.classified
    ]
    if not classified:
        return []

    cuts: dict[str, Counter] = {
        "project": Counter(),
        "task size": Counter(),
        "time of day": Counter(),
    }
    for reflection, task in classified:
        project = (task.project if task is not None else None) or "(no project)"
        cuts["project"][(project, reflection.reason)] += 1
        cuts["task size"][(_size_bucket(task), reflection.reason)] += 1
        cuts["time of day"][
            (_time_bucket(reflection.covers_until, tz), reflection.reason)
        ] += 1

    out = ["Patterns (correlations, not diagnoses):"]
    for label, tally in cuts.items():
        (bucket, reason), count = tally.most_common(1)[0]
        # Only worth a line if the leading combination is actually leading.
        if count < 2:
            continue
        out.append(f"  {label:<12} {count} x {reason} in {bucket}")

    meeting_share = facts.meeting_share()
    if meeting_share is not None:
        out.append(
            f"  meeting load  the week was {meeting_share:.0%} meetings, "
            "which is the denominator for all of the above"
        )
    return out


def _suggest(reasons: Counter, facts) -> tuple[Suggestion, ...]:
    """One suggestion from the mix, phrased as a change rather than a verdict."""
    if not reasons:
        return ()
    reason, count = reasons.most_common(1)[0]
    if count < 2:
        return ()
    # Deliberately parallel in tone: each is a change to make, and none of
    # them is a judgement about the person who answered.
    advice = {
        "estimate": "estimates are the constraint — try halving the scope of "
                    "the next few instead of doubling the time",
        "blocked": "dependencies are the constraint — the fix is chasing them "
                   "earlier, not planning harder",
        "capacity": "the weeks aren't going to plan — commit to less up front",
        "reprioritized": "the plan is being overtaken — plan a shorter horizon "
                         "rather than a fuller one",
        "avoided": "the hard-to-start ones need a smaller first step, not a "
                   "bigger block",
        REASON_UNKNOWN: "",
    }.get(reason, "")
    if not advice:
        return ()
    return (
        Suggestion(
            f"`{reason}` explains {count} of this {facts.period.kind}'s "
            f"misses — {advice}.",
            weight=3.4,
        ),
    )
