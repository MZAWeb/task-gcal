"""Settings loading, precedence, and the per-task override mini-language."""

from __future__ import annotations

import dataclasses

import pytest

from task_gcal import config as config_mod
from task_gcal.config import (
    Settings,
    apply_overrides,
    coerce_attendees,
    coerce_work_days,
    load_settings,
    parse_task_overrides,
)


# ---------------------------------------------------------------------------
# Settings basics
# ---------------------------------------------------------------------------

def test_settings_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Settings().work_start_hour = 7  # type: ignore[misc]


def test_defaults_are_the_documented_ones():
    s = Settings()
    assert (s.work_start_hour, s.work_end_hour) == (9, 18)
    assert s.work_days == frozenset({0, 1, 2, 3, 4})
    assert s.slot_align_minutes == 15
    assert s.buffer_minutes == 0
    assert s.estimate_uda == "estimate"
    assert s.override_uda == "gcal"
    assert s.calendar_id == "primary"
    assert s.overdue_horizon_days == 30
    assert s.lookback_days == 7
    assert s.removal_guard_ratio == 0.5
    assert s.attendees == ()


def test_resolve_timezone_uses_the_configured_zone():
    tz = Settings(timezone="Europe/London").resolve_timezone()
    assert str(tz) == "Europe/London"


def test_resolve_timezone_falls_back_to_local():
    assert Settings(timezone=None).resolve_timezone() is not None


def test_unknown_timezone_exits_with_a_message():
    with pytest.raises(SystemExit) as exc:
        Settings(timezone="Mars/Olympus_Mons").resolve_timezone()
    assert "Mars/Olympus_Mons" in str(exc.value)


# ---------------------------------------------------------------------------
# config.toml
# ---------------------------------------------------------------------------

def _write_config(monkeypatch, tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body)
    monkeypatch.setattr(config_mod, "USER_CONFIG_PATH", path)
    return path


def test_missing_config_file_gives_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "USER_CONFIG_PATH", tmp_path / "nope.toml")
    assert load_settings() == Settings()


def test_config_file_overrides_defaults(monkeypatch, tmp_path):
    _write_config(
        monkeypatch,
        tmp_path,
        """
        work_start_hour = 8
        work_end_hour = 16
        work_days = [0, 2, 4]
        buffer_minutes = 10
        estimate_uda = "est"
        calendar_id = "work@group.calendar.google.com"
        timezone = "Europe/London"
        removal_guard_ratio = 0.9
        """,
    )
    s = load_settings()
    assert (s.work_start_hour, s.work_end_hour) == (8, 16)
    assert s.work_days == frozenset({0, 2, 4})
    assert s.buffer_minutes == 10
    assert s.estimate_uda == "est"
    assert s.calendar_id == "work@group.calendar.google.com"
    assert s.timezone == "Europe/London"
    assert s.removal_guard_ratio == 0.9


def test_partial_config_keeps_other_defaults(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "buffer_minutes = 5\n")
    s = load_settings()
    assert s.buffer_minutes == 5
    assert s.work_start_hour == Settings().work_start_hour


def test_unknown_config_keys_are_ignored(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, 'nonsense = "yes"\nbuffer_minutes = 5\n')
    assert load_settings().buffer_minutes == 5


def test_empty_timezone_string_means_local(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, 'timezone = ""\n')
    assert load_settings().timezone is None


def test_attendees_are_never_read_from_the_config_file(monkeypatch, tmp_path):
    # Deliberate: you don't want every task event to invite the same people.
    _write_config(monkeypatch, tmp_path, 'attendees = ["a@b.com"]\n')
    assert load_settings().attendees == ()


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

def test_apply_overrides_ignores_none_values():
    base = Settings(buffer_minutes=10)
    assert apply_overrides(base, {"buffer_minutes": None}).buffer_minutes == 10


def test_apply_overrides_wins_over_the_base():
    base = Settings(buffer_minutes=10)
    assert apply_overrides(base, {"buffer_minutes": 0}).buffer_minutes == 0


def test_apply_overrides_with_nothing_returns_the_same_object():
    base = Settings()
    assert apply_overrides(base, {"buffer_minutes": None}) is base


# ---------------------------------------------------------------------------
# coerce_work_days
# ---------------------------------------------------------------------------

def test_work_days_accepts_commas_and_spaces():
    assert coerce_work_days("0,2, 4") == frozenset({0, 2, 4})
    assert coerce_work_days("1 3") == frozenset({1, 3})


def test_work_days_deduplicates():
    assert coerce_work_days("1,1,1") == frozenset({1})


@pytest.mark.parametrize("raw", ["", "   ", ",,"])
def test_work_days_rejects_empty(raw):
    with pytest.raises(ValueError, match="empty"):
        coerce_work_days(raw)


@pytest.mark.parametrize("raw", ["7", "-1", "0,9"])
def test_work_days_rejects_out_of_range(raw):
    with pytest.raises(ValueError, match="out of range"):
        coerce_work_days(raw)


def test_work_days_rejects_non_numbers():
    with pytest.raises(ValueError):
        coerce_work_days("monday")


# ---------------------------------------------------------------------------
# coerce_attendees
# ---------------------------------------------------------------------------

def test_attendees_parsed_and_trimmed():
    assert coerce_attendees(" a@b.com , c@d.org ") == ("a@b.com", "c@d.org")


def test_attendees_deduplicate_preserving_order():
    assert coerce_attendees("b@x.com,a@x.com,b@x.com") == ("b@x.com", "a@x.com")


@pytest.mark.parametrize("raw", ["nope", "a@b", "a b@c.com", "@b.com", "a@@b.com"])
def test_attendees_reject_malformed_addresses(raw):
    with pytest.raises(ValueError):
        coerce_attendees(raw)


def test_attendees_reject_empty_list():
    with pytest.raises(ValueError, match="no email"):
        coerce_attendees(" , ")


# ---------------------------------------------------------------------------
# parse_task_overrides
# ---------------------------------------------------------------------------

def test_parses_several_pairs():
    assert parse_task_overrides("work_end_hour=20 buffer_minutes=0") == {
        "work_end_hour": 20,
        "buffer_minutes": 0,
    }


def test_parses_a_list_value():
    assert parse_task_overrides("work_days=0,2,4") == {
        "work_days": frozenset({0, 2, 4})
    }


def test_parses_attendees():
    assert parse_task_overrides("attendees=a@b.com,c@d.com") == {
        "attendees": ("a@b.com", "c@d.com")
    }


def test_empty_override_string_is_no_overrides():
    assert parse_task_overrides("   ") == {}


def test_later_pair_wins_on_repeat():
    assert parse_task_overrides("buffer_minutes=5 buffer_minutes=9") == {
        "buffer_minutes": 9
    }


def test_missing_equals_is_an_error():
    with pytest.raises(ValueError, match="expected key=value"):
        parse_task_overrides("work_end_hour")


def test_unknown_key_is_an_error():
    with pytest.raises(ValueError, match="unknown or non-per-task"):
        parse_task_overrides("wrok_end_hour=20")


@pytest.mark.parametrize(
    "key", ["calendar_id", "timezone", "report", "estimate_uda", "lookback_days",
            "override_uda", "event_visibility", "removal_guard_ratio"]
)
def test_run_global_keys_are_rejected_per_task(key):
    with pytest.raises(ValueError, match="non-per-task"):
        parse_task_overrides(f"{key}=x")


def test_bad_value_names_the_key():
    with pytest.raises(ValueError, match="bad value for work_end_hour"):
        parse_task_overrides("work_end_hour=eight")


def test_overridable_keys_are_exactly_the_documented_set():
    # A reminder to update the README if this list grows.
    assert set(config_mod._TASK_OVERRIDE_COERCE) == {
        "work_start_hour",
        "work_end_hour",
        "work_days",
        "slot_align_minutes",
        "buffer_minutes",
        "event_color_id",
        "overdue_horizon_days",
        "attendees",
    }


def test_every_overridable_key_is_a_real_settings_field():
    fields = {f.name for f in dataclasses.fields(Settings)}
    assert set(config_mod._TASK_OVERRIDE_COERCE) <= fields
