"""Argument parsing and subcommand dispatch.

Bare `task-gcal` means `task-gcal schedule`, and every flag that predates
the subcommands keeps working unchanged — see `_normalize_argv`.

Only the scheduling path is imported at module level. Everything analytical is
imported inside its handler, so bare `task-gcal` never loads reporting code it
isn't going to run — the fast action path stays fast, and stays fast as the
review side grows. `test_cli.py` asserts it.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

from googleapiclient.errors import HttpError

from . import __version__
from .config import (
    VISIBILITY_CHOICES,
    apply_overrides,
    coerce_hour,
    coerce_work_days,
    load_settings,
)
from .gcal import GCal
from .schedule import reconcile

# Mirrors `review.episodes.DEFAULT_SINCE_DAYS`, for the same reason.
DEFAULT_CHECKIN_DAYS = 14

# Review section names, for `--section`. Also duplicated to keep the review
# package out of a bare run, and also checked by a test.
_SECTION_CHOICES = (
    "capacity",
    "throughput",
    "flow",
    "lead_time",
    "follow_through",
    "deadlines",
    "churn",
    "friction",
    "attempts",
    "scope",
    "boundaries",
    "stagnation",
    "trends",
)

_FORMAT_CHOICES = ("terminal", "markdown", "json", "html")


def _parse_work_days(raw: str) -> frozenset[int]:
    """argparse adapter around `coerce_work_days` (0=Mon .. 6=Sun)."""
    try:
        return coerce_work_days(raw)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _date_arg(raw: str) -> datetime:
    """A naive `YYYY-MM-DD` at midnight; the caller attaches the timezone."""
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a date as YYYY-MM-DD, got {raw!r}"
        ) from None


def _hour_arg(maximum: int):
    """argparse adapter around `coerce_hour`, so a bad hour is a usage error."""

    def parse(raw: str) -> int:
        try:
            return coerce_hour(raw, maximum=maximum)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e)) from None

    return parse


# CLI flags that override config.toml / defaults. `dest` matches the
# Settings field name so overrides can be applied generically. All default
# to None so an unset flag leaves the config value untouched.
_OVERRIDE_DESTS = (
    "work_start_hour",
    "work_end_hour",
    "work_days",
    "slot_align_minutes",
    "buffer_minutes",
    "estimate_uda",
    "calendar_id",
    "event_color_id",
    "event_visibility",
    "report",
    "timezone",
    "overdue_horizon_days",
    "lookback_days",
    "settle_days",
    "override_uda",
    "removal_guard_ratio",
)


def _overrides_parent() -> argparse.ArgumentParser:
    """Settings overrides, shared by every subcommand that reads Settings."""
    parser = argparse.ArgumentParser(add_help=False)
    g = parser.add_argument_group(
        "overrides",
        "Override config.toml / built-in defaults for this run.",
    )
    g.add_argument(
        "--calendar-id", dest="calendar_id", metavar="ID",
        help="Calendar to write to (default: primary).",
    )
    g.add_argument(
        "--report", dest="report", metavar="NAME",
        help="Taskwarrior report to pull tasks from (default: next).",
    )
    g.add_argument(
        "--estimate-uda", dest="estimate_uda", metavar="NAME",
        help="Taskwarrior UDA holding the time estimate in minutes "
             "(default: estimate).",
    )
    g.add_argument(
        "--override-uda", dest="override_uda", metavar="NAME",
        help="Taskwarrior UDA holding inline per-task overrides, e.g. "
             "gcal:'work_end_hour=20 buffer_minutes=0' (default: gcal).",
    )
    g.add_argument(
        "--timezone", dest="timezone", metavar="ZONE",
        help="IANA timezone to schedule in, e.g. Europe/London "
             "(default: system local zone).",
    )
    g.add_argument(
        "--work-start", dest="work_start_hour", type=_hour_arg(23), metavar="HOUR",
        help="Working-hours start hour, 0-23 (default: 9).",
    )
    g.add_argument(
        "--work-end", dest="work_end_hour", type=_hour_arg(24), metavar="HOUR",
        help="Working-hours end hour, exclusive, 0-24 where 24 is midnight "
             "(default: 18).",
    )
    g.add_argument(
        "--work-days", dest="work_days", type=_parse_work_days, metavar="D,D,..",
        help="Comma-separated working weekdays, 0=Mon..6=Sun "
             "(default: 0,1,2,3,4).",
    )
    g.add_argument(
        "--slot-align", dest="slot_align_minutes", type=int, metavar="MIN",
        help="Align slot start times to this minute boundary (default: 15).",
    )
    g.add_argument(
        "--buffer-minutes", dest="buffer_minutes", type=int, metavar="MIN",
        help="Free time to keep around every event; 0 packs tasks "
             "back-to-back (default: 0).",
    )
    g.add_argument(
        "--event-color", dest="event_color_id", metavar="ID",
        help="Google Calendar colorId for created events (default: 9).",
    )
    g.add_argument(
        "--event-visibility", dest="event_visibility", choices=VISIBILITY_CHOICES,
        help="Visibility of created events (default: private).",
    )
    g.add_argument(
        "--overdue-horizon-days", dest="overdue_horizon_days", type=int,
        metavar="DAYS",
        help="How many days ahead an overdue task may be scheduled "
             "(default: 30).",
    )
    g.add_argument(
        "--lookback-days", dest="lookback_days", type=int, metavar="DAYS",
        help="How far back to scan for our own past events (default: 7).",
    )
    g.add_argument(
        "--settle-days", dest="settle_days", type=int, metavar="DAYS",
        help="Keep placements starting within this many days unless they "
             "become invalid; 0 always takes the earliest slot (default: 2).",
    )
    g.add_argument(
        "--removal-guard-ratio", dest="removal_guard_ratio", type=float,
        metavar="RATIO",
        help="Refuse to remove more than this share of our own unfinished "
             "events in one run; 1.0 disables (default: 0.5).",
    )
    return parser


def _run_schedule(args, settings) -> int:
    if args.setup:
        GCal(settings)
        print("Auth OK.")
        return 0
    if args.reoptimize:
        settings = replace(settings, settle_days=0)
    return reconcile(settings, dry_run=args.dry_run, force=args.force)


def _build_parser() -> argparse.ArgumentParser:
    overrides = _overrides_parent()
    parser = argparse.ArgumentParser(
        prog="task-gcal",
        description=(
            "Schedule Taskwarrior 'next' tasks into free Google Calendar slots."
        ),
        epilog=(
            "Run with no command to schedule; `task-gcal schedule --help` "
            "lists the scheduling flags."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    schedule_p = sub.add_parser(
        "schedule",
        parents=[overrides],
        help="Reconcile the calendar with Taskwarrior (the default command).",
        description="Reconcile the calendar with Taskwarrior.",
    )
    schedule_p.add_argument(
        "--setup",
        action="store_true",
        help="Run the OAuth flow and exit (use after placing credentials.json).",
    )
    schedule_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without modifying the calendar.",
    )
    schedule_p.add_argument(
        "--force",
        action="store_true",
        help="Bypass the bulk-removal guard (see --removal-guard-ratio).",
    )
    schedule_p.add_argument(
        "--reoptimize",
        action="store_true",
        help="Re-place every block at its earliest fitting slot, giving up "
             "near-term placements (same as --settle-days 0).",
    )
    schedule_p.set_defaults(func=_run_schedule)

    review_p = sub.add_parser(
        "review",
        parents=[overrides],
        help="Report on a past week or month. Read-only.",
        description=(
            "Summarize a period in one screen. Reads Taskwarrior, the "
            "calendar and the journal; writes to none of them."
        ),
    )
    span = review_p.add_mutually_exclusive_group()
    span.add_argument(
        "--week", dest="kind", action="store_const", const="week",
        help="Report on an ISO week, Monday start (the default).",
    )
    span.add_argument(
        "--month", dest="kind", action="store_const", const="month",
        help="Report on a calendar month.",
    )
    review_p.add_argument(
        "--last", type=int, default=0, metavar="N",
        help="Report N periods back instead of the current one; "
             "--last 1 is the previous week or month.",
    )
    review_p.add_argument(
        "--section", dest="sections", action="append", metavar="NAME",
        choices=_SECTION_CHOICES,
        help="Show one section in full instead of the summary. Repeatable. "
             f"One of: {', '.join(_SECTION_CHOICES)}.",
    )
    review_p.add_argument(
        "--all", dest="all_sections", action="store_true",
        help="Summarize every section, not just the headline ones.",
    )
    review_p.add_argument(
        "--triage", action="store_true",
        help="List stagnant tasks with the exact `task` commands that would "
             "resolve each. Prints commands; changes nothing.",
    )
    review_p.add_argument(
        "--format", dest="fmt", default="terminal", choices=_FORMAT_CHOICES,
        help="Output format (default: terminal).",
    )
    review_p.add_argument(
        "--output", "-o", metavar="PATH",
        help="Write the report to a file instead of stdout.",
    )
    review_p.add_argument(
        "--open", dest="open_in_browser", action="store_true",
        help="Open the rendered report in a browser (implies a file).",
    )
    review_p.set_defaults(func=_run_review, kind="week")

    checkin_p = sub.add_parser(
        "checkin",
        parents=[overrides],
        help="Optional retrospective: what happened to blocks that passed?",
        description=(
            "Walk scheduled blocks that have ended and haven't been explained "
            "yet, and record what happened and why. Optional and "
            "retrospective; writes only its own answers, never Taskwarrior."
        ),
    )
    checkin_p.add_argument(
        "--since", type=_date_arg, metavar="YYYY-MM-DD",
        help="Reach further back when catching up (default: the last "
             f"{DEFAULT_CHECKIN_DAYS} days).",
    )
    checkin_p.set_defaults(func=_run_checkin)

    doctor_p = sub.add_parser(
        "doctor",
        parents=[overrides],
        help="Check config, auth, Taskwarrior and the journal. Read-only.",
        description="Check that everything this tool depends on is working.",
    )
    doctor_p.set_defaults(func=_run_doctor)

    return parser


def _run_review(args, settings) -> int:
    from pathlib import Path

    from .review import ReviewRequest, run

    return run(
        settings,
        ReviewRequest(
            kind=args.kind,
            offset=args.last,
            sections=tuple(args.sections or ()),
            all_sections=args.all_sections,
            fmt=args.fmt,
            open_in_browser=args.open_in_browser,
            output=Path(args.output) if args.output else None,
            triage=args.triage,
        ),
    )


def _run_checkin(args, settings) -> int:
    from .checkin import checkin

    since = None
    if args.since is not None:
        tz = settings.resolve_timezone()
        since = args.since.replace(tzinfo=tz).astimezone(timezone.utc)
    code, _summary = checkin(settings, since=since)
    return code


def _run_doctor(_args, settings) -> int:
    from .doctor import run as run_doctor

    return run_doctor(settings)


# Recognized subcommands, for `_normalize_argv`.
_SUBCOMMANDS = ("schedule", "review", "checkin", "doctor")

# Top-level flags that must not be swallowed by the implicit `schedule`.
_TOP_LEVEL_FLAGS = ("-h", "--help", "--version")


def _normalize_argv(argv: list[str]) -> list[str]:
    """Insert the implicit `schedule` subcommand.

    Bare `task-gcal` and every flag spelling that predates the subcommands
    must keep working, so anything not starting with a known subcommand is a
    `schedule` invocation. `--help` and `--version` stay top-level, where
    they can list the subcommands.
    """
    if argv and (argv[0] in _SUBCOMMANDS or argv[0] in _TOP_LEVEL_FLAGS):
        return argv
    return ["schedule", *argv]


def main(argv: Optional[list[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(_normalize_argv(raw))

    overrides = {dest: getattr(args, dest, None) for dest in _OVERRIDE_DESTS}
    settings = apply_overrides(load_settings(), overrides)

    try:
        return args.func(args, settings)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except HttpError as e:
        print(f"Google Calendar API error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
