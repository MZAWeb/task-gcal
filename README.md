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
   - For each remaining task, finds the earliest aligned working-hours
     slot of length `estimate` minutes that ends on or before the
     task's due date and does not overlap any timed event on the
     calendar. A task's `scheduled` and/or `wait` date (whichever is
     later) is honored as an inclusive earliest-start, so the task is
     only ever placed within `[scheduled, due]`.
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

## Requirements

- Python 3.10+
- [Taskwarrior](https://taskwarrior.org/) 3.x on your `PATH` (tested
  against 3.4)
- A Google account and a Google Cloud OAuth client (type: Desktop) —
  see Setup

## Setup

1. Add a numeric UDA to Taskwarrior to hold each task's duration (in
   minutes), e.g. in `~/.taskrc`:

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

3. Install:

   ```
   pip install -e .          # from a local checkout
   # or, once published:
   # pip install git+https://github.com/MZAWeb/task-gcal
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
work_start_hour     = 9            # inclusive
work_end_hour       = 18           # exclusive
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
```

Every one of these keys can also be overridden per-run with a matching
CLI flag, which takes precedence over the config file (run `task-gcal
--help` for the full list). For example:

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

## License

[MIT](LICENSE) © Daniel Dvorkin
