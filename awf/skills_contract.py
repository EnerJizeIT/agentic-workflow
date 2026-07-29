"""BD-16/29: per-role pipeline contracts.

Generic contract only — no hardcoded role registry. User can pick any skill
for any role, awf doesn't second-guess what the role should do. It only
tells the role its position in the pipeline and that it should make a
contribution.

The contract is printed by the normalize_skills stage as guidance. The
role decides how to interpret it based on its skill content.

Usage:

    from awf.skills_contract import render_pipeline_contract

    md = render_pipeline_contract(
        role="data-scientist",  # any role name
        position=2,
        total=5,
        prev_role="system-analyst",
        next_role="qa",
    )
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleContract:
    """Generic contract — applies to every role."""

    zone: str
    receives_hint: str
    produces_hint: str
    out_of_scope: tuple[str, ...]


# Generic contract — used for every role regardless of name.
_GENERIC_CONTRACT = RoleContract(
    zone=(
        "Do your part of the TODO based on the previous role's handoff and "
        "your role.md / skill instructions. You see the TODO goal and what "
        "previous roles have already done (via handoff files). Add YOUR "
        "contribution — don't redo prior work, don't do future work that "
        "isn't in your skill's zone."
    ),
    receives_hint="TODO goal + handoffs from previous roles (if any).",
    produces_hint=(
        "Your contribution + PROGRESS-{TODO-ID}.md (running notes) + "
        "DONE-{TODO-ID}.md (final summary) + DONE-{TODO-ID}.ready sentinel. "
        "Orchestrator compiles these into a handoff for the next role."
    ),
    out_of_scope=(
        "Redoing work visible in previous handoffs (waste).",
        "Work outside the TODO goal's scope.",
        "Refactoring unrelated code.",
    ),
)


def get_role_contract(role: str) -> RoleContract:
    """Return the contract for a role.

    BD-29: every role gets the generic contract. There is no role-specific
    registry — user can invent any role, it works the same way.
    """
    return _GENERIC_CONTRACT


def render_pipeline_contract(
    role: str,
    position: int,
    total: int,
    prev_role: str | None = None,
    next_role: str | None = None,
) -> str:
    """Render the ``## Pipeline contract`` markdown section for a role.

    Args:
        role: this stage's role slug (any string).
        position: 1-based position in the pipeline.
        total: total number of stages in the pipeline.
        prev_role: previous stage's role (None if first agent stage).
        next_role: next stage's role (None if last agent stage).

    Returns:
        Markdown text (without leading ``##`` — caller decides heading level).
    """
    c = get_role_contract(role)
    lines: list[str] = [
        f"**Position:** Stage {position} of {total} (role: `{role}`)",
    ]
    if prev_role:
        lines.append(f"**Receives from:** `{prev_role}` — {c.receives_hint}")
    else:
        lines.append(
            f"**Receives from:** supervisor plan (you are the first agent stage) — {c.receives_hint}"
        )
    if next_role:
        lines.append(f"**Produces for:** `{next_role}` — {c.produces_hint}")
    else:
        lines.append(
            f"**Produces for:** supervisor verify (you are the last agent stage) — {c.produces_hint}"
        )
    lines.append("")
    lines.append(f"**Zone of responsibility:** {c.zone}")
    lines.append("")
    lines.append("**Out of scope (do NOT do):**")
    for item in c.out_of_scope:
        lines.append(f"- {item}")
    lines.append("")
    lines.append(
        "**How to report your work:** write your running notes to "
        "`outbox/PROGRESS-{TODO-ID}.md` during the task. When finished, write "
        "TWO files: (1) `outbox/DONE-{TODO-ID}.md` with your summary, and "
        "(2) an empty sentinel file `outbox/DONE-{TODO-ID}.ready` (NOTE: "
        "`.ready` extension, NOT `.md.ready` — the orchestrator watches for "
        "exactly this filename). Example for TODO-0042: `outbox/DONE-TODO-0042.md` "
        "+ `outbox/DONE-TODO-0042.ready`. The orchestrator compiles these into "
        "a handoff file for the next role automatically — do NOT write handoff "
        "files yourself."
    )
    return "\n".join(lines)
