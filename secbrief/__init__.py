"""Register the secbrief operations skill only.

This plugin deliberately provides no tools, hooks, commands, scheduling, network
access, or remediation code. The agent operates the existing local secbrief CLI
through ordinary terminal commands described by the skill.
"""

from pathlib import Path

SKILL_NAME = "secbrief-operations"
SKILL_DESCRIPTION = (
    "Operate an existing local secbrief checkout for one-shot dependency inventory, "
    "OSV/KEV matching, report review, and health status. Covers explicit consent, "
    "coverage unknowns, nochange-versus-failed handling, and host-owned scheduling. "
    "Never updates, restarts, or remediates automatically."
)


def register(ctx):
    skill_md = Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"
    ctx.register_skill(SKILL_NAME, skill_md, SKILL_DESCRIPTION)
