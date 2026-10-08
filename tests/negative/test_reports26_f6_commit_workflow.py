"""REPORTS26 F6 (TODO-0166): commit-workflow — workflow-файлы проекта в git.

Source: awf-feature-20261007-workflow-fayly-pipelines-roles-dobavlennye-v-hode-proekta.md.
Роли/пайплайны/доктрина, добавленные в ходе проекта, не попадают в коммиты
(read-only юниты не коммитят вовсе) — теряются при переносе/бэкапе.
Решение владельца: (б) отдельный инструмент + (в) видимость в статусе.

Инварианты (TODO-0166):
1. ``commit_workflow(project_dir)`` коммитит не-игнорируемые
   workflow-определения ``.agentic/``: ``config.yaml``, ``roles/**``,
   ``pipelines/**``, ``phases/**``, ``doctrine/**`` (изменённые tracked +
   новые untracked, не gitignored). Subject начинается ``awf(workflow):``
   (совместимо с парсерами — TODO-id в сообщении нет), body — список
   файлов. Пусто — понятный отказ. Изолированный add (R-03): индекс
   пользователя не открывается, чужие staged-записи не затрагиваются.
   Runtime (inbox/outbox/state/logs/context/handoff) не трогается.
2. ``awf_status`` получает ``uncommitted_workflow_files`` (dry-run тех же
   путей).
3. Доставка: CLI ``awf commit-workflow`` + MCP ``awf_commit_workflow``
   (счётчик 52 → 53).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Модульный импорт (как в test_docs_counters.py): на сборке Popen ещё
# настоящий, а autouse-фикстура tests/conftest.py подменяет его только
# в телах тестов.
pytest.importorskip("mcp.server.fastmcp")
from agent_workflow_ui.tools import registry as _registry  # noqa: E402

from awf import api  # noqa: E402
from awf.api._errors import AwfApiError  # noqa: E402
from awf.api.wait_event import _AWF_COMMIT_RE  # noqa: E402
from awf.todo_ids import extract_todo_id  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def wf_repo(tmp_git_repo: Path) -> Path:
    """awf-init'ed repo с закоммиченным .agentic-скелетоном.

    Скелет закоммичен, чтобы в каждом тесте «незакоммиченными» были ровно
    те workflow-файлы, которые тест сам создаёт.
    """
    api.init_project(tmp_git_repo, project_name="F6")
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "skeleton"], cwd=tmp_git_repo, check=True)
    return tmp_git_repo


def _pipeline_file(repo: Path, name: str = "a.yaml") -> Path:
    (repo / ".agentic" / "pipelines").mkdir(parents=True, exist_ok=True)
    path = repo / ".agentic" / "pipelines" / name
    path.write_text(f"name: {name.removesuffix('.yaml')}\n", encoding="utf-8")
    return path


def _committed_files(repo: Path) -> list[str]:
    out = _git(repo, "show", "--name-only", "--format=", "HEAD")
    return [line for line in out.splitlines() if line]


def _subject(repo: Path) -> str:
    return _git(repo, "log", "-1", "--format=%s")


class TestStatusVisibility:
    """Инвариант 2: dry-run список в awf_status."""

    def test_clean_project_has_empty_list(self, wf_repo: Path):
        status = api.get_status(wf_repo)
        assert status.uncommitted_workflow_files == []
        assert status.as_dict()["uncommitted_workflow_files"] == []

    def test_status_shows_untracked_pipeline_file(self, wf_repo: Path):
        _pipeline_file(wf_repo)
        status = api.get_status(wf_repo)
        assert status.uncommitted_workflow_files == [".agentic/pipelines/a.yaml"]

    def test_status_shows_changed_tracked_config(self, wf_repo: Path):
        cfg = wf_repo / ".agentic" / "config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8") + "# wip\n", encoding="utf-8")
        status = api.get_status(wf_repo)
        assert status.uncommitted_workflow_files == [".agentic/config.yaml"]


class TestCommitWorkflow:
    """Инвариант 1: состав коммита, subject, изоляция, отказ на пустом."""

    def test_commits_exactly_the_untracked_file(self, wf_repo: Path):
        _pipeline_file(wf_repo)
        result = api.commit_workflow(wf_repo)

        assert result.files == [".agentic/pipelines/a.yaml"]
        assert _subject(wf_repo).startswith("awf(workflow):"), _subject(wf_repo)
        assert _committed_files(wf_repo) == [".agentic/pipelines/a.yaml"]
        # После коммита dry-run пуст — видимость и коммит согласованы.
        assert api.get_status(wf_repo).uncommitted_workflow_files == []

    def test_commits_changed_tracked_and_new_together(self, wf_repo: Path):
        cfg = wf_repo / ".agentic" / "config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8") + "# wip\n", encoding="utf-8")
        _pipeline_file(wf_repo)
        (wf_repo / ".agentic" / "roles").mkdir(exist_ok=True)
        (wf_repo / ".agentic" / "roles" / "qa.md").write_text("# qa\n")

        result = api.commit_workflow(wf_repo)
        assert result.files == [
            ".agentic/config.yaml",
            ".agentic/pipelines/a.yaml",
            ".agentic/roles/qa.md",
        ]
        subject = _subject(wf_repo)
        assert subject.startswith("awf(workflow): 3 files:"), subject
        assert _committed_files(wf_repo) == [
            ".agentic/config.yaml",
            ".agentic/pipelines/a.yaml",
            ".agentic/roles/qa.md",
        ]

    def test_body_lists_the_files(self, wf_repo: Path):
        _pipeline_file(wf_repo)
        result = api.commit_workflow(wf_repo)
        body = _git(wf_repo, "log", "-1", "--format=%b")
        assert "files: .agentic/pipelines/a.yaml" in body, body

    def test_empty_set_is_a_clear_refusal(self, wf_repo: Path):
        head_before = _git(wf_repo, "rev-parse", "HEAD")
        with pytest.raises(AwfApiError, match="nothing to commit"):
            api.commit_workflow(wf_repo)
        assert _git(wf_repo, "rev-parse", "HEAD") == head_before

    def test_not_a_repo_and_no_agentic_are_clear_errors(self, tmp_path: Path):
        bare = tmp_path / "bare"
        bare.mkdir()
        with pytest.raises(AwfApiError, match="not a git repo"):
            api.commit_workflow(bare)
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
        (repo / "README.md").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
        with pytest.raises(AwfApiError, match="no .agentic/"):
            api.commit_workflow(repo)

    def test_gitignored_workflow_file_is_excluded(self, wf_repo: Path):
        gitignore = wf_repo / ".gitignore"
        gitignore.write_text(
            gitignore.read_text(encoding="utf-8")
            + ".agentic/pipelines/ignored.yaml\n",
            encoding="utf-8",
        )
        (wf_repo / ".agentic" / "pipelines").mkdir(exist_ok=True)
        (wf_repo / ".agentic" / "pipelines" / "ignored.yaml").write_text("n: i\n")
        _pipeline_file(wf_repo)

        assert api.get_status(wf_repo).uncommitted_workflow_files == [
            ".agentic/pipelines/a.yaml"
        ]
        result = api.commit_workflow(wf_repo)
        assert result.files == [".agentic/pipelines/a.yaml"]
        # Игнорируемый файл остался на диске и не вошёл в git.
        assert (wf_repo / ".agentic" / "pipelines" / "ignored.yaml").is_file()
        assert _git(wf_repo, "ls-files", ".agentic/pipelines/ignored.yaml") == ""

    def test_runtime_files_are_never_touched(self, wf_repo: Path):
        inbox = wf_repo / ".agentic" / "inbox"
        inbox.mkdir(exist_ok=True)
        (inbox / "TODO-0166.md").write_text("# runtime\n")
        _pipeline_file(wf_repo)

        result = api.commit_workflow(wf_repo)
        assert result.files == [".agentic/pipelines/a.yaml"]
        assert _committed_files(wf_repo) == [".agentic/pipelines/a.yaml"]
        # Runtime-файл жив и по-прежнему untracked (не затянут в git).
        assert (inbox / "TODO-0166.md").is_file()
        assert _git(wf_repo, "ls-files", ".agentic/inbox") == ""

    def test_staged_runtime_file_stays_outside_the_commit(self, wf_repo: Path):
        """R-03: даже tracked+staged runtime-файл не попадает в коммит и не
        unstaged'ится — индекс пользователя не открывается."""
        state = wf_repo / ".agentic" / "state"
        state.mkdir(exist_ok=True)
        (state / "current.yaml").write_text("stage: x\n")
        subprocess.run(
            ["git", "add", "-f", ".agentic/state/current.yaml"], cwd=wf_repo, check=True
        )
        _pipeline_file(wf_repo)

        result = api.commit_workflow(wf_repo)
        assert result.files == [".agentic/pipelines/a.yaml"]
        assert _committed_files(wf_repo) == [".agentic/pipelines/a.yaml"]
        # Чужая staged-запись в индексе пользователя осталась на месте.
        staged = _git(wf_repo, "diff", "--cached", "--name-only")
        assert staged == ".agentic/state/current.yaml", staged

    def test_foreign_staged_source_change_survives(self, wf_repo: Path):
        """A-01: WIP вне .agentic не попадает в workflow-коммит и не теряет staging."""
        (wf_repo / "src").mkdir(exist_ok=True)
        (wf_repo / "src" / "wip.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "src/wip.py"], cwd=wf_repo, check=True)
        _pipeline_file(wf_repo)

        api.commit_workflow(wf_repo)
        assert _committed_files(wf_repo) == [".agentic/pipelines/a.yaml"]
        assert _git(wf_repo, "diff", "--cached", "--name-only") == "src/wip.py"


class TestMessageFormat:
    """Subject совместим с парсерами: начинается ``awf(workflow):``,
    TODO-id в сообщении нет."""

    def test_subject_prefix_and_no_todo_id(self, wf_repo: Path):
        _pipeline_file(wf_repo)
        result = api.commit_workflow(wf_repo)
        subject = result.message.splitlines()[0]
        assert subject.startswith("awf(workflow):"), subject
        assert extract_todo_id(subject) is None
        assert _AWF_COMMIT_RE.match(subject) is None

    def test_message_shape(self):
        from awf.api.workflow_commit import build_workflow_commit_message

        message = build_workflow_commit_message(
            [".agentic/pipelines/a.yaml", ".agentic/roles/qa.md"]
        )
        subject, _, body = message.partition("\n\n")
        assert subject == "awf(workflow): 2 files: pipelines, roles", subject
        assert body == "files: .agentic/pipelines/a.yaml, .agentic/roles/qa.md", body


class TestDelivery:
    """Инвариант 3: MCP-инструмент зарегистрирован, счётчик 53."""

    def test_mcp_tool_registered_counter_53(self):
        names = [s.name for s in _registry.TOOLS]
        assert "awf_commit_workflow" in names
        assert len(names) == 53, f"registry has {len(names)} tools, expected 53"
        counts = _registry.tool_counts()
        assert counts == {"total": 53, "awf": 48, "ui": 5}

    def test_cli_subcommand_registered(self):
        from awf import cli

        parser, _sub = cli._build_parser()
        args = parser.parse_args(["commit-workflow", "--project-dir", "."])
        assert args.command == "commit-workflow"
        assert args.project_dir == "."
