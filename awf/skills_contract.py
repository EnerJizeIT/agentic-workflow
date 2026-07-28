"""BD-16: per-role pipeline contracts.

Each role in a pipeline has a *contract*: what it receives from the previous
stage, what it must produce for the next stage, and its zone of responsibility
(what is in scope and what is explicitly out of scope).

Without a contract, a role like ``system-analysis`` may execute the entire
TODO itself because nothing tells it "your zone is requirements, not code".
The contract is injected into the role's local skill at normalize time so
the agent sees it on every run.

Usage:

    from awf.skills_contract import render_pipeline_contract, DEFAULT_ROLE_CONTRACTS

    md = render_pipeline_contract(
        role="developer",
        position=2,
        total=5,
        prev_role="system-analysis",
        next_role="qa",
    )

Unknown roles fall back to a generic contract ("do your part, hand off to next").
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleContract:
    """Static contract for a role archetype."""

    zone: str
    receives_hint: str
    produces_hint: str
    out_of_scope: tuple[str, ...]


# Registry of well-known role archetypes. Keys are role slugs (matches
# .agentic/roles/<slug>.md and the `role:` field in pipeline.yaml).
DEFAULT_ROLE_CONTRACTS: dict[str, RoleContract] = {
    "system-analysis": RoleContract(
        zone=(
            "Decompose the TODO goal into structured requirements. Identify "
            "edge cases, ambiguities, missing context. Write a clear "
            "specification the next role can implement against."
        ),
        receives_hint="TODO goal + handoff from supervisor (or none if first).",
        produces_hint=(
            "Requirements document in your handoff: explicit inputs/outputs, "
            "edge cases, acceptance criteria. NO production code."
        ),
        out_of_scope=(
            "Writing production code (that's the developer's job).",
            "Writing tests (qa's job).",
            "Final audit (project-auditor's job).",
        ),
    ),
    "developer": RoleContract(
        zone=(
            "Implement the requirements from the previous role's handoff. "
            "Write production code that satisfies the spec. Keep changes "
            "minimal and focused."
        ),
        receives_hint="Requirements/spec from system-analysis handoff + TODO goal.",
        produces_hint=(
            "Working implementation in the project source files + handoff "
            "describing what was built, files touched, known gaps."
        ),
        out_of_scope=(
            "Writing comprehensive tests (qa's job — but you may write smoke tests).",
            "Re-specifying requirements (system-analysis's job).",
            "Audit / holistic review (project-auditor's job).",
        ),
    ),
    "qa": RoleContract(
        zone=(
            "Review the developer's diff. Find bugs. Write regression tests "
            "that lock in the behaviour. Fix obvious bugs directly; escalate "
            "ambiguous ones in your handoff."
        ),
        receives_hint="Implementation from developer handoff + TODO goal.",
        produces_hint=(
            "Test files added to the project + bug fixes + handoff describing "
            "test coverage added and any unresolved issues."
        ),
        out_of_scope=(
            "New features beyond what's needed to test/fix (developer's job).",
            "Re-specifying (system-analysis's job).",
            "Strategic audit (project-auditor's job).",
        ),
    ),
    "project-auditor": RoleContract(
        zone=(
            "Holistic audit of everything done so far in this TODO. Look for "
            "design issues, security concerns, maintainability, integration "
            "risks. Report findings; do not implement new features."
        ),
        receives_hint="All previous handoffs (system-analysis + developer + qa) + TODO goal.",
        produces_hint=(
            "Audit report in handoff: findings by severity (CRITICAL/HIGH/MEDIUM/LOW), "
            "specific file:line references, recommendations."
        ),
        out_of_scope=(
            "Implementing fixes for findings you report (escalate to supervisor).",
            "Adding new features.",
            "Re-running tests (qa's job).",
        ),
    ),
    "reviewer": RoleContract(
        zone=(
            "Code review of the developer's diff. Focus on correctness, "
            "readability, conventions. Approve / request changes."
        ),
        receives_hint="Implementation from developer handoff.",
        produces_hint="Review verdict in handoff: APPROVED or CHANGES_REQUESTED with specifics.",
        out_of_scope=(
            "Writing tests (qa's job).",
            "Implementation (developer's job).",
        ),
    ),
    "tester": RoleContract(
        zone="Write and run automated tests for the current TODO's deliverable.",
        receives_hint="Implementation from developer handoff.",
        produces_hint="Test files + test run report in handoff.",
        out_of_scope=(
            "Implementation (developer's job).",
            "Strategic audit (project-auditor's job).",
        ),
    ),
    "worker": RoleContract(
        zone=(
            "Generic executor. Use the TODO + previous handoffs to do your "
            "part. If you are the first/only role in the pipeline, treat the "
            "TODO as the full task."
        ),
        receives_hint="TODO + any previous handoffs.",
        produces_hint="Implementation + handoff describing what was done.",
        out_of_scope=(
            "Anything outside what the TODO declares as the goal.",
            "Refactoring unrelated code.",
        ),
    ),
}

_GENERIC_CONTRACT = RoleContract(
    zone=(
        "Do your part of the TODO based on the previous role's handoff and "
        "your role.md instructions."
    ),
    receives_hint="TODO + any previous handoffs.",
    produces_hint=(
        "Your contribution + a handoff at .agentic/handoff/<your-role>.md "
        "describing what you did and what the next role should pick up."
    ),
    out_of_scope=(
        "Work that the next role is supposed to do (see pipeline contract).",
        "Redoing work already visible in previous handoffs.",
    ),
)


def get_role_contract(role: str) -> RoleContract:
    """Return the contract for a role, falling back to a generic one."""
    return DEFAULT_ROLE_CONTRACTS.get(role, _GENERIC_CONTRACT)


def render_pipeline_contract(
    role: str,
    position: int,
    total: int,
    prev_role: str | None = None,
    next_role: str | None = None,
) -> str:
    """Render the ``## Pipeline contract`` markdown section for a role.

    Args:
        role: this stage's role slug.
        position: 1-based position in the pipeline.
        total: total number of stages in the pipeline.
        prev_role: previous stage's role (None if first or after supervisor plan).
        next_role: next stage's role (None if last or before supervisor verify).

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
        lines.append(f"**Receives from:** supervisor TODO (you are the first agent stage) — {c.receives_hint}")
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
        "When done, write `.agentic/handoff/" + role + ".md` describing what "
        "you did, what the next role should pick up, and any open questions."
    )
    return "\n".join(lines)
