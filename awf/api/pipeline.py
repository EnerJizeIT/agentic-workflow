"""Pipeline API: start, continue, create_baseline, rollback, approve.

Foreground runs invoke ``awf.orchestrator.run_pipeline`` directly via
:class:`awf.api._background.PipelineArgs`. Background runs spawn a
detached subprocess via :func:`awf.api._background.start_in_background`.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .. import config as cfg_mod
from .. import git_utils, paths, todos
from .._atomic import atomic_write_text
from ._background import PipelineArgs, start_in_background
from ._errors import AwfApiError
from ._helpers import require_agentic
from ._results import ApproveResult, BaselineResult, RollbackResult, StartResult

# ─── approve_commit ─────────────────────────────────────────────────────


def approve_commit(project_dir: Path, todo_id: str) -> ApproveResult:
    """Create APPROVE-{todo_id}.ready signal to authorize auto-commit.

    Requires ``.agentic/`` (consistency with other api functions).
    """
    if not todo_id:
        raise AwfApiError("todo_id is required")
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)
    inbox = paths.inbox(project_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    signal = inbox / f"APPROVE-{todo_id}.ready"
    signal.touch()
    return ApproveResult(todo_id=todo_id, signal_file=str(signal))


# ─── create_baseline ────────────────────────────────────────────────────


def create_baseline(project_dir: Path, todo_id: str) -> BaselineResult:
    """Create BASELINE-{todo_id}.{sha,status,tests.log,env.log} snapshot.

    Captures git HEAD SHA, working tree status, test output, and Python
    environment. Used for A1 commit isolation and rollback targets.
    """
    if not todo_id:
        raise AwfApiError("todo_id is required")
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    context_dir = paths.context_dir(project_dir)
    context_dir.mkdir(parents=True, exist_ok=True)

    is_git = git_utils.is_git_repo(project_dir)
    if is_git:
        sha = git_utils.git_stdout(project_dir, "rev-parse", "HEAD").strip()
        atomic_write_text(context_dir / f"BASELINE-{todo_id}.sha", sha + "\n")
        status = git_utils.git_stdout(project_dir, "status", "--short", check=False)
        atomic_write_text(context_dir / f"BASELINE-{todo_id}.status", status)
    else:
        sha = "(not a git repo)"
        atomic_write_text(context_dir / f"BASELINE-{todo_id}.sha", sha + "\n")
        atomic_write_text(context_dir / f"BASELINE-{todo_id}.status", "")

    config_file = paths.config_file(project_dir)
    tests_log_path = context_dir / f"BASELINE-{todo_id}.tests.log"
    test_status = "no_config"
    test_log_excerpt = ""

    if config_file.exists():
        config_data = cfg_mod.load(project_dir)
        test_cmd = cfg_mod.get(config_data, "verification.test_cmd", "") or ""
        if test_cmd:
            parts = shlex.split(test_cmd)
            if parts:
                result = subprocess.run(
                    parts,
                    cwd=str(project_dir),
                    capture_output=True,
                    text=True,
                )
                log_content = result.stdout + result.stderr
                atomic_write_text(tests_log_path, log_content)
                test_status = "passed" if result.returncode == 0 else "failed"
                test_log_excerpt = "\n".join(log_content.splitlines()[-5:])
            else:
                atomic_write_text(tests_log_path, "No test_cmd configured, skipping test baseline.\n")
                test_status = "no_test_cmd"
        else:
            atomic_write_text(tests_log_path, "No test_cmd configured, skipping test baseline.\n")
            test_status = "no_test_cmd"
    else:
        atomic_write_text(tests_log_path, "No config.yaml found, skipping test baseline.\n")
        test_status = "no_config"

    python_cmd = "python3"
    if not shutil.which("python3") and shutil.which("python"):
        python_cmd = "python"

    env_parts: list[str] = []
    for cmd in [[python_cmd, "--version"], [python_cmd, "-m", "pip", "list"]]:
        result = subprocess.run(cmd, capture_output=True, text=True)
        env_parts.append(result.stdout + result.stderr)
    atomic_write_text(context_dir / f"BASELINE-{todo_id}.env.log", "".join(env_parts))

    files_created = sorted(
        f.name for f in context_dir.glob(f"BASELINE-{todo_id}.*") if f.is_file()
    )

    return BaselineResult(
        todo_id=todo_id,
        sha=sha,
        is_git_repo=is_git,
        files_created=files_created,
        test_status=test_status,
        test_log_excerpt=test_log_excerpt,
    )


# ─── rollback ───────────────────────────────────────────────────────────


def rollback(
    project_dir: Path,
    todo_id: str,
    *,
    mode: str = "hard",
) -> RollbackResult:
    """Rollback project to BASELINE-{todo_id}.sha.

    Modes: ``hard`` (default, ``git reset --hard``), ``soft`` (``git reset``,
    keeps working tree), ``dry-run`` (returns diff without modifying).
    """
    if not todo_id:
        raise AwfApiError("todo_id is required")
    if mode not in ("hard", "soft", "dry-run"):
        raise AwfApiError(f"invalid mode '{mode}', expected hard/soft/dry-run")

    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)
    context_dir = paths.context_dir(project_dir)
    inbox = paths.inbox(project_dir)

    baseline_sha_file = context_dir / f"BASELINE-{todo_id}.sha"
    if not baseline_sha_file.exists():
        raise AwfApiError(
            f"Baseline SHA not found: {baseline_sha_file}. "
            "Cannot rollback without baseline."
        )

    baseline_sha = baseline_sha_file.read_text(encoding="utf-8").strip()

    diff_result = subprocess.run(
        ["git", "diff", baseline_sha, "--stat"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    diff_stat = diff_result.stdout

    if mode == "dry-run":
        return RollbackResult(
            todo_id=todo_id,
            baseline_sha=baseline_sha,
            mode=mode,
            ack_file=None,
            diff_stat=diff_stat,
        )

    git_flag = "--hard" if mode == "hard" else ""
    git_cmd = ["git", "reset"]
    if git_flag:
        git_cmd.append(git_flag)
    git_cmd.append(baseline_sha)
    subprocess.run(git_cmd, cwd=str(project_dir), check=True)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ack_content = (
        f"signal: TASK_ACK\n"
        f"ack_type: DONE\n"
        f"decision: rollback\n"
        f"referenced_task_id: {todo_id}\n"
        f"baseline_sha: {baseline_sha}\n"
        f"created_by: supervisor\n"
        f"created_at: {ts}\n"
    )
    ack_path = inbox / f"ACK-{todo_id}.ready"
    atomic_write_text(ack_path, ack_content)

    return RollbackResult(
        todo_id=todo_id,
        baseline_sha=baseline_sha,
        mode=mode,
        ack_file=str(ack_path),
        diff_stat=diff_stat,
    )


# ─── start_pipeline / continue_pipeline ─────────────────────────────────


def start_pipeline(
    project_dir: Path,
    *,
    background: bool = False,
    pipeline: str | None = None,
    from_stage: str | None = None,
    auto: bool = False,
    timeout: int = 3600,
) -> StartResult:
    """Start the pipeline from the beginning.

    ``background=True`` launches a detached subprocess and returns immediately
    with a PID. Otherwise runs synchronously and returns the final exit code.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    # Dogfood-8: detect BD-36 checkpoint state + build appropriate warning
    from ..plan_checkpoint import is_checkpoint_enabled

    config_data = cfg_mod.load(project_dir)
    checkpoint_active = is_checkpoint_enabled(config_data, auto)

    if background:
        pid, log_file, _pid_file = start_in_background(
            project_dir,
            pipeline=pipeline,
            from_stage=from_stage,
            auto=auto,
            timeout=timeout,
        )
        # Dogfood-8: warn supervisor about BD-36 checkpoint
        msg = f"awf start running in background (PID {pid})"
        if checkpoint_active:
            msg += (
                ". BD-36 checkpoint ENABLED — after plan stage, HTML form opens "
                "in user's browser. Poll awf_status in 2-3 sec to get "
                "checkpoint_form_url, then tell user to approve. DO NOT call "
                "awf_approve (that's for verify stage only)."
            )
        return StartResult(
            run_mode="background",
            run_id=pid,
            log_file=str(log_file),
            exit_code=None,
            message=msg,
        )

    # Dogfood-8: foreground + checkpoint enabled = incompatible
    if checkpoint_active:
        return StartResult(
            run_mode="noop",
            run_id=None,
            log_file=None,
            exit_code=1,
            message=(
                "Foreground mode incompatible with BD-36 interactive checkpoint "
                "(stdout conflicts with MCP stdio, form won't display). "
                "Use background=True (default) or disable checkpoint via "
                "AWF_PLAN_CHECKPOINT=false env var."
            ),
        )

    from ..orchestrator import run_pipeline

    args = PipelineArgs(
        project_dir=str(project_dir),
        pipeline=pipeline,
        from_stage=from_stage,
        auto=auto,
        timeout=timeout,
    )
    try:
        exit_code = run_pipeline(args)
    except Exception as e:
        return StartResult(
            run_mode="foreground",
            run_id=None,
            log_file=None,
            exit_code=1,
            message=f"Pipeline crashed: {e}",
        )
    return StartResult(
        run_mode="foreground",
        run_id=None,
        log_file=None,
        exit_code=exit_code,
        message=f"Pipeline completed with exit code {exit_code}",
    )


def continue_pipeline(
    project_dir: Path,
    *,
    pipeline: str | None = None,
    from_stage: str | None = None,
    auto: bool = False,
    timeout: int = 3600,
) -> StartResult:
    """Resume an interrupted pipeline. Finds newest active TODO and continues."""
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    current_todo = todos.newest_active(project_dir)
    if not current_todo:
        return StartResult(
            run_mode="noop",
            run_id=None,
            log_file=None,
            exit_code=0,
            message="No active TODO found.",
        )

    from ..orchestrator import run_pipeline

    args = PipelineArgs(
        project_dir=str(project_dir),
        pipeline=pipeline,
        from_stage=from_stage,
        auto=auto,
        timeout=timeout,
    )
    try:
        exit_code = run_pipeline(args)
    except Exception as e:
        return StartResult(
            run_mode="foreground",
            run_id=None,
            log_file=None,
            exit_code=1,
            message=f"Pipeline crashed: {e}",
        )
    return StartResult(
        run_mode="foreground",
        run_id=None,
        log_file=None,
        exit_code=exit_code,
        message=f"Continued {current_todo}, exit code {exit_code}",
    )


__all__ = [
    "approve_commit",
    "create_baseline",
    "rollback",
    "start_pipeline",
    "continue_pipeline",
]
