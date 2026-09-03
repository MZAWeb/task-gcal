# Roadmap

A working document, not a commitment. Three things we want next:

- **A.** Schedule things that don't live in Taskwarrior — starting with training
  (Garmin plan, or hand-configured fitness blocks).
- **B.** Look backwards: week/month reviews. How much got done, how often
  deadlines slipped, how often due dates got pushed rather than met.
- **C.** Make the above motivating rather than depressing — some honest
  gamification.

Everything below is grouped as: what we'd have to change structurally, then
each theme, then a phased order, then open questions.

### North-star outcomes

This should help answer three practical questions, not merely produce more
charts:

1. **What is a realistic plan for the time I actually have?** Protect training
   and other commitments without silently overbooking work.
2. **What happened to the plan?** Separate completed work, changed priorities,
   meeting pressure, bad estimates, and repeated deferral.
3. **What is the smallest useful adjustment next week?** Reviews should end
   with one or two actions, not a productivity report card to feel bad about.

A useful guardrail: this remains a personal, local-first CLI. A dashboard,
cloud service, team analytics, automatic coaching, and direct Garmin account
credentials are non-goals until the local workflow proves valuable.

---

## 0. Where we are today

Worth naming the properties that make the current tool trustworthy, because
each theme below puts pressure on one of them:

1. **Stateless.** Every run recomputes everything from Taskwarrior + Calendar.
   There is no local database that can drift, corrupt, or disagree.
2. **Idempotent.** Re-running with no input changes mutates nothing.
3. **Calendar is the only thing we write.** Taskwarrior is read-only
   (`task export`, never `task modify`).
4. **One kind of input.** A pending task with an `estimate` and a `due`, placed
   in the earliest free working-hours slot before its deadline.
5. **One time window.** `work_start_hour` .. `work_end_hour` on `work_days`.

Theme A breaks (4) and (5). Theme B breaks (1) — reviews need history, and
history has to be stored. Theme C potentially breaks (3) if we ever want
triage actions to write back to Taskwarrior.

We should break these deliberately, one at a time, and keep the parts that
still hold. In particular: **(1) can be preserved in spirit** — see the
journal decision below.

There are also **no tests**. Adding a journal, multiple sources, multiple time
profiles, and a pile of review arithmetic to a 700-line untested `cli.py` is
how this project stops being fun. Test harness first (Phase 0).

**One thing shipped ahead of the phases**, because §1.2's fail-closed principle
turned out to describe a live bug rather than a future risk: every removal is
driven by what Taskwarrior reports, so a mistyped `--estimate-uda` made every
task look estimate-less and dropped all its future events (verified: 10 of 10),
and an empty `task export` is indistinguishable from "you finished everything"
while having several silent causes — an active Taskwarrior context, `TASKDATA`
pointing at another replica from a cron environment, a wrong report name. A run
now refuses to remove more than `removal_guard_ratio` (default half) of the
unfinished events it owns, and refuses outright when the task list came back
empty. Removals are planned during reconciliation and executed at the end so
the guard sees the whole plan. This is the first invariant we've *added* rather
than broken, and it should hold for every source added later.

---

## 1. Structural decisions that everything else depends on

### 1.1 The run journal — *do this first, it's time-sensitive*

Append-only JSONL, one record per run:

```
~/.local/share/task-gcal/runs/2026-09.jsonl
```

```jsonc
{"run": "2026-09-03T09:04:11Z", "settings_hash": "…",
 "meeting_minutes_in_window": 240,
 "demands": [
   {"src": "taskwarrior", "key": "f25a00fa-…", "desc": "Update cost tracker",
    "project": "monitoring", "tags": ["work"], "est": 60,
    "due": "2026-09-04T00:00:00Z", "urgency": 25.4,
    "placed": ["2026-09-03T14:00:00Z", "2026-09-03T15:00:00Z"],
    "action": "unchanged"}
 ]}
```

Rules that keep it honest:

- `schedule` **only ever appends**. It never reads the journal to make
  placement decisions. So a corrupt or deleted journal can never produce a
  wrong calendar — scheduling stays stateless where it matters.
- `review` **only ever reads** it. Calibration may later read aggregate history,
  but only behind the explicit `--calibrate` flag and must show the factor it
  applied.
- Every derived number (scores, streaks, churn counts) is *recomputed* from the
  journal, never stored as a running total. This is reproducible, not
  tamper-proof: it is a local text file and that is fine.
- Deleting it costs you history, nothing else.
- Each record gets a schema version, tool version, timezone, run mode
  (`schedule`/`dry-run`), source health, and a stable run id. Reviews ignore
  dry-runs by default and must tolerate truncated final lines and unknown newer
  fields.
- The journal records observations, not omniscient history. If a due date moves
  twice between runs, we see one move; if it moves out and back, we see none.
  Every churn metric must therefore say **observed due-date changes** and report
  the sampling period.
- Writes are atomic at the line level, files/directories are mode 0600/0700,
  and descriptions can be omitted or hashed with `journal_detail = "minimal"`.
  Task titles, projects, tags, and deadlines are sensitive personal data.
- **Metric definitions get versions too, not just records.** Reviews compare
  against a rolling 12-week median (§4.2), so a definition change silently
  spans two meanings — Phase 1's "capacity" is one work window, Phase 2's is a
  union of lanes. Record a metrics-definition version alongside the schema
  version, and refuse to draw a trend across a boundary where it (or
  `settings_hash`) changed. Annotate rather than average.
- Size is a non-issue: ~20 demands × a few runs a day is single-digit MB a
  year. No rotation, no pruning, no database.
- "Week" means ISO week, Monday start, in the journal record's own timezone.
  Write it down once here so §2.2's quotas and §3's periods can't disagree.

**Cadence is a feature, not a caveat.** If fidelity depends on how often we
run, then running often is part of the design — so add a `snapshot` subcommand
that appends a journal record and touches nothing else: no calendar writes, no
network mutation, safe to run from launchd every few hours. That turns "a lower
bound of unknown tightness" into "sampled every 6h", and it decouples history
from the habit of running `schedule`. It's the cheapest item in this document
and it should land with the journal itself.

**Why first:** almost every interesting number in themes B and C is a *diff
between two runs* (did `due` move? did the block we placed survive? was the
task still open the next day?). None of that is reconstructable after the fact.
I checked what history already exists, and it's thin:

| Source | What it gives us | Verdict |
| --- | --- | --- |
| `task export completed` (1749 tasks) | `entry`, `end`, `due`, `project`, `modified` | Good for throughput + lead time, **today** |
| …of those, 149 have a `due` | on-time rate | Usable but a small sample |
| …of those, 103 have an `estimate` | planned-vs-done minutes | Too thin to trust |
| Taskwarrior modification journal (`task <id> info`) | per-field change log with dates | **Collapsed on 2026‑05‑13** — everything predates into a single recovery entry, so there is no due-date-change history to mine |
| `taskchampion.sqlite3` `operations` table | raw op log | Couldn't read it from here (permissions); worth a look, but the recovery reset limits it either way |
| Timewarrior | actual time spent | Not installed |

So: **due-date churn — the exact thing you want to see — cannot be computed
retrospectively.** It starts accruing the day we ship the journal. Every week
we delay is a week of the review we can't have.

### 1.2 Generalize "task" → "demand"

The scheduler doesn't care that its input is a Taskwarrior task. Introduce one
internal shape and make Taskwarrior a *provider* of it:

```python
@dataclass(frozen=True)
class Demand:
    source: str            # "taskwarrior" | "fitness" | "ics:races" | …
    key: str               # stable id within the source
    title: str
    duration_minutes: int
    earliest: datetime | None
    deadline: datetime | None
    priority: float        # provider value mapped onto documented bands
    lanes: tuple[str, ...] # allowed lanes, in preference order (§1.3)
    kind: str              # "commitment" | "task" | "filler"
    metadata: Mapping[str, object]
```

Keep the scheduling value object serializable and free of callbacks; event
body rendering belongs to the provider. Recurring definitions such as "run
3x/week" are templates, not demands. The fitness provider materializes one
stable-keyed `Demand` per occurrence for the scheduling horizon.

`sources/taskwarrior.py`, `sources/fitness.py`, `sources/ics.py` each expose a
result containing demands plus source health. `scheduler.py` and reconciliation
stop importing `TaskInfo` at all.

**Quota sources need two phases, and a plain `load()` can't express them.**
Deciding how many `easy-run` occurrences to materialize requires knowing how
many already exist on the calendar this week — so the provider has to see
calendar state *before* it can produce demands, which inverts the flow
elsewhere (sources produce demands, then reconciliation reads the calendar).
Make it explicit rather than letting the fitness provider quietly grow a
calendar client:

```python
class Source(Protocol):
    def load(self, settings) -> SourceResult: ...
    # Optional. Given the occurrences we already own in the horizon,
    # return the demands still needed. Default: return result.demands.
    def materialize(self, result, existing: Sequence[Occurrence]) -> list[Demand]: ...
```

That keeps `load` pure and testable (contract tests in §1.7), keeps calendar
access in one place, and makes "how many runs do I still owe this week?" an
explicit input rather than hidden state.

**Reconciliation must fail closed per source.** If Garmin/ICS/Taskwarrior fails
to load, do not interpret its empty result as "delete all its events." Only
clean up a source namespace after a successful, complete fetch. This becomes
more important than global idempotence as soon as network sources exist.

**Event identity has to change with it.** Today: `taskUuid=<uuid>`. We need
`source` + `key`, so a fitness block and a task can't collide:

```
extendedProperties.private = {
  scheduler: "task-gcal",
  source:    "taskwarrior",
  key:       "<uuid>",
  taskUuid:  "<uuid>",   # keep writing for one release, for back-compat
}
```

Read path: prefer `source`/`key`, fall back to `taskUuid` with an implied
`source=taskwarrior`. Drop the fallback a release later. Worth writing a
`task-gcal doctor --migrate-events` that backfills the new keys in place.

### 1.3 Lanes (named time profiles)

A 7am run must not be scheduled 9–18 Mon–Fri, and a work task must not land at
7am Sunday. So `work_*` becomes one profile among several:

```toml
[lanes.work]
start_hour = 9
end_hour   = 18
days       = [0,1,2,3,4]

[lanes.personal]
start_hour = 6
end_hour   = 22
days       = [0,1,2,3,4,5,6]

[lanes.morning]
start_hour = 6
end_hour   = 8
days       = [0,1,2,3,4,5,6]
```

Back-compat: top-level `work_start_hour`/`work_end_hour`/`work_days` keep
working and define the `work` lane. Every demand declares a lane; Taskwarrior
demands default to `work` and can override per task (`gcal:'lane=personal'`).
A demand may name several lanes in preference order (`lane=morning,personal`).
Lane overlap is allowed, but capacity reporting must union overlapping windows
rather than double-counting them.

### 1.4 Placement passes

Fitness blocks aren't "urgent tasks" — they're near-fixed commitments that
tasks should route *around*. So placement becomes ordered passes, each pass's
output being busy time for the next:

1. **Pass 1 — flexible commitments.** Configured fitness sessions that need a
   time chosen. They are placed before ordinary tasks.
2. **Pass 2 — tasks.** Current behavior, now working around pass 1.
3. **Pass 3 — fillers.** Optional reading/admin items that may use leftover
   gaps but never displace the first two passes.

Fixed imported events are not demands at all when they already appear on a
calendar included in the busy-time query; they are simply busy time. An import
source needs an explicit mode: `protect` existing events, or `mirror` external
plan items onto a task-gcal-owned calendar. This avoids duplicate workouts.

Ordered passes make "protect my training time" real, but they also create a
stability problem: adding one commitment can cascade-move many task events.
Prefer existing valid placements within each pass, then place only new or
invalid items. Minimize churn before optimizing for earliest placement.

### 1.5 Subcommands

```
task-gcal                    # = task-gcal schedule (back-compat, unchanged)
task-gcal schedule [--dry-run] [--force]
task-gcal snapshot                # append a journal record, change nothing
task-gcal review  [--week | --month | --since DATE] [--md PATH]
task-gcal checkin                 # 30s: yesterday's blocks — done? how long?
task-gcal score                   # the scorecard alone
task-gcal fitness --list / --dry-run
task-gcal doctor                  # config sanity, auth, event migration
```

Bare `task-gcal` must keep meaning `schedule`, and every existing flag must
keep working — argparse subparsers with a default subcommand.

### 1.6 Target module boundaries

`cli.py` is 694 lines and does orchestration + reporting + arg parsing. Extract
these boundaries incrementally as the phases need them (not as a big-bang
rewrite):

```
cli.py          argparse + dispatch only
schedule.py     reconcile()
report.py       the schedule report (moved verbatim)
journal.py      append / read / iterate runs
review.py       the metrics
score.py        scorecard, streaks
sources/        taskwarrior.py, fitness.py, ics.py, base.py
lanes.py        time profiles
```

### 1.7 Test strategy (Phase 0 starts it)

Phase 0 adds:

- `pytest`, a `FakeGCal` implementing the four methods `reconcile` uses, and a
  fixture that stubs `task export` output as JSON.
- Characterization tests for the current behavior *before* refactoring: the
  overdue horizon, in-progress pinning, duplicate cleanup, `scheduled`/`wait`
  floors, midnight-due end-of-day bump. Assert semantic outcomes rather than
  freezing the current terminal prose in large golden files.
- `scheduler.find_earliest_slot` is pure — test it directly and hard (DST
  boundaries, buffer at window edges, alignment).

Add with the relevant later phase:

- Contract tests for every provider: stable keys, partial/network failure,
  duplicate input, malformed records, and "failed fetch deletes nothing."
- Journal tests: append interruption, schema evolution, timezone boundaries,
  repeated runs, and redacted/minimal detail.

Do not make a big-bang module split a prerequisite for recording history.
Extract seams as each tested vertical slice needs them; otherwise Phase 0 can
become an architecture project with no user-visible payoff.

---

## 2. Theme A — other sources of things to schedule

### 2.1 Garmin: reality check

I'd rather not build on what I *think* Garmin offers. My understanding, to be
verified before anything is built (see §6):

| Route | Reality | Verdict |
| --- | --- | --- |
| Garmin Connect Developer Program (Health / Activity / Training APIs) | Partner-gated: signed agreement, business review. No personal API key. The Training API is for *pushing* workouts to Garmin, not reading your plan. | Not viable for a personal CLI |
| Official ICS feed of the Garmin Connect calendar | I don't believe one exists | Verify, probably dead end |
| **intervals.icu** | Free, documented API, personal API key, auto-syncs from Garmin Connect, and exposes planned workouts + an ICS feed | **Recommended Garmin route** |
| TrainingPeaks athlete calendar ICS | Believed to exist | Verify; same importer as intervals.icu |
| `garminconnect` / `garth` (unofficial) | Works today. Needs your Garmin password + MFA, no ToS blessing, breaks whenever Garmin changes something | Opt-in extra, never in core |
| Watched folder of exported `.fit`/workout files | Dumb, robust, manual | Fallback |

The pattern that falls out: **build the generic ICS importer, and Garmin
becomes "point intervals.icu at Garmin, point us at intervals.icu."** One
importer, and it also gets us race calendars, school calendars, a partner's
shared calendar, anything.

### 2.2 Configured fitness blocks — the v1 that has no dependencies

You offered this as a fallback; I think it's actually the better *first*
feature, because it introduces the one genuinely new scheduling concept:

```toml
[[fitness]]
name     = "Easy run"
duration = 45
per_week = 3            # a weekly quota, not a due date
lane     = "morning"
spacing  = "1d"         # at least a day between sessions of this block

[[fitness]]
name     = "Long run"
duration = 90
days     = [5, 6]       # Sat/Sun only
per_week = 1
lane     = "personal"

[[fitness]]
name     = "Strength"
duration = 30
per_week = 2
lane     = "morning"
not_after = "Long run"  # don't stack it the morning after the long one
```

New machinery this needs, none of which exists today:

- **Quota templates.** "3 of these per ISO week", not "one, before Thursday".
  Materialize occurrences with deterministic keys such as
  `fitness:easy-run:2026-W36:2`. Placement means: count scheduler-owned
  occurrences this week, place the shortfall, and spread across remaining days
  rather than earliest-first — earliest-first would stack all three runs on
  Monday morning. Do not loosely match arbitrary calendar titles by default;
  false matches are worse than an explicit `adopt` command or source id.
  **Keys are pinned at first placement and never renumbered.** The ordinal is
  arbitrary — sessions within a template are interchangeable, so if you do #1
  and #3, "#2 is missing" is meaningless. Renumbering would make a moved
  session look like a deleted one plus a new one.
- **Spacing / anti-adjacency constraints** between demands.
- **An accepted limitation, stated now.** `spacing` plus cross-template
  `not_after` makes placement a constraint problem, and greedy earliest-fit
  will fail on weeks that are actually solvable. Stay greedy and best-effort:
  report unplaceable sessions in the review (a missed quota is information, not
  a bug) and don't build a backtracking solver. If that turns out to bite,
  the fix is relaxing constraints in a documented order, not a SAT solver.
- **Weekly rollover policy.** Monday comes, you did 1 of 3 runs. Do the missing
  2 vanish (recommended — you can't bank sleep) or roll over? Make it a flag,
  default vanish, and *report* the miss so it lands in the review.

Deliberately out of scope for v1: intensity, periodization, "don't run hard the
day before a race". If we later read a real plan from intervals.icu, that
information comes with it.

### 2.3 Other sources, same machinery

Once `Demand` exists these are each a small file rather than a project:

- **Things** — you already sync it into Taskwarrior (`things_uuid`,
  `things_area`), so it's covered. Worth using `things_area` as a lane hint:
  personal areas → `personal` lane.
- **GitHub** — issues assigned to you, PRs awaiting your review, as demands
  with a duration guess and a soft deadline.
- **Recurring chores / admin** — the same quota mechanism as fitness
  ("inbox zero 3×/week, 20m").
- **Reading list** (Readwise / Instapaper / a `read` tag) — low-priority
  demands that fill leftover gaps instead of leaving 25-minute holes.
- **A "someday" pressure valve** — when a week has slack, pull one waiting task
  forward. Anti-stagnation, and it makes a light week feel earned.

---

## 3. Theme B — reviews and introspection

`task-gcal review --week`. The sections below detail each metric; §3.0 is the
index — what the report should actually contain, in order, and what each line
costs to build.

### 3.0 The reporting wishlist

Ordered as the weekly report should read: **capacity first** (so every later
number has an honest denominator), then output, then integrity, then
behaviour, then exactly one suggested change. Nothing here is a grade.

| # | Report line | Question it answers | Source | Available |
| --- | --- | --- | --- | --- |
| 1 | **Capacity** — lane capacity, meetings, schedulable, scheduled, completed | "Where did the week go before I started?" | Calendar busy + lane windows | **Today** (§3.2) |
| 2 | **Throughput** — count + planned minutes, vs 12-week median, by project / tag / area | "How much moved?" | `task export completed` | **Today** (§3.1) |
| 3 | **Net backlog flow** — created vs closed vs deleted | "Am I filling the funnel faster than I empty it?" | `entry` / `end` / deleted status | **Today** |
| 4 | **Lead time** — median `entry` → `end`, and the tail | "How long does something wait before I do it?" | `task export completed` | **Today** |
| 5 | **Follow-through** — blocks honored, blocks that passed with the task still open, worst hour of day | "Did the plan survive contact?" | Past managed events + completions | **Mostly today** (§3.5) |
| 6 | **Placement churn** — how many times each task's block was moved before it happened | "Is my calendar a plan or a suggestion?" | Journal | Phase 3 (§1.8) |
| 7 | **Deadline integrity** — on-time rate, observed pushes (count, days, repeat offenders), self-deferred vs external | "Do my due dates mean anything?" | Journal + `due`/`end` | Partly today (§3.3) |
| 8 | **Boundary erosion** — evenings and weekend days claimed, widening overrides, which projects took them | "What is this costing me outside work hours?" | Override UDA + placed blocks | Partly today (§3.7) |
| 9 | **Calibration** — actual ÷ estimate by project/tag, with coverage | "Are my estimates fiction?" | Check-in | Phase 4 (§3.6) |
| 10 | **Fitness** — quota hit/missed per template, sessions moved, streak | "Did training survive the week?" | Fitness source + calendar | Phase 2 (§2.2) |
| 11 | **Stagnation** — the triage list, with a prescribed action each | "What am I lying to myself about?" | Journal | Phase 3 (§3.4) |
| 12 | **Coverage & caveats** — calendars measured, estimate coverage, unobserved days, definition changes | "How much should I trust the above?" | Journal metadata | With each phase |
| 13 | **One adjustment** — a single suggested change for next week | North-star #3 | Derived | Phase 5 |

Rules for the report as a whole:

- **Every number carries its coverage.** "38/50 completed tasks had estimates",
  "measured on 1 of 2 calendars", "4 of 7 days observed". A number without a
  denominator is a rumour.
- **Line 13 is the point.** If the report can't end with one concrete change, it
  is a dashboard, and we said we didn't want one.
- Monthly adds trends (12-week sparklines), project mix shift, and the lead-time
  distribution rather than its median. It should not add new metrics.
- `--md` output is the same content, not a richer version.

Detail follows, marked *available today* vs. what needs the journal or
check-in.

### 3.1 Throughput — *available today*

Completed count and **planned minutes** (the estimates attached to completed
tasks), by project / tag / `things_area`, against the previous period and a
12-week median. Median lead time (`entry` → `end`). Until check-ins or a time
tracker exist, never label estimate-minutes as "hours worked" or "work done."
Works across all 1749 completed tasks, so it is useful from day one, with an
explicit coverage line: "38/50 completed tasks had estimates."

Your actual monthly rate, for calibration: Mar 69, Apr 102, May 47, Jun 65,
Jul 33, Aug 50. That variance is itself the interesting thing to explain — and
§3.2/§3.6 are how we explain it.

**The denominator problem, which is bigger than the estimate-coverage line
suggests.** `report.next.filter` is `status:pending due.any: -WAITING`, so the
scheduler only ever sees tasks that already have a due date: 16 of your 22
pending tasks today, all 16 with an estimate. But of 1749 completed tasks, only
149 ever had a due date and 103 an estimate. **Roughly 9% of the work you
finish passes through the scheduler at all.** Every block-derived metric —
follow-through, placement churn, calibration, boundary erosion — measures that
slice, not your week.

That's not fatal, but it has to be explicit, and it needs a decision:

- **Label it honestly.** Two throughput numbers: *all completed work* and
  *scheduled work*, never summed into one. Cheap, and it should happen
  regardless.
- **Widen what we schedule.** A report that includes estimate-bearing tasks
  without a due date, placed on a soft horizon rather than a deadline. Changes
  scheduling behaviour, so it's a real decision, not a metric tweak.
- **Improve coverage at the source.** The review leads with it, and `checkin`
  offers to estimate the un-estimated. Probably higher leverage than the
  Calibration dial it feeds, since a perfect ratio over 9% of your work is
  still a rumour.

### 3.2 Meeting load — *available today, and I think this is the sleeper feature*

Sum of non-transparent, non-declined busy minutes inside your work lane, vs.
lane capacity, vs. minutes we were able to schedule for tasks:

```
Week 36  capacity 45.0h   meetings 22.5h (50%)   schedulable 22.5h
         scheduled 18.0h  completed 11.5h
```

Most weeks that feel like failures aren't. Leading the review with "half your
week was already gone" turns a guilt metric into a capacity metric — and makes
"say no to a meeting" a visible lever. It also gives every other number an
honest denominator.

Coverage must be explicit. "Available today" means events visible on the
currently configured calendar and OAuth scope, not necessarily every calendar
that can make you busy. Until multi-calendar free/busy reads exist, print the
calendar(s) measured beside the number rather than presenting partial meeting
load as total meeting load.

### 3.3 Deadline integrity — *partly today, churn needs the journal*

- **On-time rate**: `end <= due`. Computable now for the 149 completed tasks
  that have a due date.
- **Observed due-date churn**: per task, how many times `due` was observed to
  move *later* while the task stayed open, and cumulative days pushed. **Needs
  the journal** (§1.1), and is a lower bound whose accuracy depends on how often
  scheduling runs.
- **Push velocity**: pushes per week, and how much of your backlog's total
  "days until due" is just accumulated deferral.

The headline number I'd want: **"You pushed 14 due dates this week, totalling
41 days. 6 of those were the same 2 tasks."** That's the sentence you described
wanting.

Not every push is avoidance: scope changes, dependency delays, and externally
moved deadlines are legitimate. The weekly review can optionally classify the
small number of changed tasks (`scope`, `blocked`, `external`, `deferred`) and
show both total and self-deferred churn. Do not make an unclassified change a
moral failure or silently guess its reason.

### 3.4 Stagnation + triage — *needs a few weeks of journal*

A list, not a metric. Tasks matching any of:

- due date pushed ≥ 3 times
- scheduled ≥ 5 times, never completed
- open > 90 days with a due date that keeps moving
- estimate grew ≥ 2× since first seen (a task that's actually a project)

Each with one prescribed action: **do it, shrink it, hand it off, or kill it.**

`task-gcal review --triage` would walk the list interactively. Note this is the
first feature that wants to **write to Taskwarrior** (`task <id> modify
due: wait:someday`, `task <id> delete`), breaking invariant (3). If we do it:
explicit subcommand only, confirm each change, print the exact `task` command
it will run, and `--dry-run` by default. Or the safer version — just *print*
the commands and let you paste them. I'd start there.

### 3.5 Plan vs. reality — *mostly available today*

This needs the journal less than I first thought. Past managed events are never
moved or deleted — orphan cleanup only touches events with `start > now` — so
**the calendar already holds a complete history of past blocks**. The only thing
hiding it is `lookback_days = 7` bounding the list query, and `review` is a
read-only path that can simply look back further. Blocks + `end` on completed
tasks gives follow-through for past weeks with no journal at all, which means
Phase 1 delivers more than the phase table claims. The journal still adds what
the calendar can't show: how many times a block *moved* before it happened
(§1.8), and what the estimate and due date were at the time.

For each block we scheduled: was the task completed by the time the block
ended? Gives a **follow-through rate**, and its failure modes are diagnostic:

- Blocks that passed with the task still open → over-committed, or the block
  was at a bad time (which hour of day do your blocks fail most? that's a
  finding).
- Tasks completed with no block scheduled → you're working off-plan.
- Blocks rescheduled repeatedly → the estimate or the deadline is fiction.

### 3.6 Estimation calibration — *needs actuals; needs a decision from you*

We have no actual-time source (no Timewarrior, and `journal.time=0`). Options:

| Option | Cost to you | Quality |
| --- | --- | --- |
| **`task-gcal checkin`** — walks yesterday's blocks, asks done?/how long? | ~30s/day | Best; the only source that knows what actually happened |
| Timewarrior via the standard Taskwarrior hook | Install + remember `task start` | Good when you remember |
| Infer from the calendar (did the block survive untouched?) | Zero | Weak proxy, but free |
| Taskwarrior `start`/`stop` ops | Remember to start/stop | Same discipline problem |

I'd recommend the check-in, opt-in, skippable, and forgiving. Blank means
**unknown**, not "as estimated"; silently substituting the estimate would make
calibration look better without adding evidence. Fast inputs can still make it
cheap: `d` = done as estimated, `45` = done in 45m, `s` = skipped, Enter =
unknown. This small amount of typing unlocks §3.5, §3.6, and most of Theme C.

**And then close the loop:** with a few weeks of ratios, `schedule
--calibrate` multiplies each estimate by your historical factor for that
project/tag (e.g. "writing: ×1.8"). Applied at *schedule* time only — we still
never modify the task. That's the moment this tool stops being a calendar
writer and starts being useful about your time.

### 3.7 Boundary erosion — what this is costing your evenings and weekends

The per-task override UDA is the honest record of every time you bought time
from outside working hours to make something fit. Worth counting, because it's
the one cost that never shows up as a missed deadline.

What's already in your data — 30 of 1749 completed tasks carried a `gcal:`
override:

| Override | Uses | What it means |
| --- | --- | --- |
| `work_end_hour=20` / `=21` | 8 | Bought an evening |
| `work_days` including 5 / 6, or all seven | 8 | Bought a weekend day |
| `work_days=4` | 11 | Friday-only — **narrows** the window |
| `buffer_minutes=0` | 6 | Packed tighter; density, not hours |
| `work_start_hour` | 1 | Bought an early morning |
| `attendees=…` | 1 | Unrelated to boundaries |

So the first design constraint: **classify intent, don't count overrides.** A
naive count reports 30 boundary violations when the largest single group
(`work_days=4`, 11 uses) is the opposite — a deliberate constraint, narrowing
when a task may run. Three numbers, in increasing order of honesty:

1. **Widening overrides**, split from narrowing and density ones. Countable
   today from `task export` (the UDA is present on pending *and* completed
   tasks), but with no dates attached — "this week" needs the journal.
2. **Capacity bought** — extra minutes of window each override opened
   (`work_end_hour=21` on a 18:00 day opens 180). Measures *intent*.
3. **Evenings and weekends actually claimed** — minutes of blocks placed
   outside the default window, plus the count of distinct evenings and weekend
   days touched. This is the number that matters: an override that opened three
   hours and then placed a 30-minute block cost you half an hour, not an
   evening.

```
Boundaries  2 evenings (95m past 18:00) · 1 weekend day (90m Sat)
            overrides: 3 widening (2× work_end_hour=21, 1× +Sat), 1 narrowing
            paid for by: PIR (2), fraud-slides (1)
```

The last line is the actionable one. If the same project keeps taking your
Saturdays, the estimate or the commitment is wrong — not your weekend.

Two refinements once lanes exist (§1.3): erosion is specifically a *work-lane*
demand placed through a widened window. A personal project deliberately
scheduled in the `personal` lane at 20:00 is not erosion, and the report
shouldn't call it that. And `overdue_horizon_days` overrides belong in §3.3,
not here — extending a deadline's tolerance is deferral, not boundary loss.

**This one is never a badge.** There is no streak for working weekends. The
gamified form is the inverse: *Protected* — consecutive days with no
out-of-window block placed (§4.3).

### 3.8 Output

Terminal first, matching the existing report style. Then `--md PATH` to drop a
Markdown file (into `~/notes`, if that's the right place — see §7). Optionally
a Friday-afternoon launchd job that runs the review and mails/notifies it.

---

## 4. Theme C — gamification, done honestly

### 4.1 The incentive problem, stated up front

Every obvious metric is gameable, and the gaming *is* the failure mode:

- Score by **estimate-minutes completed** → you inflate estimates.
- Score by **task count** → you split tasks, and stop working on hard ones.
- Score by **on-time completion** → you set generous due dates, or push them
  *before* they're missed (exactly the behavior we're trying to surface).

So the design rule: **prefer metrics where the winning strategy is the
behavior you actually want.**

### 4.2 Four dials, not one number

| Dial | Definition | Why it resists gaming |
| --- | --- | --- |
| **Throughput** | *planned* minutes completed ÷ your 12-week median, capped | Relative to yourself; inflation shifts the baseline too |
| **Follow-through** | scheduled blocks completed as planned | To win you must plan a week you can actually do — i.e. be honest about capacity |
| **Deadline integrity** | `clamp(1 − pushes ÷ open tasks with due dates, 0, 1)` | Directly penalizes the push reflex |
| **Calibration** | closeness of actual ÷ estimate to 1.0 | Punishes over- *and* under-estimation, so it cancels the inflation incentive from Throughput |

Each 0–100, plus a week grade and a 12-week sparkline. Calibration is the
keystone — it's what makes the other dials safe to score.

Two corrections to my own table: Throughput is *planned* minutes completed, per
§3.1's rule — I broke that rule two sections after writing it. And Deadline
integrity needs the clamp: pushes can exceed the number of open tasks with due
dates (that's a normal week for the behaviour we're trying to surface), and an
unclamped dial would go negative.

**Boundaries** (§3.7) is deliberately *not* a fifth dial. It's a cost, reported
as minutes and days, never scored — a dial invites optimizing it, and the only
honest way to optimize a boundary metric is to stop measuring your evenings.

### 4.3 Streaks — the part that actually motivates

Streaks beat points, and these are the ones worth keeping:

- **Straight-shooter**: consecutive observed days with zero self-deferred
  due-date pushes.
- **Clean slate**: consecutive observed days ending with no overdue tasks.
- **Kept promises**: consecutive checked-in days where every scheduled block
  was honored.
- **Moved**: consecutive checked-in weeks hitting the full fitness quota
  (§2.2) — ties Theme A into Theme C for free.
- **Protected**: consecutive days with no out-of-window block placed (§3.7).
  The inverse of boundary erosion, and the only honest way to gamify it.

Missing observations pause a streak; they do not count as successful days.
Calendar presence alone proves that a block was planned, not that it happened.
A paused streak must show the gap rather than implying continuity across time
nobody observed — `straight-shooter 4d (2 days unobserved)`. Otherwise a
fortnight's holiday silently becomes a two-week streak, and the `snapshot` job
(§1.1) is what keeps those parentheses rare.

One line after every `schedule` run, always visible, never nagging:

```
Week 36 · followed 12/18 blocks · 3 pushes · straight-shooter 4d · runs 2/3
```

### 4.4 Badges, sparingly

Only ones that describe something real: *Estimator* (10 consecutive tasks
within 20%), *Closer* (cleared 5 tasks older than 60 days), *Clean Week* (a
full week with no observed due-date changes), *Consistent* (4 straight weeks
at fitness quota). They are recomputed from the journal instead of accumulating
hidden state. The goal is transparent rules, not cheat prevention.

### 4.5 Tone rules

- Every negative number ships with either a **cause** (§3.2 meeting load) or a
  **prescribed action** (§3.4 triage). No bare scolding.
- Never compare to an idealized version of you; compare to your own 12-week
  median.
- A bad week with an explanation is a *finding*, not a failure.
- Anything gamified must be switchable off in one line of config. Some weeks
  you don't want a scoreboard.

---

## 5. Phasing

Ordered by dependency and by how soon each pays off.

| Phase | What | Depends on | Payoff |
| --- | --- | --- | --- |
| **✓** | Bulk-removal guard (§0) | — | Shipped; a bad input can no longer clear the calendar |
| **0** | pytest + `FakeGCal` + characterization tests; extract only the seams Phase 1 needs | — | Refactoring safety without a big-bang rewrite |
| **1** | **Run journal** + `snapshot` + launchd cadence + `review --week` on data we already have (capacity, throughput, backlog flow, lead time, follow-through) | 0 | Immediate, and **starts the clock on history** |
| **2** | `Demand` abstraction + event identity migration; lanes + placement passes + configured fitness blocks (quotas, spacing) | 0 | The other half of your ask, no external deps |
| **3** | Churn + stagnation detection; `--triage` (print-only first) | 1 + ~3 weeks of journal | The "why do I keep pushing this" answer |
| **4** | `checkin` → actuals → calibration → `schedule --calibrate` | 1 | Estimates stop being fiction |
| **5** | Scorecard, streaks, one-line status, badges | 1, 3, 4 | Motivation, with the metrics honest by then |
| **6** | `Demand` sources: generic ICS importer → intervals.icu/Garmin; GitHub; reading list | 2 | Real training plans, and everything else for free |

Each phase ships as a usable vertical slice with an acceptance check:

| Phase | Done when |
| --- | --- |
| **0** | The five subtle behaviours (overdue horizon, in-progress pinning, duplicate cleanup, `scheduled`/`wait` floors, midnight-due bump) each fail a deliberately broken implementation, and `find_earliest_slot` is tested across a DST boundary |
| **1** | The same review regenerates byte-identically from the same journal; a failed or interrupted append never affects the calendar; `snapshot` makes no mutating API call; a truncated last line and an unknown future field both parse; every number prints its coverage |
| **2** | A 3×/week template places exactly 3 sessions, respects spacing, produces no duplicates across repeated runs, adopts nothing it doesn't own, survives a failed source without deleting a single event, and reports an unplaceable session instead of silently dropping it |
| **3** | Churn counts reproduce by hand from two journal records; `--triage` prints commands and touches nothing; every stagnation entry carries a prescribed action |
| **4** | A skipped check-in produces no calibration data (blank ≠ estimate); `--calibrate` prints the factor it applied and the sample size behind it; no Taskwarrior write |
| **5** | Every score recomputes from the journal alone; unobserved days pause streaks and are shown as gaps; the whole scoreboard switches off in one config line |
| **6** | A source that returns HTTP 500, an empty feed, and a duplicate UID are each distinguishable from "everything was cancelled" |

Two notes on the order:

- **Phase 1 should ship before Phase 2**, even though Phase 2 is the more fun
  feature — the journal only becomes valuable with age, and Phase 3 and 5 are
  gated on having a few weeks of it. Ship the boring append-only file first.
- Phase 6 is last on purpose: §2.2 gets you scheduled training with zero
  external dependencies, and by then we'll know whether reading a real plan
  actually adds anything.

---

## 6. Verify before building

Claims above I'm working from memory on. Cheap to check, expensive to be wrong
about:

- [ ] intervals.icu: personal API key, planned-workouts endpoint, ICS feed,
      Garmin auto-sync — all still true and free?
- [ ] Does Garmin Connect expose *any* official calendar/ICS feed?
- [ ] Is the Garmin Developer Program genuinely partner-only in 2026?
- [ ] TrainingPeaks ICS feed: exists? needs a paid tier?
- [ ] `taskchampion.sqlite3` `operations` table: readable, and is there anything
      usable pre-2026-05-13? (couldn't open the DB from the working dir here)
- [ ] Google Calendar: any signal that a block was *actually* attended? (I
      expect not — which is what makes §3.6's check-in necessary.)

Checked, and worth recording because two of them shaped decisions above:

- [x] `task export <bad filter>` exits 2 with a message, so a malformed filter
      can't look like an empty backlog. But **report names are prefix-matched**
      (`export nex` silently resolves to `next`), so a typo can quietly select a
      *different* report instead of failing.
- [x] No Taskwarrior contexts are currently defined, so that footgun is latent
      rather than live — see §7.9.
- [x] `report.next.filter` is `status:pending due.any: -WAITING`: the scheduler
      only ever sees tasks that already have a due date (§3.1's denominator
      problem).
- [x] The override UDA survives on completed tasks, so §3.7's override counts
      are computable today — 30 of 1749, with the intent split shown there.

---

## 7. Open questions for you

1. **Garmin path.** Is routing through intervals.icu acceptable, or do you want
   the unofficial-API approach despite the fragility? Or is §2.2's configured
   blocks enough that Garmin can wait?
2. **Are fitness blocks demands or commitments?** Should we *pick* your training
   times inside a lane (flexible, we optimize around meetings), or do you
   already own fixed times and want us to just protect them from tasks? This
   changes §1.4 a lot.
3. **Will you do the daily check-in?** Honest answer changes the roadmap: yes →
   Phase 4 unlocks calibration and half of Theme C. No → we build the weaker
   calendar-inference proxy and drop the Calibration dial.
4. **Calendars.** One calendar for tasks + training, or a second calendar for
   personal/fitness? (Multi-calendar support is a real change: busy-time reads
   would need to span both.)
5. **Where do reviews live?** Terminal only, or Markdown into `~/notes`
   (Obsidian?), or a calendar event, or emailed weekly?
6. **Gamification tone.** Four dials and streaks (§4.2/§4.3), or do you actually
   want XP/levels/badges? I've argued for the former — but you know what
   motivates you.
7. **Is writing to Taskwarrior acceptable** for triage (§3.4), or should we stay
   strictly read-only and just print the commands?
8. **The 9% problem (§3.1).** Only about a tenth of the work you complete ever
   passes through the scheduler, because `next` requires a due date. Do we (a)
   label the two populations separately, (b) widen what we schedule to
   estimate-bearing tasks without a due date, or (c) treat raising coverage as
   the goal itself? This decides how much any block-based metric is worth.
9. **Should we honour your active Taskwarrior context?** None are defined today,
   so this is free to decide now. Honouring it means `task context personal`
   plus a run would treat every work task as gone; ignoring it (`rc.context=none`)
   means the scheduler always sees your whole `next` list. I'd ignore it — the
   guard now makes the failure loud rather than destructive, but a scheduler
   shouldn't change what it plans based on which report you were last reading.
10. **Scope check.** This is a lot. If you had to pick two phases for the next
    month, which?
