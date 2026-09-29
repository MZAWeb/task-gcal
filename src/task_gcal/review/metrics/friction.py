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
    OUTCOME_LABELS,
    OUTCOME_NOT_STARTED,
    OUTCOME_PARTIAL,
    OUTCOME_PROGRESSED,
    OUTCOME_UNKNOWN,
    REASON_UNKNOWN,
    load,
)
from ..model import Coverage, Section, Suggestion
from ..words import plural

KEY = "reasons"

# What this section means, in plain language, for a report's glossary.
MEANS = (
    "What you said happened when a block didn't go to plan, from task-gcal "
    "checkin. Each reason points at a different fix, so the biggest one is "
    "the one worth acting on."
)

# Each reason and outcome in the words the check-in offered, so the review
# says back what you said rather than an internal code.
_REASON_WORDS = {
    "estimate": "needed more time",
    "avoided": "put it off",
    "capacity": "busy with other things",
    "blocked": "blocked",
    "reprioritized": "other priorities",
    "follow_up": "planned follow-up",
    REASON_UNKNOWN: "no reason given",
}
_OUTCOME_WORDS = {
    OUTCOME_NOT_STARTED: "not started",
    OUTCOME_PARTIAL: "started, not finished",
    OUTCOME_PROGRESSED: "done for now, more to come",
    OUTCOME_UNKNOWN: "unknown",
}


def _said(reason: str) -> str:
    return _REASON_WORDS.get(reason, reason)

# Outcomes that represent a commitment actually missed, and so have a "why"
# worth counting. `progressed` is left out on purpose: work that was always
# going to take more than one sitting isn't friction, and counting it would
# make good planning look like a problem. `done` and `cancelled` no longer
# get offered but may exist in older records; neither had a miss to explain.
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
            label="Reasons",
            summary=f"no check-ins this {facts.period.kind}",
            measured=False,
            # Nothing is wrong: `checkin` is opt-in and hasn't been used.
            optional=True,
            detail=(
                "Run `task-gcal checkin` to say what happened with blocks "
                "that didn't go to plan. The rest of the review works "
                "without it; this is the only place that knows why.",
            ),
            data={"answers": 0},
        )

    outcomes = Counter(r.outcome for r in answers)
    misses = [r for r in answers if r.outcome in _MISSES]
    reasons = Counter(r.reason for r in misses if r.classified)
    unclassified = sum(1 for r in misses if not r.classified)

    mix = ", ".join(
        f"{_said(reason)} {count}" for reason, count in reasons.most_common()
    )
    summary = mix or f"{plural(len(answers), 'check-in')}, no reason given"
    if unclassified:
        summary += f", no reason given {unclassified}"

    detail = [
        f"Check-ins         {len(answers)}",
        "What happened:",
    ]
    for outcome, count in outcomes.most_common():
        words = _OUTCOME_WORDS.get(outcome) or OUTCOME_LABELS.get(outcome, outcome)
        detail.append(f"  {count:>3}  {words}")
    if reasons:
        detail.append("Why it didn't go to plan:")
        for reason, count in reasons.most_common():
            detail.append(f"  {count:>3}  {_said(reason)}")
    if unclassified:
        detail.append(f"  {unclassified:>3}  no reason given")
    continuations = sum(1 for r in answers if r.outcome == OUTCOME_PROGRESSED)
    if continuations:
        detail.append(
            f"Follow-ups        {plural(continuations, 'block')} did "
            f"{'its' if continuations == 1 else 'their'} share of a longer "
            "task (not counted as a miss)"
        )

    detail.extend(_shift(reasons, previous, facts))
    detail.extend(_actual_time(answers, facts))
    detail.extend(_patterns(misses, facts))

    suggestions = _suggest(reasons, facts)

    return Section(
        key=KEY,
        label="Reasons",
        summary=summary,
        detail=tuple(detail),
        coverage=(
            Coverage(
                label="misses have a reason",
                observed=len(misses) - unclassified,
                total=len(misses),
            ),
            Coverage(
                label="check-ins recorded time spent",
                observed=sum(1 for r in answers if r.actual_minutes),
                total=len(answers),
            ),
        ),
        data={
            "answers": len(answers),
            "outcomes": dict(outcomes.most_common()),
            "reasons": dict(reasons.most_common()),
            "unclassified": unclassified,
            "continuations": continuations,
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
            moves.append(f"{_said(reason)} {change:+d}")
    label = f"Vs last {facts.period.kind}"
    if not moves:
        return [f"{label:<16}  the same mix"]
    return [f"{label:<16}  {', '.join(moves)}"]


def _actual_time(answers, facts) -> list[str]:
    """Time used against time set aside, over checked-in blocks.

    Compared with the *block* rather than the task's estimate, and that
    choice is the whole point. None of the answers the check-in offers means
    "the task is finished", so an actual can't say whether an estimate was
    right — but it can say whether the time you booked got used, which is a
    different and answerable question. Blocks-to-completion remains the
    signal about estimates.
    """
    pairs = [
        (r.planned_minutes, r.actual_minutes)
        for r in answers
        if r.actual_minutes and r.planned_minutes
    ]
    if not pairs:
        return [
            "Time spent        not recorded; enter the minutes when you "
            "check in to see how much of each block you used",
        ]
    planned = sum(p for p, _actual in pairs)
    actual = sum(a for _planned, a in pairs)
    return [
        f"Time spent        {humanize_minutes(actual)} of the "
        f"{humanize_minutes(planned)} booked ({actual / planned:.0%}), over "
        f"{plural(len(pairs), 'block')}",
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
            f"Patterns          shown from {_PATTERN_SAMPLE} misses on; "
            f"{len(misses)} so far",
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

    # Where the misses bunch up. Correlations, so each line says where, and
    # leaves why to you.
    where = {
        "project": lambda bucket: (
            "on tasks with no project"
            if bucket == "(no project)"
            else f"in project {bucket}"
        ),
        "task size": lambda bucket: {
            "small": f"on tasks under {_LARGE_TASK_MINUTES // 60}h",
            "large": f"on tasks of {_LARGE_TASK_MINUTES // 60}h or more",
        }.get(bucket, "on tasks with no estimate"),
        "time of day": lambda bucket: f"in the {bucket}",
    }
    out = ["Where the misses cluster:"]
    for label, tally in cuts.items():
        (bucket, reason), count = tally.most_common(1)[0]
        # Only worth a line if the leading combination is actually leading.
        if count < 2:
            continue
        out.append(f"  {count:>3}  {_said(reason)}, {where[label](bucket)}")

    meeting_share = facts.meeting_share()
    if meeting_share is not None:
        out.append(
            f"The {facts.period.kind} was {meeting_share:.0%} meetings, "
            "which may explain more of this than any pattern."
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
        "estimate": "Cut the next few tasks smaller rather than booking "
                    "longer blocks",
        "blocked": "Chase what you're waiting on earlier, before its block "
                   "comes round",
        "capacity": "Book less up front and leave room for what comes up",
        "reprioritized": "New priorities keep overtaking the plan; plan fewer "
                         "days ahead",
        "avoided": "Give the tasks you avoid a small, easy first step "
                   "instead of a bigger block",
        REASON_UNKNOWN: "",
    }.get(reason, "")
    if not advice:
        return ()
    return (
        Suggestion(
            f'"{_said(reason).capitalize()}" was the reason for '
            f"{count} of this {facts.period.kind}'s misses. {advice}.",
            weight=3.4,
        ),
    )
