"""Public API for agentic-workflow.

All external callers (CLI, MCP plugin) MUST go through this module.
Business logic lives in submodules — this file re-exports the public
surface so callers can ``from awf import api; api.init_project(...)``.

Layout:
- ``_errors``    — AwfApiError
- ``_results``   — Result dataclasses (InitResult, StatusResult, ...)
- ``_stack``     — detect_stack, derive_project_name
- ``_templates`` — _CONFIG_TEMPLATE, _ROLE_TEMPLATE, update_gitignore
- ``_helpers``   — require_agentic, require_git_repo, read helpers
- ``_background`` — start_in_background, check_pipeline_running, PipelineArgs
- ``lifecycle``  — init_project, get_status, get_report, reset_runtime,
                   list_orphans, remove_orphans
- ``pipeline``   — start_pipeline, continue_pipeline, create_baseline,
                   rollback, approve_commit
- ``roles``      — add_role, analyze_roles

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
    ApplyProjectSetupResult,
    ApproveResult,
    BaselineResult,
    InitResult,
    ReportResult,
    ResetResult,
    RollbackResult,
    StartResult,
    StatusResult,
)

# Public functions — organized by submodule for clarity
from ._stack import derive_project_name, detect_stack
from .lifecycle import (
    get_report,
    get_status,
    init_project,
    list_orphans,
    remove_orphans,
    reset_runtime,
)
from .pipeline import (
    approve_commit,
    continue_pipeline,
    create_baseline,
    rollback,
    start_pipeline,
)
from .roles import add_role, analyze_roles
from .setup import apply_project_setup

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
    "create_baseline",
    "rollback",
    "approve_commit",
    # Roles
    "add_role",
    "analyze_roles",
    # Project-setup materialization
    "apply_project_setup",
]
