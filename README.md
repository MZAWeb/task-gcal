# task-gcal

Schedules your Taskwarrior `next` tasks into free slots on your Google
Calendar, sorted by urgency.

## What it does

On every manual run:

1. Pulls tasks from `task export next` (so it honors your own `next`
   report definition).
2. Finds existing scheduler-owned events on your calendar (events
   tagged via `extendedProperties.private.scheduler=task-gcal`).
3. Reconciles:
   - Future events for tasks no longer in `next` (done, deleted, etc.)
     are removed. Past events are kept as history.
   - Blocks starting within `settle_days` (default 2) stay where they
     are, unless they've become invalid (see below). Everything else is
     placed at the earliest aligned working-hours slot of length
     `estimate` minutes that ends on or before the task's due date and
     does not overlap any timed event on the calendar. A task's
     `scheduled` and/or `wait` date (whichever is later) is honored as
     an inclusive earliest-start, so the task is only ever placed
     within `[scheduled, due]`.
   - All-day events, "Free"-transparency events, and meetings you
     declined are ignored when computing busy time (matches Google's
     own free/busy semantics).
   - Existing events for the task are patched in place (preserves event
     id, attendees, reminders), or created if missing. Past events are
     never moved; if the only existing event for a task is in the past
     and the task is still pending, a fresh event is created at the new
     time.
   - Overdue tasks are scheduled ASAP (the deadline is treated as
     "as soon as possible" rather than "before yesterday").
4. Prints a report:
   - What was scheduled / updated / left alone (overdue items flagged).
   - Tasks with no `estimate` UDA.
   - Tasks with no due date (skipped).
   - Tasks that don't fit before their due date.
   - Future events removed (orphan / duplicate / stale).

The tool is idempotent: re-running with no input changes results in no
calendar mutations.

### Schedule stability

A block you've planned your day around shouldn't move because a meeting
got cancelled. So a placement starting within `settle_days` (default 2)
is a commitment: it's kept unless it has become **invalid**, which means
exactly one of

- its `estimate` changed, so the block is the wrong length,
- a meeting now overlaps it,
- it falls outside working hours (you narrowed the window),
- it ends after its due date (the deadline moved earlier), or
- it starts before its `scheduled`/`wait` date.

Only invalid and new placements are re-derived, and settled blocks are
reserved *first*, so a newly urgent task takes the earliest slot that is
still free rather than one that was already promised. Placement is
therefore no longer globally optimal — that's the trade: urgency shifts
every hour via Taskwarrior's age coefficient, and chasing it is what
made the calendar untrustworthy. When a block does move, the report says
why (`(moved: overlaps a calendar event)`).

Beyond the window nothing is sticky — you haven't planned next Thursday
yet, so re-optimizing it is free. `task-gcal --reoptimize` (or
`settle_days = 0`) gives up every near-term placement and takes the
earliest fit, as the tool used to. An in-progress block is always
sticky, whatever `settle_days` says.

### The bulk-removal guard

Every removal above is driven by what Taskwarrior reports, so one bad input
can turn a routine run into a mass deletion: a mistyped `--estimate-uda`
makes every task look estimate-less, and an empty `task export` (wrong report
name, an active Taskwarrior context, `TASKDATA` pointing at another replica —
say from a cron job with a different environment) looks exactly like "you
finished everything".

So a run refuses to remove more than `removal_guard_ratio` (default half) of
the unfinished events it owns, and refuses outright to clear anything when the
task list came back empty. It reports what it held back and exits non-zero,
having still created and updated everything else. `--force` overrides it;
`removal_guard_ratio = 1.0` disables it.

## Reviews

```console
task-gcal review --week
task-gcal review --month
task-gcal review --week --last 1          # the week before this one
task-gcal review --week --all             # every section, in full
task-gcal review --week --section time
task-gcal review --triage                 # what needs a decision
task-gcal review --month --format markdown -o notes/2026-09.md
task-gcal review --week --format json
task-gcal review --month --format html --open
```

A review is a **document, not an application**: read-only,
non-interactive, and one screen by default.

```text
Week 37 · 4 of 7 days seen

Time           42h working, 10h in meetings, 32h left
Finished       5 tasks (3 by the same point last week), 5h of estimates
Backlog        18 new, 14 done, 3 dropped — the list is 1 longer
Blocks         18 blocks came and went. 12 ended with the task done, 6 are
               still open. 3 tasks finished with no block at all.
Dates          2 tasks moved a due date, 3 times between them, the longest
               by 6 days. Of the 5 finished with a date, 4 landed on it.
Reasons        capacity 4 · avoided 3 · estimate 1 · 2 unclassified
After hours    1h35 on 2 evenings, 1h30 on 1 weekend day

Coverage
  Anything the journal didn't see is missing, not zero — see the day count
  beside the title.
  Measured on calendar primary.

Also: task-gcal review --section lead_time · rescheduling · sittings ·
  growth · stuck · trends

Look at: "Prepare PIR" was deferred for the 3rd time — decide whether it is
real. This is the 2nd week running that I've closed with this.
```

Five rules shape everything in it:

1. **A number is written as a sentence.** `2 of 17 blocks honoured` is a
   true statement and a terrible sentence — and "honoured" was the metric
   doing the moralising, not the number. Nothing here is scored, graded or
   rewarded; the moment a number can shame you it stops being told the
   truth.
2. **Time comes first**, because meeting load is what every completion
   number under it should be read against. A week with two thirds of its
   hours in meetings is a different thing from an unexplained miss — and
   the sections are only ever placed next to each other, never joined by a
   claim about cause.
3. **Every number carries its coverage.** `38/50 completed tasks had
   estimates`, `4 of 7 days seen`, which calendar was measured. A number
   without a denominator is a rumour, and missing data never quietly
   becomes zero. "Not measured" is reserved for data that should have been
   there, so it still means something on the week it matters.
4. **Planned time is not time worked.** Estimate-minutes are what you
   thought it would take. Nothing here claims to know how long anything
   actually took.
5. **It ends with one thing to look at** — and says when that's the same
   thing it said last week. The closing line is the only part with any
   authority, and a line that repeats itself in the same tone every Friday
   trains you to skip it.

`--all` prints every section in full — reach for that first. `--section
NAME` (repeatable) narrows it to the ones you want once you know which
those are. The default stays a one-screen summary of the headline
sections, because a report that prints thirteen metrics every time is a
dashboard:

| Section | Answers |
| --- | --- |
| `time` | What the week had room for, once meetings are out |
| `finished` | What you finished, and by project |
| `backlog` | Is work arriving faster than you finish it? |
| `lead_time` | How long something waits before you do it |
| `blocks` | Time you set aside: did the task get done in it? |
| `dates` | Due dates you moved, and whether the work landed |
| `rescheduling` | How often blocks moved, and who moved them |
| `reasons` | What you said got in the way (needs `checkin`) |
| `sittings` | How many sittings a task really took |
| `growth` | Tasks that quietly turned into bigger tasks |
| `after_hours` | Evenings and weekends actually claimed |
| `stuck` | What needs a decision |
| `trends` | The same three numbers over 12 weeks (in a monthly summary) |

The names are the plain ones, on purpose. Internally they keep precise
names (`capacity`, `follow_through`, `stagnation`) — but "stagnation" is a
word about a person, and "stuck" is a word about a task.

### `task-gcal review --triage`

Lists the stuck tasks with the exact commands that would resolve each,
and **runs none of them**:

```text
Stuck (3):
  #32 Follow up on post-offsite tasks — 5 blocks passed
      task 32 modify estimate:20        # shrink it to a first step you'd actually start
      task 32 delete                    # be honest

Nothing above has been run. Paste the ones you agree with.
```

Taskwarrior stays the source of truth: this tool never writes to it.
That's also what makes triage safe to run casually — there's no
confirmation to misclick and no `--dry-run` to forget.

A task earns a place on that list by accumulating evidence — blocks that
passed, deadlines that kept moving, an estimate that doubled — not by
being old. Plenty of old tasks are fine where they are.

### `task-gcal checkin`

The **optional** retrospective. It walks scheduled blocks that have ended
and haven't been explained yet, and asks one question:

```text
Tue 08 Sep 09:00  Write proposal (estimated 60m)
  Tue 08 Sep 10:00  block ended with the task still open (60m planned)
  Wed 09 Sep 18:00  due date moved later, after the old date had passed (+2d)
  Suggests: likely unfinished, then deferred
    1. Made a start, but it needs more time than I set aside
    2. Didn't feel like starting it, so I put it off
    3. Was busy with something else — needs rescheduling
    4. Blocked on someone or something else, so it has to wait
    5. Did this session's work; there's a follow-up still to come
  Which of these? 1
  How many minutes did you spend on it (60m set aside)? 20
  Anything worth remembering?
```

The stored record still has two dimensions — *what happened* and *why* —
because the metrics need them apart. The prompt doesn't: of the thirty
combinations those axes allow, about five actually happen, so it offers
those five and maps each to a pair.

"Done" and "cancelled" aren't offered on purpose. A task you genuinely
finished gets `task done`; one you gave up on gets `task delete`. Neither
is something you'd come here to say. Nor is "unknown" — pressing Enter
skips, which leaves the episode open to answer later, and that's the
honest version of not knowing.

Option 5 is deliberately *not* counted as friction: work that always
needed another sitting isn't a problem, and counting it would make good
planning look like one.

Run it daily, every few days, or not at all — reflections stay open until
answered, so running it alongside the weekly review works just as well as
a morning routine. Nothing here assumes a daily cadence, and reviews are
still measured without it.

What it deliberately won't do:

- **Never records an outcome without confirmation.** It follows an
  evidence ladder: *fact* — a block ended with the task still open;
  *fact* — a deadline moved after its old date had passed; *suggestion* —
  likely unfinished or deferred; *confirmation* — only you can say. A
  due-date push on its own means a deadline moved, not that no work
  happened.
- **Never guesses a reason.** `unknown` is offered, kept, and reported as
  missing classification rather than folded into a plausible bucket.
- **Never turns unknown time into the estimate.** Actual minutes are
  optional, and only compared against estimates for work you finished —
  20 minutes spent on something unfinished says nothing about whether the
  estimate was right.
- **Asks about a miss episode, not a block.** Four passed blocks and two
  deadline pushes on the same task are one prompt, not six.
- **Never scores an answer** — least of all `avoided`. A metric that
  costs you something for admitting avoidance trains you to lie to it.

Everything is skippable, answers can be corrected by answering again, and
it writes only its own file — never Taskwarrior.

### `task-gcal doctor`

Read-only sanity check on everything the tool depends on, where each
failure names its fix rather than just the symptom:

```text
[ ok ] config        no config.toml; using built-in defaults
[ ok ] timezone      Europe/Amsterdam (from system)
[ ok ] taskwarrior   `task export next` returned 13 task(s), 13 with a `estimate`
[ ok ] google auth   token present, mode 600
[warn] task history  0 change(s) held
                     → nothing harvested yet — the next `schedule` or `review`
                       imports everything Taskwarrior remembers (23836 operations)
[ ok ] run journal   no scheduling runs recorded yet
[ ok ] check-ins     none recorded (optional — reviews work without them)
```

A monthly review adds a 12-week trend. It adds no new *metric* — the same
numbers over time — and it refuses to average across a boundary where the
settings or a metric's definition changed, because the same field then
meant two different things:

```text
Trend          12w · done 27 → 2 · median 6.5
  Completed                 ▇▂▄▂▃▃▁▂█▂▂▁  27 → 2
  Follow-through            █▁▁▁▃▂▁▁▃▃▁▁  67% → 0%
  Observed deadline pushes  ▁▁▁▁▁▁▁▁▁▁▁▁  0 → 0
```

`--format` renders the same internal model four ways: `terminal`
(default), `markdown` (durable weekly notes), `json` (the escape hatch —
charts, notebooks, or anything else, without any of that becoming the
scheduler's problem), and `html`. The HTML report is a single
self-contained file with inline SVG charts, light and dark, no
JavaScript, and no network access at all — your task titles never leave
the machine.

Both `markdown` and `html` close with a plain-language definition of every
section they showed, naming what each one counts. They're the formats you
keep or hand to someone else, and a metric nobody can define is a metric
nobody acts on.

Bare `task-gcal` is unaffected by any of this: none of the review code is
even imported unless you run `review`.

## Where history comes from

Reviews need to know what your tasks *used to* look like — when a due date
moved, when an estimate grew. None of that is in a task's current state.

**Taskwarrior already records all of it.** `taskchampion.sqlite3` has an
`operations` table with every field change, timestamped. That's the same
history `task <uuid> info` renders, except structured and one query instead
of one subprocess per task: 38ms against 26s across 1,800 tasks.

So there is **no cron and no separate import step**. Any command that
already talks to Taskwarrior copies unseen operations into

```
~/.local/share/task-gcal/changes.jsonl
```

Field changes happen when *you* edit a task, so the next run picks them up
exactly, however long since the last one. The first run imports everything
Taskwarrior still remembers.

It's copied rather than read live for one reason: the operations table is a
*synchronisation* log, and `purge.on-sync` can prune it. Reading it directly
would mean last month's review could answer differently after a sync, and a
report you can't reproduce is worth very little.

Because it's Taskwarrior's private storage, reads are careful: `mode=ro`, one
short transaction, a small busy timeout, gated on a schema version we've
actually tested. A busy or unrecognised database is a missed harvest, never
an error — and never something that can slow down or fail a `task` command.
`task-gcal doctor` reports what it holds, how far back it's exact, and
whether anything was pruned or unreadable:

```text
[ ok ] task history  10898 change(s) held, exact since 2026-05-13
```

The one thing Taskwarrior cannot know is where your calendar blocks were, so
that stays ours. Every real scheduling run appends one record to

```
~/.local/share/task-gcal/runs/YYYY-MM.jsonl
```

One entry per block, keyed on the Google event id — no task titles, because
those live in the change history above. A block deleted and recreated
elsewhere is a *new* block, which is the difference between "it moved" and
"it was replaced".

The scheduler is not the only thing that can move a block, though: you can
drag one in your calendar. So every event we write carries a stamp of where we
left it and what we called it, and each run compares the two. A block that
isn't where we put it gets reported once, then adopted — re-stamped where it
now sits and marked as *your* position, which outranks `settle_days`:

```text
Changed outside task-gcal (1):
  (noticed once — not reported again)
  ~ Prepare PIR (moved from Tue 08 Sep 09:00 to Tue 08 Sep 14:00 — kept)
```

Drag it and it stays dragged. It only yields if the position stops working — a
meeting lands on it, or its deadline moves in front of it — and then the run
says which. Renames are the exception: reported, but the task's title still
wins, exactly as the event description warns.

Blocks already in the past are left alone in both directions: not reported, not
moved, and a still-unfinished task simply gets a fresh slot.

Because the answer travels on the event, none of this needs a timer, a cron, or
even this machine's journal — and `--section churn` can then say how often
blocks moved and what moved them, your own edits counted separately.

Three rules keep both stores honest:

- **Scheduling only ever appends.** It never reads history to decide where a
  block goes, so a corrupt or deleted store cannot produce a wrong calendar.
  Deleting either file costs you history and nothing else.
- **Nothing derived is stored.** No totals, no scores, no streaks — reviews
  recompute from raw changes, so altering a metric's definition can't leave
  numbers behind that meant something else. Records carry a schema version, a
  metrics-definition version, and a hash of the settings that affect meaning,
  so a review can refuse to draw a trend across a boundary rather than
  averaging two different things.
- **A dry run records nothing.** Its placements were never made.

Everything is mode 0600 in a 0700 directory and nothing leaves your machine,
but task titles are sensitive, so there's a dial. It covers everything we
record, titles included — which today means the change history, since the run
journal holds no titles at all:

```toml
journal_detail = "full"      # default
journal_detail = "minimal"   # store a digest of each title instead of the title
journal_detail = "off"       # record nothing at all
```

`minimal` keeps every date, estimate and status as it is — there's nothing
private about a deadline moving, and hashing it would make the number
useless. Only free text is digested, which is still enough to see *that* a
task was renamed. Changing the dial doesn't rewrite what's already stored.

Your check-in answers live separately again, in `reflections.jsonl` —
machine observations and your own words have different lifecycles, and
answers get corrected while observations never do.

**Not** stored in Taskwarrior's own database, and not as UDAs. Your task
directory contains a `taskchampion.sqlite3.bak-before-recovery-…` file: that
recovery rebuilt the database and is what truncated your history. Anything of
ours living there would have gone with it. It also has its own `version`
table, so it migrates; sync wouldn't replicate our tables; and it would turn
the worst case from "we lose our own history" into "your task data is
damaged".

## Requirements

- [uv](https://docs.astral.sh/uv/) (it manages the Python toolchain and
  dependencies for you)
- Python 3.10+ (uv will fetch one if you don't have it)
- [Taskwarrior](https://taskwarrior.org/) 3.x on your `PATH` (tested
  against 3.4)
- A Google account and a Google Cloud OAuth client (type: Desktop) —
  see Setup

## Setup

1. Add a numeric UDA to Taskwarrior to hold each task's duration (in
   minutes), in your Taskwarrior config (`~/.taskrc`, or
   `~/.config/task/taskrc` on an XDG setup):

   ```
   uda.estimate.type=numeric
   uda.estimate.label=Est(min)
   ```

   The UDA is named `estimate` by default. If you already use a
   different name (e.g. `est`), keep yours and point the tool at it via
   `estimate_uda` in `config.toml` or the `--estimate-uda` flag — no
   need to rename your existing UDA.

2. Create a Google Cloud OAuth client (type: Desktop), download the
   JSON, and save it as `~/.config/task-gcal/credentials.json`.

3. Install. As a standalone CLI tool (recommended) with
   [uv](https://docs.astral.sh/uv/):

   ```
   uv tool install .          # from a local checkout
   # or, once published:
   # uv tool install git+https://github.com/MZAWeb/task-gcal
   ```

   This puts `task-gcal` on your `PATH`. To hack on the code instead, use
   a synced project environment and prefix commands with `uv run`:

   ```
   uv sync                    # create .venv with deps + the project
   uv run task-gcal --help
   ```

4. Authorize:

   ```
   task-gcal --setup
   ```

5. Run:

   ```
   task-gcal              # do it
   task-gcal --dry-run    # preview
   ```

## Configuration

All defaults can be overridden in `~/.config/task-gcal/config.toml`
(or `$TASK_GCAL_CONFIG_DIR/config.toml`). Every key is optional:

```toml
work_start_hour     = 9            # inclusive, 0-23
work_end_hour       = 18           # exclusive, 0-24 (24 = midnight)
work_days           = [0, 1, 2, 3, 4]   # Mon..Fri (Mon=0)
slot_align_minutes  = 15
buffer_minutes      = 0            # free gap kept around every event
estimate_uda        = "estimate"   # name of your duration UDA
calendar_id         = "primary"    # or a specific calendar id
event_color_id      = "9"          # 9 = Blueberry
event_visibility    = "private"    # default | public | private | confidential
report              = "next"
timezone            = "Europe/London"   # omit to use the system local zone
overdue_horizon_days = 30          # how far ahead overdue tasks may land
lookback_days       = 7            # how far back to scan for our own events
settle_days         = 2            # blocks this close stay put unless invalid
removal_guard_ratio = 0.5          # max share of our events one run may remove
journal_detail      = "full"       # full | minimal | off
```

Every one of these keys can also be overridden per-run with a matching
CLI flag, which takes precedence over the config file (run `task-gcal
schedule --help` for the full list). For example:

```bash
# A one-off run on a work calendar, in London time, with a 15-minute
# breather between tasks and events colored Tomato.
task-gcal --calendar-id work@group.calendar.google.com \
          --timezone Europe/London \
          --buffer-minutes 15 \
          --event-color 11
```

Pointing `calendar_id` at a dedicated calendar (e.g. one called
"task-gcal") is the safest setup -- the tool only ever touches events
on the configured calendar, but using a non-primary calendar means a
bug here can't ever delete a meeting.

### Per-task overrides

Individual tasks can override scheduling settings via a string UDA
(named `gcal` by default; configurable with `override_uda` /
`--override-uda`). Declare it once in your Taskwarrior config:

```
uda.gcal.type=string
uda.gcal.label=gcal
```

Then attach `key=value` pairs (whitespace-separated; same key names as
the config file) to any task:

```bash
# Fine to run this long task until 8pm.
task add "write the big report" estimate:240 due:fri gcal:'work_end_hour=20'

# This one doesn't need the usual buffer around it.
task add "quick prep" estimate:10 gcal:'buffer_minutes=0'

# Only schedule on Mon/Wed/Fri (commas separate list values).
task add "weekly sync notes" estimate:30 gcal:'work_days=0,2,4'

# Invite people to the event, to share the task (commas separate addresses).
task add "draft Q3 deck" estimate:90 due:fri gcal:'attendees=alice@co.com,bob@co.com'
```

Precedence is **defaults → `config.toml` → CLI flags → per-task UDA**.
Overridable keys: `work_start_hour`, `work_end_hour`, `work_days`,
`slot_align_minutes`, `buffer_minutes`, `event_color_id`,
`overdue_horizon_days`, `attendees`. Run-global keys (`calendar_id`,
`timezone`, `report`, etc.) can't be set per task; a typo or unknown key
prints a warning (naming the task) and the task falls back to global
settings.

`attendees` is **per-task only** (it has no `config.toml` key or CLI flag --
you rarely want to invite the same people to *every* task). The addresses are
invited and **emailed** an invitation. It's **additive**: new addresses are
added on each run, but removing an address from the UDA does not un-invite
anyone, and attendees you add by hand in Google Calendar are preserved. Invited
people can see the event even though it's created with `visibility=private`.

## Auth and security notes

- OAuth scope: `calendar.events.owned` (the narrowest scope that lets
  us write events on calendars the user owns).
- The relationship between a task and a calendar event is stored on the
  event side, in `extendedProperties.private` (`scheduler=task-gcal`,
  `taskUuid=<uuid>`). Taskwarrior is not modified.
- `token.json` is written with mode 0600, in a 0700 config directory.
- Events are created with `visibility=private` so they aren't exposed
  on shared calendars.

If you change OAuth scopes (e.g. after upgrade) or the refresh token
gets revoked, just delete `~/.config/task-gcal/token.json` and re-run
`task-gcal --setup`. The tool also handles the most common refresh
failures automatically.

## Development

```
uv sync --group dev
uv run pytest
```

The suite uses no network and no Taskwarrior: the calendar, the `task export`
subprocess, and the clock are all faked (see `tests/conftest.py`). Tests always
set `timezone` explicitly, so nothing depends on the machine's local zone.

If you check the suite by mutating the source (breaking something on purpose to
confirm a test notices), disable bytecode caching:

```
find src -name __pycache__ -exec rm -rf {} +
PYTHONDONTWRITEBYTECODE=1 uv run pytest
```

A mutation that doesn't change a file's *length* — `23` to `25`, say — can be
reverted within the same filesystem second, and CPython's `(mtime, size)`
staleness check will then happily reuse the mutant's bytecode. That mis-scores
the run in both directions.

Roughly half of it is characterization tests for the subtle scheduling rules
— in-progress pinning, schedule stability, the overdue horizon, duplicate
cleanup, `scheduled`/`wait` floors, the midnight-due bump — which exist so a
refactor can't quietly undo one. If you change behavior on purpose, expect to
change a test and say why in the commit.

The other half tests the review side, where the failure mode is different: a
wrong number looks plausible. So those tests mostly pin *restraint* — that
churn is described as observed, that a narrowing override isn't counted as
boundary erosion, that missing data doesn't become zero, that no check-in
answer is scored, and that a suggestion never reads as a verdict.

Two structural invariants have tests of their own, because a stray import
would break either silently:

- The journal's write path can't read history (`test_journal.py`), so a
  corrupt journal can never produce a wrong calendar.
- Bare `task-gcal` doesn't import any review code (`test_cli.py`), so the
  fast path stays fast as the analytical side grows.

The journal directory is a per-test temp dir, set by an autouse fixture. It
has to be unconditional: any command may append an observation, and a suite
that writes into your real history is a bug that only shows up later as
mysterious extra data.

## License

[MIT](LICENSE) © Daniel Dvorkin
