"""BD-31: ``awf analyze-roles`` — interactive skill-aware role normalization.

Supervisor (current opencode in user's chat) reads all role .md files,
identifies overlaps/duplications in zones of responsibility, and proposes
targeted patches to strengthen each role's instructions for the current
pipeline. User confirms patches before they're applied.

This is NOT the legacy BD-13 auto_normalize_skills (which blindly copied
global skills). BD-31 is semantic analysis: it understands the pipeline
context and tailors each role to its specific slot.

Usage:
    awf analyze-roles [--project-dir PATH] [--dry-run]
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths


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
    from .pipeline import load_stages, resolve_pipeline_file

    try:
        pipeline_file = resolve_pipeline_file(project_dir, None, config)
    except FileNotFoundError:
        return []
    stages = load_stages(pipeline_file)
    return [s.role for s in stages if s.role != "supervisor"]


_ZONES_OF_RESPONSIBILITY = {
    # role-substring → zone label
    "разработчик": "writes code",
    "developer": "writes code",
    "implement": "writes code",
    "тест": "writes/runs tests",
    "tester": "writes/runs tests",
    "test-automator": "writes/runs tests",
    "qa": "verifies implementation against requirements",
    "review": "verifies implementation against requirements",
    "audit": "verifies code health / project quality",
    "analyst": "writes requirements / vision",
    "аналитик": "writes requirements / vision",
    "system-analyst": "writes requirements / vision",
    "system-analyst".replace("-", " "): "writes requirements / vision",
    "architect": "designs system structure",
    "refactor": "improves structure without behavior change",
    "debug": "isolates bugs",
    "security": "security review",
    "performance": "performance investigation",
}


def _infer_zone(role_slug: str, content: str) -> str:
    """Best-effort guess of a role's zone from its slug + content head."""
    head = content[:600].lower()
    slug_lower = role_slug.lower()
    for needle, zone in _ZONES_OF_RESPONSIBILITY.items():
        if needle in slug_lower or needle in head:
            return zone
    return "generalist (undefined zone)"


def _detect_overlaps(roles_with_zones: dict[str, str]) -> list[tuple[str, str, str]]:
    """Find pairs of roles whose zones overlap.

    Returns list of (role_a, role_b, shared_zone) tuples.
    """
    overlaps: list[tuple[str, str, str]] = []
    items = list(roles_with_zones.items())
    for i, (a, za) in enumerate(items):
        for b, zb in items[i + 1:]:
            if za == zb and za != "generalist (undefined zone)":
                overlaps.append((a, b, za))
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
        # Heuristic disambiguation rules
        if zone == "verifies implementation against requirements":
            if "qa" in role.lower():
                parts.append("- Check TODO requirements are met + write/run tests.")
                parts.append(f"- Do NOT do full project audit (that's `{'project-auditor' if 'project-auditor' in same_zone else same_zone[0]}`).")
            elif "review" in role.lower() or "audit" in role.lower():
                parts.append("- Focus on code health: maintainability, design, risky patterns.")
                parts.append("- Do NOT re-verify TODO requirements line-by-line (qa did that).")
        elif zone == "writes code":
            if "implement" in role.lower():
                parts.append("- You are the primary code writer. Other 'code' roles refactor/review, not write.")
            elif "refactor" in role.lower():
                parts.append("- You improve existing structure. Do NOT add new features.")
        elif zone == "writes/runs tests":
            parts.append("- You write and run tests. Do NOT fix bugs you find — report them via BLOCKED signal.")
        elif zone == "writes requirements / vision":
            parts.append("- You write the vision/requirements file. Do NOT write code or tests.")
        parts.append("")

    parts.append("**Pipeline contract:** read the handoff from the previous role before starting. Do NOT redo prior work.")
    parts.append("")
    return "\n".join(parts)


def run(args: Any) -> int:
    """Execute ``awf analyze-roles``."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    dry_run = getattr(args, "dry_run", False)

    if not (paths.agentic_dir(project_dir) / "roles").is_dir():
        print(f"No .agentic/roles/ at {project_dir}. Run 'awf init' first.")
        return 1

    config = cfg_mod.load(project_dir)
    role_contents = _read_role_files(project_dir)
    pipeline_roles = _read_pipeline_roles(project_dir, config)

    if not role_contents:
        print("No role .md files found in .agentic/roles/.")
        return 1

    # Skip supervisor — it's built-in, not part of team analysis.
    team_roles = {r: c for r, c in role_contents.items() if r != "supervisor"}
    if not team_roles:
        print("No team role files (only supervisor.md found).")
        return 1

    print("=" * 60)
    print("  BD-31: SKILL-AWARE ROLE ANALYSIS")
    print("=" * 60)
    print()
    print(f"Pipeline team (in order): {', '.join(pipeline_roles) or '(no pipeline.yaml found)'}")
    print(f"Role files analyzed: {len(team_roles)}")
    print()

    # Step 1: infer zone for each role
    zones: dict[str, str] = {}
    print("Inferred zones:")
    for role, content in team_roles.items():
        zone = _infer_zone(role, content)
        zones[role] = zone
        marker = "✓" if zone != "generalist (undefined zone)" else "?"
        print(f"  {marker} {role}: {zone}")
    print()

    # Step 2: detect overlaps
    overlaps = _detect_overlaps(zones)
    if overlaps:
        print(f"Overlaps detected ({len(overlaps)}):")
        for a, b, zone in overlaps:
            print(f"  ⚠ {a}  ⟷  {b}  (both: {zone})")
        print()
    else:
        print("No overlaps detected. Roles are well-differentiated.")
        print()

    # Step 3: build disambiguation addenda
    patches: dict[str, str] = {}
    print("Proposed patches:")
    for role in team_roles:
        addendum = _build_disambiguation_addendum(role, zones[role], overlaps, pipeline_roles)
        if "BD-31: Pipeline-specific disambiguation" in addendum:
            patches[role] = addendum
            preview = addendum.replace("\n", " ")[:120]
            print(f"  + {role}.md: {preview}...")
    print()

    if not patches:
        print("Nothing to patch. Roles look clean.")
        return 0

    if dry_run:
        print("--dry-run: patches generated but NOT applied.")
        print("Re-run without --dry-run to write them.")
        return 0

    # Step 4: apply patches (append to each role.md)
    print(f"Applying patches to {len(patches)} role file(s)...")
    roles_dir = paths.agentic_dir(project_dir) / "roles"
    for role, addendum in patches.items():
        path = roles_dir / f"{role}.md"
        try:
            current = path.read_text(encoding="utf-8")
            # Avoid double-patching: check if BD-31 marker already present
            if "BD-31: Pipeline-specific disambiguation" in current:
                # Replace existing addendum
                pattern = re.compile(
                    r"\n*---\n\n## BD-31: Pipeline-specific disambiguation.*$",
                    re.DOTALL,
                )
                current = pattern.sub("", current).rstrip()
            new_content = current.rstrip() + "\n" + addendum
            path.write_text(new_content, encoding="utf-8")
            print(f"  ✓ patched {role}.md")
        except OSError as e:
            print(f"  ✗ failed {role}.md: {e}")

    print()
    print(f"Done. {len(patches)} role(s) strengthened with pipeline-specific disambiguation.")
    print("Re-commit role files if you want to track changes in git.")
    return 0
