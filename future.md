# Future strategy

> **Status: built.** This document was the plan; the README now describes
> what exists. Everything in the recommended delivery order (§"Suggested
> delivery order" 1–4) shipped, plus the Markdown, JSON and HTML renderers.
>
> Still deliberately unbuilt, for the reasons argued below:
>
> - **A TUI** (§"Why I would defer a TUI"). Terminal reports plus
>   `--section` drill-down have not proved awkward, and the renderer
>   architecture means adding one later wastes none of this work.
> - **Other scheduling sources** — demands, occurrences, lanes, configured
>   fitness (§"Suggested delivery order" 5). Waiting on the
>   scheduler/report boundary having settled in real use.
> - **Gamification** — streaks and dials (§principle 10). The metrics need
>   useful coverage before anything is allowed to reward or penalise an
>   answer.
>
> Two deliberate deviations, both from using it:
>
> - §3 specifies two check-in questions, "what happened" and "why". The
>   stored record still has both — the metrics need them apart — but the
>   *prompt* asks once, offering the five (outcome, reason) pairs that
>   actually occur in plain language. `done` and `cancelled` aren't offered
>   at all: those get `task done` and `task delete`. Two questions was twice
>   the friction for no extra truth.
> - `review --reflect` is gone. It was `checkin` followed by a report, which
>   made two entry points to one interaction and gave `review` a mode that
>   could block on questions. `task-gcal checkin && task-gcal review --week`
>   is the same thing with nothing to explain.
>
> Kept as the argument for why the thing is shaped this way, which is worth
> more than the plan was.

We are deciding the product shape, not implementing it yet.

My strategic recommendation is: **keep task-gcal CLI-first and do not commit to a TUI yet.**

## Proposed product shape

Treat task-gcal as three related workflows with strict boundaries:

## 1. Scheduling: fast action path

```console
task-gcal
task-gcal --dry-run
```

This remains the core experience:

- Starts quickly.
- Reads tasks and calendars.
- Reconciles events.
- Appends a journal observation.
- Never loads historical analytics or reporting code.
- Produces the same concise operational report it does today.

Reviews must not make this path slower or conceptually heavier.

## 2. Reviews: read-only report path

```console
task-gcal review --week
task-gcal review --month
task-gcal review --week --section deadlines
task-gcal review --week --reflect
task-gcal review --month --format markdown
task-gcal review --week --format json
```

A review is fundamentally a document, not an application. It should produce a useful one-screen summary:

```text
Week 36

Capacity       45h available - 22h meetings - 18h planned
Completed      14 tasks / 11.5 planned hours
Follow-through 12 of 18 scheduled blocks
Deadlines      3 observed pushes across 2 tasks
Friction       3 overloaded - 2 avoided - 1 underestimated
Boundaries     95m in evenings, 90m on Saturday

Look at: "Prepare PIR" was deferred for the third time.
```

Then optional sections provide detail. The default should not print all thirteen metrics currently listed in the roadmap. It remains read-only and non-interactive, but reports how many strong signals still need explanation. Explicit `--reflect` runs the same retrospective prompts as `task-gcal checkin` before rendering the report; this keeps normal output scriptable while allowing the weekly review to be the moment when missing context is captured.

All output formats should render the same internal report model:

- `terminal`: concise human output
- `markdown`: durable weekly/monthly notes
- `json`: scripting, experiments, or future visualizations

JSON is an important escape hatch: it lets us experiment with charts, notebooks, or another application without putting those concerns into the scheduler.

## 3. Guided actions: lightweight and optional

```console
task-gcal checkin
task-gcal review --triage
```

`checkin` is an optional retrospective workflow, not a daily habit requirement. It walks scheduled blocks that have ended and have not already been reviewed, whether they are from yesterday or several days ago:

```text
Tue 09:00  Write proposal (estimated 60m)
Evidence: block ended with task open; due date later moved from Tue to Fri
What happened? [d]one / [p]artial / [n]ot started / [x]cancelled / [u]nknown: p
Primary reason:
  [e] estimate or scope was wrong
  [b] blocked by a dependency
  [w] week changed or capacity disappeared
  [r] consciously reprioritized
  [a] avoided it / procrastinated
  [u] unknown or other
Reason: a
Actual minutes (optional): 20
```

Important behavior:

- Run it daily, every few days, or not at all.
- By default, show unreviewed blocks from a bounded recent period; allow an explicit `--since` when catching up.
- Store each answer once so repeated check-ins do not ask about the same block again.
- Allow skipping any item. Unknown remains unknown and never silently becomes the estimate.
- Reviews use actual-time metrics only for checked-in blocks and always show coverage.
- Completion-derived signals such as blocks-to-completion remain available when no check-in exists.
- Existing evidence may suggest that a commitment was not met, but never guesses the reason or records an outcome without confirmation. A due-date push alone means a deadline moved, not that no work happened.
- Ask about a **miss episode**, not every calendar block. Several passed blocks and deadline changes for the same still-open task should normally become one reflection prompt.
- Keep two dimensions separate: what happened (`done`, `partial`, `not_started`, `cancelled`, `unknown`) and why (`estimate`, `blocked`, `capacity`, `reprioritized`, `avoided`, `unknown`).
- Ask for one primary reason to keep the interaction fast. An optional note can preserve nuance; do not build a taxonomy editor.
- Reflections remain open until answered, so running this every few days or during the weekly review works equally well.
- Answers can be corrected later. They are retrospective labels, not immutable facts.

Triage stays non-mutating and prints exact Taskwarrior commands for the user to inspect and paste:

```text
Stagnant: Prepare PIR - due pushed 4x, two blocks passed
  task 40 modify wait:someday
  task 40 modify estimate:30
  task 40 delete
```

This preserves Taskwarrior as the source of truth without requiring a full-screen TUI.

## Why I would defer a TUI

A TUI is useful when the primary workflow involves repeatedly:

- navigating lists
- filtering and sorting
- expanding details
- comparing periods
- taking several actions without leaving the interface

We do not yet know that this is how reviews will be used. A weekly review may simply be something you read once, act on, and save to Markdown.

A TUI would introduce a second application inside task-gcal:

- terminal framework and dependencies
- keyboard navigation
- screen resizing and color handling
- accessibility concerns
- much larger snapshot/testing surface
- persistent interaction state
- temptation to become a dashboard

That works against “simple and fast” unless interactive exploration proves central.

## If richer visualization becomes useful

For trends and charts, I would consider a **standalone HTML report before a TUI**:

```console
task-gcal review --month --format html --open
```

A generated local HTML file can provide:

- sparklines and charts
- expandable sections
- project breakdowns
- no server or daemon
- no effect on scheduling
- a better medium for monthly trends than terminal graphics

It should remain an optional renderer, not a dashboard service.

## Internal strategy

The clean boundary is:

```text
collect facts -> compute report -> render report
```

### Collection

Raw, append-only observations:

- task snapshots
- placements and movement
- completion timestamps
- due-date changes
- optional reflection outcomes, primary reasons, notes, and actual minutes
- calendar capacity

Collection should not calculate scores or store running totals. The roadmap's journal rules are worth retaining:

- scheduling may append observations but never reads history to make placement decisions
- a non-mutating `snapshot` command can collect observations on a regular cadence
- records carry schema version, tool version, timezone, source health, and settings hash
- reviews tolerate a truncated final record and unknown future fields
- this data volume is small enough for JSONL; do not introduce a database
- task titles and deadlines are sensitive, so journal files remain local and private

### Computation

Pure metric functions turn observations into a `Review`:

```text
capacity(...)
throughput(...)
deadline_changes(...)
follow_through(...)
friction_reasons(...)
boundary_erosion(...)
```

These functions should not print, prompt, or mutate anything.

### Presentation

Renderers consume the resulting `Review`:

```text
TerminalRenderer
MarkdownRenderer
JsonRenderer
possibly HtmlRenderer later
```

A future TUI could consume exactly the same model. Deferring it therefore does not close the door or waste work.

## Product principles worth carrying over from the roadmap

The long roadmap contains several ideas that sharpen this smaller plan:

1. **Capacity comes first.** Meeting load and available time provide the denominator for every completion statistic. A difficult week with half its capacity consumed by meetings is different from an unexplained miss.
2. **Every number carries coverage.** Say `38/50 tasks had estimates`, `4/7 days observed`, and which calendar was measured. Missing data must not quietly become zero.
3. **End with one adjustment.** A review should recommend one concrete thing to inspect or change. Otherwise it is only a dashboard.
4. **Do not confuse planned time with actual work.** Estimate-minutes are planned minutes. Actual-time metrics use only optional check-in data and state their coverage; completion timestamps and blocks-to-completion remain the fallback.
5. **Do not compare incompatible periods.** Settings, workflow, or metric-definition changes create trend boundaries that should be annotated rather than averaged across.
6. **Make the calendar stable before measuring follow-through.** Existing near-term placements should remain fixed unless invalid. Otherwise placement churn measures the scheduler's restlessness rather than the user's behavior.
7. **Keep scheduling fail-closed.** An empty or failed source must never mean "delete everything." Source health and the existing removal guard remain scheduling invariants.
8. **Distinguish things to place from things already present.** A `Demand` needs a slot; an `Occurrence` is existing calendar evidence and busy time. This prevents imported or hand-created fitness sessions from being duplicated.
9. **Prefer honest, coarse signals to invented precision.** Optional check-ins provide actuals when available; blocks-to-completion remains a coarser signal grounded in completion facts. Unknown remains unknown.
10. **Gamification follows trustworthy measurement.** Streaks and dials come after the underlying metrics have useful coverage, and can be disabled entirely. Never reward or penalize a particular reflection answer—especially `avoided`—or the system will train the user to give dishonest answers.

## What the existing data already supports

A read-only analysis of Taskwarrior history and task-gcal-owned calendar events from 2026-06-03 through 2026-09-03 found substantially more usable history than the original plan assumed:

- 142 tasks completed, 23 currently pending, and 3 deleted in the inspected population.
- Taskwarrior retained individual due-date, estimate, scheduled-date, wait-date, and description changes for recent tasks.
- 86 later-due changes occurred across only 28 tasks, moving deadlines by a cumulative 777 days. This is concentrated behavior: 12 tasks account for 64 of the 86 pushes.
- 22 due dates were moved only after the old due day had passed.
- 163 managed calendar blocks remain available across 81 task UUIDs. Among the correlated recent tasks, 33 had multiple blocks.
- 11 still-pending tasks already had 26 past blocks.
- For completed tasks with a final due date, 18 missed the original date but met the renegotiated date; 3 missed both. Another 32 missed an unchanged final date.
- History also showed 26 estimate changes across 13 tasks, 43 scheduled-floor changes across 25 tasks, 7 wait changes across 4 tasks, and 14 description changes across 8 tasks.

These observations support several features without pretending the data says more than it does.

### Deadline inference, with confidence levels

Changing a due date after its previous due day is strong evidence of a **missed deadline**, but not proof that a scheduled work block was skipped. The task may have been partially completed, blocked, or given a genuinely changed external deadline.

A stronger combined signal is available when all three facts are true:

1. a task-gcal block ended while the task was still open;
2. the old due day then passed; and
3. the due date was moved later.

There were 12 such due-date changes in the inspected period. Another 21 pushes happened within three days of a block ending while the task remained open. These are good **likely unfinished/deferred** suggestions for optional check-in, not automatic verdicts.

Use an evidence ladder in reports and check-ins:

- **Fact:** deadline moved after its old date.
- **Fact:** scheduled block ended while task remained open.
- **Suggestion:** likely skipped, partial, or deferred.
- **Confirmation:** the optional check-in records what actually happened and any actual minutes.

### Features enabled by the existing history

1. **Promise ledger.** Preserve original, current, and final due dates; number of pushes; cumulative days moved; and whether completion met the original or only the renegotiated promise.
2. **Reactive vs proactive replanning.** Separate pushes made before the old deadline from pushes made after it. Neither is automatically bad, but they describe different behavior.
3. **Reflection-assisted check-in.** Turn strong signals into unresolved miss episodes, then ask what happened and why. Prioritize the strongest evidence first, coalesce repeated blocks for the same task, and never infer the cause. The five useful primary reasons are:
   - **Estimate/scope:** the task was larger or less clear than planned.
   - **Blocked:** another person, dependency, or system prevented progress.
   - **Capacity:** meetings, incidents, illness, or the week otherwise changed.
   - **Reprioritized:** consciously chose something more important.
   - **Avoided:** procrastinated or did not feel like starting it.

   `unknown/other` remains an escape hatch and is reported as missing classification, not forced into a misleading bucket.
4. **Attempts or blocks-to-completion.** Count blocks that passed before completion. Multiple blocks may mean an underestimated task, interruption, partial progress, or deliberate multi-session work, so report the count without guessing the cause.
5. **Stagnation queue.** Surface pending tasks with past blocks, repeated deadline pushes, or both. These are stronger triage candidates than old tasks alone.
6. **Estimate and scope churn.** Upward estimate revisions and repeated description changes can identify tasks that were really projects. Scheduled/wait changes reveal deliberate deferral separately from deadline churn.
7. **Missed unchanged deadlines.** Do not focus only on pushed dates: the data contains many tasks completed after a deadline that was never changed.
8. **Friction mix.** Show the distribution of confirmed reasons, its coverage, and changes over time: `capacity 4 - avoided 3 - estimate 1 - blocked 1`. This is more actionable than a generic follow-through score.
9. **Pattern discovery.** Once coverage is adequate, break reasons down by project, task size, time of day, and meeting load. Examples: avoidance concentrated in large ambiguous tasks suggests decomposition; capacity misses concentrated in meeting-heavy weeks suggest planning less. Require a reasonable sample and present these as correlations, not diagnoses.
10. **Source-aware scoring.** Recurring tasks and externally synchronized tasks should be identified so automated changes are not attributed to personal behavior without evidence.
11. **Historical backfill.** A one-time, read-only importer can seed recent history from Taskwarrior `info` plus past managed calendar events, then use the JSONL journal going forward. This avoids waiting several weeks before the first useful review.

Taskwarrior's human-readable `info` output is useful for backfill but should not become the permanent analytics API: parsing it for every task is slower and more fragile than collecting structured journal snapshots. Past calendar events also reveal only their final stored times, not every move made before they occurred. The journal remains necessary for reliable future placement-churn analysis.

## Suggested delivery order

The CLI-first strategy suggests a narrower sequence than implementing every roadmap theme at once:

1. **Calendar stability and safety.** Preserve valid near-term placements and retain fail-closed deletion guards.
2. **Observation and backfill.** Add the append-only journal and non-mutating `snapshot` command, then seed recent history from Taskwarrior modification history and past managed events so the first review is immediately useful.
3. **Small weekly review.** Ship capacity, throughput, backlog flow, lead time, and follow-through in one terminal screen.
4. **Deeper sections and reflection.** Add observed deadline churn, stagnation, boundaries, blocks-to-completion, friction reasons, and optional check-in calibration behind `--section`; let explicit `review --reflect` resolve unexplained miss episodes before rendering.
5. **Other scheduling sources.** Add demands, occurrences, lanes, and configured fitness only after the core scheduler/report boundary is stable.
6. **Motivation and richer renderers.** Add streaks only after the metrics are trusted; add Markdown, JSON, or HTML when an actual use calls for them.
7. **Reconsider a TUI.** Do this only if repeated filtering, drill-down, and multi-action review sessions prove awkward in normal CLI output.

Each step should be independently useful. The review engine should not wait for fitness support, and fitness support should not require gamification.

## Scope reduction I would apply to the roadmap

The roadmap currently risks making reviews into a large analytics product. I would establish these constraints:

1. Bare `task-gcal` remains scheduling-only in behavior and performance.
2. The default weekly review fits on one terminal screen.
3. Detailed metrics are requested by section rather than always printed.
4. Every metric must lead to a decision or be removed.
5. No plugin framework for metrics or sources initially.
6. No TUI until terminal reports and section-based drill-down are demonstrably awkward.
7. No separate `score` command initially; include motivation in the review.
8. Terminal output ships first. Markdown and JSON are deferred renderers; HTML still comes before a TUI.
9. Analytics dependencies, if any, are lazy or optional.
10. Review code never participates in calendar reconciliation.
11. Reviews, check-ins, and triage never write to Taskwarrior.
12. Check-in is optional and retrospective; no metric or streak assumes a daily cadence.

So the planned interface could initially remain as small as:

```console
task-gcal                         # schedule
task-gcal --dry-run
task-gcal snapshot               # non-mutating observation
task-gcal review --week
task-gcal review --week --reflect     # optionally classify unexplained misses
task-gcal review --month
task-gcal checkin                    # optional; reviews unreviewed past blocks
task-gcal review --triage
task-gcal doctor
```

My overall recommendation is therefore:

> **One executable, separate fast and analytical paths, document-style reviews, optional retrospective check-ins, safe command suggestions for triage, and a renderer architecture that leaves room for HTML or a TUI later.**
