"""AUD11-12: the repo-root templates/roles/ copy must stay in sync.

``get_phase_prompt`` resolves prompts from ``<project>/templates/roles/
supervisor/`` BEFORE the package templates (awf/phase.py). For the awf
repo itself (dogfood) that means the git-tracked root ``templates/roles/``
copy wins over ``awf/templates/roles/`` — the source of truth. Without
this test a future edit to only one tree silently desyncs dogfood
behavior from what a PyPI install receives.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PKG_ROLES = REPO_ROOT / "awf" / "templates" / "roles"
REPO_ROLES = REPO_ROOT / "templates" / "roles"


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_both_copies_exist():
    assert PKG_ROLES.is_dir(), f"missing package templates: {PKG_ROLES}"
    assert REPO_ROLES.is_dir(), f"missing repo-root templates: {REPO_ROLES}"


def test_role_template_trees_are_identical():
    pkg = _files(PKG_ROLES)
    repo = _files(REPO_ROLES)
    missing_in_repo = set(pkg) - set(repo)
    extra_in_repo = set(repo) - set(pkg)
    assert not missing_in_repo, (
        f"files in awf/templates/roles/ absent from templates/roles/: {sorted(missing_in_repo)}"
    )
    assert not extra_in_repo, (
        f"files in templates/roles/ absent from awf/templates/roles/: {sorted(extra_in_repo)}"
    )
    diffs = [name for name in pkg if pkg[name] != repo[name]]
    assert not diffs, f"content drift between template copies: {sorted(diffs)}"
