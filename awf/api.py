"""Public API for agentic-workflow.

All external callers (CLI, MCP plugin) MUST go through this module.
Business logic lives here as pure functions returning Result dataclasses.
Side effects (printing, argparse) belong in cmd_*.py / plugin tools.

Error convention: every function returns either a Result dataclass (success)
or raises AwfApiError with a human-readable message. Callers wrap in try/except.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import git_utils, paths, todos
from ._atomic import atomic_write_text


class AwfApiError(Exception):
    """Raised on any API-level failure (missing .agentic/, git error, etc.).

    The message is human-readable and can be shown directly to the user.
    """


# ─── Stack detection ────────────────────────────────────────────────────


def detect_stack(project_dir: Path) -> dict[str, str]:
    """Auto-detect test/lint/typecheck/build commands from project files.

    Returns a dict with keys: ``test_cmd``, ``lint_cmd``, ``typecheck_cmd``,
    ``build_cmd``, ``stack``. Empty strings when unknown. ``stack`` is one of:
    ``"python"``, ``"typescript"``, ``"rust"``, ``"go"``, ``"unknown"``.

    Detection order: package.json → pyproject.toml/setup.py → Cargo.toml →
    go.mod → file-extension heuristic.
    """
    result: dict[str, str] = {
        "test_cmd": "",
        "lint_cmd": "",
        "typecheck_cmd": "",
        "build_cmd": "",
        "stack": "unknown",
    }

    # 1. Node/TypeScript — package.json scripts.* are authoritative
    pkg = project_dir / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
            result["test_cmd"] = str(scripts.get("test", "")) or ""
            result["lint_cmd"] = str(scripts.get("lint", "")) or ""
            result["build_cmd"] = str(scripts.get("build", "")) or ""
            result["typecheck_cmd"] = str(scripts.get("typecheck", "")) or ""
            # Stack sub-flavor from devDependencies
            dev_deps = data.get("devDependencies", {}) if isinstance(data, dict) else {}
            deps = data.get("dependencies", {}) if isinstance(data, dict) else {}
            all_deps = {**dev_deps, **deps}
            tsconfig_present = (project_dir / "tsconfig.json").is_file()
            if "typescript" in all_deps or tsconfig_present:
                result["stack"] = "typescript"
            else:
                result["stack"] = "javascript"
            # Fallbacks if scripts empty
            if not result["typecheck_cmd"] and tsconfig_present:
                result["typecheck_cmd"] = "tsc --noEmit"
            return result
        except (json.JSONDecodeError, OSError):
            pass

    # 2. Python — pyproject.toml [tool.pytest] / [tool.ruff] / [tool.mypy]
    pyproject = project_dir / "pyproject.toml"
    setup_py = project_dir / "setup.py"
    if pyproject.is_file() or setup_py.is_file():
        result["stack"] = "python"
        result["test_cmd"] = "pytest"
        result["lint_cmd"] = "ruff check ."
        # typecheck only if mypy config present
        try:
            text = pyproject.read_text(encoding="utf-8") if pyproject.is_file() else ""
        except OSError:
            text = ""
        if "[tool.mypy]" in text or (project_dir / "mypy.ini").is_file():
            result["typecheck_cmd"] = "mypy ."
        if "[build-system]" in text:
            result["build_cmd"] = "pip install -e ."
        return result

    # 3. Rust — Cargo.toml
    if (project_dir / "Cargo.toml").is_file():
        result["stack"] = "rust"
        result["test_cmd"] = "cargo test"
        result["lint_cmd"] = "cargo clippy"
        result["typecheck_cmd"] = "cargo check"
        result["build_cmd"] = "cargo build"
        return result

    # 4. Go — go.mod
    if (project_dir / "go.mod").is_file():
        result["stack"] = "go"
        result["test_cmd"] = "go test ./..."
        result["lint_cmd"] = "golangci-lint run"
        result["typecheck_cmd"] = "go vet ./..."
        result["build_cmd"] = "go build ./..."
        return result

    # 5. Heuristic — file extensions
    try:
        files = list(project_dir.iterdir())
    except OSError:
        files = []
    extensions = {f.suffix.lower() for f in files if f.is_file()}
    if ".ts" in extensions or ".tsx" in extensions:
        result["stack"] = "typescript"
        result["test_cmd"] = "bun test"
        result["typecheck_cmd"] = "tsc --noEmit"
    elif ".py" in extensions:
        result["stack"] = "python"
        result["test_cmd"] = "pytest"
        result["lint_cmd"] = "ruff check ."

    return result


def derive_project_name(project_dir: Path) -> str:
    """Derive human-readable project name from directory name.

    ``jira-epic-presenter`` → ``Jira Epic Presenter``.
    Underscores and hyphens treated as word separators; result is Title Case.
    Existing capitals in camelCase names are preserved (``myApp`` → ``My App``).
    """
    name = project_dir.name
    # Insert space before each capital letter that follows a lowercase letter
    # (split camelCase: myApp → my App)
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    # Replace separators with spaces
    name = name.replace("_", " ").replace("-", " ")
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name).strip()
    # Title Case each word
    words = name.split(" ")
    titled = " ".join(w[:1].upper() + w[1:] for w in words if w)
    return titled or "Project"


# ─── Result dataclasses ─────────────────────────────────────────────────


@dataclass
class InitResult:
    """Result of :func:`init_project`. Returned to CLI/MCP caller."""

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
    todo_id: str
    baseline_sha: str
    mode: str
    ack_file: str | None
    diff_stat: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApproveResult:
    todo_id: str
    signal_file: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportResult:
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
    cleaned_dirs: list[str]
    orphan_ids: list[str]
    mode: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AddRoleResult:
    role_name: str
    role_file: str
    model: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StartResult:
    run_mode: str
    run_id: int | None
    log_file: str | None
    exit_code: int | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalyzeRolesResult:
    overlaps: list[dict[str, Any]]
    patches_applied: list[dict[str, str]]
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Helpers ────────────────────────────────────────────────────────────


def _require_agentic(project_dir: Path) -> Path:
    """Ensure .agentic/ exists. Raise AwfApiError if not."""
    agentic = paths.agentic_dir(project_dir)
    if not agentic.is_dir():
        raise AwfApiError(f"No .agentic/ found at {project_dir}. Run 'awf init' first.")
    return agentic


def _require_git_repo(project_dir: Path) -> None:
    """Ensure project is a git repo. Raise AwfApiError if not."""
    if not git_utils.is_git_repo(project_dir):
        raise AwfApiError(f"Not a git repository: {project_dir}. Run 'git init' first.")


def _read_file_text(path: Path, max_chars: int | None = None) -> str:
    """Read file as text, optionally truncated to max_chars."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"(error reading {path.name}: {e})"
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"
    return text


# ─── Public API: approve_commit ─────────────────────────────────────────


def approve_commit(project_dir: Path, todo_id: str) -> ApproveResult:
    """Create APPROVE-{todo_id}.ready signal to authorize auto-commit.

    Idempotent — creates .agentic/inbox/ if missing. Raises AwfApiError
    only if todo_id is empty.
    """
    if not todo_id:
        raise AwfApiError("todo_id is required")
    project_dir = Path(project_dir).resolve()
    inbox = paths.inbox(project_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    signal = inbox / f"APPROVE-{todo_id}.ready"
    signal.touch()
    return ApproveResult(todo_id=todo_id, signal_file=str(signal))


# ─── Public API: create_baseline ────────────────────────────────────────


def create_baseline(project_dir: Path, todo_id: str) -> BaselineResult:
    """Create BASELINE-{todo_id}.{sha,status,tests.log,env.log} snapshot.

    Saves git HEAD SHA, git status, test output, and Python env info.
    """
    if not todo_id:
        raise AwfApiError("todo_id is required")
    project_dir = Path(project_dir).resolve()
    _require_agentic(project_dir)

    context_dir = paths.context_dir(project_dir)
    context_dir.mkdir(parents=True, exist_ok=True)

    # 1. Git SHA + status
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

    # 2. Test baseline
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
                tests_log_path.write_text(log_content, encoding="utf-8")
                test_status = "passed" if result.returncode == 0 else "failed"
                test_log_excerpt = "\n".join(log_content.splitlines()[-5:])
            else:
                tests_log_path.write_text(
                    "No test_cmd configured, skipping test baseline.\n",
                    encoding="utf-8",
                )
                test_status = "no_test_cmd"
        else:
            tests_log_path.write_text(
                "No test_cmd configured, skipping test baseline.\n",
                encoding="utf-8",
            )
            test_status = "no_test_cmd"
    else:
        tests_log_path.write_text(
            "No config.yaml found, skipping test baseline.\n",
            encoding="utf-8",
        )
        test_status = "no_config"

    # 3. Python environment
    python_cmd = "python3"
    if not shutil.which("python3") and shutil.which("python"):
        python_cmd = "python"

    env_parts: list[str] = []
    for cmd in [[python_cmd, "--version"], [python_cmd, "-m", "pip", "list"]]:
        result = subprocess.run(cmd, capture_output=True, text=True)
        env_parts.append(result.stdout + result.stderr)
    (context_dir / f"BASELINE-{todo_id}.env.log").write_text(
        "".join(env_parts), encoding="utf-8"
    )

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


# ─── Public API: rollback ───────────────────────────────────────────────


def rollback(
    project_dir: Path,
    todo_id: str,
    *,
    mode: str = "hard",
) -> RollbackResult:
    """Rollback project to BASELINE-{todo_id}.sha.

    mode: ``"hard"`` (default), ``"soft"``, or ``"dry-run"``.
    Dry-run returns diff without modifying anything.
    """
    if not todo_id:
        raise AwfApiError("todo_id is required")
    if mode not in ("hard", "soft", "dry-run"):
        raise AwfApiError(f"invalid mode '{mode}', expected hard/soft/dry-run")

    project_dir = Path(project_dir).resolve()
    _require_agentic(project_dir)
    context_dir = paths.context_dir(project_dir)
    inbox = paths.inbox(project_dir)

    baseline_sha_file = context_dir / f"BASELINE-{todo_id}.sha"
    if not baseline_sha_file.exists():
        raise AwfApiError(
            f"Baseline SHA not found: {baseline_sha_file}. "
            "Cannot rollback without baseline."
        )

    baseline_sha = baseline_sha_file.read_text(encoding="utf-8").strip()

    # Always compute diff stat (for dry-run return; for hard/soft informational)
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

    # Hard or soft reset
    git_flag = "--hard" if mode == "hard" else ""
    git_cmd = ["git", "reset"]
    if git_flag:
        git_cmd.append(git_flag)
    git_cmd.append(baseline_sha)
    subprocess.run(git_cmd, cwd=str(project_dir), check=True)

    # Create ACK file
    from datetime import datetime, timezone

    inbox.mkdir(parents=True, exist_ok=True)
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


# ─── Public API: get_status ─────────────────────────────────────────────


def _count_done_blocked(
    inbox: Path, outbox: Path
) -> tuple[int, int, list[str]]:
    """Count DONE/BLOCKED TODOs. Returns (done_count, blocked_count, blocked_ids)."""
    from .signals import find_signal_file

    done_count = 0
    blocked_count = 0
    blocked_ids: list[str] = []

    if not inbox.exists():
        return done_count, blocked_count, blocked_ids

    for ready_file in sorted(inbox.glob("TODO-*.ready")):
        if not ready_file.is_file():
            continue
        todo_id = ready_file.stem
        done = find_signal_file(outbox, "DONE", todo_id, ".ready")
        blocked = find_signal_file(outbox, "BLOCKED", todo_id, ".ready")
        if done:
            done_count += 1
        elif blocked:
            blocked_count += 1
            blocked_ids.append(todo_id)

    return done_count, blocked_count, blocked_ids


def _read_progress(outbox: Path, todo_id: str) -> dict[str, Any]:
    """Parse PROGRESS-{todo_id}.md for task counts and last entry."""
    progress_file = outbox / f"PROGRESS-{todo_id}.md"
    if not progress_file.exists():
        return {}
    text = progress_file.read_text(encoding="utf-8")
    task_lines = [line for line in text.splitlines() if line.startswith("## Task")]
    total = len(task_lines)
    done = sum(1 for ln in task_lines if "[x]" in ln)
    failed = sum(1 for ln in task_lines if "[!]" in ln)
    last = task_lines[-1] if task_lines else ""
    return {"total": total, "done": done, "failed": failed, "last": last}


def _read_ack(inbox: Path, todo_id: str) -> str | None:
    """Read ACK decision from inbox, or None if no ACK file."""
    ack_file = inbox / f"ACK-{todo_id}.ready"
    if not ack_file.exists():
        return None
    text = ack_file.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "decision" in line:
            return line.strip()
    return "none"


def get_status(project_dir: Path) -> StatusResult:
    """Get current workflow state — active TODOs, progress, blocked, conflicts."""
    project_dir = Path(project_dir).resolve()
    _require_agentic(project_dir)

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    config_data = cfg_mod.load(project_dir)
    project_name = (
        cfg_mod.get(config_data, "project.name", "Project") or "Project"
    )

    active_ids = todos.list_active_todos(inbox, outbox)
    done_count, blocked_count, blocked_ids = _count_done_blocked(inbox, outbox)

    active_todos_list: list[dict[str, Any]] = []
    for todo_id in active_ids:
        entry: dict[str, Any] = {"todo_id": todo_id}
        ack = _read_ack(inbox, todo_id)
        if ack is not None:
            entry["ack"] = ack
        progress = _read_progress(outbox, todo_id)
        if progress:
            entry["progress"] = progress
        else:
            entry["progress"] = None
        active_todos_list.append(entry)

    conflict_warning: str | None = None
    if len(active_ids) > 1:
        newest = active_ids[0]
        conflict_warning = (
            f"{len(active_ids)} active TODOs detected. "
            f"Orchestrator will run: {newest} (highest NNNN). "
            f"Others are stale — rollback or 'awf reset --orphans'."
        )

    suggestion: str | None = None
    if not active_ids and blocked_count == 0:
        suggestion = (
            "No active tasks. Supervisor should create the next TODO, "
            "then run: awf start"
        )

    # MCP-4: detect running pipeline subprocess via PID file
    pipeline_running, pipeline_pid, log_tail = _check_pipeline_running(project_dir)

    return StatusResult(
        project_name=project_name,
        active_todos=active_todos_list,
        done_count=done_count,
        blocked_count=blocked_count,
        blocked_ids=blocked_ids,
        conflict_warning=conflict_warning,
        suggestion=suggestion,
        pipeline_running=pipeline_running,
        pipeline_pid=pipeline_pid,
        log_tail=log_tail,
    )


def _check_pipeline_running(
    project_dir: Path,
    *,
    log_tail_lines: int = 20,
) -> tuple[bool, int | None, str | None]:
    """MCP-4: detect a running awf pipeline by reading PID file + os.kill probe.

    Returns ``(is_running, pid, log_tail)``:
    - ``is_running``: True if PID file exists AND process is alive.
    - ``pid``: PID from file, or None.
    - ``log_tail``: last N lines of .agentic/logs/awf-start.out (None if absent).

    Stale PID files (process exited) are cleaned up automatically.
    """
    import os

    logs_dir = paths.agentic_dir(project_dir) / "logs"
    pid_file = logs_dir / "awf-start.pid"
    log_file = logs_dir / "awf-start.out"

    if not pid_file.is_file():
        return False, None, _read_log_tail(log_file, log_tail_lines)

    try:
        pid_str = pid_file.read_text(encoding="utf-8").strip()
        pid = int(pid_str)
    except (OSError, ValueError):
        # Corrupt PID file — clean up
        try:
            pid_file.unlink()
        except OSError:
            pass
        return False, None, _read_log_tail(log_file, log_tail_lines)

    # Probe process liveness: signal 0 = "is this process reachable?"
    try:
        os.kill(pid, 0)
        alive = True
    except (OSError, ProcessLookupError):
        alive = False

    if not alive:
        # Stale PID — clean up
        try:
            pid_file.unlink()
        except OSError:
            pass
        return False, None, _read_log_tail(log_file, log_tail_lines)

    return True, pid, _read_log_tail(log_file, log_tail_lines)


def _read_log_tail(log_file: Path, n: int) -> str | None:
    """Read last N lines of a log file. Returns None if file is absent."""
    if not log_file.is_file():
        return None
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines:
        return ""
    tail = lines[-n:] if len(lines) > n else lines
    return "\n".join(tail)


# ─── Public API: get_report ─────────────────────────────────────────────


def get_report(project_dir: Path) -> ReportResult:
    """Generate workflow report — task statuses, git diff, latest test log."""
    from datetime import datetime

    project_dir = Path(project_dir).resolve()
    _require_agentic(project_dir)

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    config_data = cfg_mod.load(project_dir)
    project_name = (
        cfg_mod.get(config_data, "project.name", "Project") or "Project"
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    items: list[dict[str, str]] = []
    done_count = 0
    blocked_count = 0

    if inbox.exists():
        from .signals import find_signal_file

        for ready_file in sorted(inbox.glob("TODO-*.ready")):
            if not ready_file.is_file():
                continue
            todo_id = ready_file.stem
            done = find_signal_file(outbox, "DONE", todo_id, ".ready")
            blocked = find_signal_file(outbox, "BLOCKED", todo_id, ".ready")
            if done:
                items.append({"todo_id": todo_id, "status": "OK"})
                done_count += 1
            elif blocked:
                items.append({"todo_id": todo_id, "status": "BLK"})
                blocked_count += 1
            else:
                items.append({"todo_id": todo_id, "status": "..."})

    # Git diff stat
    diff_result = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    git_diff = diff_result.stdout if diff_result.returncode == 0 else ""

    # Latest test log tail
    latest_test_log_tail: str | None = None
    if outbox.exists():
        logs = sorted(
            outbox.glob("TEST-RESULTS-*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if logs:
            try:
                content = logs[0].read_text(encoding="utf-8")
                lines = content.splitlines()
                tail = lines[-5:] if len(lines) > 5 else lines
                latest_test_log_tail = "\n".join(tail)
            except OSError:
                pass

    return ReportResult(
        project_name=project_name,
        generated_at=now,
        items=items,
        done_count=done_count,
        blocked_count=blocked_count,
        git_diff=git_diff,
        latest_test_log_tail=latest_test_log_tail,
    )


# ─── Public API: reset_runtime ──────────────────────────────────────────


def reset_runtime(
    project_dir: Path,
    *,
    tasks_only: bool = False,
    full: bool = False,
    orphans: bool = False,
) -> ResetResult:
    """Clean runtime data.

    Modes:
    - ``tasks_only=True``: clean only inbox + outbox
    - ``full=True``: clean inbox/outbox/context/logs/reports
    - ``orphans=True``: remove orphan TODOs (active without progress)
    - default: clean inbox/outbox/context/logs/reports (keep phases)
    """
    project_dir = Path(project_dir).resolve()
    agentic = project_dir / ".agentic"
    if not agentic.is_dir():
        # Idempotent — nothing to reset
        return ResetResult(cleaned_dirs=[], orphan_ids=[], mode="noop")

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    if orphans:
        return _reset_orphans(inbox, outbox)

    if full or not tasks_only:
        dirs_to_clean = ["inbox", "outbox", "context", "logs", "reports"]
        mode = "full" if full else "default"
    else:
        dirs_to_clean = ["inbox", "outbox"]
        mode = "tasks_only"

    cleaned: list[str] = []
    for d in dirs_to_clean:
        dirpath = agentic / d
        if dirpath.is_dir():
            for f in dirpath.iterdir():
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    shutil.rmtree(f)
            cleaned.append(d)

    return ResetResult(cleaned_dirs=cleaned, orphan_ids=[], mode=mode)


def _reset_orphans(inbox: Path, outbox: Path) -> ResetResult:
    """Remove orphan TODOs — active ones with no progress."""
    active_ids = todos.list_active_todos(inbox, outbox)
    orphan_ids: list[str] = []
    for todo_id in active_ids:
        if not todos.has_progress(outbox, todo_id):
            orphan_ids.append(todo_id)

    for tid in orphan_ids:
        ready = inbox / f"{tid}.ready"
        md = inbox / f"{tid}.md"
        if ready.exists():
            ready.unlink()
        if md.exists():
            md.unlink()

    return ResetResult(cleaned_dirs=[], orphan_ids=orphan_ids, mode="orphans")


# ─── Public API: add_role ───────────────────────────────────────────────


_ROLE_TEMPLATE = """# ROLE: {role_name}

**Role:** {description}
**Runs as:** `opencode run --agent {role_name}`
**Model:** {model}

## 1. Who you are

<describe the responsibility of this role>

## 2. Input

This role receives:
- <what comes from the previous stage>

## 3. Actions

<describe what to do with the input>

## 4. Output

When finished, create one of:
- `.agentic/outbox/APPROVED-{{NNNN}}.md` + `.ready` — success
- `.agentic/outbox/REJECTED-{{NNNN}}.md` + `.ready` — needs fixes
- `.agentic/outbox/BLOCKED-{{NNNN}}.md` + `.ready` — needs supervisor

## 5. Prohibitions

<list what this role must NOT do>
"""


def add_role(
    project_dir: Path,
    role_name: str,
    *,
    description: str = "",
    model: str = "",
) -> AddRoleResult:
    """Generate a new role template at ``.agentic/roles/{role_name}.md``."""
    if not role_name:
        raise AwfApiError("role_name is required")
    project_dir = Path(project_dir).resolve()
    _require_agentic(project_dir)

    if not model:
        model = "<set-me-in-.agentic/config.yaml>"

    roles_dir = project_dir / ".agentic" / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)

    content = _ROLE_TEMPLATE.format(
        role_name=role_name,
        description=description or "new role",
        model=model,
    )
    role_file = roles_dir / f"{role_name}.md"
    atomic_write_text(role_file, content)

    return AddRoleResult(
        role_name=role_name,
        role_file=str(role_file),
        model=model,
    )


# ─── Public API: init_project ───────────────────────────────────────────


def init_project(
    project_dir: Path,
    *,
    force: bool = False,
    project_name: str | None = None,
    test_cmd: str | None = None,
    lint_cmd: str | None = None,
    typecheck_cmd: str | None = None,
    build_cmd: str | None = None,
    dry_run: bool = False,
) -> InitResult:
    """Initialize ``.agentic/`` skeleton in project_dir.

    All command parameters are auto-detected via :func:`detect_stack` when
    not provided explicitly. Project name is derived from directory when not
    provided.

    Returns :class:`InitResult` with supervisor.md, vision excerpt, plan.md
    content — caller (CLI/MCP) has everything needed to assume the supervisor
    role without further file reads.

    Raises :class:`AwfApiError` on:
    - Not a git repository
    - .agentic/ already exists (unless ``force=True``)
    """
    project_dir = Path(project_dir).resolve()
    _require_git_repo(project_dir)

    agentic = project_dir / ".agentic"
    if agentic.exists() and not force:
        raise AwfApiError(
            f".agentic/ already exists at {agentic}. Use force=True to overwrite."
        )

    # Auto-derive what wasn't provided
    if project_name is None:
        project_name = derive_project_name(project_dir)

    stack_info = detect_stack(project_dir)
    if test_cmd is None:
        test_cmd = stack_info["test_cmd"]
    if lint_cmd is None:
        lint_cmd = stack_info["lint_cmd"]
    if typecheck_cmd is None:
        typecheck_cmd = stack_info["typecheck_cmd"]
    if build_cmd is None:
        build_cmd = stack_info["build_cmd"]

    # Vision detection
    vision_path = paths.find_vision_file(project_dir)

    warnings: list[str] = []
    if vision_path is None:
        warnings.append("Vision/README not found in project root")

    if dry_run:
        return InitResult(
            project_name=project_name,
            project_dir=str(project_dir),
            stack=stack_info["stack"],
            vision_path=str(vision_path) if vision_path else None,
            vision_excerpt="",
            supervisor_md="",
            plan_md="",
            pipeline_configured=False,
            next_action="[dry-run] no files written",
            warnings=warnings,
            created_files=[],
        )

    # Create directories
    created_files: list[str] = []
    for d in [
        "roles",
        "pipelines",
        "phases",
        "inbox",
        "outbox",
        "context",
        "logs",
        "reports",
    ]:
        (agentic / d).mkdir(parents=True, exist_ok=True)
        created_files.append(f".agentic/{d}/")

    # Generate config.yaml
    config_content = _CONFIG_TEMPLATE.format(
        project_name=project_name,
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        typecheck_cmd=typecheck_cmd,
        build_cmd=build_cmd,
    )
    atomic_write_text(agentic / "config.yaml", config_content)
    created_files.append(".agentic/config.yaml")

    # Copy supervisor.md template
    framework_dir = Path(__file__).resolve().parent.parent
    templates_dir = framework_dir / "templates"
    supervisor_template = templates_dir / "roles" / "supervisor.md"
    supervisor_dest = agentic / "roles" / "supervisor.md"
    if supervisor_template.is_file():
        shutil.copy2(supervisor_template, supervisor_dest)
        created_files.append(".agentic/roles/supervisor.md")
    else:
        warnings.append(f"supervisor.md template not found at {supervisor_template}")

    # plan.md stub — point to vision if found
    if vision_path is not None:
        rel = Path("..") / ".." / vision_path.name
        plan_body = (
            f"# {project_name} — Plan\n\n"
            f"> Контекст проекта: прочитай `{rel}` перед планированием.\n\n"
            f"Steps:\n"
            f"- [ ] (supervisor заполнит после изучения vision)\n"
        )
    else:
        plan_body = (
            f"# {project_name} — Plan\n\n"
            f"> Vision/README не найден в корне проекта. Спроси пользователя "
            f"о контексте перед планированием.\n\n"
            f"Steps:\n"
            f"- [ ] (supervisor заполнит)\n"
        )
    plan_path = agentic / "phases" / "plan.md"
    plan_path.write_text(plan_body, encoding="utf-8")
    created_files.append(".agentic/phases/plan.md")

    # Update .gitignore (idempotent)
    _update_gitignore(project_dir)

    # Read back content for result (so caller has it without extra reads)
    supervisor_md = _read_file_text(supervisor_dest) if supervisor_dest.is_file() else ""
    plan_md = _read_file_text(plan_path)
    vision_excerpt = _read_file_text(vision_path, max_chars=4000) if vision_path else ""

    # Determine next action
    next_action = (
        "Открой project-setup форму (MCP tool open_form, template='project-setup') "
        "для выбора pipeline и ролей. После submit — awf start."
    )

    return InitResult(
        project_name=project_name,
        project_dir=str(project_dir),
        stack=stack_info["stack"],
        vision_path=str(vision_path) if vision_path else None,
        vision_excerpt=vision_excerpt,
        supervisor_md=supervisor_md,
        plan_md=plan_md,
        pipeline_configured=False,
        next_action=next_action,
        warnings=warnings,
        created_files=created_files,
    )


_CONFIG_TEMPLATE = '''project:
  name: "{project_name}"
  root: "."

models:
  supervisor:
    description: "Current session model"
# Agent roles (system-analyst, developer, qa, project-auditor, ...) are added
# dynamically by the project-setup form on first submit (BD-12 auto-maps each
# role to agent_name="worker"). Don't pre-populate here.

verification:
  test_cmd: "{test_cmd}"
  lint_cmd: "{lint_cmd}"
  typecheck_cmd: "{typecheck_cmd}"
  build_cmd: "{build_cmd}"
  coverage_cmd: ""

phases:
  current: ".agentic/phases/plan.md"

default_pipeline: "default"

retry:
  max_attempts: 3
  backoff_seconds: 0
'''


def _update_gitignore(project_dir: Path) -> None:
    """Idempotent: add .agentic runtime dirs to .gitignore."""
    gitignore_block = (
        ".agentic/inbox/\n.agentic/outbox/\n.agentic/context/\n"
        ".agentic/logs/\n.agentic/reports/\n"
        ".agentic/inputs/\n.agentic/dashboards/\n"
    )
    gitignore = project_dir / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if ".agentic/inbox/" not in content:
            gitignore.write_text(
                content + "\n# Agentic workflow runtime files\n" + gitignore_block + "\n",
                encoding="utf-8",
            )
        elif ".agentic/inputs/" not in content:
            gitignore.write_text(
                content + "\n# agent-workflow-ui runtime\n.agentic/inputs/\n.agentic/dashboards/\n",
                encoding="utf-8",
            )
    else:
        gitignore.write_text(
            "# Agentic workflow runtime files\n" + gitignore_block + "\n",
            encoding="utf-8",
        )


# ─── Public API: start_pipeline / continue_pipeline ─────────────────────
# These wrap orchestrator.run_pipeline via subprocess (background) or direct
# call (foreground). Long-running — MCP-4 defines the run_id/poll contract.


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

    When ``background=True``, launches a detached subprocess and returns
    immediately with a PID. Otherwise runs synchronously and returns the
    final exit code.
    """
    project_dir = Path(project_dir).resolve()
    _require_agentic(project_dir)

    if background:
        return _start_in_background(
            project_dir,
            pipeline=pipeline,
            from_stage=from_stage,
            auto=auto,
            timeout=timeout,
        )

    # Foreground: invoke orchestrator directly via args namespace
    from .orchestrator import run_pipeline

    args = _PipelineArgs(
        project_dir=str(project_dir),
        pipeline=pipeline,
        from_stage=from_stage,
        auto=auto,
        timeout=timeout,
    )
    exit_code = run_pipeline(args)
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
    _require_agentic(project_dir)

    current_todo = todos.newest_active(project_dir)
    if not current_todo:
        return StartResult(
            run_mode="noop",
            run_id=None,
            log_file=None,
            exit_code=0,
            message="No active TODO found.",
        )

    from .orchestrator import run_pipeline

    args = _PipelineArgs(
        project_dir=str(project_dir),
        pipeline=pipeline,
        from_stage=from_stage,
        auto=auto,
        timeout=timeout,
    )
    exit_code = run_pipeline(args)
    return StartResult(
        run_mode="foreground",
        run_id=None,
        log_file=None,
        exit_code=exit_code,
        message=f"Continued {current_todo}, exit code {exit_code}",
    )


@dataclass
class _PipelineArgs:
    """Minimal args namespace for orchestrator.run_pipeline (getattr-based)."""

    project_dir: str = "."
    pipeline: str | None = None
    from_stage: str | None = None
    auto: bool = False
    timeout: int = 3600
    command: str = "start"


def _start_in_background(
    project_dir: Path,
    *,
    pipeline: str | None,
    from_stage: str | None,
    auto: bool,
    timeout: int,
) -> StartResult:
    """Re-launch awf start detached, log to .agentic/logs/awf-start.out.

    MCP-4: writes PID file (.agentic/logs/awf-start.pid) so awf_status can
    detect a running pipeline and tail the log for progress.
    """
    logs_dir = paths.agentic_dir(project_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "awf-start.out"
    pid_file = logs_dir / "awf-start.pid"

    child_argv: list[str] = [
        sys.executable,
        "-m",
        "awf",
        "start",
        "--project-dir",
        str(project_dir),
    ]
    if pipeline:
        child_argv += ["--pipeline", pipeline]
    if from_stage:
        child_argv += ["--from-stage", from_stage]
    if auto:
        child_argv.append("--auto")
    child_argv += ["--timeout", str(timeout)]

    with open(log_file, "wb") as out:
        proc = subprocess.Popen(
            child_argv,
            cwd=str(project_dir),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    # MCP-4: persist PID for status polling (atomic — small file)
    atomic_write_text(pid_file, f"{proc.pid}\n")

    return StartResult(
        run_mode="background",
        run_id=proc.pid,
        log_file=str(log_file),
        exit_code=None,
        message=f"awf start running in background (PID {proc.pid})",
    )


# ─── Public API: analyze_roles ──────────────────────────────────────────


def analyze_roles(
    project_dir: Path,
    *,
    dry_run: bool = False,
) -> AnalyzeRolesResult:
    """Analyze team roles for zone overlaps, optionally add disambiguation.

    Returns overlaps found and patches applied (or proposed if dry_run).
    Currently wraps cmd_analyze_roles.run via args namespace; output text is
    captured and returned in ``patches_applied`` for caller convenience.
    Refactor to extract pure logic — tracked as MCP-1 follow-up.
    """
    project_dir = Path(project_dir).resolve()
    _require_agentic(project_dir)

    import contextlib
    import io

    from . import cmd_analyze_roles

    args = _AnalyzeArgs(project_dir=str(project_dir), dry_run=dry_run)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_analyze_roles.run(args)
    output = buf.getvalue()

    # Parse output for structured result (pragmatic; refactor later)
    overlaps: list[dict[str, Any]] = []
    patches_applied: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("⚠"):
            overlaps.append({"description": line})
        elif line.startswith("+") and ".md:" in line:
            parts = line.split(":", 2)
            if len(parts) >= 2:
                patches_applied.append({
                    "role_file": parts[0].strip().lstrip("+").strip(),
                    "preview": parts[1].strip() if len(parts) > 1 else "",
                })

    return AnalyzeRolesResult(
        overlaps=overlaps,
        patches_applied=patches_applied,
        dry_run=dry_run,
    )


@dataclass
class _AnalyzeArgs:
    """Minimal args namespace for cmd_analyze_roles.run (getattr-based)."""

    project_dir: str = "."
    dry_run: bool = False
