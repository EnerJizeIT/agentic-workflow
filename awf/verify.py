"""Verify commands and auto-DONE logic."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from . import config as cfg_mod
from . import git_utils
from ._atomic import atomic_write_text

# Per-command timeout for verify commands (test/lint/typecheck/build).
# A hung watcher (`pytest --watch`) or stdin-prompt would otherwise block
# the orchestrator indefinitely — `attempt_auto_done` lives in the main
# pipeline loop, so a hang freezes the whole pipeline.
# Override via env: AWF_VERIFY_TIMEOUT=0 disables timeout (legacy behavior).
DEFAULT_VERIFY_TIMEOUT = 600  # 10 minutes per command


def _verify_cmd_timeout() -> int:
    """Per-command timeout override via AWF_VERIFY_TIMEOUT env var.

    Returns 0 to disable (caller passes None to subprocess.run).
    Falls back to default on invalid value (e.g. "abc", "10s").
    """
    raw = os.environ.get("AWF_VERIFY_TIMEOUT", str(DEFAULT_VERIFY_TIMEOUT))
    try:
        return max(0, int(raw))
    except (ValueError, TypeError):
        return DEFAULT_VERIFY_TIMEOUT


def detect_work_evidence(
    project_dir: str | Path,
    baseline_sha: str,
    todo_id: str = "",
) -> bool:
    """Return True if there are tracked changes OR new untracked files vs baseline.

    AUD-1: When ``todo_id`` is provided, pre-existing untracked files
    (snapshotted in ``BASELINE-{todo_id}.untracked`` at baseline time)
    are excluded — only worker-created files count as work evidence.
    Without ``todo_id``, all untracked files count (backward compat).
    """
    cwd = Path(project_dir)
    if not git_utils.is_git_repo(cwd):
        return False
    if git_utils.has_diff(cwd, baseline_sha):
        return True

    untracked = git_utils.untracked_files(cwd)
    if todo_id:
        snapshot = cwd / ".agentic" / "context" / f"BASELINE-{todo_id}.untracked"
        if snapshot.exists():
            try:
                pre_existing = {
                    line.strip()
                    for line in snapshot.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                }
                untracked = [f for f in untracked if f not in pre_existing]
            except OSError:
                pass

    return bool(untracked)


def run_verify_commands(
    config: dict,
    project_dir: str | Path | None = None,
    *,
    todo_id: str | None = None,
) -> bool:
    """Run each non-empty verify command. Returns True if all pass.

    ``project_dir`` is forwarded to ``subprocess.run(cwd=...)`` so commands
    like ``pytest`` / ``npm test`` execute in the project, not in the
    orchestrator's CWD. If None, runs in current CWD (legacy behaviour).

    ``todo_id`` — when provided, captured stdout/stderr of each command is
    persisted to ``.agentic/outbox/TEST-RESULTS-{todo_id}.log`` (atomic,
    appended per-command). This enables ``awf_report`` /
    ``awf.api.get_report`` to surface recent test output instead of returning
    ``None`` (audit fix). When None, output is discarded (back-compat for
    ``cmd_baseline`` which captures tests separately).
    """
    cmd_keys = ["test_cmd", "lint_cmd", "typecheck_cmd", "build_cmd"]
    cmds = []
    for key in cmd_keys:
        val = cfg_mod.get(config, f"verification.{key}", "")
        if val:
            cmds.append(val)

    if not cmds:
        return False  # no commands configured → can't verify

    cwd = Path(project_dir) if project_dir is not None else None
    log_path: Path | None = None
    if todo_id and cwd is not None:
        log_path = cwd / ".agentic" / "outbox" / f"TEST-RESULTS-{todo_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

    log_chunks: list[str] = []
    cmd_timeout = _verify_cmd_timeout()
    for cmd in cmds:
        parts = shlex.split(cmd)
        if not parts:
            return False
        try:
            result = subprocess.run(
                parts,
                capture_output=True,
                text=True,
                check=False,
                cwd=str(cwd) if cwd is not None else None,
                timeout=cmd_timeout or None,
            )
        except subprocess.TimeoutExpired:
            # Hung command (watcher, stdin prompt, infinite loop). Treat
            # as failure — orchestrator must not block forever.
            if log_path is not None:
                log_chunks.append(
                    f"$ {cmd}\nTIMEOUT after {cmd_timeout}s — command did not exit\n"
                )
                atomic_write_text(log_path, "".join(log_chunks))
            return False
        except (FileNotFoundError, OSError) as e:
            if log_path is not None:
                log_chunks.append(f"$ {cmd}\nABORTED: {e}\n")
                atomic_write_text(log_path, "".join(log_chunks))
            return False
        if log_path is not None:
            chunk = f"$ {cmd}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n"
            log_chunks.append(chunk)
        if result.returncode != 0:
            if log_path is not None:
                atomic_write_text(log_path, "".join(log_chunks))
            return False

    if log_path is not None:
        atomic_write_text(log_path, "".join(log_chunks))
    return True


def attempt_auto_done(
    project_dir: str | Path,
    todo_id: str,
    config: dict,
    baseline_sha: str,
) -> bool:
    """Try to synthesize DONE when worker left no signal but work + verify pass.

    Returns True if DONE was synthesized.
    """
    cwd = Path(project_dir)
    outbox = cwd / ".agentic" / "outbox"

    # Check automation.auto_done opt-out
    # Auditor HIGH: YAML parses `true` as Python bool True, not string "true".
    # Was: `if auto_done_enabled not in ("true", "1")` — failed for bool.
    auto_done_enabled = cfg_mod.get(config, "automation.auto_done", "true")
    if isinstance(auto_done_enabled, bool):
        if not auto_done_enabled:
            return False
    elif isinstance(auto_done_enabled, str):
        if auto_done_enabled.lower() not in ("true", "1"):
            return False
    elif isinstance(auto_done_enabled, int):
        if auto_done_enabled == 0:
            return False

    # Must have work evidence
    if not detect_work_evidence(cwd, baseline_sha, todo_id):
        return False

    # DF5-3: if verify commands are configured, they must all pass.
    # If NO commands configured (greenfield/doc-heavy projects), treat as
    # "no blocking checks" — work evidence alone is sufficient for auto-DONE.
    # Supervisor review stage is still in place as a safety net.
    has_verify_cmds = any(
        cfg_mod.get(config, f"verification.{k}", "")
        for k in ("test_cmd", "lint_cmd", "typecheck_cmd", "build_cmd")
    )
    if has_verify_cmds:
        if not run_verify_commands(config, project_dir=project_dir, todo_id=todo_id):
            return False

    # Synthesize DONE
    done_md = outbox / f"DONE-{todo_id}.md"
    atomic_write_text(done_md,
        f"# DONE-{todo_id} (AUTO-GENERATED by orchestrator)\n"
        f"\n"
        f"The worker finished the stage but did not write a DONE signal (most likely turn-budget\n"
        f"exhaustion before the report-writing step). The orchestrator synthesized this DONE\n"
        f"because ALL configured verify commands (typecheck/build/test) passed AND git changes\n"
        f"were detected vs the baseline. Supervisor review is still recommended; the worker's\n"
        f"own DONE report (design notes, decisions) is absent.\n",
        encoding="utf-8",
    )
    (outbox / f"DONE-{todo_id}.ready").touch()
    return True
