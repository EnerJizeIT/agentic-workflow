"""Shared builders for the SPEC IMP2-IMP5 tests.

The package documents themselves come from the IMP1 fixture
(``tests.unit.conftest._spec_documents``): one package shape, one place.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import api
from awf import spec_package as sp
from tests.unit.conftest import _spec_documents


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def init_project_repo(tmp_path: Path, name: str = "SPEC") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name=name,
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def write_package(
    repo: Path,
    scope: str = "pkg",
    documents: dict[str, str] | None = None,
    commit: bool = True,
) -> Path:
    root = repo / "spec" / scope
    root.mkdir(parents=True, exist_ok=True)
    docs = _spec_documents(scope)
    docs.update(documents or {})
    for name, text in docs.items():
        (root / name).write_text(text, encoding="utf-8")
    sha = sp.package_fingerprint(root)
    readiness = root / f"{scope}.readiness.md"
    readiness.write_text(
        readiness.read_text(encoding="utf-8").replace("__PACKAGE_SHA__", sha),
        encoding="utf-8",
    )
    if commit:
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "package")
    return root


def package_content(
    todo_id: str, refs: list[str] | None = None, scope: str = "spec/pkg"
) -> str:
    refs = refs or [f"{scope}/pkg.md#R1 — Первое правило"]
    links = "".join(f"  - {ref}\n" for ref in refs)
    return (
        "---\n"
        f"spec_package: {scope}\n"
        "source_refs:\n"
        f"{links}"
        "---\n"
        f"# {todo_id} — unit\n\nтело\n"
    )
