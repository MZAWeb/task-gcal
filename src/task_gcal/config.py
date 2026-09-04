"""Static defaults and runtime-loaded user config.

User-overridable values live in `~/.config/task-gcal/config.toml`
(or `$TASK_GCAL_CONFIG_DIR/config.toml`). All keys are optional. Every key
can also be overridden per-invocation by the matching CLI flag, which takes
precedence over the config file (see `cli.py`).

Example config.toml:

    work_start_hour = 8
    work_end_hour   = 16
    work_days       = [0, 1, 2, 3, 4]   # Mon..Fri
    slot_align_minutes = 15
    buffer_minutes  = 10                # free gap kept around each event
    estimate_uda    = "est"
    calendar_id     = "abc123@group.calendar.google.com"
    event_color_id  = "9"               # Blueberry
    event_visibility = "private"
    report          = "next"
    timezone        = "Europe/London"   # omit to use the system local zone
    overdue_horizon_days = 30
    lookback_days   = 7
    settle_days     = 2                 # near-term placements stay put
    override_uda    = "gcal"            # task UDA holding per-task overrides
    removal_guard_ratio = 0.5           # max share of our events one run may remove
    journal_detail  = "full"            # full | minimal | off
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone


CONFIG_DIR = Path(
    os.environ.get("TASK_GCAL_CONFIG_DIR")
    or (Path.home() / ".config" / "task-gcal")
)
CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"
TOKEN_PATH = CONFIG_DIR / "token.json"
USER_CONFIG_PATH = CONFIG_DIR / "config.toml"

# Smallest scope that lets us write events on calendars the user owns.
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.events.owned"]

# Tag stamped on every event we manage. Not user-tunable: changing it
# would orphan all existing managed events.
SCHEDULER_TAG = "task-gcal"

# Allowed Google Calendar event visibility values.
VISIBILITY_CHOICES = ("default", "public", "private", "confidential")

# How much detail a journal record keeps. See `Settings.journal_detail`.
JOURNAL_DETAIL_CHOICES = ("full", "minimal", "off")


@dataclass(frozen=True)
class Settings:
    work_start_hour: int = 9
    work_end_hour: int = 18  # exclusive
    work_days: frozenset[int] = field(default_factory=lambda: frozenset({0, 1, 2, 3, 4}))
    slot_align_minutes: int = 15
    # Free time to keep around every event (ours and others'); 0 packs
    # tasks back-to-back.
    buffer_minutes: int = 0
    estimate_uda: str = "estimate"
    calendar_id: str = "primary"
    event_color_id: str = "9"  # Blueberry
    event_visibility: str = "private"
    report: str = "next"
    # IANA name (e.g. "Europe/London"). None -> system local zone.
    timezone: Optional[str] = None
    # When a task is overdue, schedule ASAP; the effective deadline
    # becomes now + this many days.
    overdue_horizon_days: int = 30
    # How far in the past to look for our own previously-created events
    # when reconciling. Bounds the events.list query.
    lookback_days: int = 7
    # A placement starting within this many days is a commitment: keep it
    # unless it has become invalid, rather than chasing the earliest slot
    # that fits. 0 restores always-earliest placement. See `stability.py`.
    settle_days: int = 2
    # Fraction of our own unfinished events a single run may remove before
    # it refuses and asks for `--force`. Guards against one bad input
    # (empty task source, wrong UDA name) turning a run into a mass
    # deletion. 1.0 disables the guard.
    removal_guard_ratio: float = 0.5
    # Taskwarrior UDA holding inline per-task overrides (see
    # `parse_task_overrides`).
    override_uda: str = "gcal"
    # How much of what we record is kept in the clear. "full" stores task
    # titles and projects as they are; "minimal" replaces each with a short
    # digest — enough to tell two apart and to notice a rename, not enough to
    # read; "off" records nothing at all, neither placements nor task changes.
    # Applies to the change history as well as the run journal, because that is
    # where titles live now. Changing it doesn't rewrite what's already stored.
    journal_detail: str = "full"
    # Email addresses to invite to created events. Empty by default and
    # only ever set per task (via the override UDA), never globally: you
    # don't want every task event to invite the same people.
    attendees: tuple[str, ...] = ()

    def resolve_timezone(self):
        """Return the tzinfo to schedule in: configured zone, else local."""
        if not self.timezone:
            return get_localzone()
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise SystemExit(f"Unknown timezone {self.timezone!r}: {e}") from None


def load_settings() -> Settings:
    if not USER_CONFIG_PATH.exists():
        return Settings()
    with open(USER_CONFIG_PATH, "rb") as fh:
        raw = tomllib.load(fh)

    def get(key, default):
        return raw[key] if key in raw else default

    defaults = Settings()
    work_days_raw = get("work_days", list(defaults.work_days))
    timezone_raw = get("timezone", defaults.timezone)
    try:
        return Settings(
            work_start_hour=coerce_hour(
                get("work_start_hour", defaults.work_start_hour)
            ),
            work_end_hour=coerce_hour(
                get("work_end_hour", defaults.work_end_hour), maximum=24
            ),
            work_days=frozenset(int(d) for d in work_days_raw),
            slot_align_minutes=int(
                get("slot_align_minutes", defaults.slot_align_minutes)
            ),
            buffer_minutes=int(get("buffer_minutes", defaults.buffer_minutes)),
            estimate_uda=str(get("estimate_uda", defaults.estimate_uda)),
            calendar_id=str(get("calendar_id", defaults.calendar_id)),
            event_color_id=str(get("event_color_id", defaults.event_color_id)),
            event_visibility=str(get("event_visibility", defaults.event_visibility)),
            report=str(get("report", defaults.report)),
            timezone=str(timezone_raw) if timezone_raw else None,
            overdue_horizon_days=int(
                get("overdue_horizon_days", defaults.overdue_horizon_days)
            ),
            lookback_days=int(get("lookback_days", defaults.lookback_days)),
            settle_days=int(get("settle_days", defaults.settle_days)),
            journal_detail=coerce_journal_detail(
                get("journal_detail", defaults.journal_detail)
            ),
            override_uda=str(get("override_uda", defaults.override_uda)),
            removal_guard_ratio=float(
                get("removal_guard_ratio", defaults.removal_guard_ratio)
            ),
        )
    except ValueError as e:
        # A bad value should name the file it came from, not raise a traceback.
        raise SystemExit(f"Invalid setting in {USER_CONFIG_PATH}: {e}") from None


def apply_overrides(settings: Settings, overrides: dict) -> Settings:
    """Return a copy of `settings` with non-None `overrides` applied.

    Used to let CLI flags win over the config file and built-in defaults.
    """
    filtered = {k: v for k, v in overrides.items() if v is not None}
    return replace(settings, **filtered) if filtered else settings


def coerce_hour(raw, *, maximum: int = 23) -> int:
    """Parse an hour-of-day, rejecting anything outside 0..maximum.

    `work_end_hour` is exclusive, so its maximum is 24 (midnight ending the
    day); a start hour can only be 0..23. Without this an out-of-range value
    reaches `datetime` and surfaces as a bare traceback.
    """
    hour = int(raw)
    if not 0 <= hour <= maximum:
        raise ValueError(f"hour must be between 0 and {maximum}, got {hour}")
    return hour


def coerce_journal_detail(raw) -> str:
    value = str(raw)
    if value not in JOURNAL_DETAIL_CHOICES:
        raise ValueError(
            f"journal_detail must be one of "
            f"{', '.join(JOURNAL_DETAIL_CHOICES)}, got {value!r}"
        )
    return value


def coerce_work_days(raw: str) -> frozenset[int]:
    """Parse a comma/space-separated weekday list (0=Mon .. 6=Sun)."""
    days = {int(p) for p in re.split(r"[,\s]+", raw.strip()) if p}
    if not days:
        raise ValueError("work days list is empty")
    bad = sorted(d for d in days if not 0 <= d <= 6)
    if bad:
        raise ValueError(f"weekday(s) out of range (0=Mon..6=Sun): {bad}")
    return frozenset(days)


# A pragmatic email check: one `@`, no whitespace, and a dotted domain.
# Not RFC-complete (nor should it be); just enough to catch typos before
# we hand the address to Google as an attendee.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def coerce_attendees(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated list of attendee email addresses.

    Whitespace around each address is ignored. Duplicates are dropped
    while preserving first-seen order. Raises ``ValueError`` if the list
    is empty or any address looks malformed.
    """
    seen: dict[str, None] = {}
    for part in raw.split(","):
        email = part.strip()
        if not email:
            continue
        if not _EMAIL_RE.match(email):
            raise ValueError(f"not a valid email address: {email!r}")
        seen.setdefault(email, None)
    if not seen:
        raise ValueError("no email addresses given")
    return tuple(seen)


# Settings keys that make sense to override per task, each with a value
# coercer. Run-global keys (calendar_id, timezone, report, estimate_uda,
# lookback_days, override_uda) are deliberately excluded.
_TASK_OVERRIDE_COERCE = {
    "work_start_hour": coerce_hour,
    "work_end_hour": partial(coerce_hour, maximum=24),
    "work_days": coerce_work_days,
    "slot_align_minutes": int,
    "buffer_minutes": int,
    "event_color_id": str,
    "overdue_horizon_days": int,
    "attendees": coerce_attendees,
}


def parse_task_overrides(raw: str) -> dict:
    """Parse an inline per-task override string into a Settings-override dict.

    Pairs are ``key=value``, separated by whitespace; commas are reserved
    for list values (e.g. ``work_days``). Unknown or non-per-task keys, and
    unparseable values, raise ``ValueError`` (the caller turns that into a
    warning and falls back to global settings).

        "work_end_hour=20 buffer_minutes=0"  ->  {"work_end_hour": 20, ...}
        "work_days=0,2,4"                     ->  {"work_days": frozenset(...)}
    """
    overrides: dict = {}
    for tok in raw.split():
        if not tok:
            continue
        key, sep, val = tok.partition("=")
        if not sep:
            raise ValueError(f"expected key=value, got {tok!r}")
        key = key.strip()
        coerce = _TASK_OVERRIDE_COERCE.get(key)
        if coerce is None:
            raise ValueError(f"unknown or non-per-task key {key!r}")
        try:
            overrides[key] = coerce(val.strip())
        except ValueError as e:
            raise ValueError(f"bad value for {key}={val.strip()!r}: {e}") from None
    return overrides
