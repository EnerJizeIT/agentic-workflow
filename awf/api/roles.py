"""Role management API: add_role + analyze_roles + zone analysis logic.

All business logic for role analysis lives here (was in
``awf.cmd_analyze_roles`` — audit T2.3 fixed wrong-direction dependency
where api imported from cmd). ``cmd_analyze_roles.run`` is now a thin
CLI wrapper that imports from this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import config as cfg_mod
from .. import paths
from .._atomic import atomic_write_text
from ._errors import AwfApiError
from ._helpers import require_agentic
from ._results import AddRoleResult, AnalyzeRolesResult
from ._templates import _ROLE_TEMPLATE

# ─── add_role ───────────────────────────────────────────────────────────


def add_role(
    project_dir: Path,
    role_name: str,
    *,
    description: str = "",
    model: str = "",
) -> AddRoleResult:
    """Generate a new role template at ``.agentic/roles/{role_name}.md``."""
    if not role_name:
        raise AwfApiError("role_name is required")
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    if not model:
        model = "<set-me-in-.agentic/config.yaml>"

    roles_dir = project_dir / ".agentic" / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)

    content = _ROLE_TEMPLATE.format(
        role_name=role_name,
        description=description or "new role",
        model=model,
    )
    role_file = roles_dir / f"{role_name}.md"
    atomic_write_text(role_file, content)

    return AddRoleResult(
        role_name=role_name,
        role_file=str(role_file),
        model=model,
    )


# ─── Role analysis internals ────────────────────────────────────────────
#
# Moved here from awf/cmd_analyze_roles.py (audit T2.3 — wrong-direction
# dependency). cmd_*.py is now a thin CLI wrapper, this module owns the
# business logic.


class AnalyzeError(Exception):
    """Raised on analyze_roles_core precondition failures (missing roles dir, etc.).

    Message is user-readable; callers print + return non-zero exit.
    """


@dataclass
class AnalyzeData:
    """Structured result of :func:`analyze_roles_core`.

    All fields are pure data — no print/stdout side effects. Callers
    (cmd_analyze_roles.run for CLI, analyze_roles for MCP) format
    this for their medium.
    """

    zones: dict[str, str] = field(default_factory=dict)
    overlaps: list[tuple[str, str, str]] = field(default_factory=list)
    patches: dict[str, str] = field(default_factory=dict)
    pipeline_roles: list[str] = field(default_factory=list)
    applied: bool = False  # True if at least one patch was written to disk
    failed: list[str] = field(default_factory=list)  # roles whose patch write raised OSError


def _read_role_files(project_dir: Path) -> dict[str, str]:
    """Read all .agentic/roles/*.md → {role_slug: content}."""
    roles_dir = paths.agentic_dir(project_dir) / "roles"
    if not roles_dir.is_dir():
        return {}
    result: dict[str, str] = {}
    for f in sorted(roles_dir.glob("*.md")):
        result[f.stem] = f.read_text(encoding="utf-8")
    return result


def _read_pipeline_roles(project_dir: Path, config: dict) -> list[str]:
    """Return ordered list of agent role slugs from default pipeline.

    Skips supervisor (it's built-in). Returns [] if no pipeline.yaml.
    """
    from ..pipeline import load_stages, resolve_pipeline_file

    try:
        pipeline_file = resolve_pipeline_file(project_dir, None, config)
    except FileNotFoundError:
        return []
    stages = load_stages(pipeline_file)
    return [s.role for s in stages if s.role != "supervisor"]


_ZONES_OF_RESPONSIBILITY = {
    # role-substring → zone label.
    # BD-31a fix: ordered by SPECIFICITY — most specific first.
    # _infer_zone checks slug BEFORE content head to avoid false matches
    # (e.g. system-analyst skill.md contains "implement features" in body,
    # but slug "system-analyst" must win → "writes requirements").
    "system-analyst": "writes requirements / vision",
    "system analyst": "writes requirements / vision",
    "analyst": "writes requirements / vision",
    "аналитик": "writes requirements / vision",
    "architect": "designs system structure",
    "refactor": "improves structure without behavior change",
    "debug": "isolates bugs",
    "security": "security review",
    "performance": "performance investigation",
    "test-automator": "writes/runs tests",
    "tester": "writes/runs tests",
    "тест": "writes/runs tests",
    "project-auditor": "verifies code health / project quality",
    "audit": "verifies code health / project quality",
    "qa-review": "verifies implementation against requirements",
    "qa": "verifies implementation against requirements",
    "review": "verifies implementation against requirements",
    "developer": "writes code",
    "разработчик": "writes code",
    "implement": "writes code",
}


def _infer_zone(role_slug: str, content: str) -> str:
    """Best-effort guess of a role's zone from its slug + content head.

    BD-31a fix: slug match takes PRIORITY over content match. Previously,
    system-analyst matched 'implement' from skill body text before reaching
    'analyst' key, classifying it as 'writes code' instead of requirements.
    """
    slug_lower = role_slug.lower()
    # Pass 1: slug-only (highest confidence)
    for needle, zone in _ZONES_OF_RESPONSIBILITY.items():
        if needle in slug_lower:
            return zone
    # Pass 2: content head (fallback for roles with generic slug)
    head = content[:600].lower()
    for needle, zone in _ZONES_OF_RESPONSIBILITY.items():
        if needle in head:
            return zone
    return "generalist (undefined zone)"


def _zone_family(zone: str) -> str:
    """BD-31c fix: group zones into families for broader overlap detection.

    'verifies implementation against requirements' and
    'verifies code health / project quality' are different zones, but both
    are 'verify' family → potential overlap in pipeline.
    """
    if zone.startswith("verifies"):
        return "verify"
    if zone.startswith("writes code"):
        return "code"
    if zone.startswith("writes/runs tests"):
        return "test"
    if zone.startswith("writes requirements"):
        return "requirements"
    return zone  # unique zones are their own family


def _detect_overlaps(roles_with_zones: dict[str, str]) -> list[tuple[str, str, str]]:
    """Find pairs of roles whose zones overlap.

    BD-31c fix: uses _zone_family for broader matching. Two roles with
    different verify zones (qa-review + project-auditor) still overlap
    because both do verification work — pipeline risks duplicate effort.

    Returns list of (role_a, role_b, shared_zone) tuples.
    """
    overlaps: list[tuple[str, str, str]] = []
    items = list(roles_with_zones.items())
    for i, (a, za) in enumerate(items):
        fa = _zone_family(za)
        if fa == za and za == "generalist (undefined zone)":
            continue
        for b, zb in items[i + 1:]:
            fb = _zone_family(zb)
            if fb == zb and zb == "generalist (undefined zone)":
                continue
            # Exact zone match OR same family (broader)
            if za == zb:
                overlaps.append((a, b, za))
            elif fa == fb and fa not in (za, zb):
                # Same family but different specific zones — still overlap
                overlaps.append((a, b, f"{fa} (different focus: {za} vs {zb})"))
    return overlaps


def _build_disambiguation_addendum(
    role: str,
    zone: str,
    overlaps: list[tuple[str, str, str]],
    pipeline_roles: list[str],
) -> str:
    """Build a markdown addendum that disambiguates this role from others
    sharing the same zone. Strengthened instruction = unique contribution."""
    same_zone = [other for a, b, _ in overlaps for other in (a, b)
                 if other != role and (a == role or b == role)]
    same_zone = sorted(set(same_zone))

    # Pipeline position determines priority
    try:
        my_pos = pipeline_roles.index(role) if role in pipeline_roles else -1
    except ValueError:
        my_pos = -1

    position_hint = ""
    if my_pos >= 0:
        if my_pos == 0:
            position_hint = "You are the FIRST agent in the pipeline — start the work, set the foundation."
        elif my_pos == len(pipeline_roles) - 1:
            position_hint = f"You are the LAST agent in the pipeline (position {my_pos + 1}/{len(pipeline_roles)}) — final pass before supervisor verify."
        else:
            position_hint = f"You are at position {my_pos + 1}/{len(pipeline_roles)} in the pipeline."

    parts = [
        "",
        "---",
        "",
        "## BD-31: Pipeline-specific disambiguation (added by awf analyze-roles)",
        "",
        f"**Your zone:** {zone}",
        "",
    ]
    if position_hint:
        parts.append(position_hint)
        parts.append("")

    if same_zone:
        parts.append(f"**Other role(s) with similar zone:** {', '.join(f'`{r}`' for r in same_zone)}")
        parts.append("")
        parts.append("**To avoid wasted duplicate work, your unique contribution is:**")
        # Heuristic disambiguation rules — role-specific
        disambiguation_added = False
        if zone.startswith("verif"):  # covers "verifies..." and "verify..."
            if "qa" in role.lower():
                parts.append("- Check TODO requirements are met + write/run tests.")
                parts.append(f"- Do NOT do full project audit (that's `{'project-auditor' if 'project-auditor' in same_zone else same_zone[0]}`).")
                disambiguation_added = True
            elif "review" in role.lower() or "audit" in role.lower() or "project-auditor" in role.lower():
                parts.append("- Focus on code health: maintainability, design, risky patterns.")
                parts.append("- Do NOT re-verify TODO requirements line-by-line (qa did that).")
                disambiguation_added = True
        elif zone == "writes code" or zone == "code":
            if "implement" in role.lower() or role.lower() in ("dev", "developer", "worker"):
                parts.append("- You are the primary code writer. Other 'code' roles (system-analyst, architect) write requirements/design, not code.")
                disambiguation_added = True
            elif "refactor" in role.lower():
                parts.append("- You improve existing structure. Do NOT add new features.")
                disambiguation_added = True
        elif zone == "writes/runs tests" or zone == "test":
            parts.append("- You write and run tests. Do NOT fix bugs you find — report them via BLOCKED signal.")
            disambiguation_added = True
        elif zone == "writes requirements / vision" or zone == "requirements":
            parts.append("- You write the vision/requirements file. Do NOT write code or tests — `dev` does that.")
            disambiguation_added = True
        # BD-31b fix: fallback if no role-specific rule matched
        if not disambiguation_added:
            parts.append(f"- Focus on YOUR specific zone: {zone}.")
            parts.append(f"- Do NOT duplicate work that {', '.join(f'`{r}`' for r in same_zone)} already did.")
        parts.append("")

    parts.append("**Pipeline contract:** read the handoff from the previous role before starting. Do NOT redo prior work.")
    parts.append("")
    return "\n".join(parts)


def analyze_roles_core(
    project_dir: Path,
    *,
    dry_run: bool = False,
) -> AnalyzeData:
    """Pure-data role analysis — no printing, no stdout capture.

    Steps:
    1. Read role .md files (excluding supervisor).
    2. Read pipeline.yaml for role ordering.
    3. Infer zone for each role (slug + content heuristic).
    4. Detect overlaps (same zone OR same zone-family).
    5. Build disambiguation addendum per role.
    6. Apply patches to disk unless ``dry_run=True``.

    Raises :class:`AnalyzeError` on precondition failures:
    - No .agentic/roles/ directory
    - No team role files (only supervisor.md)

    Returns :class:`AnalyzeData` with zones, overlaps, patches,
    pipeline_roles, and ``applied`` flag.
    """
    if not (paths.agentic_dir(project_dir) / "roles").is_dir():
        raise AnalyzeError(
            f"No .agentic/roles/ at {project_dir}. Run 'awf init' first."
        )

    config = cfg_mod.load(project_dir)
    role_contents = _read_role_files(project_dir)
    pipeline_roles = _read_pipeline_roles(project_dir, config)

    if not role_contents:
        raise AnalyzeError("No role .md files found in .agentic/roles/.")

    # Skip supervisor — it's built-in, not part of team analysis.
    team_roles = {r: c for r, c in role_contents.items() if r != "supervisor"}
    if not team_roles:
        raise AnalyzeError("No team role files (only supervisor.md found).")

    # Step 1: infer zone for each role
    zones: dict[str, str] = {}
    for role, content in team_roles.items():
        zones[role] = _infer_zone(role, content)

    # Step 2: detect overlaps
    overlaps = _detect_overlaps(zones)

    # Step 3: build disambiguation addenda
    patches: dict[str, str] = {}
    for role in team_roles:
        addendum = _build_disambiguation_addendum(
            role, zones[role], overlaps, pipeline_roles
        )
        if "BD-31: Pipeline-specific disambiguation" in addendum:
            patches[role] = addendum

    data = AnalyzeData(
        zones=zones,
        overlaps=overlaps,
        patches=patches,
        pipeline_roles=pipeline_roles,
        applied=False,
    )

    if not patches or dry_run:
        return data

    # Step 4: apply patches to disk (idempotent via BD-31 marker check)
    roles_dir = paths.agentic_dir(project_dir) / "roles"
    failed: list[str] = []
    for role, addendum in patches.items():
        path = roles_dir / f"{role}.md"
        try:
            current = path.read_text(encoding="utf-8")
            if "BD-31: Pipeline-specific disambiguation" in current:
                pattern = re.compile(
                    r"\n*---\n\n## BD-31: Pipeline-specific disambiguation.*$",
                    re.DOTALL,
                )
                current = pattern.sub("", current).rstrip()
            new_content = current.rstrip() + "\n" + addendum
            atomic_write_text(path, new_content)
        except OSError:
            failed.append(role)

    if failed:
        # Surface failed writes in the structured result so callers (api/MCP)
        # can report them accurately instead of claiming success.
        data.failed = failed

    data.applied = True  # apply step ran; per-patch success derivable from `failed`
    return data


# ─── Public MCP-facing analyze_roles ────────────────────────────────────


def analyze_roles(
    project_dir: Path,
    *,
    dry_run: bool = False,
) -> AnalyzeRolesResult:
    """Analyze team roles for zone overlaps, optionally add disambiguation.

    Wraps :func:`analyze_roles_core` (pure data, no I/O side effects beyond
    patch writes) and formats the result for MCP/CLI consumption.
    Patch application is idempotent via BD-31 marker check.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    try:
        data = analyze_roles_core(project_dir, dry_run=dry_run)
    except AnalyzeError as e:
        raise AwfApiError(str(e))

    overlaps = [
        {"role_a": a, "role_b": b, "zone": zone}
        for a, b, zone in data.overlaps
    ]
    patches_applied = [
        {
            "role": role,
            "preview": addendum.replace("\n", " ")[:200],
            "applied": data.applied and role not in data.failed,
        }
        for role, addendum in data.patches.items()
    ]

    return AnalyzeRolesResult(
        overlaps=overlaps,
        patches_applied=patches_applied,
        dry_run=dry_run,
    )


__all__ = [
    "add_role",
    "analyze_roles",
    "analyze_roles_core",
    "AnalyzeData",
    "AnalyzeError",
]
