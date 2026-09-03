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
history has to be stored. **(2) and (3) now survive permanently**: §7.7 chose
print-only triage, so Taskwarrior stays read-only for good, and §1.8's stability
rule strengthens idempotence rather than weakening it.

We should break the rest deliberately, one at a time, and keep the parts that
still hold. In particular: **(1) can be preserved in spirit** — see the
journal decision below.

There were also **no tests**; there are now 269, with the five subtle
scheduling rules pinned by characterization tests and verified by mutation
(§1.7, §6.1). That was the precondition for everything below: adding a journal,
multiple sources, multiple time profiles, and a pile of review arithmetic to a
700-line untested `cli.py` is how this project stops being fun.

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
- `review` **only ever reads** it. No derived history ever feeds back into
  placement: `--calibrate` was the one planned exception and §7.3 removed it, so
  the rule is now absolute.
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

**A lane ending at midnight now works** (`end_hour = 24` used to crash; fixed,
see §6.1). Two limits remain for overnight work, both known and accepted:
windows are built per calendar day, so **no session can cross midnight** — a
23:00-01:00 block isn't expressible even with `end_hour = 24` — and a block
spanning a DST transition gets the wrong amount of real time. Neither blocks an
evening or early-morning lane; both block a genuinely nocturnal one.

Back-compat: top-level `work_start_hour`/`work_end_hour`/`work_days` keep
working and define the `work` lane. Every demand declares a lane; Taskwarrior
demands default to `work` and can override per task (`gcal:'lane=personal'`).
A demand may name several lanes in preference order (`lane=morning,personal`).
Lane overlap is allowed, but capacity reporting must union overlapping windows
rather than double-counting them.

### 1.4 Placement passes

Fitness blocks aren't "urgent tasks" — they're commitments that tasks should
route *around*. Some of them we place; some already exist (§2.2). So placement
becomes ordered passes, each pass's output being busy time for the next:

0. **Pass 0 — classify. No writes.** Find the commitments that *already* exist
   for this period: ones we own, ones mirrored from a feed, and ones you put
   there yourself. They're already protected — busy-time subtraction has always
   done that — so the work here isn't scheduling, it's *recognition*: knowing
   that Saturday's long run counts toward "1 long run per week".
1. **Pass 1 — place the shortfall.** Only the flexible sessions pass 0 says are
   still owed, spread across the remaining days.
2. **Pass 2 — tasks.** Current behavior, now working around passes 0 and 1.
3. **Pass 3 — fillers.** Optional reading/admin items that may use leftover
   gaps but never displace the earlier passes.

**This splits the model in two, and §1.2 should say so.** A `Demand` is
something we still have to place. An **`Occurrence`** is something already on a
calendar — which we may own, mirror, or merely observe. Occurrences are inputs:
busy time, plus evidence against a quota. Only demands get placed. Conflating
them is how you get duplicate workouts.

An import source therefore needs an explicit mode: `observe` (it's already on a
calendar we read — do nothing but count it) or `mirror` (copy feed items onto a
calendar we own, because we can't otherwise see them). `observe` is nearly free
and should be the default.

Ordered passes make "protect my training time" real, but they also create a
stability problem: adding one commitment can cascade-move many task events.
Prefer existing valid placements within each pass, then place only new or
invalid items. Minimize churn before optimizing for earliest placement — but
that's a bigger decision than pass ordering, it applies to today's single-pass
scheduler already, and it gets its own section (§1.8).

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

### 1.8 Schedule stability — is the calendar a plan or a suggestion?

This is a change to the behaviour the tool has today, independent of every
theme above, and probably the highest-value small thing in this document.

**Today:** each run re-derives every placement from scratch and takes the
earliest slot that fits. So a task sitting at Thursday 14:00 moves to Tuesday
09:00 the moment a meeting is cancelled, and a newly urgent task shuffles
everything behind it. The README promises exactly this ("finds the earliest
aligned slot"), and for a scheduler that runs once it's the right answer. For
one that runs several times a day — which §1.1's `snapshot` cadence makes more
likely, not less — it means the calendar you looked at this morning is not the
calendar you'll act on this afternoon.

Three costs, in order of how much they matter:

1. **You stop trusting it.** A block that might move isn't a commitment, so you
   don't plan around it, so the tool degrades into a list with timestamps.
2. **Follow-through (§3.5) becomes unmeasurable.** You can't miss a plan you
   never had; churn and follow-through would measure the scheduler's
   restlessness, not your behaviour.
3. **Every run patches events that didn't need patching** — API calls, and
   notification noise for any block with attendees.

**Proposed:** keep an existing placement unless it is *invalid*, where invalid
means precisely — overlaps busy time, falls outside its lane's window, ends
after the effective deadline, starts before the `scheduled`/`wait` floor, or
its duration no longer matches the estimate. Re-place only invalid and new
items. This generalizes the in-progress pinning that already exists: an
in-progress block is just the extreme case of sticky.

With one nuance, because pure stickiness would leave tasks needlessly late:

```toml
settle_days = 2      # placements within 2 days are sticky unless invalid
                     # 0 restores today's always-earliest behaviour
```

Tomorrow shouldn't move. Next Thursday can be re-optimized freely, because
you haven't planned your Thursday yet.

Two consequences to accept deliberately:

- **Placement is no longer globally optimal.** A newly urgent task takes the
  earliest *free* slot rather than the best one, because the better slot
  already belongs to a promise you made yesterday. That's the right trade: the
  scheduler should not be allowed to overrule your past self for a marginal
  gain, and urgency already changes every hour via Taskwarrior's age
  coefficient — chasing it is what causes the churn.
- **`--reoptimize` exists for when you do want the reshuffle**, and the review
  can point out when it would help ("3 tasks could start a day earlier").

**Sequencing note:** this should land *before* Phase 1's journal, or the first
weeks of placement-churn history describe a policy we're about to replace —
the same definition-versioning trap as §1.1. If it lands after, `settle_days`
must be part of `settings_hash` so reviews can see the boundary.

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

### 2.2 Fitness: fixed sessions and flexible quotas

**Decided (§7.2): both.** Long runs and classes are fixed — you already own the
time. Easy runs and strength are flexible — declare intent and let the tool
find the slot. That mix is the realistic one, and it costs less than it sounds,
because the fixed half needs almost no scheduling code (see pass 0 in §1.4).

```toml
# Flexible: we choose the time.
[[fitness]]
name     = "Easy run"
duration = 45
per_week = 3            # a weekly quota, not a due date
lane     = "morning"
spacing  = "1d"         # at least a day between sessions of this block

[[fitness]]
name      = "Strength"
duration  = 30
per_week  = 2
lane      = "morning"
not_after = "Long run"  # don't stack it the morning after the long one

# Fixed: you own the time; we only recognise and protect it.
[[fitness]]
name     = "Long run"
fixed    = "sat 08:00"
duration = 90

[[fitness]]
name     = "Class"
fixed    = "tue 19:00"
duration = 60
counts_as = "Strength"  # satisfies one Strength session for the week
```

**The hard part is recognition, not placement.** A fixed session you scheduled
by hand is already protected; what we need is to know it happened, so the quota
maths and the review are right. That means matching calendar events to
templates, and matching heuristically is worse than not matching at all — a
false positive silently tells you you've trained when you haven't. So, in
order of preference:

1. ~~A dedicated fitness calendar.~~ Ruled out by §7.4 — one calendar for
   everything. It would have been the unambiguous option; noting the trade-off
   because recognition is now the fiddly part of Phase 2.
2. **Explicit per-template patterns** (`matches = ["Long run", "🏃"]`). Now the
   default mechanism. Anchored matches only — exact title, or a prefix, or an
   emoji marker — never substring similarity.
3. **`task-gcal fitness --adopt <event>`**, which stamps an existing event with
   our `source`/`key` so it's unambiguously ours from then on. The escape hatch
   when a pattern would be too loose.

Never adopt by fuzzy title similarity, and never silently. A false positive
tells you you've trained when you haven't, which is worse than the tool
noticing nothing at all.

Two knock-on rules once both kinds exist:

- **`counts_as` is explicit.** A fixed class shouldn't quietly absorb a flexible
  quota unless you say so; otherwise a busy week of classes makes the strength
  quota vanish.
- **Spacing constraints span both kinds.** `not_after = "Long run"` has to see
  the fixed Saturday session, which is exactly why pass 0 runs first.

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
| 9 | **Blocks-to-completion** — how many blocks a task needs, by project/tag | "Are my estimates fiction?" | Journal + `end` | Phase 4 (§3.6) |
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

**Coverage is a history problem, not a workflow problem.** `report.next.filter`
is `status:pending due.any: -WAITING`, so the scheduler only sees tasks that
already carry a due date — and across all 1749 completed tasks that looks
alarming (149 with a due date, 103 with an estimate). Splitting at this repo's
first commit shows why that aggregate is misleading:

| Completed | n | with `due` | with `estimate` |
| --- | --- | --- | --- |
| before 2026-06-14 | 1626 | 3.7% | 1.0% |
| since 2026-06-14 | 123 | **72.4%** | **70.7%** |

The old numbers describe a way of using Taskwarrior that has since been
replaced: the workflow standardized on due + estimate when this repo started,
and coverage should keep climbing toward 100%. So:

- **Forward-looking metrics are fine.** Don't design around a limitation that's
  already been fixed at the source. Every block-derived metric —
  follow-through, placement churn, calibration, boundary erosion — applies to
  essentially all new work.
- **Retrospective baselines are not.** A rolling 12-week median is safe; "versus
  last year" is not. Reviews must refuse to compare across 2026-06-14 rather
  than silently averaging two different workflows — the same boundary rule as
  §1.1's definition versions, and the first concrete instance of it.
- **Keep printing coverage per period anyway** (§3.0's rule). Not because the
  workflow is in doubt, but because a drop is how we'd notice it slipping.
- The remaining ~28% is worth a nudge in the review, not a redesign.

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

**§7.4 chose a single calendar for everything**, which makes this number
complete rather than partial — no multi-calendar free/busy reads needed, and the
denominator is trustworthy as long as every meeting really does land there.
Still print the calendar measured beside the number: it costs one line and it's
the tripwire for the day a second calendar quietly appears.

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

**Decided (§7.7): `review --triage` prints commands and changes nothing.** It
lists each stagnant task with the exact `task ...` invocations that would
resolve it, and you paste the ones you agree with:

```
Stagnant (3):
  #40 Analyze peakon — due pushed 4x, +31d, 2 blocks passed
      task 40 modify wait:someday   # not now
      task 40 modify estimate:30    # shrink to a first step
      task 40 delete                # be honest
```

So invariant (3) holds permanently: this tool never writes to Taskwarrior. That
also keeps triage safe to run casually — there's no confirmation to misclick,
and no `--dry-run` to forget.

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
ended? With no check-in (§7.3) this is the load-bearing measurement, and it
holds up — completion is a Taskwarrior fact. Gives a **follow-through rate**, and its failure modes are diagnostic:

- Blocks that passed with the task still open → over-committed, or the block
  was at a bad time (which hour of day do your blocks fail most? that's a
  finding).
- Tasks completed with no block scheduled → you're working off-plan.
- Blocks rescheduled repeatedly → the estimate or the deadline is fiction.

### 3.6 What we can infer without actuals — *decided: no check-in*

**Decided (§7.3): no daily check-in.** So there is no source of actual time
spent — no Timewarrior, `journal.time = 0`, and Google Calendar has no "did you
attend" signal. Two consequences, stated plainly because one is a real loss:

- **`schedule --calibrate` is off the table.** Multiplying each estimate by a
  historical actual÷estimate factor needs actuals, and there is no factor to
  compute. If your estimates are systematically wrong *in minutes*, this tool
  will not find out.
- **The Calibration dial goes with it** (§4.2) — and it was the keystone that
  made the other dials safe to score. §4.2 has the replacement.

Everything keyed on *completion* survives, because Taskwarrior timestamps that
exactly:

| Signal | Derived from | Strength |
| --- | --- | --- |
| Was the task completed by the time its block ended? | block time + `end` | Strong — a fact, not an inference |
| How many blocks passed before it was completed? | journal + `end` | Strong: a task that needed 3 blocks was under-estimated ~3× |
| Was the estimate revised upward while the task was open? | journal | Strong signal of a bad first estimate |
| Did the block survive untouched to its end? | our own event history | Weak proxy for "you did the thing" |

**The substitute for calibration is blocks-to-completion.** We can't say "you
take 1.8× longer than you think", but we *can* say "writing tasks take 2.4
blocks on average, so your 60m estimate is really about 150m." Same actionable
finding, derived from completions instead of stopwatch data, and it costs you
nothing. It's coarser — quantised to whole blocks, and it can't see a task you
finished early — but it's honest, and it needs no discipline to keep working.

If you ever change your mind, the check-in is additive: it would sharpen these
numbers rather than replace them.

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

**Decided (§7.5): terminal only.** Same style as the existing schedule report,
no files, no notifications, nothing to clean up. `--md PATH` and a Friday
launchd job are deferred, not designed away — the review builds its content as
data and renders at the end, so adding an output format later is a renderer,
not a rewrite.

One consequence worth accepting deliberately: a terminal-only review leaves no
trail. Trends still work, because they're recomputed from the journal rather
than from past reports — but you won't be able to reread what you concluded in
week 34. If that starts to matter, `--md` is the answer.

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
| **Estimate stability** | upward drift in your median estimate against the 12-week baseline, inverted | Inflating estimates to farm Throughput shows up here the same week |

Each 0–100, plus a week grade and a 12-week sparkline.

**Calibration was going to be the fourth dial and the keystone** — the thing
that made scoring Throughput safe, because over- and under-estimating both cost
you points. §7.3 chose no check-in, so there are no actuals and no Calibration
(§3.6). Estimate stability is the replacement, and it's worth being honest
about the downgrade:

- It detects *drift*, not *error*. Estimates that have been wrong by the same
  factor for a year look perfectly stable.
- So **Throughput is now the weakest of the four**. Inflating estimates buys a
  real bump for a few weeks until your own rolling median absorbs it. The
  exposure is bounded, not eliminated.
- Blocks-to-completion (§3.6) is the honest accuracy *finding*, but it must not
  become a dial: scoring it would reward inflating estimates, since padded
  blocks finish within their first block more often. Report it, never score it.

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
- **Kept promises**: consecutive days where every scheduled block's task was
  actually completed.
- **Moved**: consecutive weeks hitting the full fitness quota (§2.2) — ties
  Theme A into Theme C for free.
- **Protected**: consecutive days with no out-of-window block placed (§3.7).
  The inverse of boundary erosion, and the only honest way to gamify it.

All five rest on completion, not attendance: with no check-in (§7.3) we can
prove a task was *closed*, never that you spent the time. A streak is therefore
about finishing what you planned, which is the behaviour worth rewarding anyway.

Missing observations pause a streak; they do not count as successful days. A day
with no run and no `snapshot` is unobserved, and calendar presence alone proves
a block was planned, not that anything happened in it.
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
completed inside their first block), *Closer* (cleared 5 tasks older than 60 days), *Clean Week* (a
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
| **✓** | Test battery: 269 tests, 28 of 31 mutants caught (§1.7, §6.1) | — | Shipped; the subtle rules are now pinned |
| **0** | Extract only the seams Phase 1 needs | ✓ | Refactoring safety without a big-bang rewrite |
| **0.5** | **Schedule stability** (§1.8): keep valid placements inside `settle_days` | 0 | The calendar becomes a plan; makes follow-through measurable |
| **1** | **Run journal** + `snapshot` + launchd cadence + `review --week` on data we already have (capacity, throughput, backlog flow, lead time, follow-through) | 0 | Immediate, and **starts the clock on history** |
| **2** | `Demand` abstraction + event identity migration; lanes + placement passes + configured fitness blocks (quotas, spacing) | 0 | The other half of your ask, no external deps |
| **3** | Churn + stagnation detection; `--triage` (print-only first) | 1 + ~3 weeks of journal | The "why do I keep pushing this" answer |
| **4** | Inference from completions: blocks-to-completion, estimate drift, upward-revision detection | 1 | Estimates get a reality check without a stopwatch |
| **5** | Scorecard (four dials), streaks, one-line status, badges | 1, 3, 4 | Motivation, with the metrics honest by then |
| **6** | `Demand` sources: generic ICS importer → intervals.icu/Garmin; GitHub; reading list | 2 | Real training plans, and everything else for free |

Each phase ships as a usable vertical slice with an acceptance check:

| Phase | Done when |
| --- | --- |
| **0** | ✓ **Met.** The five subtle behaviours each fail a deliberately broken implementation, and `find_earliest_slot` is tested across both DST boundaries. Verified by mutation, not coverage: 31 deliberate breaks, 28 caught, the 3 survivors provably equivalent. Run mutations with `PYTHONDONTWRITEBYTECODE=1` — a same-length mutant can otherwise be scored against stale bytecode |
| **0.5** | Two consecutive runs with a cancelled meeting between them move nothing inside `settle_days`; an invalid placement still moves; `settle_days = 0` reproduces today's behaviour exactly |
| **1** | The same review regenerates byte-identically from the same journal; a failed or interrupted append never affects the calendar; `snapshot` makes no mutating API call; a truncated last line and an unknown future field both parse; every number prints its coverage |
| **2** | A 3×/week template places exactly 3 sessions, respects spacing, produces no duplicates across repeated runs, adopts nothing it doesn't own, survives a failed source without deleting a single event, and reports an unplaceable session instead of silently dropping it |
| **3** | Churn counts reproduce by hand from two journal records; `--triage` prints commands and touches nothing; every stagnation entry carries a prescribed action |
| **4** | Every accuracy number reproduces by hand from the journal plus `end` timestamps, prints its sample size, and is labelled a completion-derived estimate rather than measured time; no Taskwarrior write |
| **5** | Every score recomputes from the journal alone; unobserved days pause streaks and are shown as gaps; blocks-to-completion is reported but never scored; the whole scoreboard switches off in one config line |
| **6** | A source that returns HTTP 500, an empty feed, and a duplicate UID are each distinguishable from "everything was cancelled" |

**Decided, but with no phase of their own:**

- **`rc.context=none`** in `load_next_tasks` (§7.9). One line plus a test; it
  removes a latent way to make the removal guard fire.
- **`--md` and a Friday launchd job** are explicitly deferred, not rejected
  (§3.8, §7.5).

Three notes on the order:

- **Phase 0.5 is small and comes early** because it changes behaviour the
  journal is about to start recording (§1.8), and because it's the difference
  between a calendar you plan around and one you re-read.
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

### 6.1 Findings from the Phase 0 test pass

Three real defects surfaced while pinning current behavior. None was reachable
with the default 9-18 working window; two are fixed and one is a deliberate
non-goal.

1. **Slot arithmetic is wall-clock, not elapsed time. — accepted, not fixing.**
   `slot_start + timedelta(minutes=estimate)` on a zone-aware datetime advances
   the wall clock, so a block spanning a DST transition doesn't contain
   `estimate` minutes of real time: 6h becomes 5 real hours over spring-forward
   and 7 over fall-back. Strictly wrong, since an estimate is minutes of *work*
   — but it needs timezone surgery to fix and can only bite a block crossing
   01:00-02:00 twice a year. Pinned by
   `test_slot_length_is_wall_clock_not_elapsed_time` so the behavior is
   deliberate rather than accidental, with the correct semantics left as a
   strict xfail. Revisit only if a nocturnal lane ever ships.
2. **`work_end_hour = 24` raised `ValueError`. — fixed.** It had two crash
   sites, not one: `time(24, 0)` in `_work_windows` and `replace(hour=24)` in
   `_effective_due`. Both now offset from midnight. Hours are also validated
   where they enter, so an out-of-range value is a usage error, a message
   naming `config.toml`, or a warn-and-fall-back per task — never a traceback.
3. **A fractional estimate could round to a zero-minute block. — fixed.**
   `_coerce_estimate` rejected values `<= 0` before rounding, so `estimate:0.4`
   survived as `0`. It now rounds first; anything under a minute counts as
   missing, which lands the task in the no-estimate list instead of on the
   calendar as a zero-length event.

Three rough edges left alone, recorded so they aren't rediscovered:

- **`slot_align_minutes = 0` raises `ZeroDivisionError`** (`minute % 0`). Same
  family as #2 — an unvalidated numeric setting reaching arithmetic.
- **`work_end_hour <= work_start_hour` silently schedules nothing.** Every
  window is empty, so every task reports "could not fit" with no hint that the
  configuration is impossible. Wants the same validation pass.
- **Windows can't span midnight** (see §1.3), because they're built per calendar
  day. Relevant to lanes and to any late-evening fitness session.

Also worth knowing, as a property rather than a defect: a scheduler-tagged
event with **no** `taskUuid` is collected as an orphan when it's in the future.
That's the right call — it can only come from a bug or a hand-edited copy — but
it was a guess until the test made it explicit, and §1.2's `source`/`key`
migration must not accidentally make every pre-migration event look like one.

---

## 7. Open questions for you

1. **Garmin path.** Is routing through intervals.icu acceptable, or do you want
   the unofficial-API approach despite the fragility? Or is §2.2's configured
   blocks enough that Garmin can wait?
2. ~~**Are fitness blocks demands or commitments?**~~ **Answered: both.** Long
   runs and classes are fixed; easy runs and strength are flexible quotas. This
   split the model into `Demand` vs `Occurrence` and added pass 0 (§1.4, §2.2).
   Follow-up worth deciding before Phase 2: **do you want a dedicated fitness
   calendar?** It's the only unambiguous way to recognise sessions you schedule
   by hand, and it makes §7.4 partly moot.
3. ~~**Will you do the daily check-in?**~~ **Answered: no.** So no actuals, no
   `--calibrate`, and no Calibration dial. Replaced by blocks-to-completion and
   estimate drift, both derived from completions (§3.6, §4.2). The cost is that
   systematic estimate error in *minutes* stays invisible.
4. ~~**Calendars.**~~ **Answered: one calendar for everything.** No
   multi-calendar busy reads needed, and meeting load is complete rather than
   partial (§3.2). The cost lands on §2.2: recognising hand-scheduled training
   now needs explicit patterns or `--adopt`.
5. ~~**Where do reviews live?**~~ **Answered: terminal only.** `--md` and a
   Friday launchd job are deferred; the review renders at the end so a second
   output format stays a renderer rather than a rewrite (§3.8).
6. ~~**Gamification tone.**~~ **Answered: four dials and streaks.** Note the
   interaction with question 3: dropping the check-in removed Calibration, which
   was the dial that made Throughput safe to score (§4.2).
7. ~~**Is writing to Taskwarrior acceptable?**~~ **Answered: no — print the
   commands.** Read-only becomes a permanent invariant rather than a provisional
   one (§0, §3.4).
8. ~~**The 9% problem.**~~ **Answered:** the low aggregate is historical. Since
   2026-06-14 coverage is 72% due / 71% estimate and rising, because the
   workflow standardized on both when this repo started (§3.1). The only
   consequence left is that reviews must not compare across that boundary.
9. ~~**Should we honour your active Taskwarrior context?**~~ **Answered: no.**
   `load_next_tasks` should pass `rc.context=none` alongside the
   `rc.verbose`/`rc.confirmation` overrides it already sets, so what you were
   last reading in the terminal can't change what gets planned. A one-line
   change, listed below as pending.
10. **Scope check.** This is a lot. If you had to pick two phases for the next
    month, which?
