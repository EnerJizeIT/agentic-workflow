"""Entry point for ``awf normalize`` and ``awf normalize --check-drift``."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from . import paths

GLOBAL_SKILLS_DIR = Path.home() / ".config" / "opencode" / "skills"


def _sha256(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split YAML frontmatter from markdown body. Returns (dict, body)."""
    if not content.startswith("---\n"):
        return {}, content
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content
    fm_text = content[4:end]
    body = content[end + 5:]
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return {}, content
    return fm if isinstance(fm, dict) else {}, body


def run(args: Any) -> int:
    """Entry: either mark normalize needed (default) or check drift."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    check_drift = getattr(args, "check_drift", False)

    skills = paths.skills_dir(project_dir)

    if check_drift:
        return _check_drift(skills)

    state_dir = paths.agentic_dir(project_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "needs_normalize.yaml"
    target.write_text("needed: true\ntrigger: manual\n")
    print("Marked normalize needed. Next 'awf start' will run normalize stage.")
    print("To check for skill drift without normalizing: awf normalize --check-drift")
    return 0


def _check_drift(skills_dir: Path) -> int:
    """Compare global_sha in local skills to current SHA of global skills.

    Reports stale locals. Returns 0 if all up to date, 1 if any drifted.
    """
    if not skills_dir.is_dir():
        print("No local skills directory — nothing to check.")
        return 0

    drifted = []
    up_to_date = []
    missing_global = []

    for local in sorted(skills_dir.glob("*.md")):
        content = local.read_text(encoding="utf-8")
        fm, _ = _parse_frontmatter(content)
        if not fm.get("derived_from_global"):
            up_to_date.append((local.name, "(no frontmatter)"))
            continue
        global_path_str = fm.get("global_path", "")
        stored_sha = fm.get("global_sha", "")
        global_path = Path(global_path_str).expanduser()
        if not global_path.exists():
            missing_global.append((local.name, global_path_str))
            continue
        current_sha = _sha256(global_path)
        if current_sha == stored_sha:
            up_to_date.append((local.name, global_path.name))
        else:
            drifted.append((local.name, global_path.name, stored_sha[:8], current_sha[:8]))

    print("=== Skill drift report ===")
    if up_to_date:
        print(f"\n✓ Up to date ({len(up_to_date)}):")
        for name, src in up_to_date:
            print(f"  {name} <- {src}")
    if drifted:
        print(f"\n⚠ Drifted ({len(drifted)}):")
        for name, src, old, new_sha in drifted:
            print(f"  {name} <- {src} (was {old}, now {new_sha})")
    if missing_global:
        print(f"\n✗ Missing global ({len(missing_global)}):")
        for name, src in missing_global:
            print(f"  {name} (declared global: {src})")

    return 1 if drifted or missing_global else 0
