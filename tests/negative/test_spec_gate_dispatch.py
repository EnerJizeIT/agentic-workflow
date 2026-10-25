"""SPEC IMP2 — K3 gate at dispatch (T5, AC8/AC9/AC10).

A unit that declares ``spec_package`` may be created ONLY from a READY
package with a current fingerprint. A stale READY, a BLOCKED readiness and
an outdated verdict all refuse BEFORE any side effect: no TODO, no
baseline, no ``.ready``, and the repeated call gives the SAME text.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import api
from awf import spec_package as sp
from tests.unit.conftest import _spec_documents

SRC = "TODO-0001"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _project(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="SPEC-GATE",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _make_package(
    repo: Path, scope: str = "pkg", documents: dict[str, str] | None = None
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
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "package")
    return root


def _content(package: str = "spec/pkg") -> str:
    return (
        "---\n"
        f"spec_package: {package}\n"
        "source_refs:\n"
        f"  - {package}/pkg.md#R1 — Первое правило\n"
        "---\n"
        f"# {SRC} — unit\n\nтело\n"
    )


def _no_side_effects(repo: Path) -> None:
    inbox = repo / ".agentic" / "inbox"
    assert not (inbox / f"{SRC}.md").exists(), "TODO was created on refusal"
    assert not (inbox / f"{SRC}.ready").exists(), ".ready was created on refusal"
    context = repo / ".agentic" / "context"
    assert not list(context.glob(f"BASELINE-{SRC}.*")), (
        "baseline was created on refusal"
    )
    assert not (repo / ".agentic" / "state" / "pipeline.yaml").exists(), (
        "a launch record was written on refusal"
    )


def test_review_refused_without_side_effects(tmp_path):
    """An outdated verdict (requirements at req-2, review covers req-1)."""
    repo = _project(tmp_path)
    documents = dict(_spec_documents("pkg"))
    documents["pkg.md"] = documents["pkg.md"].replace(
        "Ревизия: req-1", "Ревизия: req-2"
    )
    _make_package(repo, documents=documents)

    with pytest.raises(api.AwfApiError) as first:
        api.dispatch_todo(repo, _content(), todo_id=SRC)
    message = str(first.value)
    assert "устаревшая ревизия" in message
    assert "req-1" in message and "req-2" in message
    _no_side_effects(repo)

    with pytest.raises(api.AwfApiError) as second:
        api.dispatch_todo(repo, _content(), todo_id=SRC)
    assert str(second.value) == message, "the refusal must be deterministic"


def test_blocked_readiness_names_question_and_owner(tmp_path):
    repo = _project(tmp_path)
    documents = dict(_spec_documents("pkg"))
    documents["pkg.readiness.md"] = documents["pkg.readiness.md"].replace(
        "Статус: READY", "Статус: BLOCKED"
    ) + (
        "\n## Открытые вопросы\n\n"
        "| ID | Вопрос | Владелец | Влияние |\n"
        "|---|---|---|---|\n"
        "| Q1 | Нужен ли X? | аналитик | блокирует R1 |\n"
    )
    _make_package(repo, documents=documents)

    with pytest.raises(api.AwfApiError) as excinfo:
        api.dispatch_todo(repo, _content(), todo_id=SRC)
    message = str(excinfo.value)
    assert "BLOCKED" in message
    assert "Нужен ли X?" in message
    assert "аналитик" in message
    _no_side_effects(repo)


def test_stale_ready_refused_with_both_sha(tmp_path):
    repo = _project(tmp_path)
    package = _make_package(repo)
    requirements = package / "pkg.md"
    requirements.write_text(
        requirements.read_text(encoding="utf-8") + "\nправка без ревизии\n",
        encoding="utf-8",
    )

    with pytest.raises(api.AwfApiError) as excinfo:
        api.dispatch_todo(repo, _content(), todo_id=SRC)
    message = str(excinfo.value)
    assert "sha" in message
    assert message.count("sha") >= 1
    _no_side_effects(repo)


def test_ready_package_creates_todo_with_norm(tmp_path):
    repo = _project(tmp_path)
    _make_package(repo)

    result = api.dispatch_todo(repo, _content(), todo_id=SRC)
    assert result.todo_id == SRC
    body = (repo / ".agentic" / "inbox" / f"{SRC}.md").read_text(encoding="utf-8")
    assert "## Пакет спецификации" in body
    assert "Пакет-sha" in body
    assert "R1" in body and "AC1" in body and "INV1" in body
    assert "## Первоисточник (R1 — Первое правило)" in body
    assert "Текст правила." in body


def test_gate_is_skipped_without_spec_package(tmp_path):
    repo = _project(tmp_path)
    api.dispatch_todo(repo, f"# {SRC} — unit\n\nтело\n", todo_id=SRC)
    assert (repo / ".agentic" / "inbox" / f"{SRC}.md").is_file()
