"""Public API for agentic-workflow.

All external callers (CLI, MCP plugin) MUST go through this module.
Business logic lives in submodules — this file re-exports the public
surface so callers can ``from awf import api; api.init_project(...)``.

Layout:
- ``_errors``      — AwfApiError
- ``_results``     — Result dataclasses (InitResult, StatusResult,
                     DispatchTodoResult, ApplyIncrementPlanResult, ...)
- ``_stack``       — detect_stack, derive_project_name
- ``_templates``   — _CONFIG_TEMPLATE, _ROLE_TEMPLATE, update_gitignore
- ``_helpers``     — require_agentic, require_git_repo, read helpers
- ``_background``  — start_in_background, check_pipeline_running, PipelineArgs
- ``lifecycle``    — init_project, get_status, get_report, reset_runtime,
                     list_orphans, remove_orphans
- ``pipeline``     — start_pipeline, continue_pipeline, create_baseline,
                     rollback, approve_commit
- ``roles``        — add_role, analyze_roles
- ``setup``        — apply_project_setup (project-setup form materialization)
- ``dispatch``     — dispatch_todo (atomic TODO + baseline + signal)
- ``context``      — load_supervisor_context (aggregate bootstrap payload),
                     _extract_stage_info, _compute_expected_action
- ``planning``     — apply_increment_plan (increment variant persistence)

Error convention: every function returns a Result dataclass (success)
or raises AwfApiError with a human-readable message. Callers wrap in
try/except.
"""
from __future__ import annotations

# Public types
from ._errors import AwfApiError
from ._results import (
    AddRoleResult,
    AnalyzeRolesResult,
    ApplyIncrementPlanResult,
    ApplyProjectSetupResult,
    ApproveResult,
    BaselineResult,
    DispatchTodoResult,
    InitResult,
    ReportResult,
    ResetResult,
    RollbackResult,
    StartResult,
    StatusResult,
    SupervisorContextResult,
    WaitEventResult,
)

# Public functions — organized by submodule for clarity
from ._stack import derive_project_name, detect_stack
from .context import load_supervisor_context
from .dispatch import dispatch_todo
from .lifecycle import (
    get_report,
    get_status,
    init_project,
    list_orphans,
    remove_orphans,
    reset_runtime,
)
from .model_check import check_model_config
from .pipeline import (
    approve_commit,
    continue_pipeline,
    create_baseline,
    kill_pipeline,
    rollback,
    start_pipeline,
)
from .planning import apply_increment_plan
from .roles import add_role, analyze_roles
from .setup import apply_project_setup
from .wait_event import wait_for_event

__all__ = [
    # Exception
    "AwfApiError",
    # Result dataclasses
    "InitResult",
    "StatusResult",
    "BaselineResult",
    "RollbackResult",
    "ApproveResult",
    "ReportResult",
    "ResetResult",
    "AddRoleResult",
    "StartResult",
    "AnalyzeRolesResult",
    "ApplyProjectSetupResult",
    "DispatchTodoResult",
    "SupervisorContextResult",
    "ApplyIncrementPlanResult",
    "WaitEventResult",
    # Stack detection
    "detect_stack",
    "derive_project_name",
    # Lifecycle
    "init_project",
    "get_status",
    "get_report",
    "reset_runtime",
    "list_orphans",
    "remove_orphans",
    # Pipeline
    "start_pipeline",
    "continue_pipeline",
    "kill_pipeline",
    "create_baseline",
    "rollback",
    "approve_commit",
    # Roles
    "add_role",
    "analyze_roles",
    # Project-setup materialization
    "apply_project_setup",
    # TODO dispatch + supervisor context (dogfood-2 automation)
    "dispatch_todo",
    "load_supervisor_context",
    # Increment planning (dogfood-7 — user picks decomposition variant)
    "apply_increment_plan",
    # Supervisor wake-up (DASH Phase 3 — no more polling)
    "wait_for_event",
    # Model configuration validation
    "check_model_config",
]
