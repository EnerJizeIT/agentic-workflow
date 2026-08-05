"""Pipeline API: start, continue, create_baseline, rollback, approve.

Foreground runs invoke ``awf.orchestrator.run_pipeline`` directly via
:class:`awf.api._background.PipelineArgs`. Background runs spawn a
detached subprocess via :func:`awf.api._background.start_in_background`.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .. import config as cfg_mod
from .. import git_utils, paths, todos
from .._atomic import atomic_write_text
from ..pipeline_state import read_state
from ._background import PipelineArgs, start_in_background
from ._errors import AwfApiError
from ._helpers import require_agentic
from ._results import ApproveResult, BaselineResult, RollbackResult, StartResult


def _is_pipeline_running(project_dir: Path) -> int | None:
    """DF5-6: Check if a pipeline subprocess is still alive.

    Reads ``pipeline_pid`` from state file and probes via ``os.kill(pid, 0)``.
    QA-4: on Linux, also verifies ``/proc/<pid>/cmdline`` contains ``awf``
    to guard against PID reuse (another process took the same PID after
    awf pipeline exited).
    Returns the live PID, or None if no pipeline / process is dead / PID reused.
    """
    import os

    state = read_state(project_dir)
    if not state:
        return None
    pid = state.get("pipeline_pid")
    if not pid:
        return None
    try:
        pid_int = int(pid)
    except (ValueError, TypeError):
        return None
    try:
        os.kill(pid_int, 0)
    except (ProcessLookupError, PermissionError):
        return None
    except OSError:
        return None

    # QA-4: PID reuse defense — verify the process is actually an awf pipeline.
    # On Linux, /proc/<pid>/cmdline contains the process command line.
    cmdline_path = Path(f"/proc/{pid_int}/cmdline")
    if cmdline_path.exists():
        try:
            cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
            if "awf" not in cmdline and "python" not in cmdline.lower():
                return None  # PID reused by unrelated process
        except OSError:
            pass  # Can't read — assume it's ours (best effort)

    return pid_int


def _verify_child_alive(pid: int, log_file: Path | None = None) -> bool:
    """DF5-10: Wait briefly, then check if a background child is still alive.

    Returns True if the process is running after a short delay.
    Extracted as a standalone function so tests can mock it.
    """
    import os
    import time as _time

    _time.sleep(1.0)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _reconcile(project_dir: Path) -> None:
    """DF6-2: Reconcile project state before start/continue.

    Ensures consistent state by cleaning up:
    1. Stale pipeline PID in state file → clear state.
    2. Duplicate ACK+APPROVE signals → remove older one.
    3. Multiple active TODOs → keep newest, archive rest as superseded.

    Note: does NOT auto-archive TODOs based on ACK/APPROVE presence —
    that's done by orchestrator verify stage (DF6-1). Tests pre-create
    APPROVE signals for BD-8 auto-approval; auto-archiving would break them.
    """
    import os

    from .. import paths, todos
    from .._log import log as _log
    from ..pipeline_state import read_state

    inbox = paths.inbox(project_dir)
    logs_dir = project_dir / ".agentic" / "logs"
    cleaned: list[str] = []

    # 1. Clear stale PID — but PRESERVE other state fields (stage_name, etc.)
    # so continue_pipeline can still resume from the correct stage.
    state = read_state(project_dir)
    if state and state.get("pipeline_pid"):
        pid_str = state["pipeline_pid"]
        try:
            pid_int = int(pid_str)
            os.kill(pid_int, 0)
        except (ProcessLookupError, PermissionError, ValueError, TypeError, OSError):
            # Remove just the PID, keep stage info for continue_pipeline.
            # Write YAML directly — write_state merges, can't remove keys.
            state.pop("pipeline_pid", None)
            import yaml as _yaml

            from ..paths import agentic_dir
            state_file = agentic_dir(project_dir) / "state" / "current.yaml"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(
                _yaml.dump(state, default_flow_style=False, allow_unicode=True),
                encoding="utf-8",
            )
            cleaned.append(f"cleared stale PID {pid_str}")

    # 2. Deduplicate ACK+APPROVE (keep APPROVE, remove ACK)
    if inbox.is_dir():
        for ack in inbox.glob("ACK-*.ready"):
            todo_id = ack.name.replace("ACK-", "").replace(".ready", "")
            approve = inbox / f"APPROVE-{todo_id}.ready"
            if approve.exists():
                ack.unlink()
                cleaned.append(f"dedup: removed ACK-{todo_id} (APPROVE exists)")

    # 3. Multiple active TODOs → keep newest, archive rest
    if inbox.is_dir():
        active = sorted(inbox.glob("TODO-*.ready"), key=lambda p: p.stat().st_mtime, reverse=True)
        if len(active) > 1:
            for old_ready in active[1:]:
                todo_id = old_ready.stem
                result = todos.archive_todo(project_dir, todo_id)
                if result:
                    cleaned.append(f"superseded {todo_id} (newer TODO exists)")

    if cleaned and logs_dir.is_dir():
        _log(logs_dir, f"DF6-2 reconcile: {'; '.join(cleaned)}")

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
        # QA-1: snapshot untracked files at baseline time so commit_gate
        # can distinguish worker-created files from pre-existing untracked.
        untracked = git_utils.git_stdout(
            project_dir, "ls-files", "--others", "--exclude-standard", check=False,
        )
        atomic_write_text(context_dir / f"BASELINE-{todo_id}.untracked", untracked)
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
                try:
                    result = subprocess.run(
                        parts,
                        cwd=str(project_dir),
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                except subprocess.TimeoutExpired:
                    # test_cmd hung (watcher / stdin prompt / infinite loop).
                    # Don't block baseline creation — record failure, continue.
                    atomic_write_text(
                        tests_log_path,
                        f"test_cmd timed out after 300s: {test_cmd}\n",
                    )
                    test_status = "failed"
                    test_log_excerpt = f"test_cmd timed out: {test_cmd}"
                else:
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
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            env_parts.append(result.stdout + result.stderr)
        except subprocess.TimeoutExpired:
            env_parts.append(f"{cmd[0]} timed out after 30s\n")
        except (FileNotFoundError, OSError) as e:
            env_parts.append(f"{cmd[0]} failed: {e}\n")
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

    # DF6-2: reconcile state before starting — archive completed TODOs,
    # clear stale PIDs, deduplicate signals. Only for background mode
    # (MCP/supervisor path). Foreground/CI tests may pre-create APPROVE
    # signals that should not trigger archival.
    if background:
        _reconcile(project_dir)

    # Dogfood-10: guard — refuse to start pipeline without active TODO.
    # Only for background mode (production supervisor flow). Foreground
    # mode is used by tests/CI — skip guard there.
    if background and not from_stage:
        active = todos.newest_active(project_dir)
        if not active:
            return StartResult(
                run_mode="noop",
                run_id=None,
                log_file=None,
                exit_code=0,
                message=(
                    "No active TODO in inbox. Pipeline needs a TODO to work on. "
                    "Create one via awf_dispatch_todo(project_dir, content, role), "
                    "then call awf_start again."
                ),
            )

    # DF5-6: refuse to start if a pipeline is already running.
    live_pid = _is_pipeline_running(project_dir)
    if live_pid and background:
        return StartResult(
            run_mode="noop",
            run_id=None,
            log_file=None,
            exit_code=0,
            message=(
                f"Pipeline already running (PID {live_pid}). "
                f"Use awf_status to check progress, or kill PID {live_pid} to force restart."
            ),
        )

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

        # DF5-10: wait briefly, then check if child died immediately.
        child_alive = _verify_child_alive(pid, log_file)

        if not child_alive:
            log_tail = ""
            try:
                log_tail = log_file.read_text(encoding="utf-8")[-500:] if log_file else ""
            except OSError:
                pass
            return StartResult(
                run_mode="error",
                run_id=pid,
                log_file=str(log_file) if log_file else None,
                exit_code=1,
                message=(
                    f"Pipeline started (PID {pid}) but exited immediately. "
                    f"Last log output:\n{log_tail}"
                ),
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
    # DF6-5: EXCEPT when this process is a background child (AWF_BACKGROUND_CHILD=1).
    # In background mode, stdout goes to a log file, not MCP stdio — so the
    # checkpoint form can safely open in browser without conflicts.
    if checkpoint_active and not os.environ.get("AWF_BACKGROUND_CHILD"):
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
            message=f"Pipeline crashed: {e}\n{traceback.format_exc()}",
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
    """Resume an interrupted pipeline. Finds newest active TODO and continues.

    DF5-2: reads ``pipeline_state`` to determine which stage to resume from.
    If state file has ``stage_name``, uses it as ``from_stage`` — so the
    pipeline resumes from the stage that was running when it stopped,
    NOT from stage 0 (plan).
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    # DF6-2: reconcile state before continuing
    _reconcile(project_dir)
    live_pid = _is_pipeline_running(project_dir)
    if live_pid:
        return StartResult(
            run_mode="noop",
            run_id=None,
            log_file=None,
            exit_code=0,
            message=(
                f"Pipeline already running (PID {live_pid}). "
                f"Use awf_status to check progress. If stuck, kill PID {live_pid} first."
            ),
        )

    current_todo = todos.newest_active(project_dir)
    if not current_todo:
        return StartResult(
            run_mode="noop",
            run_id=None,
            log_file=None,
            exit_code=0,
            message="No active TODO found.",
        )

    # DF5-2: read pipeline state to determine resume point.
    # If from_stage not explicitly given, use stage_name from state file.
    if not from_stage:
        state = read_state(project_dir)
        if state and state.get("stage_name"):
            from_stage = state["stage_name"]

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
            message=f"Pipeline crashed: {e}\n{traceback.format_exc()}",
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
