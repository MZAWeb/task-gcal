"""`task-gcal snapshot`: observe, change nothing.

Almost every number a review wants is a diff between two observations, so
fidelity depends on how often we look. Making that a scheduled job rather
than a side effect of scheduling is the point: it turns "a lower bound of
unknown tightness" into "sampled every six hours", and it decouples history
from the habit of running `schedule`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import Settings
from .gcal import GCal
from .journal import MODE_SNAPSHOT, ObservedBlock, record_run
from .placement import pick_keepers
from .taskw import load_next_tasks


def snapshot(settings: Settings) -> int:
    """Append one journal record and touch nothing else.

    Safe to run from cron or launchd: it makes no calendar writes at all,
    and a failed journal write warns rather than raising.
    """
    now = datetime.now(timezone.utc)
    tasks = load_next_tasks(
        settings.report,
        estimate_uda=settings.estimate_uda,
        override_uda=settings.override_uda,
    )
    gcal = GCal(settings)
    existing = gcal.list_scheduler_events(
        time_min=now - timedelta(days=settings.lookback_days),
        time_max=now + timedelta(days=400),
    )
    keepers = pick_keepers(existing, now)

    # An empty task source is the same ambiguity `reconcile` guards against:
    # usually a wrong report name or a stray Taskwarrior context, not an
    # empty backlog. Nothing is deleted here, so the record is still written
    # — but flagged, so a review doesn't read it as an observed day on which
    # you happened to have no work.
    source_ok = bool(tasks) or not existing

    # `action` stays unset: this run observed the blocks, it didn't decide
    # them, and "unchanged" would be a claim it hasn't earned.
    written = record_run(
        settings=settings,
        mode=MODE_SNAPSHOT,
        at=now,
        tasks=tasks,
        blocks={
            uuid: ObservedBlock(start=ev.start, end=ev.end)
            for uuid, ev in keepers.items()
        },
        source_ok=source_ok,
    )

    if not written:
        print('Journal is off (journal_detail = "off"); nothing recorded.')
        return 0

    task_uuids = {t.uuid for t in tasks}
    with_block = sum(1 for uuid in keepers if uuid in task_uuids)
    print(
        f"Observed {len(tasks)} task(s), {with_block} with a block."
        + ("" if source_ok else "  ! task source came back empty")
    )
    return 0
