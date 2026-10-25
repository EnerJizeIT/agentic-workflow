"""Shared fixtures for unit tests.

AUD12-08: ``tmp_git_repo`` lives in tests/conftest.py (single home for the
git boilerplate) — unit tests get it by fixture cascade.
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _spec_documents(scope: str) -> dict[str, str]:
    """The six documents of a minimal READY package (SPEC IMP1 fixtures)."""
    requirements = f"""# Требования

Статус: REVIEW
Ревизия: req-1
Входы: {scope}.context.md@ctx-1; TODO-0001
База кода: commit `abcdef1234`

## Требования

### R1 — Первое правило

Текст правила.

AC1 — Первый критерий.

### INV1 — Не должно измениться

Текст инварианта.

### C1 — Сценарий

Шаги сценария.
"""
    architecture = f"""# Архитектура

Статус: REVIEW
Ревизия: arch-1
Входы: {scope}.md@req-1
База кода: commit `abcdef1234`

## 1. K1 — Первый контракт

Текст контракта.

## Решения

| ID | Решение |
|---|---|
| D1 | Первое решение |
"""
    context = """# Контекст

Статус: REVIEW
Ревизия: ctx-1
Входы: TODO-0001
База кода: commit `abcdef1234`
"""
    test_spec = f"""# Тестовая спецификация

Статус: REVIEW
Ревизия: qa-1
Входы: {scope}.md@req-1
База кода: commit `abcdef1234`

## T1 — Первая проверка

Текст проверки.
"""
    review = f"""# Ревью требований

Статус: PASS
Ревизия: rev-1
Входы: {scope}.md@req-1
База кода: commit `abcdef1234`
Проверено: {scope}.md=req-1
"""
    readiness = f"""# Готовность

Статус: READY
Ревизия: ready-1
Входы: {scope}.md@req-1
База кода: commit `abcdef1234`
Проверено: {scope}.context.md=ctx-1, {scope}.md=req-1, {scope}.requirements-review.md=rev-1, {scope}.architecture.md=arch-1, {scope}.test-spec.md=qa-1, {scope}.readiness.md=ready-1
Пакет-sha: __PACKAGE_SHA__

## Матрица покрытия

| ID | Источник | Сценарий/контракт | Проверка |
|---|---|---|---|
| R1 | {scope}.md#R1 | C1/K1/D1 | T1/IMP1 |
| AC1 | {scope}.md#R1 | C1/K1 | T1/IMP1 |
| INV1 | {scope}.md#INV1 | C1/K1 | метод: pytest tests/test_x.py |
"""
    return {
        f"{scope}.context.md": context,
        f"{scope}.md": requirements,
        f"{scope}.requirements-review.md": review,
        f"{scope}.architecture.md": architecture,
        f"{scope}.test-spec.md": test_spec,
        f"{scope}.readiness.md": readiness,
    }


@pytest.fixture
def make_spec_package(tmp_path: Path):
    """Build a READY six-document package under ``tmp_path``.

    Returns a factory ``factory(scope="pkg", documents=None) -> Path``.
    ``documents`` overrides individual files before the package sha is
    computed; a missing readiness body gets the fixture default.
    """
    from awf import spec_package

    def factory(scope: str = "pkg", documents: dict[str, str] | None = None) -> Path:
        root = tmp_path / scope
        root.mkdir(parents=True, exist_ok=True)
        docs = _spec_documents(scope)
        docs.update(documents or {})
        for name, text in docs.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        sha = spec_package.package_fingerprint(root)
        readiness = root / f"{scope}.readiness.md"
        readiness.write_text(
            readiness.read_text(encoding="utf-8").replace("__PACKAGE_SHA__", sha),
            encoding="utf-8",
        )
        return root

    return factory


@pytest.fixture
def sample_simple_config() -> dict:
    """Config dict matching what awf init --template simple produces."""
    return {
        "project": {"name": "test-project"},
        "default_pipeline": "default",
        "phases": {"current": ".agentic/phases/plan.md"},
        "verification": {
            "test_cmd": "pytest",
            "lint_cmd": "ruff check .",
            "typecheck_cmd": "mypy .",
            "build_cmd": "",
        },
        "automation": {"auto_done": True},
        "models": {
            "worker": {"agent_name": "worker"},
            "reviewer": {"agent_name": "rev1"},
        },
    }


@pytest.fixture
def sample_full_config() -> dict:
    """Config dict matching what awf init --template full produces."""
    return {
        "project": {"name": "test-project"},
        "default_pipeline": "full",
        "phases": {"current": ".agentic/phases/plan.md"},
        "verification": {
            "test_cmd": "pytest",
            "lint_cmd": "ruff check .",
            "typecheck_cmd": "mypy .",
            "build_cmd": "",
        },
        "automation": {"auto_done": True},
        "models": {
            "worker": {"agent_name": "worker"},
            "reviewer": {"agent_name": "rev1"},
            "tester": {"agent_name": "tester"},
        },
    }


@pytest.fixture
def tmp_pipeline_file(tmp_path: Path) -> tuple[Path, dict]:
    """Write inline pipeline YAMLs (simple + full) into tmp.

    BD-28: templates/pipelines/{simple,full}.yaml were removed (UI form
    generates pipeline now). Tests embed the YAML inline to stay independent
    of templates/.
    """
    agentic = tmp_path / ".agentic"
    pipelines = agentic / "pipelines"
    pipelines.mkdir(parents=True)

    simple_yaml = (
        'name: "default"\n'
        'description: "Supervisor plans, Worker implements, Supervisor verifies"\n\n'
        'stages:\n'
        '  - name: "plan"\n'
        '    role: "supervisor"\n'
        '    description: "Supervisor studies the plan and creates a TODO"\n\n'
        '  - name: "implement"\n'
        '    role: "worker"\n'
        '    description: "Worker executes the TODO"\n'
        '    on_blocked: "escalate"\n'
        '    max_retries: 3\n\n'
        '  - name: "verify"\n'
        '    role: "supervisor"\n'
        '    description: "Supervisor verifies the result"\n'
        '    on_approved: "commit_and_next"\n'
        '    on_rejected: "replan"\n'
    )

    full_yaml = (
        'name: "full"\n'
        'description: "Plan → implement → review → test → finalize"\n\n'
        'stages:\n'
        '  - name: "plan"\n'
        '    role: "supervisor"\n'
        '  - name: "implement"\n'
        '    role: "worker"\n'
        '    on_blocked: "escalate"\n'
        '    max_retries: 3\n'
        '  - name: "review"\n'
        '    role: "reviewer"\n'
        '    on_rejected: "rollback_to:implement"\n'
        '    max_retries: 2\n'
        '  - name: "test"\n'
        '    role: "tester"\n'
        '    on_failed: "escalate"\n'
        '    max_retries: 2\n'
        '  - name: "finalize"\n'
        '    role: "supervisor"\n'
        '    on_approved: "commit_and_report"\n'
        '    on_rejected: "replan"\n'
    )

    (pipelines / "simple.yaml").write_text(simple_yaml)
    (pipelines / "full.yaml").write_text(full_yaml)
    (pipelines / "default.yaml").write_text(simple_yaml)

    config = {
        "project": {"name": "test-project"},
        "default_pipeline": "default",
        "models": {
            "worker": {"agent_name": "worker"},
            "reviewer": {"agent_name": "rev1"},
        },
    }
    (agentic / "config.yaml").write_text(
        "project:\n  name: test-project\ndefault_pipeline: default\n"
    )

    return tmp_path, config
