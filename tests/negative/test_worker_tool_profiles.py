"""ORCH M2.2 (V-03, аудит 27.09): профили tools по стадии.

До: все 47 MCP-tools были видны любой сессии — execute-воркер по
случаю мог approve-нуть свой же юнит, убить пайплайн или диспатчить
новый TODO. Решение владельца: честная формулировка — «снижение
случайных ошибок, не запрет».

Механизм (проба с живым opencode): в 2.x ``permissions`` — упорядоченный
список правил ``{action, resource, effect}``, побеждает последнее
совпадение. Профайл подаётся в ``OPENCODE_CONFIG_CONTENT`` правилами
стадии; ``locked_deny`` идёт последним — только для execute-стадий;
plan/verify сохраняют полный набор. Имена действий 2.x: ``shell``
(бывший ``bash``), ``edit`` (бывшие ``edit``/``write``).

Список :data:`CONTROL_TOOL_NAMES` — спека юнита (что реально управляет
вердиктом/состоянием), а не копия production-константы: тест 1
проверяет конфиг стадии именно по этой спеке.

Покрывают:
- test_execute_stage_config_hides_control_tools — конфиг execute-стадии
  запрещает управляющие tools для агента стадии (prove_red)
- test_plan_verify_stages_keep_full_toolset — в конфиге plan/verify
  запретов нет (супервизор сохраняет полный набор)
- test_execute_config_keeps_shell_edit — shell/edit остаются
  разрешёнными («shell остаётся»)
- test_control_tools_cover_run_control_surface — production-список —
  суперсет управляющей поверхности из TODO (страховка от сужения)
"""
from __future__ import annotations

import json
from pathlib import Path

from awf._env import awf_subprocess_env

MCP_CONTROL_SERVER = "agent-workflow-ui"

# ORCH M2.2: управляющие tools (вердикты, жизненный цикл пайплина/юнита/
# забега, state проекта и фаз).
CONTROL_TOOL_NAMES = (
    "awf_approve",
    "awf_reject",
    "awf_start",
    "awf_continue",
    "awf_kill",
    "awf_retry_stage",
    "awf_reset",
    "awf_rollback",
    "awf_restore",
    "awf_unblock",
    "awf_init",
    "awf_baseline",
    "awf_dispatch_todo",
    "awf_todo_remove",
    "awf_todo_retire",
    "awf_todo_update",
    "awf_run_start",
    "awf_run_next",
    "awf_run_finish",
    "awf_run_note",
    "awf_set_goal",
    "awf_confirm_normalized",
)


def _env_project(base: Path, monkeypatch, name: str) -> Path:
    """Проект с .agentic/ и изолированным XDG (без user's opencode.json)."""
    proj = base / name
    ag = proj / ".agentic"
    ag.mkdir(parents=True)
    (ag / "config.yaml").write_text("project:\n  name: test\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "xdg-nothing"))
    return proj


def _config(env: dict) -> dict:
    return json.loads(env["OPENCODE_CONFIG_CONTENT"])


def _effect(rules: list[dict], action: str) -> str | None:
    """The effect of the last matching rule (2.x: last match wins)."""
    effect = None
    for rule in rules:
        if rule.get("action") == action and rule.get("resource") == "*":
            effect = rule.get("effect")
    return effect


def _agent_permission(env: dict, agent: str) -> dict:
    """The agent's rules as an action→effect map (last match wins)."""
    rules = _config(env).get("agents", {}).get(agent, {}).get("permissions", [])
    resolved: dict[str, str] = {}
    for rule in rules:
        if rule.get("resource") == "*":
            resolved[str(rule.get("action"))] = str(rule.get("effect"))
    return resolved


def _execute_env(proj: Path) -> dict:
    """Вызов ровно как в agent_stage для execute-стадии.

    Fallback на старую сигнатуру даёт на baseline ASSERTION failure,
    а не TypeError — это реальный красный для prove-red.
    """
    try:
        return awf_subprocess_env(
            role="agent-implementer",
            project_dir=proj,
            agent_name="worker",
            restrict_control_tools=True,
        )
    except TypeError:  # baseline до фикса: параметров ещё нет
        return awf_subprocess_env(role="agent-implementer", project_dir=proj)


def test_execute_stage_config_hides_control_tools(tmp_path, monkeypatch):
    """Конфиг execute-стадии запрещает управляющие tools для агента."""
    proj = _env_project(tmp_path, monkeypatch, "execute")
    perm = _agent_permission(_execute_env(proj), "worker")
    missing = [
        key
        for key in (f"{MCP_CONTROL_SERVER}_{t}" for t in CONTROL_TOOL_NAMES)
        if perm.get(key) != "deny"
    ]
    assert not missing, f"execute-конфиг не запрещает управляющие tools: {missing}"


def test_plan_verify_stages_keep_full_toolset(tmp_path, monkeypatch):
    """plan/verify: запретов в конфиге нет — полный набор сохраняется."""
    proj = _env_project(tmp_path, monkeypatch, "plan")
    env = awf_subprocess_env(role="supervisor", project_dir=proj)
    denies: dict[str, str] = {}
    for agent_def in (_config(env).get("agents") or {}).values():
        if isinstance(agent_def, dict) and isinstance(agent_def.get("permissions"), list):
            denies.update(
                {r["action"]: r["effect"] for r in agent_def["permissions"]
                 if r.get("effect") == "deny"}
            )
    assert not denies, f"в конфиге не-execute стадии появились запреты: {denies}"


def test_execute_config_keeps_shell_edit(tmp_path, monkeypatch):
    """Профайл не забирает shell/edit — «shell остаётся»."""
    proj = _env_project(tmp_path, monkeypatch, "keep")
    rules = _config(_execute_env(proj))["permissions"]
    assert _effect(rules, "shell") == "allow"
    assert _effect(rules, "edit") == "allow"


def test_control_tools_cover_run_control_surface():
    """Production-список — суперсет управляющей поверхности из TODO."""
    from awf._env import CONTROL_TOOLS  # до фикса константы нет — только в дереве после

    surface = {
        "awf_approve",
        "awf_reject",
        "awf_kill",
        "awf_reset",
        "awf_rollback",
        "awf_restore",
        "awf_run_start",
        "awf_run_next",
        "awf_run_finish",
        "awf_run_note",
        "awf_todo_remove",
        "awf_todo_retire",
        "awf_todo_update",
        "awf_dispatch_todo",
    }
    missing = surface - set(CONTROL_TOOLS)
    assert not missing, f"из списка выпала управляющая поверхность: {missing}"
