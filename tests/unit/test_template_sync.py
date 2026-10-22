"""TODO-0230 (Y3): templates live in the package only.

The repo-root ``templates/`` directory (a symlink to ``awf/templates/``)
is gone: the engine stages changed paths one by one, and a path behind a
symlink fails with ``fatal: pathspec ... is beyond a symbolic link``.
``awf/phase.py`` still checks a project's own ``templates/`` first (a
project may override prompts); for the awf repo itself there is no such
directory and the package is the only source.

Guarded here:
- the root copy/symlink does not exist and must not come back;
- the package ships the supervisor prompt tree;
- the dogfood ``scripts/suite-canonical.sh`` matches the shipped template.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PKG_TEMPLATES = REPO_ROOT / "awf" / "templates"
PKG_ROLES = PKG_TEMPLATES / "roles"
PKG_SCRIPTS = PKG_TEMPLATES / "scripts"
REPO_TEMPLATES = REPO_ROOT / "templates"
LIVE_SCRIPTS = REPO_ROOT / "scripts"


def test_root_template_copy_is_gone():
    assert not REPO_TEMPLATES.exists(), (
        f"repo-root templates/ is back: {REPO_TEMPLATES}. The package "
        "directory is the single source (TODO-0230 Y1)."
    )
    assert not REPO_TEMPLATES.is_symlink(), (
        f"repo-root templates/ symlink is back: {REPO_TEMPLATES}"
    )


def test_package_templates_exist():
    assert PKG_ROLES.is_dir(), f"package templates missing: {PKG_ROLES}"
    assert (PKG_ROLES / "supervisor.md").is_file()
    assert (PKG_ROLES / "supervisor" / "_core.md").is_file()


def test_live_canonical_suite_script_matches_the_shipped_template():
    """`awf init` copies awf/templates/scripts/suite-canonical.sh: the
    dogfood copy in scripts/ must not drift from what new projects
    receive."""
    name = "suite-canonical.sh"
    live = LIVE_SCRIPTS / name
    shipped = PKG_SCRIPTS / name
    assert live.is_file() and shipped.is_file()
    assert live.read_bytes() == shipped.read_bytes(), (
        f"{live} differs from the shipped template {shipped}"
    )
