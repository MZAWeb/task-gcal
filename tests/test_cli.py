"""Argument parsing and the flag -> Settings mapping.

`main()` itself builds a GCal (and so wants credentials), so what's tested
here is the parser and the override plumbing around it.
"""

from __future__ import annotations

import dataclasses

import pytest

from task_gcal import cli as cli_mod
from task_gcal.config import Settings, apply_overrides


def parse(*argv: str):
    """Parse as `main` does, implicit `schedule` subcommand included."""
    return cli_mod._build_parser().parse_args(
        cli_mod._normalize_argv(list(argv))
    )


def settings_from(*argv: str) -> Settings:
    args = parse(*argv)
    overrides = {dest: getattr(args, dest) for dest in cli_mod._OVERRIDE_DESTS}
    return apply_overrides(Settings(), overrides)


# ---------------------------------------------------------------------------
# Defaults and plumbing
# ---------------------------------------------------------------------------

def test_no_flags_leaves_every_setting_alone():
    assert settings_from() == Settings()


def test_every_override_dest_is_a_settings_field():
    fields = {f.name for f in dataclasses.fields(Settings)}
    assert set(cli_mod._OVERRIDE_DESTS) <= fields


def test_every_override_dest_defaults_to_none():
    # An unset flag must not overwrite a config.toml value.
    args = parse()
    for dest in cli_mod._OVERRIDE_DESTS:
        assert getattr(args, dest) is None, dest


def test_flags_reach_settings():
    s = settings_from(
        "--work-start", "8",
        "--work-end", "20",
        "--work-days", "0,2,4",
        "--buffer-minutes", "15",
        "--calendar-id", "cal@x",
        "--timezone", "Europe/London",
        "--removal-guard-ratio", "0.9",
    )
    assert (s.work_start_hour, s.work_end_hour) == (8, 20)
    assert s.work_days == frozenset({0, 2, 4})
    assert s.buffer_minutes == 15
    assert s.calendar_id == "cal@x"
    assert s.timezone == "Europe/London"
    assert s.removal_guard_ratio == 0.9


def test_dry_run_and_force_are_not_settings():
    args = parse("--dry-run", "--force")
    assert args.dry_run is True
    assert args.force is True
    assert "dry_run" not in cli_mod._OVERRIDE_DESTS
    assert "force" not in cli_mod._OVERRIDE_DESTS


def test_setup_is_a_flag():
    assert parse("--setup").setup is True
    assert parse().setup is False


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------

def test_bare_invocation_means_schedule():
    assert cli_mod._normalize_argv([]) == ["schedule"]
    assert parse().command == "schedule"


def test_pre_subcommand_flag_spellings_still_work():
    # These are how the tool was invoked before subcommands existed; the
    # normalizer exists so they never became a usage error.
    assert cli_mod._normalize_argv(["--dry-run"]) == ["schedule", "--dry-run"]
    assert parse("--dry-run", "--work-end", "20").dry_run is True


def test_an_explicit_subcommand_is_left_alone():
    assert cli_mod._normalize_argv(["schedule", "--force"]) == [
        "schedule", "--force"
    ]


@pytest.mark.parametrize("flag", ["-h", "--help", "--version"])
def test_top_level_flags_are_not_swallowed(flag):
    # Prepending `schedule` here would show the subcommand's help instead
    # of the command list.
    assert cli_mod._normalize_argv([flag]) == [flag]


def test_every_subcommand_dispatches_to_a_handler():
    for name in cli_mod._SUBCOMMANDS:
        args = cli_mod._build_parser().parse_args([name])
        assert callable(args.func), name


def test_every_subcommand_accepts_the_settings_overrides():
    # A subcommand that silently ignored --timezone would read the journal in
    # one zone and report in another.
    for name in cli_mod._SUBCOMMANDS:
        args = cli_mod._build_parser().parse_args([name, "--timezone", "UTC"])
        assert args.timezone == "UTC", name


def test_reoptimize_is_schedule_only():
    assert parse("--reoptimize").reoptimize is True
    with pytest.raises(SystemExit):
        cli_mod._build_parser().parse_args(["snapshot", "--reoptimize"])


def test_reoptimize_zeroes_the_settle_window(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli_mod, "reconcile",
        lambda settings, **_kw: seen.setdefault("settle", settings.settle_days),
    )
    cli_mod._run_schedule(parse("--reoptimize"), Settings())
    assert seen["settle"] == 0


def test_without_reoptimize_the_settle_window_is_left_alone(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli_mod, "reconcile",
        lambda settings, **_kw: seen.setdefault("settle", settings.settle_days),
    )
    cli_mod._run_schedule(parse(), Settings(settle_days=4))
    assert seen["settle"] == 4


# ---------------------------------------------------------------------------
# backfill --since
# ---------------------------------------------------------------------------

def test_a_since_date_is_parsed():
    args = cli_mod._build_parser().parse_args(["backfill", "--since", "2026-06-01"])
    assert (args.since.year, args.since.month, args.since.day) == (2026, 6, 1)


def test_since_defaults_to_unset():
    assert cli_mod._build_parser().parse_args(["backfill"]).since is None


@pytest.mark.parametrize("value", ["yesterday", "01-06-2026", "2026-13-01"])
def test_a_bad_since_date_is_a_usage_error(value):
    with pytest.raises(SystemExit):
        cli_mod._build_parser().parse_args(["backfill", "--since", value])


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

def review_args(*argv: str):
    return cli_mod._build_parser().parse_args(["review", *argv])


def test_review_defaults_to_the_current_week_in_the_terminal():
    args = review_args()
    assert (args.kind, args.last, args.fmt) == ("week", 0, "terminal")
    assert args.sections is None


def test_month_and_week_are_mutually_exclusive():
    assert review_args("--month").kind == "month"
    with pytest.raises(SystemExit):
        review_args("--week", "--month")


def test_sections_accumulate():
    args = review_args("--section", "capacity", "--section", "flow")
    assert args.sections == ["capacity", "flow"]


def test_an_unknown_section_is_a_usage_error():
    with pytest.raises(SystemExit):
        review_args("--section", "vibes")


def test_an_unknown_format_is_a_usage_error():
    with pytest.raises(SystemExit):
        review_args("--format", "pdf")


def test_the_parsers_section_choices_match_the_registry():
    # The names are duplicated in `cli.py` so that building the parser
    # doesn't import the review package. That's only safe if they agree.
    from task_gcal.review import section_keys

    assert set(cli_mod._SECTION_CHOICES) == set(section_keys())


def test_the_parsers_format_choices_match_the_renderers():
    from task_gcal.review.render import FORMATS

    assert set(cli_mod._FORMAT_CHOICES) == set(FORMATS)


def test_the_default_backfill_window_matches_the_importer():
    from task_gcal.backfill import DEFAULT_BACKFILL_DAYS

    assert cli_mod.DEFAULT_BACKFILL_DAYS == DEFAULT_BACKFILL_DAYS


def test_the_default_checkin_window_matches_the_episode_finder():
    from task_gcal.review.episodes import DEFAULT_SINCE_DAYS

    assert cli_mod.DEFAULT_CHECKIN_DAYS == DEFAULT_SINCE_DAYS


def test_triage_reflect_and_all_are_review_flags():
    args = review_args("--triage", "--reflect", "--all")
    assert (args.triage, args.reflect, args.all_sections) == (True, True, True)


def test_review_flags_default_to_off():
    args = review_args()
    assert (args.triage, args.reflect, args.all_sections) == (False, False, False)


# ---------------------------------------------------------------------------
# checkin and doctor
# ---------------------------------------------------------------------------

def test_checkin_accepts_a_since_date():
    args = cli_mod._build_parser().parse_args(
        ["checkin", "--since", "2026-08-01"]
    )
    assert args.since.month == 8


def test_doctor_takes_no_arguments_of_its_own():
    args = cli_mod._build_parser().parse_args(["doctor"])
    assert callable(args.func)


# ---------------------------------------------------------------------------
# The fast path stays fast
# ---------------------------------------------------------------------------

def test_every_command_the_readme_documents_still_parses():
    """Catch the README drifting from the parser.

    Renaming a flag and forgetting the docs is the easiest way to leave a
    user following instructions that don't work, and it's invisible to every
    other test here.
    """
    import re
    from pathlib import Path

    readme = Path(__file__).resolve().parent.parent / "README.md"
    # Whole lines that are an invocation, with any trailing `# comment`
    # dropped. Lines continued with a backslash are skipped: only their first
    # fragment is on the line, so parsing it would fail for the wrong reason.
    invocation = re.compile(r"^ *task-gcal((?: +[^\s#]+)*) *(?:#.*)?$", re.M)
    documented = sorted(
        {
            args
            for line, args in (
                (m.group(0), m.group(1)) for m in invocation.finditer(readme.read_text())
            )
            if not line.rstrip().endswith("\\")
        }
    )
    assert len(documented) > 10, "the README stopped documenting commands"

    for argv in documented:
        tokens = argv.split()
        try:
            cli_mod._build_parser().parse_args(cli_mod._normalize_argv(tokens))
        except SystemExit:  # pragma: no cover - only on a real regression
            raise AssertionError(f"README documents `task-gcal {argv}`") from None


def test_scheduling_never_imports_the_review_code():
    # "Bare `task-gcal` never loads historical analytics or reporting code."
    # A stray module-level import in cli.py would break it silently, so the
    # check is a subprocess with a clean interpreter.
    import subprocess
    import sys

    probe = (
        "import sys; import task_gcal.cli as cli;"
        "cli._build_parser();"
        "leaked=[m for m in sys.modules if m.startswith('task_gcal.review')];"
        "print(leaked)"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]", out.stdout


# ---------------------------------------------------------------------------
# Hour validation
# ---------------------------------------------------------------------------

def test_work_end_24_is_accepted():
    assert parse("--work-end", "24").work_end_hour == 24


@pytest.mark.parametrize("value", ["25", "-1", "nine"])
def test_a_bad_work_end_is_a_usage_error(value, capsys):
    with pytest.raises(SystemExit) as exc:
        parse("--work-end", value)
    assert exc.value.code == 2
    assert "--work-end" in capsys.readouterr().err


def test_work_start_24_is_a_usage_error():
    # A start hour of midnight-tomorrow is meaningless.
    with pytest.raises(SystemExit):
        parse("--work-start", "24")


def test_work_start_0_is_fine():
    assert parse("--work-start", "0").work_start_hour == 0


@pytest.mark.parametrize("value", ["7", "-1", "monday"])
def test_a_bad_work_days_list_is_a_usage_error(value):
    with pytest.raises(SystemExit):
        parse("--work-days", value)


def test_an_unknown_visibility_is_a_usage_error():
    with pytest.raises(SystemExit):
        parse("--event-visibility", "semi-public")


def test_visibility_choices_are_accepted():
    for choice in ("default", "public", "private", "confidential"):
        assert parse("--event-visibility", choice).event_visibility == choice
