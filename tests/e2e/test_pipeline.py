"""E2E tests for `awf start` pipeline execution — happy path, blocked, auto-done."""
import re
import subprocess
import time
from pathlib import Path

from conftest import run_awf


def _two_stage_pipeline(proj: Path) -> None:
    """Replace default.yaml with implement → verify (no plan stage).

    AUD12-10: the stub replaces `opencode` for EVERY stage, so a
    crash/hang behavior would hit the plan (supervisor) stage first. The
    supervisor subprocess is not bounded by --timeout (its hard_timeout
    stays at the 3600s default), so the crash/hang must reach the WORKER
    stage to exercise the real failure/kill paths.
    """
    pipeline = proj / ".agentic" / "pipelines" / "default.yaml"
    pipeline.write_text(
        'name: "default"\n'
        'stages:\n'
        '  - name: "implement"\n'
        '    role: "worker"\n'
        '    on_blocked: "escalate"\n'
        '    max_retries: 3\n'
        '  - name: "verify"\n'
        '    role: "supervisor"\n'
        '    on_approved: "commit_and_next"\n'
        '    on_rejected: "replan"\n',
        encoding="utf-8",
    )


def _orphan_sleep_pids(seconds: int) -> list[str]:
    """Scan /proc for leftover `sleep <seconds>` processes (AUD12-10).

    psutil is not a test dependency; /proc is sufficient on the Linux CI
    box and needs no extra install.
    """
    found = []
    for pid_dir in sorted(Path("/proc").glob("[0-9]*")):
        try:
            cmdline = (pid_dir / "cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue  # process vanished or not readable
        parts = [p for p in cmdline.split("\x00") if p]
        if "sleep" in parts and str(seconds) in parts:
            found.append(pid_dir.name)
    return found


def _create_todo(proj: Path, todo_id: str = "TODO-0001"):
    """Create a TODO in inbox so the orchestrator can find it.

    Also pre-creates APPROVE-TODO-NNNN.ready signal so BD-8 auto-mode
    commit gate doesn't block pipeline. Tests run with --auto, which
    (per BD-8) requires explicit approval before commit_and_next commits.
    """
    inbox = proj / ".agentic/inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nstub task\n")
    (inbox / f"{todo_id}.ready").write_text(f"signal: TASK_READY\ntask_id: {todo_id}\n")
    # BD-8: pre-authorize commit so pipeline doesn't wait in --auto mode
    (inbox / f"APPROVE-{todo_id}.ready").write_text(
        f"signal: APPROVE\ntask_id: {todo_id}\n"
    )
    (proj / ".agentic/context").mkdir(exist_ok=True)


class TestPipeline:

    def test_pipeline_happy_path(self, initialized_project: Path, awf_bin: str, awf_env: dict):
        """Pipeline runs end-to-end with stub writing DONE signal."""
        _create_todo(initialized_project)

        result = run_awf(
            awf_bin, ["start", "--auto"],
            cwd=initialized_project, env=awf_env, input_data=b"", timeout=60
        )

        # DF6-1: after verify approve, TODO archived to done/{todo_id}/
        assert (initialized_project / ".agentic/done/TODO-0001").is_dir(), \
            f"Expected done/TODO-0001/ (DF6-1 archive). stdout={result.stdout.decode()!r} stderr={result.stderr.decode()!r}"

        # AUD10-05: the dashboard server was a daemon of the orchestrator —
        # after a clean exit the port file must be gone, or
        # awf_open_pipeline_dashboard would open a dead URL.
        port_file = initialized_project / ".agentic" / "state" / "dashboard_port"
        assert not port_file.exists(), "stale dashboard_port file after pipeline exit"

    def test_pipeline_blocked_then_replan(self, initialized_project: Path, awf_bin: str, awf_env: dict):
        """When stub writes BLOCKED, pipeline detects it and exits non-zero."""
        _create_todo(initialized_project)
        awf_env["AWF_TEST_OPENCODE_BEHAVIOR"] = "blocked"

        result = run_awf(
            awf_bin, ["start", "--auto"],
            cwd=initialized_project, env=awf_env, input_data=b"", timeout=60
        )

        # BLOCKED signal should exist
        assert (initialized_project / ".agentic/outbox/BLOCKED-TODO-0001.ready").exists(), \
            f"Expected BLOCKED signal. stderr={result.stderr.decode()!r}"

        # Pipeline should NOT silently succeed — exit code reflects BLOCKED state
        assert result.returncode != 0, "Pipeline should exit non-zero on BLOCKED"

    def test_pipeline_orphan_auto_done(self, initialized_project: Path, awf_bin: str, awf_env: dict):
        """Auto-DONE triggers when verify passes + work evidence present + no signal."""
        _create_todo(initialized_project)

        # Write baseline file so detect_work_evidence has a reference
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=initialized_project, capture_output=True, text=True
        ).stdout.strip()
        (initialized_project / ".agentic/context/BASELINE-TODO-0001.sha").write_text(sha + "\n")

        # Configure verification to always pass — attempt_auto_done checks
        # typecheck_cmd, build_cmd, and test_cmd; all non-empty ones must pass.
        # Quote-style-agnostic regex: handles both " and ' (MCP-audit fix
        # changed CONFIG_TEMPLATE to single quotes for YAML safety).
        cfg = (initialized_project / ".agentic/config.yaml").read_text()
        cfg = re.sub(r'typecheck_cmd:\s*["\'][^"\']*["\']', 'typecheck_cmd: "true"', cfg)
        cfg = re.sub(r'test_cmd:\s*["\'][^"\']*["\']', 'test_cmd: "true"', cfg)
        cfg = re.sub(r'lint_cmd:\s*["\'][^"\']*["\']', 'lint_cmd: ""', cfg)
        (initialized_project / ".agentic/config.yaml").write_text(cfg)

        awf_env["AWF_TEST_OPENCODE_BEHAVIOR"] = "auto_done"

        result = run_awf(
            awf_bin, ["start", "--auto"],
            cwd=initialized_project, env=awf_env, input_data=b"", timeout=60
        )

        # DF6-1: after verify approve, TODO archived to done/{todo_id}/
        done_dir = initialized_project / ".agentic/done/TODO-0001"
        assert done_dir.is_dir(), \
            f"Expected done/TODO-0001/ (DF6-1 archive). stderr={result.stderr.decode()!r} stdout={result.stdout.decode()!r}"

        # Content should indicate it was auto-generated
        done_md = done_dir / "DONE.md"
        if done_md.exists():
            content = done_md.read_text()
            assert "AUTO-GENERATED by orchestrator" in content, \
                f"Expected AUTO-GENERATED marker in DONE report. Got: {content[:200]}"

    def test_pipeline_background_mode(self, initialized_project: Path, awf_bin: str, awf_env: dict):
        """awf start --background returns immediately and runs the pipeline detached."""
        _create_todo(initialized_project)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=initialized_project, capture_output=True, text=True
        ).stdout.strip()
        (initialized_project / ".agentic/context/BASELINE-TODO-0001.sha").write_text(sha + "\n")

        awf_env["AWF_TEST_OPENCODE_BEHAVIOR"] = "done"

        result = run_awf(
            awf_bin, ["start", "--background", "--auto"],
            cwd=initialized_project, env=awf_env, input_data=b"",
            timeout=15,
        )

        stdout = result.stdout.decode()
        assert "running in background" in stdout, f"Expected background banner. stdout={stdout!r}"
        assert "PID" in stdout

        log_file = initialized_project / ".agentic/logs/awf-start.out"
        assert log_file.exists(), f"Log file not created: {log_file}"

        # DF6-1: after verify approve, TODO archived to done/{todo_id}/
        # AUD12-09: budget raised 30s → 60s — a full plan→worker→verify+commit
        # cycle can exceed 30s on a loaded machine (false red before).
        done_dir = initialized_project / ".agentic/done/TODO-0001"
        deadline = time.time() + 60
        while time.time() < deadline:
            if done_dir.is_dir():
                break
            time.sleep(1)

        assert done_dir.is_dir(), (
            f"Detached pipeline did not complete within 60s. "
            f"Log content:\n{log_file.read_text() if log_file.exists() else '<no log>'}"
        )

    def test_pipeline_worker_crash_no_false_done(self, initialized_project: Path, awf_bin: str, awf_env: dict):
        """AUD12-10: stub exits 1 with no signal — the pipeline must stop
        non-zero and must not archive a fake DONE for a dead worker."""
        _create_todo(initialized_project)
        _two_stage_pipeline(initialized_project)
        awf_env["AWF_TEST_OPENCODE_BEHAVIOR"] = "crash"

        result = run_awf(
            awf_bin, ["start", "--auto"],
            cwd=initialized_project, env=awf_env, input_data=b"", timeout=90
        )

        stderr = result.stderr.decode()
        assert result.returncode != 0, \
            f"crashed worker must stop the pipeline. stderr={stderr!r}"
        assert "crashed" in stderr, \
            f"expected the crash to be reported loudly. stderr={stderr!r}"
        # No fake completion: no DONE signal, no done/ archive.
        assert not (initialized_project / ".agentic/outbox/DONE-TODO-0001.ready").exists(), \
            "a crashed worker must not leave a DONE signal"
        assert not (initialized_project / ".agentic/done/TODO-0001").is_dir(), \
            "a crashed worker must not be archived as done"

    def test_pipeline_worker_hang_killed_by_timeout(self, initialized_project: Path, awf_bin: str, awf_env: dict):
        """AUD12-10: stub hangs past --timeout — the hard timeout must kill
        the whole process tree (bash stub + its sleep child) and leave no
        orphan behind (AUD04-06)."""
        _create_todo(initialized_project)
        _two_stage_pipeline(initialized_project)
        awf_env["AWF_TEST_OPENCODE_BEHAVIOR"] = "hang"
        awf_env["AWF_TEST_OPENCODE_HANG_SECONDS"] = "599"

        result = run_awf(
            awf_bin, ["start", "--auto", "--timeout", "5"],
            cwd=initialized_project, env=awf_env, input_data=b"", timeout=120
        )

        stderr = result.stderr.decode()
        assert result.returncode != 0, \
            f"hung worker must stop the pipeline. stderr={stderr!r}"
        # The killed tree must not survive the pipeline exit.
        orphans = _orphan_sleep_pids(599)
        assert not orphans, \
            f"orphan sleep processes after pipeline exit: {orphans}"
