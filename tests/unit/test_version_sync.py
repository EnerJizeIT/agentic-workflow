"""AUD16-07: version constants must match the pyproject metadata.

The three old constants (awf 0.4.0, plugin 0.1.0, bin/awf 0.4.0) drifted
from pyproject (1.0.0) and lied to anyone running ``import awf`` or the
CLI. These tests pin every ``__version__`` to its pyproject ``[project]
version`` so a bump in one place without the other fails CI.

``bin/awf`` no longer carries a version constant (removed — it was never
read), so there is nothing to pin there.
"""
from __future__ import annotations

import re
from pathlib import Path

import agent_workflow_ui
import awf

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _pyproject_version(path: Path) -> str:
    """Extract [project] version without tomllib (Python 3.10 compat)."""
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, f"no [project] version line in {path}"
    return match.group(1)


def test_awf_version_matches_pyproject():
    declared = _pyproject_version(REPO_ROOT / "pyproject.toml")
    assert awf.__version__ == declared, (
        f"awf.__version__={awf.__version__!r} but pyproject says {declared!r}"
    )


def test_plugin_version_matches_pyproject():
    declared = _pyproject_version(REPO_ROOT / "agent_workflow_ui" / "pyproject.toml")
    assert agent_workflow_ui.__version__ == declared, (
        f"agent_workflow_ui.__version__={agent_workflow_ui.__version__!r} "
        f"but pyproject says {declared!r}"
    )


def test_plugin_depends_on_awf_at_its_own_version():
    """The plugin must not pull an older awf core from PyPI."""
    text = (REPO_ROOT / "agent_workflow_ui" / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'"awf>=([\d.]+)"', text)
    assert match, "plugin pyproject must pin awf>=X"
    assert match.group(1) == _pyproject_version(REPO_ROOT / "pyproject.toml")
