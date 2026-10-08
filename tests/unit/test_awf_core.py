"""TODO-0166: core API surface — commit-workflow guard.

Файл назван контрактом юнита (verify: ``tests/unit/test_awf_core.py``), но
до юнита в репозитории не существовал (проверено по git-истории) — создан
вместе с юнитом как core-guard новой публичной поверхности:

- ``awf.api`` экспортирует ``commit_workflow`` / ``uncommitted_workflow_files``;
- ``StatusResult`` несёт поле ``uncommitted_workflow_files`` (dry-run);
- CLI-сабкоманда ``commit-workflow`` зарегистрирована и диспатчится;
- сообщение коммита: subject ``awf(workflow):`` без TODO-id, body — файлы;
- MCP-реестр несёт ``awf_commit_workflow`` (53 = 5 UI + 48 workflow).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Модульный импорт (как в test_docs_counters.py): на сборке Popen ещё
# настоящий, autouse-фикстура подменяет его только в телах тестов.
pytest.importorskip("mcp.server.fastmcp")
from agent_workflow_ui.tools import registry as _registry  # noqa: E402

from awf import api  # noqa: E402
from awf.todo_ids import extract_todo_id  # noqa: E402


@pytest.fixture
def core_repo(tmp_git_repo: Path) -> Path:
    """awf-init'ed repo с закоммиченным .agentic-скелетоном."""
    api.init_project(tmp_git_repo, project_name="Core")
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "skeleton"], cwd=tmp_git_repo, check=True)
    return tmp_git_repo


def test_api_exports_commit_workflow():
    assert callable(api.commit_workflow)
    assert callable(api.uncommitted_workflow_files)
    assert "commit_workflow" in api.__all__
    assert "uncommitted_workflow_files" in api.__all__
    assert "WorkflowCommitResult" in api.__all__


def test_status_result_carries_uncommitted_workflow_files(core_repo: Path):
    status = api.get_status(core_repo)
    assert status.uncommitted_workflow_files == []
    payload = status.as_dict()
    assert "uncommitted_workflow_files" in payload
    assert payload["uncommitted_workflow_files"] == []

    (core_repo / ".agentic" / "pipelines").mkdir(exist_ok=True)
    (core_repo / ".agentic" / "pipelines" / "a.yaml").write_text("name: a\n")
    status = api.get_status(core_repo)
    assert status.uncommitted_workflow_files == [".agentic/pipelines/a.yaml"]


def test_cli_commit_workflow_subcommand():
    from awf import cli

    parser, _sub = cli._build_parser()
    args = parser.parse_args(["commit-workflow", "--project-dir", "."])
    assert args.command == "commit-workflow"
    assert args.project_dir == "."

    import awf.cmd_workflow_commit  # noqa: F401 — модуль диспатча существует


def test_workflow_commit_message_contract():
    from awf.api.workflow_commit import build_workflow_commit_message

    message = build_workflow_commit_message(
        [".agentic/pipelines/a.yaml", ".agentic/roles/qa.md"]
    )
    subject, _, body = message.partition("\n\n")
    assert subject.startswith("awf(workflow):"), subject
    # Парсеры: TODO-id в сообщении нет — коммит не подменяет коммит юнита.
    assert extract_todo_id(subject) is None
    assert body == "files: .agentic/pipelines/a.yaml, .agentic/roles/qa.md", body


def test_registry_lists_commit_workflow():
    names = [s.name for s in _registry.TOOLS]
    assert "awf_commit_workflow" in names
    counts = _registry.tool_counts()
    assert counts == {"total": 53, "awf": 48, "ui": 5}
