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
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
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
    return Settings(
        work_start_hour=int(get("work_start_hour", defaults.work_start_hour)),
        work_end_hour=int(get("work_end_hour", defaults.work_end_hour)),
        work_days=frozenset(int(d) for d in work_days_raw),
        slot_align_minutes=int(get("slot_align_minutes", defaults.slot_align_minutes)),
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
    )


def apply_overrides(settings: Settings, overrides: dict) -> Settings:
    """Return a copy of `settings` with non-None `overrides` applied.

    Used to let CLI flags win over the config file and built-in defaults.
    """
    filtered = {k: v for k, v in overrides.items() if v is not None}
    return replace(settings, **filtered) if filtered else settings
