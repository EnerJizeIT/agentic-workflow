"""A28: stage real scripts into a fixture repo, with the shared library.

``check-contracts.sh`` and ``affected-tests.sh`` source the interpreter
resolver in ``scripts/lib/``; a copy of either script without the library
cannot start. One staging rule lives here, so every test copies the pair the
same way. Kept out of ``test_verify_pack.py`` because that module sits at the
ratchet line budget.
"""
from __future__ import annotations

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def copy_scripts(target: Path, *names: str) -> None:
    """Copy the named ``scripts/`` files and ``scripts/lib/`` into ``target``."""
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(REPO_ROOT / "scripts" / name, target / name)
    shutil.copytree(REPO_ROOT / "scripts" / "lib", target / "lib", dirs_exist_ok=True)


def copy_gate_scripts(repo: Path) -> None:
    """Copy ``scripts/check-contracts.sh`` and ``scripts/lib/`` into ``repo``."""
    copy_scripts(repo / "scripts", "check-contracts.sh")
