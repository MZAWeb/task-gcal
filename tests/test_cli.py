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
    return cli_mod._build_parser().parse_args(list(argv))


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
