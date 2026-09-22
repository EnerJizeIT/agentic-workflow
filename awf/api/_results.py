"""Result dataclasses for awf.api public functions.

Each ``*Result`` carries the structured outcome of one API call. All have
``as_dict()`` for JSON serialization (MCP responses, plugin payloads).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class InitResult:
    """Result of :func:`awf.api.init_project`."""

    project_name: str
    project_dir: str
    stack: str
    vision_path: str | None
    vision_excerpt: str
    supervisor_md: str
    plan_md: str
    pipeline_configured: bool
    next_action: str
    warnings: list[str] = field(default_factory=list)
    created_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StatusResult:
    """Result of :func:`awf.api.get_status`."""

    project_name: str
    active_todos: list[dict[str, Any]]
    done_count: int
    blocked_count: int
    blocked_ids: list[str]
    conflict_warning: str | None
    suggestion: str | None
    # MCP-4: long-running pipeline state
    pipeline_running: bool = False
    pipeline_pid: int | None = None
    log_tail: str | None = None
    # Dogfood-2: pipeline stage visibility (extracted from log + pipeline.yaml)
    current_stage_name: str | None = None
    next_stage_role: str | None = None
    last_signal: str | None = None
    # Dogfood-6: structural determinism — supervisor sees expected action,
    # doesn't have to read rules from supervisor.md
    current_stage_kind: str | None = None  # "plan" | "execute" | "verify" | "idle"
    expected_action: str | None = None  # what supervisor should do NOW
    checkpoint_pending: bool = False  # BD-36 form open in user's browser
    checkpoint_port: int | None = None  # for debugging / direct access
    # Dogfood-8: form URL supervisor can tell user about
    checkpoint_form_url: str | None = None  # file://path or http://127.0.0.1:PORT
    # DF5-4: salvage state (worker didn't signal)
    salvage_needed: bool = False
    salvage_stage: str | None = None
    # RUN3 #1: named pipelines — active (config default_pipeline, "default"
    # when undeclared) + number of pipeline files in .agentic/pipelines/
    active_pipeline: str | None = None
    pipeline_count: int = 0
    # SPEC A-run: autonomous run state (active run only; None otherwise)
    run_state: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BaselineResult:
    """Result of :func:`awf.api.create_baseline`."""

    todo_id: str
    sha: str
    is_git_repo: bool
    files_created: list[str]
    test_status: str
    test_log_excerpt: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RollbackResult:
    """Result of :func:`awf.api.rollback`."""

    todo_id: str
    baseline_sha: str
    mode: str
    ack_file: str | None
    diff_stat: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApproveResult:
    """Result of :func:`awf.api.approve_commit` (U11: +verified_sha_file)."""

    todo_id: str
    signal_file: str
    evidence_file: str = ""
    verified_sha_file: str = ""  # U11 (B5): context/VERIFIED-{todo}.sha, on match
    # AUD05-05 (rest): non-empty when a reject won the race ('approved' ⇒ rejects == 0)
    message: str = ""
    # RUN5 #1 (Part B): rejected-attempt files that would be SILENTLY
    # excluded from this commit (still untracked AND in the baseline's
    # untracked list). The approve itself is NOT blocked — the list is the
    # loud warning (re-issue with carry_over_from, or commit consciously).
    orphaned_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RejectResult:
    """Result of :func:`awf.api.reject_commit` (SPEC A-run.5 accounting)."""

    todo_id: str
    review_file: str
    rejects: int = 0
    run_stopped: bool = False
    report_file: str = ""
    message: str = ""
    # RUN5 #1 (Part A.1): untracked NEW files of the rejected attempt,
    # recorded to context/REJECT-{todo}.files (leak-gate). Empty when the
    # attempt created no new files or the snapshot could not be taken.
    reject_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportResult:
    """Result of :func:`awf.api.get_report`."""

    project_name: str
    generated_at: str
    items: list[dict[str, str]]
    done_count: int
    blocked_count: int
    git_diff: str
    latest_test_log_tail: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResetResult:
    """Result of :func:`awf.api.reset_runtime` and :func:`awf.api.remove_orphans`."""

    cleaned_dirs: list[str]
    orphan_ids: list[str]
    mode: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AddRoleResult:
    """Result of :func:`awf.api.add_role`."""

    role_name: str
    role_file: str
    model: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StartResult:
    """Result of :func:`awf.api.start_pipeline` and :func:`awf.api.continue_pipeline`."""

    run_mode: str
    run_id: int | None
    log_file: str | None
    exit_code: int | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalyzeRolesResult:
    """Result of :func:`awf.api.analyze_roles`."""

    overlaps: list[dict[str, Any]]
    patches_applied: list[dict[str, str]]
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplyProjectSetupResult:
    """Result of :func:`awf.api.apply_project_setup`."""

    pipeline_file: str | None
    config_updated: bool
    supervisor_md_updated: bool
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DispatchTodoResult:
    """Result of :func:`awf.api.dispatch_todo`."""

    todo_id: str
    baseline_sha: str
    role_hint: str | None
    files_written: list[str]
    pre_check_warnings: list[str] = field(default_factory=list)
    # RUN5 #1 (Part A.2): leak-gate carry-over for a retry.
    carry_over_from: str | None = None  # the rejected origin TODO id
    carry_over_files: list[str] = field(default_factory=list)  # paths pulled in

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SupervisorContextResult:
    """Result of :func:`awf.api.load_supervisor_context`.

    Aggregate payload for one-shot supervisor bootstrap: everything needed
    to act as supervisor in a single MCP response.
    """

    project_name: str
    project_dir: str
    vision_path: str | None
    vision_excerpt: str
    plan_md: str
    supervisor_md: str
    phase: str  # SMO: current phase (init/goal/form/normalize/brief/run/verify/done)
    phase_prompt: str  # SMO: compact phase-specific instructions
    active_todos: list[dict[str, Any]]
    done_count: int
    blocked_count: int
    pipeline_running: bool
    pipeline_pid: int | None
    log_tail: str | None
    next_stage_role: str | None
    next_role_prohibitions: str | None
    current_stage_name: str | None
    last_signal: str | None
    git_diff_stat: str
    warnings: list[str] = field(default_factory=list)
    # Dogfood-9: structural trigger for increment planning
    increment_planning_needed: bool = False
    final_stage_commit_policy: str | None = None  # on_approved value from last stage

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplyIncrementPlanResult:
    """Result of :func:`awf.api.apply_increment_plan`."""

    plan_path: str
    selected_variant_id: str | None
    variants_offered: int | None
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WaitEventResult:
    """Result of :func:`awf.api.wait_for_event` (Phase 3 — supervisor wake-up).

    event_type is one of:
    - ``verify`` — pipeline reached verify stage
    - ``blocked`` — worker wrote BLOCKED
    - ``checkpoint`` — BD-36 checkpoint form open
    - ``done`` — pipeline exited
    - ``timeout`` — no event within timeout
    - ``idle`` — pipeline not running
    """

    event_type: str
    message: str
    state_snapshot: dict[str, Any] = field(default_factory=dict)
    # SPEC A-run: recommended wait size for the next call (median stage/3,
    # clamped to the MCP transport cap, wait_event.TRANSPORT_CAP); 0 when
    # not computed for this event type.
    suggested_timeout: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Autonomous run (забег) — SPEC A-run v1 ─────────────────────────────


@dataclass
class RunStartResult:
    """Result of :func:`awf.api.run_start`."""

    active: bool
    # RUN3 #2: normalized items {"todo_id", "pipeline"} (empty pipeline =
    # the config default).
    queue: list[dict[str, str]]
    position: str
    budget_minutes: int
    stop_flags: dict[str, list[str]]
    message: str
    next_action: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunStatusResult:
    """Result of :func:`awf.api.run_status`."""

    active: bool
    position: str
    # RUN3 #2: normalized items {"todo_id", "pipeline"} (empty pipeline =
    # the config default).
    queue: list[dict[str, str]]
    index: int
    current: str
    completed: list[str]
    rejects: dict[str, int]
    stop_flags: dict[str, list[str]]
    budget_minutes: int
    budget_left_minutes: int
    elapsed_minutes: int
    stop_reason: str
    report_file: str
    message: str
    no_checkpoints: bool = False
    # B2: the budget counts productive minutes (elapsed − downtime)
    downtime_minutes: int = 0
    productive_minutes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunNextResult:
    """Result of :func:`awf.api.run_next`."""

    # AUD05-09: dead "finished" removed from the contract (never produced).
    action: str  # started | stopped | refused
    todo_id: str
    message: str
    run_mode: str = ""
    run_id: int | None = None
    log_file: str = ""
    report_file: str = ""
    next_action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RestoreResult:
    """Result of :func:`awf.api.restore_todo` (NEG-2026-09-19 R2a safety net)."""

    todo_id: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UnblockResult:
    """Result of :func:`awf.api.unblock_todo` (RUN3 #4 stale-closure clearing)."""

    todo_id: str
    moved: list[str]
    trace: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RemoveTodoResult:
    """Result of :func:`awf.api.remove_todo` (RUN3 #5 never-started removal)."""

    todo_id: str
    trace_path: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WritePipelineResult:
    """Result of :func:`awf.api.write_pipeline` (RUN3 #1 named pipelines)."""

    name: str
    file: str
    stages: int  # stage count written
    overwritten: bool  # True when force=True replaced an existing file

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ListPipelinesResult:
    """Result of :func:`awf.api.list_pipelines` (RUN3 #1 named pipelines)."""

    pipelines: list[str]  # names of .agentic/pipelines/*.yaml (sorted)
    active: str  # name the runtime uses (config default_pipeline / "default")
    active_exists: bool  # whether the active pipeline file is present

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunFinishResult:
    """Result of :func:`awf.api.run_finish`."""

    active: bool
    reason: str
    report_file: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "InitResult",
    "StatusResult",
    "BaselineResult",
    "RollbackResult",
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
    "RemoveTodoResult",
]
