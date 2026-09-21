"""AUD13-02: the installed awf package must ship its data files.

A PyPI install without ``awf/data/role_zones.yaml`` silently degrades
BD-31 zone inference to the 5-key fallback (15 of 20 keys lost). The CI
``package`` job installs the built wheel in a clean venv and runs this
test against it, so package-data drift fails the build.
"""
from __future__ import annotations

from pathlib import Path

import awf

PKG_DIR = Path(awf.__file__).resolve().parent


def test_role_zones_yaml_shipped_in_package():
    path = PKG_DIR / "data" / "role_zones.yaml"
    assert path.is_file(), (
        f"{path} missing — check [tool.setuptools.package-data] in "
        "pyproject.toml (data/** must be declared)"
    )


def test_supervisor_templates_shipped_in_package():
    for name in ["roles/supervisor.md", "roles/supervisor/_core.md"]:
        assert (PKG_DIR / "templates" / name).is_file(), f"templates/{name} missing"
    phase_files = sorted((PKG_DIR / "templates" / "roles" / "supervisor").glob("phase-*.md"))
    assert len(phase_files) >= 7, f"expected >=7 phase-*.md, got {len(phase_files)}"
    assert (PKG_DIR / "templates" / "dashboard.html.j2").is_file()


def test_role_zones_yaml_parses():
    import yaml

    path = PKG_DIR / "data" / "role_zones.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data, "role_zones.yaml must be a non-empty mapping"
    # BD-31 key set: 20 zone keys (en + ru slugs) — catching a half-broken
    # file (empty dict) is enough for the package-data regression.
    assert len(data) >= 15, f"role_zones.yaml should carry >=15 keys, got {len(data)}"
