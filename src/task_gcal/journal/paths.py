"""Where the journal lives on disk.

Config and data are deliberately separate: `~/.config/task-gcal` holds
things you write (credentials, config.toml), `~/.local/share/task-gcal`
holds things we write and you can delete without losing setup.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


def data_dir() -> Path:
    """The journal's root directory, honoring XDG.

    `TASK_GCAL_DATA_DIR` overrides everything, which is what the tests use
    and what a second profile would use.
    """
    override = os.environ.get("TASK_GCAL_DATA_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "task-gcal"


def runs_dir() -> Path:
    return data_dir() / "runs"


def run_file(when: datetime) -> Path:
    """The monthly file a record timestamped `when` belongs in.

    Monthly files keep any single file small enough to read whole and make
    "which months do I have?" a directory listing. The name is always UTC,
    so a record never lands in two different months depending on the reader's
    zone.
    """
    return runs_dir() / f"{when.astimezone(timezone.utc):%Y-%m}.jsonl"


def month_key(path: Path) -> str:
    """The `YYYY-MM` a run file covers, for range filtering."""
    return path.stem


def ensure_private(path: Path) -> None:
    """Create `path` as a 0700 directory, tightening it if it exists.

    Task titles and deadlines are sensitive personal data, so the journal is
    never group- or world-readable.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
