"""REPORTS26 F6 (TODO-0166): ``commit_workflow`` — workflow-определения в git.

Source: awf-feature-20261007-workflow-fayly-pipelines-roles-dobavlennye-v-hode-proekta.md.
Роли/пайплайны/доктрина, добавленные в ходе проекта, не попадают в коммиты
юнитов (коммит юнита несёт только его дифф; read-only юниты не коммитят
вовсе) — переносимый состав ролей/пайплайнов теряется при переносе/бэкапе.
Решение владельца: (б) отдельный инструмент + (в) видимость в статусе.

- :func:`uncommitted_workflow_files` — dry-run: какие workflow-файлы
  ``commit_workflow`` коммитит. Изменённые tracked (vs HEAD) + новые
  untracked (не gitignored) в ``.agentic/``: ``config.yaml``, ``roles/``,
  ``pipelines/``, ``phases/``, ``doctrine/``. Runtime (inbox/outbox/state/
  logs/context/handoff) не трогается — не входит в :data:`WORKFLOW_PATHS`.
- :func:`commit_workflow` — коммитит ровно этот набор через одноразовый
  ``GIT_INDEX_FILE`` (паттерн R-03, как в ``commit_gate``): индекс
  пользователя не открывается — чужие staged-записи ни в коммит не
  попадут, ни не будут unstaged; после успеха — pinpoint-синхронизация
  индекса по путям (R-03-F1). Subject начинается ``awf(workflow):``
  (совместимо с парсерами: TODO-id в сообщении нет —
  ``todo_ids.extract_todo_id`` вернёт None); body — список файлов.
  Пустой набор — понятный отказ (:class:`~awf.api._errors.AwfApiError`).
"""
from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import commit_gate, git_utils, paths
from ..commit_plan import OUTCOME_COMMITTED, CommitPlan
from ._errors import AwfApiError

#: Workflow-определения (относительно корня проекта): единственный список,
#: по которому считаются и dry-run (статус), и состав коммита.
WORKFLOW_PATHS: tuple[str, ...] = (
    ".agentic/config.yaml",
    ".agentic/roles",
    ".agentic/pipelines",
    ".agentic/phases",
    ".agentic/doctrine",
)


@dataclass
class WorkflowCommitResult:
    """Result of :func:`awf.api.commit_workflow`."""

    sha: str
    files: list[str] = field(default_factory=list)
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def uncommitted_workflow_files(project_dir: str | Path) -> list[str]:
    """Dry-run: workflow-файлы, которые ``commit_workflow`` коммитит.

    Изменённые tracked (``git diff HEAD`` — изменённые и удалённые, staged
    и unstaged) + untracked не-gitignored (``ls-files --others
    --exclude-standard``) в :data:`WORKFLOW_PATHS`. Отсортированно,
    дедуплицировано, пути относительно корня проекта. Не-git-репо (или
    репо без HEAD) — пустой список; git-ошибки не поднимаются (статус не
    должен падать из-за git).
    """
    project_dir = Path(project_dir).resolve()
    if not git_utils.is_git_repo(project_dir):
        return []
    files: set[str] = set()
    # Каждый git-вызов деградирует отдельно: статус не должен падать из-за
    # git (зависший git — timeout, битое репо — rc 128).
    try:
        changed = git_utils.git_stdout(
            project_dir, "diff", "--name-only", "-z", "HEAD", "--", *WORKFLOW_PATHS,
            check=False,
        )
        files.update(p for p in changed.split("\0") if p)
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        untracked = git_utils.git_stdout(
            project_dir, "ls-files", "--others", "--exclude-standard", "-z", "--",
            *WORKFLOW_PATHS,
            check=False,
        )
        files.update(p for p in untracked.split("\0") if p)
    except (subprocess.SubprocessError, OSError):
        pass
    return sorted(files)


def _group_of(rel_path: str) -> str:
    """Группа файла для subject: ``config.yaml`` или имя каталога (``roles``)."""
    rel = rel_path.removeprefix(".agentic/")
    return rel.split("/", 1)[0]


def build_workflow_commit_message(files: list[str]) -> str:
    """Сообщение коммита: subject ``awf(workflow): <n> file(s): <группы>``.

    Subject совместим с парсерами (``todo_ids.extract_todo_id`` — None:
    TODO-id в сообщении нет). Группы — по :data:`WORKFLOW_PATHS` в
    порядке первого вхождения; при пяти группах subject ≤ 71 символ.
    Body — список файлов (та же форма строки, что у unit-коммитов
    ``commit_gate.build_commit_message``).
    """
    groups: list[str] = []
    for f in files:
        g = _group_of(f)
        if g not in groups:
            groups.append(g)
    n = len(files)
    plural = "" if n == 1 else "s"
    subject = f"awf(workflow): {n} file{plural}: {', '.join(groups)}"
    return f"{subject}\n\nfiles: {', '.join(files)}"


def commit_workflow(project_dir: str | Path) -> WorkflowCommitResult:
    """Коммитит не-игнорируемые workflow-определения ``.agentic/``.

    Состав — :func:`uncommitted_workflow_files` (изменённые tracked +
    новые untracked, не gitignored). Коммит идёт через одноразовый
    ``GIT_INDEX_FILE`` (R-03): индекс пользователя не открывается, чужие
    staged-записи не затрагиваются. Runtime-файлы (inbox/outbox/state/
    logs/context/handoff) в коммит не попадают — путь мимо
    :data:`WORKFLOW_PATHS`.

    Args:
        project_dir: корень проекта (корень git-репо).

    Raises:
        AwfApiError: не-git-репо, нет ``.agentic/``, пустой набор (понятный
            отказ), git-ошибка коммита.
    """
    project_dir = Path(project_dir).resolve()
    if not git_utils.is_git_repo(project_dir):
        raise AwfApiError(
            f"not a git repo: {project_dir} — cannot commit workflow files"
        )
    if not paths.agentic_dir(project_dir).is_dir():
        raise AwfApiError("no .agentic/ in the project — run awf_init first")
    files = uncommitted_workflow_files(project_dir)
    if not files:
        raise AwfApiError(
            "no uncommitted workflow files — .agentic/config.yaml, roles/, "
            "pipelines/, phases/ and doctrine/ are clean (or gitignored); "
            "nothing to commit"
        )
    message = build_workflow_commit_message(files)
    plan = CommitPlan(
        todo_id="workflow", generation=0, verified_sha="", files=tuple(files)
    )
    outcome = commit_gate._commit_via_isolated_index(project_dir, plan, message)
    if outcome.status == OUTCOME_COMMITTED:
        return WorkflowCommitResult(sha=outcome.sha, files=files, message=message)
    raise AwfApiError(
        f"workflow commit {outcome.status}: {outcome.reason}"
    )


__all__ = [
    "WORKFLOW_PATHS",
    "WorkflowCommitResult",
    "build_workflow_commit_message",
    "commit_workflow",
    "uncommitted_workflow_files",
]
