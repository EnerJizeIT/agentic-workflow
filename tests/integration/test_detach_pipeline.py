"""FU-19 (AUD12-13): the REAL detach path — no fake Popen.

Every other background test fakes ``subprocess.Popen`` (the conftest
orchestrator-intercept or a test-local fake), so the actual detach
mechanics — real process spawn, PID file, ``state.pipeline_pid``,
kill-by-PID — had only one e2e cover (``bin/awf`` full-pipeline run).

This test drives ``api.start_pipeline(background=True)`` in-process with
the real Popen. A stub ``opencode`` (behavior ``sleep``) keeps the
detached child alive mid-stage so all four mechanics are observable.

SAFETY: the child is a session leader (start_new_session=True) → its
pgid equals its pid, so kill-by-pgid is kill-by-pid. The conftest
``_forbid_session_kill`` tripwire (pid > 1) applies to every call here.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from awf import api

# Captured at module import — BEFORE the autouse conftest fixture patches
# subprocess.Popen with its orchestrator intercept. This is the real Popen.
_REAL_POPEN = subprocess.Popen

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STUBS_DIR = REPO_ROOT / "tests" / "stubs"


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "detach"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _wait(cond, timeout: float, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def _state(pid: int) -> str | None:
    """Process state letter from /proc, None when the pid is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return stat.rsplit(")", 1)[1].split()[0]
    except OSError:
        return None


def _alive(pid: int) -> bool:
    """Running or sleeping — not gone, not a zombie."""
    return _state(pid) not in (None, "Z")


def _dead(pid: int) -> bool:
    """Gone or zombie (a zombie is dead, just not reaped yet)."""
    return _state(pid) in (None, "Z")


class TestRealDetach:
    def test_detached_start_pid_file_state_and_kill(self, tmp_path, monkeypatch):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="Detach")

        # Minimal pipeline (same shape as the initialized_project fixture)
        # so the child can enter the plan stage and stay there.
        pipes = repo / ".agentic" / "pipelines"
        pipes.mkdir(parents=True, exist_ok=True)
        (pipes / "default.yaml").write_text(
            'name: "default"\n'
            'description: "detach test"\n\n'
            'stages:\n'
            '  - name: "plan"\n'
            '    role: "supervisor"\n'
            '    action: "create_todo"\n'
            '  - name: "implement"\n'
            '    role: "worker"\n'
            '    action: "execute_todo"\n'
            '    on_blocked: "escalate"\n',
            encoding="utf-8",
        )
        # worker.md: without it the implement stage crashes at runtime
        # (the child would exit before we can observe it).
        roles = repo / ".agentic" / "roles"
        (roles / "worker.md").write_text("# Worker\n\nExecute the TODO.\n", encoding="utf-8")
        inbox = repo / ".agentic" / "inbox"
        (inbox / "TODO-0001.md").write_text("# Task\n", encoding="utf-8")
        (inbox / "TODO-0001.ready").touch()

        # Child env: stub opencode that sleeps → child alive mid-stage.
        monkeypatch.setenv("PATH", f"{STUBS_DIR}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("AWF_TEST_OPENCODE_BEHAVIOR", "sleep")
        monkeypatch.setenv("AWF_TEST_OPENCODE_SLEEP_SECONDS", "60")
        # AUD04-10 class: a leaked value would change the child's behavior.
        monkeypatch.delenv("AWF_BACKGROUND_CHILD", raising=False)
        # Bypass the conftest Popen intercept — this test NEEDS the real spawn.
        monkeypatch.setattr("subprocess.Popen", _REAL_POPEN)

        pid: int | None = None
        try:
            result = api.start_pipeline(repo, background=True)
            assert result.run_mode == "background", result.message
            pid = result.run_id
            assert isinstance(pid, int) and pid > 1

            # 1) the PID file names the child
            pid_file = repo / ".agentic" / "logs" / "awf-start.pid"
            assert pid_file.is_file(), "PID file not written"
            assert int(pid_file.read_text(encoding="utf-8").strip()) == pid

            # 2) the child writes state.pipeline_pid = its own pid
            from awf import pipeline_state

            def _state_pid_ok() -> bool:
                st = pipeline_state.read_state(repo)
                return bool(st) and st.get("pipeline_pid") == pid

            assert _wait(_state_pid_ok, timeout=20), (
                "state.pipeline_pid was never set to the detached child's pid"
            )

            # 3) at this point the child is REALLY alive (in the agent stage,
            # stub opencode sleeping) — not a zombie of an early crash
            assert _alive(pid), (
                f"child {pid} is not running mid-stage (state={_state(pid)!r}); "
                "log: " + (repo / ".agentic" / "logs" / "awf-start.out")
                .read_text(encoding="utf-8", errors="replace")[-500:]
            )

            # 4) kill-by-PID works (session leader: pgid == pid)
            os.killpg(pid, signal.SIGTERM)
            assert _wait(lambda: _dead(pid), timeout=15), (
                f"child {pid} survived SIGTERM to its process group"
            )
        finally:
            if pid is not None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
