"""R1.4 (F-R2, требование 6): preview разрушения и подтверждение плана.

CLI/MCP не сносят состояние «вслепую»: сначала :func:`preview_destruction`
со scope (что именно будет удалено), ``expected_revision`` (CAS-токен
StateStore) и digest плана; исполнение принимает ТОТ ЖЕ план
(``confirm`` = digest). Пока digest не совпал — отказ. Изменение состояния
(revision сдвинулся) инвалидирует preview: ``execute_destruction`` обязан
сделать новый.

Legacy-путь (``confirm_digest=None``) сохраняется: внутренние доверенные
вызовы (reset → remove_orphans, тесты) не обязаны проходить preview.
Адаптер, требующий решения человека, идёт через
:func:`execute_destruction` — он фиксирует источник решения (``decided_by``).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .. import paths
from ..infra import state_store
from ._errors import AwfApiError

#: Каталоги, которые чистит ``reset_runtime``. Единый источник —
#: :func:`reset_dirs` ниже; её же зовёт ``lifecycle``, поэтому scope в
#: preview не может разойтись с фактическим удалением.
RESET_BASE_DIRS = ["inbox", "outbox", "context", "logs"]
RESET_FULL_EXTRA = ["handoff", "inputs", "dashboards"]
#: Runtime-содержимое, которое сносит ``init_project(force=True)``.
#: Единый источник: ``_clean_runtime`` в ``lifecycle`` берёт список отсюда
#: (force-init и его preview обязаны удалять одно и то же).
INIT_RUNTIME_DIRS = [
    "inbox", "outbox", "handoff", "done", "state",
    "logs", "context", "dashboards", "inputs",
]
#: A-1: ``init_project(force=True)`` после сноса runtime падает в общий
#: путь инициализации и ПЕРЕЗАПИСЫВАЕТ скелет — эти файлы, а не только
#: каталоги выше. Без них подтверждающий digest не видел бы перегенерацию
#: ``config.yaml`` (требование 6: «preview недоговаривает план»).
INIT_REWRITTEN_FILES = [
    ".agentic/config.yaml",
    ".agentic/roles/supervisor.md",
    ".agentic/phases/plan.md",
]
#: Проекции, которые reset всегда очищает в ``state/``.
RESET_STATE_FILES = ["state/current.yaml", "state/run.yaml"]

_KINDS = ("reset", "init", "remove-orphans", "rollback")


@dataclass(frozen=True)
class DestructivePreview:
    """План разрушения: scope, ожидаемая revision и digest плана."""

    kind: str
    mode: str
    scope: list[str]
    expected_revision: int
    digest: str
    label: str = ""
    todo_id: str = ""


def reset_dirs(*, tasks_only: bool, full: bool) -> list[str]:
    """Какие каталоги сносит reset в этом режиме (единый источник)."""
    if full or not tasks_only:
        dirs = list(RESET_BASE_DIRS)
        if full:
            dirs += RESET_FULL_EXTRA
        return dirs
    return ["inbox", "outbox"]


def reset_mode(*, tasks_only: bool, full: bool) -> str:
    """Режим reset: ``full`` / ``default`` / ``tasks_only``."""
    if full:
        return "full"
    return "default" if not tasks_only else "tasks_only"


def _revision(project_dir: Path) -> int:
    snap = state_store.read_snapshot(project_dir)
    return int(snap.revision) if snap.status == "valid" else 0


def _digest(payload: dict) -> str:
    blob = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def preview_destruction(
    project_dir: str | Path,
    kind: str,
    *,
    mode: str = "",
    tasks_only: bool = False,
    full: bool = False,
    orphan_ids: list[str] | None = None,
    baseline_sha: str = "",
    todo_id: str = "",
) -> DestructivePreview:
    """Read-only план разрушения: чего коснётся, при какой revision, digest.

    Ничего не меняет (в т.ч. не создаёт ``state/``): безопасно звать до
    подтверждения. ``expected_revision`` — CAS-токен StateStore на момент
    плана; сдвиг revision означает, что план устарел.
    """
    project = Path(project_dir).resolve()
    if kind == "reset":
        rmode = reset_mode(tasks_only=tasks_only, full=full)
        scope = [
            f".agentic/{d}/"
            for d in reset_dirs(tasks_only=tasks_only, full=full)
        ]
        scope += [f".agentic/{s}" for s in RESET_STATE_FILES]
        # F-5: reset also clears the Store MIRROR of the active run
        # (``state_store.clear_active_run``), not just the two projection
        # files — name it so the preview does not under-describe the plan.
        # The durable ``run_generation`` counter is deliberately preserved.
        scope.append(
            "store: clear active run mirror (run_generation counter preserved)"
        )
        label = f"reset ({rmode})"
    elif kind == "init":
        scope = [f".agentic/{d}/" for d in INIT_RUNTIME_DIRS]
        # A-1: force-init rewrites the skeleton config/roles/phases files too
        # (fall-through in ``lifecycle.init_project``) — name them so the
        # confirmed digest covers the actual plan, not just the wipe.
        scope += list(INIT_REWRITTEN_FILES)
        label = "init_project(force=True)"
        rmode = "force"
    elif kind == "remove-orphans":
        scope = sorted(orphan_ids or [])
        label = f"remove_orphans ({len(scope)})"
        rmode = ""
    elif kind == "rollback":
        if not baseline_sha and todo_id:
            sha_file = paths.context_dir(project) / f"BASELINE-{todo_id}.sha"
            try:
                baseline_sha = sha_file.read_text(encoding="utf-8").strip()
            except OSError:
                baseline_sha = ""
        scope = [
            f"git reset to {baseline_sha or '<baseline>'}",
            f"mode={mode}",
        ]
        label = f"rollback({mode})"
        rmode = mode
    else:
        raise AwfApiError(
            f"unknown destruction kind {kind!r}; expected one of {list(_KINDS)}"
        )
    expected_revision = _revision(project)
    digest = _digest(
        {
            "kind": kind,
            "mode": rmode,
            "scope": scope,
            "expected_revision": expected_revision,
        }
    )
    return DestructivePreview(
        kind=kind,
        mode=rmode,
        scope=scope,
        expected_revision=expected_revision,
        digest=digest,
        label=label,
        todo_id=todo_id,
    )


def verify_confirm(
    project_dir: str | Path,
    kind: str,
    confirm_digest: str | None,
    *,
    mode: str = "",
    tasks_only: bool = False,
    full: bool = False,
    orphan_ids: list[str] | None = None,
    baseline_sha: str = "",
    todo_id: str = "",
) -> None:
    """Сверить ``confirm_digest`` со СВЕЖИМ планом (вызывается под секцией).

    ``None`` — legacy-путь без подтверждения (доверенный внутренний вызов),
    проверок нет. Иначе — план пересчитывается здесь же, под критической
    секцией, и несовпадение digest (в т.ч. сдвиг revision = stale preview)
    отвергается ДО первого удаления.
    """
    if confirm_digest is None:
        return
    fresh = preview_destruction(
        project_dir,
        kind,
        mode=mode,
        tasks_only=tasks_only,
        full=full,
        orphan_ids=orphan_ids,
        baseline_sha=baseline_sha,
        todo_id=todo_id,
    )
    if fresh.digest != confirm_digest:
        raise AwfApiError(
            "destructive execution refused — stale preview: the state moved "
            "since the plan was confirmed (now at revision "
            f"{fresh.expected_revision}); re-run the preview and confirm the "
            "new digest."
        )


def format_preview(plan: DestructivePreview) -> str:
    """Человекочитаемый preview для CLI/MCP (scope + revision + digest)."""
    lines = [f"{plan.label} — preview (nothing removed yet)"]
    lines.append(f"  expected store revision: {plan.expected_revision}")
    lines.append("  will remove / change:")
    for item in plan.scope:
        lines.append(f"    - {item}")
    lines.append(f"  confirm digest: {plan.digest}")
    lines.append("  re-run with --confirm-digest <digest> to execute")
    return "\n".join(lines)


def _record_decision(
    project: Path, plan: DestructivePreview, decided_by: str
) -> None:
    """Зафиксировать источник решения в журнале проекта.

    Пишется ПОСЛЕ разрушения: ``reset``/``init`` чистят ``.agentic/logs``
    вместе с runtime, поэтому запись «до» была бы стёрта тем самым
    действием, о котором она сообщает. ``dry-run`` ничего не разрушает и
    не записывается; в проекте без ``.agentic`` журнал не создаётся.
    """
    if not decided_by or plan.mode == "dry-run":
        return
    if not (project / ".agentic").is_dir():
        return
    try:
        from .._log import log as _log

        _log(
            paths.agentic_dir(project) / "logs",
            f"[destructive] kind={plan.kind} mode={plan.mode} "
            f"decided_by={decided_by} digest={plan.digest[:12]}",
        )
    except Exception:  # noqa: BLE001 — журнал не должен ломать снос
        pass


def execute_destruction(
    project_dir: str | Path,
    plan: DestructivePreview | None,
    *,
    confirm: str = "",
    decided_by: str = "",
    apply_kwargs: dict | None = None,
):
    """Исполнить разрушение ТОЛЬКО по подтверждённому плану.

    Отказ, если плана нет или ``confirm`` не совпал с его digest. Свежесть
    подтверждается повторно под критической секцией (в самом действии):
    между preview и исполнением состояние могло сдвинуться. ``decided_by``
    — источник решения человека (``"cli:reset"``, ``"mcp:awf_reset"`` …);
    пишется в журнал проекта ПОСЛЕ операции (:func:`_record_decision`).
    ``apply_kwargs`` — довесок к действию (force-init передаёт сюда уже
    собранные ``project_name``/``test_cmd``/… из интерактивного CLI).
    """
    if not isinstance(plan, DestructivePreview):
        raise AwfApiError(
            "destructive execution refused — a preview plan is required: "
            "call preview_destruction first and pass its result."
        )
    if confirm != plan.digest:
        raise AwfApiError(
            "destructive execution refused — the confirmation digest does not "
            "match the preview. Re-run the preview and confirm the exact plan."
        )
    project = Path(project_dir).resolve()
    extra = dict(apply_kwargs or {})

    from . import lifecycle, pipeline

    if plan.kind == "reset":
        result = lifecycle.reset_runtime(
            project,
            tasks_only=plan.mode == "tasks_only",
            full=plan.mode == "full",
            confirm_digest=confirm,
        )
    elif plan.kind == "init":
        result = lifecycle.init_project(
            project, force=True, confirm_digest=confirm, **extra
        )
    elif plan.kind == "remove-orphans":
        result = lifecycle.remove_orphans(
            project, list(plan.scope), confirm_digest=confirm
        )
    elif plan.kind == "rollback":
        result = pipeline.rollback(
            project, plan.todo_id, mode=plan.mode, confirm_digest=confirm
        )
    else:
        raise AwfApiError(f"unknown destruction kind {plan.kind!r}")

    _record_decision(project, plan, decided_by)
    return result


__all__ = [
    "DestructivePreview",
    "execute_destruction",
    "format_preview",
    "preview_destruction",
    "reset_dirs",
    "reset_mode",
    "verify_confirm",
]
