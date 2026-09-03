"""The bulk-removal guard.

Its own module because it is the one thing standing between a bad input and
a wiped calendar, and because being pure makes it testable without a
calendar at all.
"""

from __future__ import annotations

from typing import Optional

# Below this many removals a run is never blocked: clearing a couple of
# finished or duplicated events is routine, and a fresh calendar shouldn't
# need `--force` on its first cleanup.
_REMOVAL_GUARD_FLOOR = 3


def removal_guard_error(
    *, removals: int, owned_unfinished: int, ratio: float
) -> Optional[str]:
    """Explain why a run should refuse to remove this many events, or None.

    One bad input can make every task look unschedulable and turn a normal
    run into a mass deletion: a task source that returns nothing (wrong
    report name, an active Taskwarrior context, `TASKDATA` pointing at
    another replica), or a mistyped `--estimate-uda` so no task has an
    estimate. Removing a few of our events is routine; removing most of what
    we own means the *input* is wrong, not the calendar.

    `ratio` is the share of our unfinished events a run may remove; 1.0
    disables the guard, since a run can never remove more than all of them.
    """
    if owned_unfinished <= 0 or removals < _REMOVAL_GUARD_FLOOR:
        return None
    if removals <= ratio * owned_unfinished:
        return None
    return (
        f"refusing to remove {removals} of {owned_unfinished} unfinished "
        f"events we own ({removals / owned_unfinished:.0%}; the guard trips "
        f"above {ratio:.0%}).\n"
        "  An unusually large cleanup usually means the input is wrong, not "
        "the calendar.\n"
        "  Check the report/UDA names above, then re-run with --force to "
        "remove them anyway\n"
        "  (or raise removal_guard_ratio in config.toml)."
    )
