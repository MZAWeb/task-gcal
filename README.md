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

## The observation journal

Almost every interesting question about your own behaviour — did that
deadline move? did the block survive? was the task still open the next
day? — is a *diff between two observations*, and none of it is
reconstructable after the fact. So every real run appends one record to
an append-only journal:

```
~/.local/share/task-gcal/runs/2026-09.jsonl
```

(`$XDG_DATA_HOME` is honored; `TASK_GCAL_DATA_DIR` overrides both.)

Three rules keep it honest:

- **Scheduling only ever appends.** It never reads history to decide
  where a block goes, so a corrupt or deleted journal cannot produce a
  wrong calendar. Deleting it costs you history and nothing else.
- **Nothing derived is stored.** No totals, no scores, no streaks —
  reviews recompute everything from raw observations, so changing a
  metric's definition can't leave numbers behind that meant something
  else. Each record carries a schema version, a metrics-definition
  version, and a hash of the settings that affect meaning, so a review
  can refuse to draw a trend across a boundary rather than averaging two
  different things.
- **A dry run records nothing.** Its placements were never made.

Files are mode 0600 in a 0700 directory and nothing leaves your machine,
but task titles and deadlines are sensitive, so there's a dial:

```toml
journal_detail = "full"      # store descriptions (default)
journal_detail = "minimal"   # store a 12-hex digest instead
journal_detail = "off"       # record nothing at all
```

`minimal` still tells two tasks apart and still notices a retitle, which
is all the churn metrics need. At roughly 20 tasks a few times a day the
whole thing is single-digit megabytes a year, so there's no rotation, no
pruning, and deliberately no database.

### `task-gcal snapshot`

How much the journal sees depends on how often it looks, so looking often
is part of the design. `snapshot` appends one observation and touches
nothing else — no calendar writes at all — which makes it safe to run on
a timer:

```bash
task-gcal snapshot     # every few hours from cron or launchd
```

### `task-gcal backfill`

Waiting weeks for the first useful review isn't necessary: Taskwarrior's
own per-task modification log already holds recent due-date, estimate and
scheduled-date changes. `backfill` reads it and reconstructs one daily
observation per day of a past window:

```bash
task-gcal backfill                      # the last 90 days
task-gcal backfill --since 2026-06-01
```

It is read-only with respect to Taskwarrior and the calendar, it never
overwrites a day that was actually observed (so re-running it is safe and
a real snapshot always wins), and it costs one `task info` subprocess per
recent task — which is why it's a one-time import and not how reviews
read history.

Three limits, which every number derived from a backfilled day inherits:

- **No calendar blocks.** Past events reveal only their *final* stored
  times, not the moves made before they happened, so placement churn
  starts accruing from the journal rather than being invented here.
- **A field the log never mentions is assumed to have always held its
  current value.** Inventing a change would be worse than assuming
  stability.
- **Timestamps come from a local-zone rendering with no offset**, so a
  machine that has moved timezones is off by the difference.

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

Most of it is characterization tests for the subtle scheduling rules —
in-progress pinning, the overdue horizon, duplicate cleanup,
`scheduled`/`wait` floors, the midnight-due bump — which exist so a refactor
can't quietly undo one. If you change behavior on purpose, expect to change a
test and say why in the commit.

## License

[MIT](LICENSE) © Daniel Dvorkin
