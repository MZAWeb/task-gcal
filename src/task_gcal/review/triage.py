"""`task-gcal review --triage`: print the commands, run nothing.

The invariant this preserves is worth more than the convenience it gives up:
**this tool never writes to Taskwarrior.** Triage lists each stagnant task
with the exact invocations that would resolve it, and you paste the ones you
agree with.

That also makes it safe to run casually — there is no confirmation to misclick
and no `--dry-run` to forget — and it keeps Taskwarrior the source of truth
rather than something this program has opinions about.
"""

from __future__ import annotations

from .stagnation import find, prescribe

# Kept out of the copyable command lines so a blind paste of the whole block
# can't run a comment as a command on some shell.
_COMMENT_COLUMN = 34


def render(facts) -> str:
    entries = find(
        list(facts.tasks),
        timelines=facts.timelines,
        blocks_by_task=facts.blocks_by_task(),
        now=facts.now,
    )
    if not entries:
        return (
            "Nothing is stagnant: no open task has enough evidence against it "
            "to need a decision.\n"
        )

    lines = [f"Stagnant ({len(entries)}):"]
    for entry in entries:
        lines.append(f"  {entry.summary}")
        for command, why in prescribe(entry).commands:
            lines.append(f"      {command.ljust(_COMMENT_COLUMN)}# {why}")
        lines.append("")
    lines.append("Nothing above has been run. Paste the ones you agree with.")
    return "\n".join(lines) + "\n"
