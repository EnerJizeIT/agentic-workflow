"""Role management API: add_role + analyze_roles.

``analyze_roles`` delegates to :func:`awf.cmd_analyze_roles.analyze_roles_core`
which returns structured data (no stdout capture). ``add_role`` formats a
template and writes it atomically.
"""
from __future__ import annotations

from pathlib import Path

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


# ─── analyze_roles ──────────────────────────────────────────────────────


def analyze_roles(
    project_dir: Path,
    *,
    dry_run: bool = False,
) -> AnalyzeRolesResult:
    """Analyze team roles for zone overlaps, optionally add disambiguation.

    Delegates to :func:`awf.cmd_analyze_roles.analyze_roles_core` which
    returns pure structured data. Patch application is idempotent via
    BD-31 marker check.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    from ..cmd_analyze_roles import AnalyzeError, analyze_roles_core

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
            "applied": data.applied,
        }
        for role, addendum in data.patches.items()
    ]

    return AnalyzeRolesResult(
        overlaps=overlaps,
        patches_applied=patches_applied,
        dry_run=dry_run,
    )


__all__ = ["add_role", "analyze_roles"]
