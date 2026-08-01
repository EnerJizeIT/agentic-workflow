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
    """Result of :func:`awf.api.approve_commit`."""

    todo_id: str
    signal_file: str

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


__all__ = [
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
]
