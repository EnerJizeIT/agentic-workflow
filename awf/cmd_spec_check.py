"""``awf spec check`` — readiness verdict for a specification package.

Prints the verdict, the findings and the package sha. Exit codes: 0 — ready,
1 — not ready, 2 — the package cannot be evaluated (no such directory). The
verdict is :func:`awf.spec_package.spec_gate` verbatim, so the CLI and the
API cannot disagree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import spec_package


def run(args: Any) -> int:
    """Execute ``awf spec check <path>`` and return the exit code."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    raw = str(getattr(args, "path", "")).strip()
    if not raw:
        print("ERROR: путь к каталогу пакета не задан")
        return 2

    package = Path(raw)
    if not package.is_absolute():
        package = project_dir / package
    package = package.resolve()
    if not package.is_dir():
        print(f"ERROR: каталог пакета не найден: {raw}")
        return 2

    gate = spec_package.spec_gate(project_dir, package)
    if gate.ok:
        print(f"READY: {gate.package_dir}")
    else:
        print(f"NOT READY: {gate.package_dir}")
        print(f"  причина: {gate.reason}")
        for detail in gate.details:
            print(f"  - {detail}")

    sha = gate.package_sha or gate.actual_sha
    if sha:
        print(f"Пакет-sha: {sha}")
    return 0 if gate.ok else 1
