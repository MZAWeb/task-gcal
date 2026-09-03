"""Tests for `task-gcal doctor`.

Two things matter: it never modifies anything, and every failure names the fix
rather than just the symptom. A diagnostic that only says "failed" moves the
work instead of doing it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from task_gcal import doctor as doctor_mod
from task_gcal import journal
from task_gcal.config import Settings

from conftest import NOW, a_task


@pytest.fixture
def health(monkeypatch, capsys, tmp_path, isolated_journal):
    """`doctor` with every external dependency faked."""

    class Health:
        def __init__(self) -> None:
            self.settings = Settings(timezone="UTC")
            self.tasks = [a_task(uuid="a", estimate=60)]
            self.task_on_path = True
            self.config_exists = False
            self.credentials = True
            self.token = True

        def configure(self, **kwargs):
            from dataclasses import replace

            self.settings = replace(self.settings, **kwargs)
            return self

        def run(self, *, now=None):
            code = doctor_mod.run(self.settings, now=now or NOW)
            self.out = capsys.readouterr().out
            self.code = code
            return code

        def line(self, name: str) -> str:
            for line in self.out.splitlines():
                if name in line:
                    return line
            return ""

    h = Health()

    def fake_which(_name):
        return "/usr/bin/task" if h.task_on_path else None

    def fake_load(report, **_kwargs):
        return h.tasks

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("task_gcal.taskw.load_next_tasks", fake_load)
    monkeypatch.setattr("task_gcal.doctor.USER_CONFIG_PATH", tmp_path / "none.toml")

    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    token = tmp_path / "token.json"
    token.write_text("{}")
    token.chmod(0o600)
    monkeypatch.setattr("task_gcal.doctor.CREDENTIALS_PATH", creds)
    monkeypatch.setattr("task_gcal.doctor.TOKEN_PATH", token)
    h.paths = {"credentials": creds, "token": token}
    return h


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_healthy_setup_passes(health):
    journal.append(
        journal.build_record(
            settings=health.settings,
            mode=journal.MODE_SNAPSHOT,
            at=NOW,
            observations=(),
        )
    )
    assert health.run() == 0


def test_every_check_is_reported(health):
    health.run()
    for name in (
        "config", "timezone", "taskwarrior", "google auth", "journal",
        "check-ins",
    ):
        assert health.line(name), name


def test_doctor_writes_nothing(health, isolated_journal):
    health.run()
    assert journal.load().records == []
    assert not (isolated_journal / "reflections.jsonl").exists()


# ---------------------------------------------------------------------------
# Failures name their fix
# ---------------------------------------------------------------------------

def test_a_missing_taskwarrior_is_a_failure_with_a_fix(health):
    health.task_on_path = False
    assert health.run() == 1
    assert "FAIL" in health.line("taskwarrior")
    # cron and launchd don't share a shell's PATH, which is the usual cause.
    assert "PATH" in health.out


def test_an_empty_report_is_a_warning_not_a_failure(health):
    # It might be right. It's usually a wrong report name or a stray context,
    # and scheduling already refuses to clear the calendar when it happens.
    health.tasks = []
    assert health.run() == 0
    assert "warn" in health.line("taskwarrior")
    assert "context" in health.out


def test_no_estimates_at_all_is_a_warning_about_the_uda_name(health):
    health.tasks = [a_task(uuid="a", estimate=None)]
    health.run()
    assert "warn" in health.line("taskwarrior")
    assert "really called" in health.out


def test_missing_credentials_is_a_failure(health):
    health.paths["credentials"].unlink()
    assert health.run() == 1
    assert "--setup" in health.out


def test_credentials_without_a_token_is_a_warning(health):
    health.paths["token"].unlink()
    assert health.run() == 0
    assert "warn" in health.line("google auth")


def test_a_world_readable_token_is_a_warning_with_the_chmod(health):
    health.paths["token"].chmod(0o644)
    health.run()
    assert "warn" in health.line("google auth")
    assert "chmod 600" in health.out


def test_impossible_working_hours_are_a_failure(health):
    health.configure(work_start_hour=18, work_end_hour=9)
    assert health.run() == 1
    assert "no slot can ever fit" in health.out


def test_an_unknown_timezone_is_a_failure(health):
    health.configure(timezone="Mars/Olympus_Mons")
    assert health.run() == 1
    assert "IANA" in health.out


# ---------------------------------------------------------------------------
# Journal health
# ---------------------------------------------------------------------------

def test_an_empty_journal_says_how_to_seed_it(health):
    health.run()
    assert "warn" in health.line("journal")
    assert "backfill" in health.out


def test_a_disabled_journal_is_a_warning(health):
    health.configure(journal_detail="off")
    health.run()
    assert "warn" in health.line("journal")
    assert "minimal" in health.out


def test_sparse_sampling_is_flagged_as_a_lower_bound(health):
    journal.append(
        journal.build_record(
            settings=health.settings,
            mode=journal.MODE_SNAPSHOT,
            at=NOW,
            observations=(),
        )
    )
    health.run()
    assert "lower bound" in health.out


def test_dense_sampling_passes(health):
    for day in range(10):
        journal.append(
            journal.build_record(
                settings=health.settings,
                mode=journal.MODE_SNAPSHOT,
                at=NOW - timedelta(days=day),
                observations=(),
            )
        )
    health.run()
    assert " ok " in health.line("journal")


def test_unreadable_journal_lines_are_surfaced(health, isolated_journal):
    journal.append(
        journal.build_record(
            settings=health.settings,
            mode=journal.MODE_SNAPSHOT,
            at=NOW,
            observations=(),
        )
    )
    path = next((isolated_journal / "runs").glob("*.jsonl"))
    path.write_text("{broken}\n" + path.read_text())

    health.run()
    assert "unreadable" in health.out


# ---------------------------------------------------------------------------
# Notable-but-fine settings
# ---------------------------------------------------------------------------

def test_a_disabled_removal_guard_is_mentioned(health):
    health.configure(removal_guard_ratio=1.0).run()
    assert "removal guard disabled" in health.out


def test_disabled_stability_is_mentioned(health):
    health.configure(settle_days=0).run()
    assert "stability off" in health.out


def test_an_empty_work_week_is_a_failure(health):
    health.configure(work_days=frozenset()).run()
    assert health.code == 1
    assert "nowhere to schedule" in health.out
