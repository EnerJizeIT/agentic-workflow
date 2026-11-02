"""T1: набор имён MCP заморожен — 54 имени (49 ``awf_*`` + 5 UI).

Эталон — база ``40bb6cc``: 56 имён. IMP1 снимает ровно два дубля,
поэтому 54. Тест красный на добавление любого нового имени, на снятие
другого тула и на тихое возвращение дубля.
"""
from __future__ import annotations

import pytest
from agent_workflow_ui.tools import registry

pytest.importorskip("mcp")
from agent_workflow_ui.server import create_server  # noqa: E402

#: База ``40bb6cc``: 56 имён до IMP1.
BASE_TOOL_NAMES: frozenset[str] = frozenset({
    # UI tools
    "open_form",
    "read_submit",
    "cancel_form",
    "list_pending_forms",
    "list_templates",
    # awf workflow tools
    "awf_init",
    "awf_status",
    "awf_start",
    "awf_continue",
    "awf_retry_stage",
    "awf_baseline",
    "awf_rollback",
    "awf_approve",
    "awf_reject",
    "awf_report",
    "awf_reset",
    "awf_add_role",
    "awf_analyze_roles",
    "awf_dispatch_todo",
    "awf_load_supervisor_context",
    "awf_open_project_setup_form",
    "awf_open_increment_planning_form",
    "awf_open_pipeline_dashboard",
    "awf_wait_for_event",
    "awf_check_model_config",
    "awf_doctor",
    "awf_kill",
    "awf_current_step",
    "awf_brief",
    "awf_set_goal",
    "awf_confirm_normalized",
    "awf_run_start",
    "awf_run_status",
    "awf_run_next",
    "awf_run_finish",
    "awf_run_note",
    "awf_run_revise",
    "awf_run_service_start",
    "awf_run_service_status",
    "awf_run_service_finish",
    "awf_run_service_approve",
    "awf_restore",
    "awf_unblock",
    "awf_todo_remove",
    "awf_todo_retire",
    "awf_todo_update",
    "awf_review_append",
    "awf_blocked_draft",
    "awf_tree_sha",
    "awf_prove_red",
    "awf_verify_pack",
    "awf_metrics",
    "awf_write_pipeline",
    "awf_pipelines",
    "awf_feedback",
    "awf_commit_workflow",
})

REMOVED: frozenset[str] = frozenset({
    "awf_current_step",
    "awf_load_supervisor_context",
})

EXPECTED: frozenset[str] = BASE_TOOL_NAMES - REMOVED


def test_registry_has_exactly_54_names() -> None:
    names = registry.tool_names()
    assert len(names) == 54, names
    assert len(set(names)) == 54, "дубль имени в реестре"
    assert set(names) == EXPECTED


def test_difference_from_base_is_exactly_the_two_removed() -> None:
    assert BASE_TOOL_NAMES - set(registry.tool_names()) == REMOVED


def test_counts_are_49_awf_plus_5_ui() -> None:
    assert registry.tool_counts() == {"total": 54, "awf": 49, "ui": 5}


def test_removed_names_are_not_in_the_registry() -> None:
    names = set(registry.tool_names())
    assert not (names & REMOVED)


def test_tool_map_agrees_with_the_registry() -> None:
    from awf import brief

    assert brief.map_coverage() == {"missing_from_map": [], "unknown_in_map": []}
    dumped = repr(brief.load_tool_map())
    for name in REMOVED:
        assert name not in dumped


def test_server_registers_the_same_names() -> None:
    server = create_server()
    assert set(server._tool_manager._tools) == EXPECTED
