"""`task-gcal doctor`: is everything this tool depends on actually working?

Read-only, and it should stay boring. Each check names what it looked at and
what to do when the answer is wrong, because a diagnostic that only says
"failed" moves the work rather than doing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import (
    CREDENTIALS_PATH,
    JOURNAL_DETAIL_CHOICES,
    TOKEN_PATH,
    USER_CONFIG_PATH,
    Settings,
)
from .journal import data_dir, load, run_files
from .reflections import path as reflections_path

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: Optional[str] = None


def run(settings: Settings, *, now: Optional[datetime] = None) -> int:
    """Print every check. Exit code is non-zero only for a real failure."""
    now = now or datetime.now(timezone.utc)
    checks = [
        _check_config(settings),
        _check_timezone(settings),
        _check_taskwarrior(settings),
        _check_credentials(),
        _check_journal(settings, now),
        _check_reflections(),
    ]
    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"[{_mark(check.status)}] {check.name.ljust(width)}  {check.detail}")
        if check.fix:
            print(f"       {' ' * width}  → {check.fix}")
    failures = sum(1 for c in checks if c.status == FAIL)
    warnings = sum(1 for c in checks if c.status == WARN)
    print()
    if failures:
        print(f"{failures} failure(s), {warnings} warning(s).")
    elif warnings:
        print(f"No failures, {warnings} warning(s).")
    else:
        print("Everything checks out.")
    return 1 if failures else 0


def _mark(status: str) -> str:
    return {OK: " ok ", WARN: "warn", FAIL: "FAIL"}[status]


def _check_config(settings: Settings) -> Check:
    """Check the *effective* settings, wherever they came from.

    Deliberately not gated on config.toml existing: a CLI flag can produce
    exactly the same impossible combination, and an unschedulable working day
    is worth catching either way.
    """
    if settings.work_start_hour >= settings.work_end_hour:
        return Check(
            "config",
            FAIL,
            f"work_start_hour ({settings.work_start_hour}) is not before "
            f"work_end_hour ({settings.work_end_hour}), so no slot can ever fit",
            "fix the hours in config.toml or drop the flag that set them",
        )
    if not settings.work_days:
        return Check(
            "config",
            FAIL,
            "work_days is empty, so there is nowhere to schedule anything",
            "set at least one weekday (0=Mon..6=Sun)",
        )
    if settings.journal_detail not in JOURNAL_DETAIL_CHOICES:
        return Check(
            "config",
            FAIL,
            f"journal_detail={settings.journal_detail!r} is not valid",
            f"set it to one of {', '.join(JOURNAL_DETAIL_CHOICES)}",
        )

    notes = [
        f"read {USER_CONFIG_PATH}"
        if USER_CONFIG_PATH.exists()
        else f"no {USER_CONFIG_PATH.name}; using built-in defaults"
    ]
    if settings.removal_guard_ratio >= 1.0:
        notes.append("removal guard disabled (removal_guard_ratio = 1.0)")
    if settings.settle_days == 0:
        notes.append("stability off (settle_days = 0): blocks may move each run")
    return Check("config", OK, "; ".join(notes))


def _check_timezone(settings: Settings) -> Check:
    try:
        tz = settings.resolve_timezone()
    except SystemExit as e:  # resolve_timezone exits on an unknown zone
        return Check(
            "timezone", FAIL, str(e), "use an IANA name, e.g. Europe/Madrid"
        )
    source = "config.toml" if settings.timezone else "system"
    return Check("timezone", OK, f"{tz} (from {source})")


def _check_taskwarrior(settings: Settings) -> Check:
    import shutil

    if shutil.which("task") is None:
        return Check(
            "taskwarrior",
            FAIL,
            "`task` is not on PATH",
            "install Taskwarrior, or fix PATH for the environment that runs "
            "this (cron and launchd don't share your shell's)",
        )
    from .taskw import load_next_tasks

    try:
        tasks = load_next_tasks(
            settings.report,
            estimate_uda=settings.estimate_uda,
            override_uda=settings.override_uda,
        )
    except SystemExit as e:
        return Check("taskwarrior", FAIL, str(e))

    with_estimate = sum(1 for t in tasks if t.estimate_minutes is not None)
    detail = (
        f"`task export {settings.report}` returned {len(tasks)} task(s), "
        f"{with_estimate} with a `{settings.estimate_uda}`"
    )
    if not tasks:
        return Check(
            "taskwarrior",
            WARN,
            detail,
            "an empty report is usually a wrong report name, an active "
            "context, or TASKDATA pointing elsewhere — scheduling refuses to "
            "clear the calendar when this happens",
        )
    if with_estimate == 0:
        return Check(
            "taskwarrior",
            WARN,
            detail,
            f"nothing can be scheduled without an estimate; check that the "
            f"UDA is really called `{settings.estimate_uda}`",
        )
    return Check("taskwarrior", OK, detail)


def _check_credentials() -> Check:
    if not CREDENTIALS_PATH.exists():
        return Check(
            "google auth",
            FAIL,
            f"no OAuth client secrets at {CREDENTIALS_PATH}",
            "download a Desktop OAuth client JSON and save it there, then run "
            "`task-gcal --setup`",
        )
    if not TOKEN_PATH.exists():
        return Check(
            "google auth",
            WARN,
            "client secrets present, but not authorized yet",
            "run `task-gcal --setup`",
        )
    mode = oct(TOKEN_PATH.stat().st_mode)[-3:]
    if mode != "600":
        return Check(
            "google auth",
            WARN,
            f"token.json is mode {mode}",
            f"chmod 600 {TOKEN_PATH}",
        )
    return Check("google auth", OK, f"token present, mode {mode}")


def _check_journal(settings: Settings, now: datetime) -> Check:
    if settings.journal_detail == "off":
        return Check(
            "journal",
            WARN,
            'journal_detail = "off": nothing is being recorded',
            'set it to "minimal" to keep history without storing task titles',
        )
    files = run_files()
    if not files:
        return Check(
            "journal",
            WARN,
            f"no records under {data_dir()}",
            "run `task-gcal backfill` to seed history, and `task-gcal "
            "snapshot` on a timer to keep it",
        )
    read = load(since=now - timedelta(days=30))
    days = len({r.at.date() for r in read.records})
    detail = (
        f"{len(files)} month file(s); {len(read.records)} record(s) over "
        f"{days} day(s) in the last 30"
    )
    if read.unreadable_lines:
        return Check(
            "journal",
            WARN,
            f"{detail}; {read.unreadable_lines} unreadable line(s)",
            "the unreadable lines are skipped; reviews will say so",
        )
    if days < 7:
        return Check(
            "journal",
            WARN,
            detail,
            "churn metrics are a lower bound at this sampling rate; run "
            "`task-gcal snapshot` every few hours",
        )
    return Check("journal", OK, detail)


def _check_reflections() -> Check:
    target = reflections_path()
    if not target.is_file():
        return Check(
            "check-ins",
            OK,
            "none recorded (optional — reviews work without them)",
        )
    from .reflections import load as load_reflections

    answers = load_reflections()
    classified = sum(1 for r in answers.values() if r.classified)
    return Check(
        "check-ins",
        OK,
        f"{len(answers)} answered episode(s), {classified} with a reason",
    )
