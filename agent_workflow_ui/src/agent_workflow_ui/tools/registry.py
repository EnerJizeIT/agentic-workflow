"""R-06 (TODO-0112): единый реестр MCP tools — источник регистрации.

``TOOLS`` — единственный список, который читает ``create_server()``
(``server.py``) и docs-счёт A-08 (``test_audit25_w6_docs_ci.py``, строка
счётчиков в USAGE.md/USAGE.ru.md). Метаданные сервера и доков больше не
разъезжаются: добавление/переименование инструмента — одна правка здесь,
и либо тесты, либо docs-счёт упадут, либо MCP-контракт изменится явно.

Каждый элемент — ``ToolSpec(name, fn, description)``:
- ``name`` — имя инструмента в MCP (ключ ``add_tool(name=...)``);
- ``fn`` — обёртка из ``tools/awf.py`` / ``tools/forms.py`` /
  ``tools/templates.py``; сигнатуры и docstrings обёрток не трогаются —
  внешний MCP-контракт неизменен (docstring — полное описание, его
  видит модель);
- ``description`` — краткое описание (первая строка docstring),
  метаданные для доков и проверок; выводится из ``fn.__doc__``, чтобы
  не дублировать и не разъезжаться с полным описанием.

Никаких генераторов: инструменты остаются явными функциями, реестр —
только список регистрации + метаданные.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import awf, forms, templates


@dataclass(frozen=True)
class ToolSpec:
    """Одна строка реестра: имя, функция-обёртка, краткое описание."""

    name: str
    fn: Callable[..., Any]
    description: str


def _first_line(doc: str | None) -> str:
    """Первая непустая строка docstring — краткое описание."""
    for line in (doc or "").strip().splitlines():
        if line.strip():
            return line.strip()
    return ""


def _spec(name: str, fn: Callable[..., Any]) -> ToolSpec:
    return ToolSpec(name=name, fn=fn, description=_first_line(fn.__doc__))


#: Единственный источник регистрации MCP tools (порядок — как в
#: ``create_server()`` до R-06: UI-инструменты, затем awf workflow).
TOOLS: tuple[ToolSpec, ...] = (
    # ── UI tools: form lifecycle ─────────────────────────────────────────
    _spec("open_form", forms.open_form),
    _spec("read_submit", forms.read_submit),
    _spec("cancel_form", forms.cancel_form),
    _spec("list_pending_forms", forms.list_pending_forms),
    # ── UI tools: template discovery ─────────────────────────────────────
    _spec("list_templates", templates.list_templates),
    # ── awf workflow tools (MCP-3) ───────────────────────────────────────
    _spec("awf_init", awf.awf_init),
    _spec("awf_status", awf.awf_status),
    _spec("awf_start", awf.awf_start),
    _spec("awf_continue", awf.awf_continue),
    _spec("awf_retry_stage", awf.awf_retry_stage),
    _spec("awf_baseline", awf.awf_baseline),
    _spec("awf_rollback", awf.awf_rollback),
    _spec("awf_approve", awf.awf_approve),
    _spec("awf_reject", awf.awf_reject),
    _spec("awf_report", awf.awf_report),
    _spec("awf_reset", awf.awf_reset),
    _spec("awf_add_role", awf.awf_add_role),
    _spec("awf_analyze_roles", awf.awf_analyze_roles),
    # Dogfood-2 automation: dispatch + context
    _spec("awf_dispatch_todo", awf.awf_dispatch_todo),
    _spec("awf_load_supervisor_context", awf.awf_load_supervisor_context),
    # Dogfood-5: shortcut tools (deterministic UX, no prompt-only guidance)
    _spec("awf_open_project_setup_form", awf.awf_open_project_setup_form),
    # Dogfood-7: increment planning (user picks decomposition variant)
    _spec("awf_open_increment_planning_form", awf.awf_open_increment_planning_form),
    # DASH Phase 2: pipeline dashboard
    _spec("awf_open_pipeline_dashboard", awf.awf_open_pipeline_dashboard),
    # DASH Phase 3: supervisor wake-up (no more polling)
    _spec("awf_wait_for_event", awf.awf_wait_for_event),
    # Model configuration validation (dogfood-10)
    _spec("awf_check_model_config", awf.awf_check_model_config),
    # Kill pipeline (SELF-2)
    _spec("awf_kill", awf.awf_kill),
    # SMO: Phase-aware supervisor tools
    _spec("awf_current_step", awf.awf_current_step),
    # RUN4 #1: supervisor onboarding/recovery card (live state + tool map)
    _spec("awf_brief", awf.awf_brief),
    _spec("awf_set_goal", awf.awf_set_goal),
    _spec("awf_confirm_normalized", awf.awf_confirm_normalized),
    # SPEC A-run: autonomous run (забег)
    _spec("awf_run_start", awf.awf_run_start),
    _spec("awf_run_status", awf.awf_run_status),
    _spec("awf_run_next", awf.awf_run_next),
    _spec("awf_run_finish", awf.awf_run_finish),
    _spec("awf_run_note", awf.awf_run_note),
    # ORCH M3.4: queue revision (preview + apply by idempotency key)
    _spec("awf_run_revise", awf.awf_run_revise),
    # ORCH M5.2: service run (role creation during the main run)
    _spec("awf_run_service_start", awf.awf_run_service_start),
    _spec("awf_run_service_status", awf.awf_run_service_status),
    _spec("awf_run_service_finish", awf.awf_run_service_finish),
    _spec("awf_run_service_approve", awf.awf_run_service_approve),
    _spec("awf_restore", awf.awf_restore),
    # RUN3 #4/#5: state hygiene (stale closures, never-started removal)
    _spec("awf_unblock", awf.awf_unblock),
    _spec("awf_todo_remove", awf.awf_todo_remove),
    # RUN5 #2: retire a rejected/abandoned TODO that stays "active"
    _spec("awf_todo_retire", awf.awf_todo_retire),
    # RUN6 #4: reword a not-started TODO (number/.ready/baseline preserved)
    _spec("awf_todo_update", awf.awf_todo_update),
    # RUN6 #4: working-tree fingerprint (MCP parity for the CLI tree-sha)
    _spec("awf_tree_sha", awf.awf_tree_sha),
    # U4: machine proof that tests are red on the baseline sha
    _spec("awf_prove_red", awf.awf_prove_red),
    # U5: one deterministic verify report (GATES-<todo>.md)
    _spec("awf_verify_pack", awf.awf_verify_pack),
    # U8: token/cost metrics of the work program (report to desktop)
    _spec("awf_metrics", awf.awf_metrics),
    # RUN3 #1: named pipelines (create + list; run by name via awf_start)
    _spec("awf_write_pipeline", awf.awf_write_pipeline),
    _spec("awf_pipelines", awf.awf_pipelines),
    # RUN4 #2: feedback contour (supervisor friction → report to owner)
    _spec("awf_feedback", awf.awf_feedback),
)


def tool_specs() -> tuple[ToolSpec, ...]:
    """Реестр (неизменяемый кортеж ``ToolSpec``)."""
    return TOOLS


def tool_names() -> list[str]:
    """Имена инструментов реестра, в порядке регистрации."""
    return [spec.name for spec in TOOLS]


def tool_counts() -> dict[str, int]:
    """Счётчики для docs-проверки A-08: total / awf_* / UI."""
    names = tool_names()
    awf_count = sum(1 for n in names if n.startswith("awf_"))
    return {"total": len(names), "awf": awf_count, "ui": len(names) - awf_count}
