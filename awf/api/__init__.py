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
- ``pipelines``    — write_pipeline, list_pipelines (RUN3 #1 named pipelines)
- ``feedback``     — feedback (RUN4 #2 feedback contour: report to owner)
- ``brief``        — brief (RUN4 #1 supervisor onboarding/recovery card)

Error convention: every function returns a Result dataclass (success)
or raises AwfApiError with a human-readable message. Callers wrap in
try/except.
"""
from __future__ import annotations

from ..brief import BriefResult
from ..feedback import FeedbackResult, feedback
from ..metrics import MetricsResult, collect_metrics
from ..prove_red import ProveRedResult, prove_red
from ..verify_pack import VerifyPackResult, verify_pack

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
    ListPipelinesResult,
    RejectResult,
    RemoveTodoResult,
    ReportResult,
    ResetResult,
    RestoreResult,
    RetireTodoResult,
    RollbackResult,
    RunFinishResult,
    RunNextResult,
    RunStartResult,
    RunStatusResult,
    StartResult,
    StatusResult,
    SupervisorContextResult,
    UnblockResult,
    UpdateTodoResult,
    WaitEventResult,
    WritePipelineResult,
)

# Public functions — organized by submodule for clarity
from ._stack import derive_project_name, detect_stack
from .brief import brief
from .context import load_supervisor_context
from .dispatch import dispatch_todo
from .hygiene import remove_todo, retire_todo, unblock_todo, update_todo
from .lifecycle import (
    get_report,
    get_status,
    init_project,
    list_orphans,
    remove_orphans,
    reset_runtime,
    restore_todo,
)
from .model_check import check_model_config
from .pipeline import (
    approve_commit,
    continue_pipeline,
    create_baseline,
    kill_pipeline,
    reject_commit,
    retry_stage,
    rollback,
    start_pipeline,
)
from .pipelines import list_pipelines, write_pipeline
from .planning import apply_increment_plan
from .roles import add_role, analyze_roles
from .run import run_brief, run_finish, run_next, run_note, run_start, run_status
from .setup import apply_project_setup
from .wait_event import TRANSPORT_CAP, wait_cap, wait_for_event

__all__ = [
    # Exception
    "AwfApiError",
    # Result dataclasses
    "InitResult",
    "StatusResult",
    "BaselineResult",
    "RollbackResult",
    "ProveRedResult",
    "VerifyPackResult",
    "MetricsResult",
    "ApproveResult",
    "RejectResult",
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
    "RunStartResult",
    "RunStatusResult",
    "RunNextResult",
    "RunFinishResult",
    "RestoreResult",
    "UnblockResult",
    "UpdateTodoResult",
    "RemoveTodoResult",
    "RetireTodoResult",
    "WritePipelineResult",
    "ListPipelinesResult",
    "FeedbackResult",
    "BriefResult",
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
    "prove_red",
    "verify_pack",
    "collect_metrics",
    "approve_commit",
    "reject_commit",
    "retry_stage",
    "restore_todo",
    # TODO state hygiene (RUN3 #4/#5 — stale closures, never-started removal;
    # RUN5 #2 — rejected/abandoned TODO archive)
    "unblock_todo",
    "remove_todo",
    "retire_todo",
    "update_todo",
    # Autonomous run (забег)
    "run_start",
    "run_status",
    "run_next",
    "run_finish",
    "run_brief",
    "run_note",
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
    # Named pipelines (RUN3 #1 — create/list, run by name via start/continue)
    "write_pipeline",
    "list_pipelines",
    # Feedback contour (RUN4 #2 — supervisor friction → report to owner)
    "feedback",
    # Supervisor onboarding/recovery card (RUN4 #1 — live state + tool map)
    "brief",
    # Supervisor wake-up (DASH Phase 3 — no more polling)
    "TRANSPORT_CAP",
    "wait_cap",
    "wait_for_event",
    # Model configuration validation
    "check_model_config",
]
