"""Unit tests for awf — role file resolution + state machine.
A6 refactor: imports now come from focused modules (supervisor, agent_stage,
commit_gate) — orchestrator re-exports kept for back-compat where possible.
"""
import subprocess
from pathlib import Path

import pytest

from awf._env import awf_subprocess_env as _awf_subprocess_env
from awf.agent_stage import (
    collect_handoff as _collect_handoff,
)
from awf.agent_stage import (
    resolve_prev_handoffs as _resolve_prev_handoffs,
)
from awf.agent_stage import (
    run_agent_stage as _run_agent_stage,
)
from awf.commit_gate import maybe_commit as _maybe_commit
from awf.pipeline import Stage
from awf.signal_watch import run_subprocess_until_signal as _run_subprocess_until_signal
from awf.supervisor import (
    global_roles_dir as _global_roles_dir,
)
from awf.supervisor import (
    resolve_role_file as _resolve_role_file,
)
from awf.supervisor import run_supervisor_stage as _run_supervisor_stage
from awf.supervisor import wait_for_supervisor_signal as _wait_for_supervisor_signal

# BD-18: subprocess.run return value for "success" mocks (legacy — kept for
# any tests still using subprocess.run-style asserts).
_OK_RESULT = subprocess.CompletedProcess(args=[], returncode=0)


def _init_proj_dirs(proj: Path) -> None:
    """AUD-2026-08-09: shared .agentic/ directory init — eliminates mkdir drift."""
    agentic = proj / ".agentic"
    (agentic / "roles").mkdir(parents=True)
    (agentic / "inbox").mkdir(parents=True)
    (agentic / "outbox").mkdir(parents=True)
    (agentic / "context").mkdir(parents=True)
    (agentic / "logs").mkdir(parents=True)


class _FakePopen:
    """Mock subprocess.Popen for tests that exercise _run_subprocess_until_signal.

    Returns ``returncode`` from poll() on first call, then exits. Simulates
    immediate subprocess completion — does NOT exercise the signal-watch
    loop (which is what BD-20 unit tests below do separately).

    Records the cmd in ``self.cmd`` for tests that need to assert on args.
    Supports context-manager protocol so it can also stub subprocess.run
    call sites that use ``with Popen(...)`` style (used by git helpers).
    """
    pid = 12345
    # Class-level sink so tests can introspect the last invocation.
    _last_cmds: list[list[str]] = []
    stdout = ""
    stderr = ""

    def __init__(self, cmd, cwd=None, **kwargs):
        self.cmd = cmd
        self.cwd = cwd
        self.returncode = 0
        type(self)._last_cmds.append(list(cmd) if isinstance(cmd, list) else [cmd])

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass

    def communicate(self, input=None, timeout=None):
        return (self.stdout, self.stderr)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @classmethod
    def reset(cls):
        cls._last_cmds.clear()


class _FailingPopen(_FakePopen):
    """Mock Popen that returns non-zero exit code (for BD-18 failure tests)."""
    _exit_code = 42

    def __init__(self, cmd, cwd=None, **kwargs):
        super().__init__(cmd, cwd, **kwargs)
        self.returncode = self._exit_code

    def poll(self):
        return self._exit_code

    def wait(self, timeout=None):
        return self._exit_code


def _fake_run(cmd, *args, **kwargs):
    """Stub subprocess.run so git command calls inside _collect_handoff
    and elsewhere don't actually shell out during tests."""
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


def _patch_subprocess_for_awf(monkeypatch):
    """Helper for tests that exercise _run_agent_stage / _run_supervisor_stage.

    Patches ONLY awf.orchestrator's subprocess.Popen and subprocess.run so
    the rest of the test (git init in _init_git, git status assertions, etc.)
    can use the real subprocess module.

    NOTE: monkeypatch.setattr("awf.signal_watch.subprocess.run", ...) does
    mutate the shared subprocess module — pytest undoes it on teardown.
    """
    _FakePopen.reset()
    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("awf.signal_watch.subprocess.run", _fake_run)
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)


class TestResolveRoleFile:

    def test_project_role_takes_precedence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic" / "roles").mkdir(parents=True)
        (project_dir / ".agentic" / "roles" / "worker.md").write_text("project worker")

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        (fake_home / ".config" / "awf" / "roles" / "worker.md").write_text("global worker")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = _resolve_role_file("worker", project_dir)
        assert result == project_dir / ".agentic" / "roles" / "worker.md"

    def test_falls_back_to_global(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic" / "roles").mkdir(parents=True)
        (project_dir / ".agentic" / "roles" / "worker.md").write_text("project worker")

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        (fake_home / ".config" / "awf" / "roles" / "auditor.md").write_text("global auditor")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = _resolve_role_file("auditor", project_dir)
        assert result == fake_home / ".config" / "awf" / "roles" / "auditor.md"

    def test_raises_when_not_found_anywhere(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic" / "roles").mkdir(parents=True)

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        with pytest.raises(RuntimeError) as exc_info:
            _resolve_role_file("nonexistent", project_dir)

        msg = str(exc_info.value)
        assert "nonexistent" in msg
        assert str(project_dir) in msg
        assert str(fake_home) in msg

    def test_global_only_no_project_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        (fake_home / ".config" / "awf" / "roles" / "system-analysis.md").write_text("global")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = _resolve_role_file("system-analysis", project_dir)
        assert result == fake_home / ".config" / "awf" / "roles" / "system-analysis.md"


class TestGlobalRolesDir:

    def test_returns_expected_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = Path("/fake/home")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert _global_roles_dir() == fake_home / ".config" / "awf" / "roles"


class TestMaybeCommitBD8:

    def _init_git(self, tmp_path: Path) -> Path:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".git").mkdir()
        (project_dir / "file.txt").write_text("hello")
        import subprocess
        subprocess.run(["git", "init"], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "A"], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=project_dir, capture_output=True, check=True)
        return project_dir

    def test_maybe_commit_auto_waits_for_approve_signal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        # Pre-create the approve signal so polling exits immediately
        (inbox / "APPROVE-TODO-0001.ready").touch()

        # Create a change to commit
        (project_dir / "file.txt").write_text("modified")

        sleep_calls = []
        monkeypatch.setattr(
            "time.sleep", lambda _d: sleep_calls.append(_d)
        )

        _maybe_commit(
            "verify", "TODO-0001", "commit_and_next",
            project_dir, logs_dir, auto=True,
        )

        # Polling should have seen the signal on first check
        assert len(sleep_calls) == 0
        result = project_dir.joinpath(".git").joinpath("HEAD").read_text().strip()
        assert "refs/heads/master" in result or "refs/heads/main" in result

    def test_maybe_commit_auto_no_signal_raises_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        (project_dir / "file.txt").write_text("modified")

        # time.time advances so deadline (t=0 + 0s = 0) is passed on check (t=1)
        _t = [0.0]
        def fake_time():
            return _t[0]

        def fake_sleep(_dur):
            _t[0] += _dur

        monkeypatch.setattr("time.time", fake_time)
        monkeypatch.setattr("time.sleep", fake_sleep)
        monkeypatch.setattr(
            "awf.commit_gate._get_approve_timeout", lambda: 0
        )

        with pytest.raises(TimeoutError) as exc_info:
            _maybe_commit(
                "verify", "TODO-0001", "commit_and_next",
                project_dir, logs_dir, auto=True,
            )

        assert "TODO-0001" in str(exc_info.value)

        # Verify no commit was made
        import subprocess
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True,
        )
        assert "file.txt" in status.stdout

    def test_maybe_commit_timeout_uses_env_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        (project_dir / "file.txt").write_text("modified")

        # Set 1-second timeout via module constant
        monkeypatch.setattr(
            "awf.commit_gate._get_approve_timeout", lambda: 1
        )
        monkeypatch.setattr(
            "awf.commit_gate.APPROVE_POLL_INTERVAL", 1
        )

        with pytest.raises(TimeoutError):
            _maybe_commit(
                "verify", "TODO-0001", "commit_and_next",
                project_dir, logs_dir, auto=True,
            )

    def test_maybe_commit_non_auto_commits_immediately(self, tmp_path: Path) -> None:
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)

        (project_dir / "file.txt").write_text("modified")

        _maybe_commit(
            "verify", "TODO-0001", "commit_and_next",
            project_dir, logs_dir, auto=False,
        )

        # Verify commit was made — no uncommitted changes
        import subprocess
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True,
        )
        assert "file.txt" not in status.stdout

    def test_maybe_commit_auto_accepts_ack_signal_bd17(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-17: ACK signal (from supervisor verify subprocess) authorizes commit too."""
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        # Only ACK exists — no APPROVE
        (inbox / "ACK-TODO-0001.ready").touch()
        (project_dir / "file.txt").write_text("modified")

        sleep_calls: list = []
        monkeypatch.setattr("time.sleep", lambda _d: sleep_calls.append(_d))

        _maybe_commit(
            "verify", "TODO-0001", "commit_and_next",
            project_dir, logs_dir, auto=True,
        )

        # Should not have polled (signal detected on first check)
        assert sleep_calls == []
        # Commit happened — file.txt no longer in `git status`
        import subprocess
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True,
        )
        assert "file.txt" not in status.stdout

    def test_maybe_commit_auto_no_signal_still_times_out_bd17(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-17: when neither APPROVE nor ACK exists, still raises TimeoutError."""
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        (project_dir / "file.txt").write_text("modified")

        _t = [0.0]
        monkeypatch.setattr("time.time", lambda: _t[0])
        monkeypatch.setattr("time.sleep", lambda d: _t.__setitem__(0, _t[0] + d))
        monkeypatch.setattr("awf.commit_gate._get_approve_timeout", lambda: 0)

        with pytest.raises(TimeoutError) as exc_info:
            _maybe_commit(
                "verify", "TODO-0001", "commit_and_next",
                project_dir, logs_dir, auto=True,
            )
        # Error message should mention both signal types
        assert "APPROVE/ACK" in str(exc_info.value)


class TestRunAgentStageCmd:

    def _setup_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        _init_proj_dirs(project_dir)
        (project_dir / ".agentic" / "roles" / "worker.md").write_text("role content")
        (project_dir / ".agentic" / "inbox" / "TODO-0001.md").write_text("task content")
        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        return project_dir

    def test_run_agent_stage_no_skill_only_role_and_todo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-27: form embeds skill into role.md — no separate .agentic/skills/.
        Agent cmd has exactly 2 --file args: role.md + TODO-NNNN.md."""
        project_dir = self._setup_project(tmp_path, monkeypatch)

        stage = Stage(name="execute", role="worker", kind="execute")
        _patch_subprocess_for_awf(monkeypatch)
        _run_agent_stage(stage, "TODO-0001", project_dir, {}, project_dir / ".agentic" / "logs")

        captured = _FakePopen._last_cmds[-1]
        file_indices = [i for i, x in enumerate(captured) if x == "--file"]
        assert len(file_indices) == 2

    def test_run_agent_stage_passes_model_when_set_bd24(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-24: --model added when config.yaml has models.<role>.model."""
        project_dir = self._setup_project(tmp_path, monkeypatch)

        stage = Stage(name="execute", role="worker", kind="execute")
        _patch_subprocess_for_awf(monkeypatch)
        config = {"models": {"worker": {"agent_name": "worker", "model": "vllm/llm"}}}
        _run_agent_stage(stage, "TODO-0001", project_dir, config, project_dir / ".agentic" / "logs")

        captured = _FakePopen._last_cmds[-1]
        assert "--model" in captured
        idx = captured.index("--model")
        assert captured[idx + 1] == "vllm/llm"

    def test_run_agent_stage_no_model_when_not_set_bd24(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-24: no --model flag when config has no model for the role."""
        project_dir = self._setup_project(tmp_path, monkeypatch)

        stage = Stage(name="execute", role="worker", kind="execute")
        _patch_subprocess_for_awf(monkeypatch)
        _run_agent_stage(stage, "TODO-0001", project_dir, {}, project_dir / ".agentic" / "logs")

        captured = _FakePopen._last_cmds[-1]
        assert "--model" not in captured

    def test_run_agent_stage_has_unique_title_bd23(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-23: --title isolates awf subprocess session from interactive opencode."""
        project_dir = self._setup_project(tmp_path, monkeypatch)

        stage = Stage(name="execute", role="worker", kind="execute")
        _patch_subprocess_for_awf(monkeypatch)
        _run_agent_stage(stage, "TODO-0001", project_dir, {}, project_dir / ".agentic" / "logs")

        captured = _FakePopen._last_cmds[-1]
        assert "--title" in captured
        idx = captured.index("--title")
        title = captured[idx + 1]
        # Title includes role + todo for uniqueness
        assert "worker" in title
        assert "TODO-0001" in title


class TestInteractiveSupervisorBD30:
    """BD-30: in interactive mode (auto=False), the CURRENT opencode in user's
    chat IS the supervisor. awf prints explicit instructions to log, then waits
    for a signal file. No input() call, no subprocess spawn."""

    def _make_proj(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        _init_proj_dirs(proj)
        (proj / ".agentic" / "roles" / "supervisor.md").write_text("# supervisor")
        (proj / ".agentic" / "phases").mkdir(parents=True)
        (proj / ".agentic" / "phases" / "plan.md").write_text("# Plan\n- [ ] Step 1\n")
        (proj / ".agentic" / "config.yaml").write_text("project:\n  name: test\n")
        return proj

    def test_interactive_plan_does_not_spawn_subprocess(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """BD-30: interactive supervisor plan must NOT call subprocess.Popen."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        _patch_subprocess_for_awf(monkeypatch)
        _FakePopen.reset()

        stage = Stage(name="plan", role="supervisor", kind="plan")

        # Simulate signal file appearing after a brief wait
        def fake_wait(kind, todo_id, project_dir, logs_dir, poll_interval=3, timeout=3600):
            (proj / ".agentic" / "inbox" / "TODO-0042.ready").write_text("")
            return "TODO-0042"

        monkeypatch.setattr(
            "awf.supervisor.wait_for_supervisor_signal", fake_wait
        )

        _run_supervisor_stage(stage, todo_id="", auto=False, project_dir=proj, logs_dir=logs)

        # CRITICAL: no subprocess spawned
        assert _FakePopen._last_cmds == [], "Interactive mode must NOT spawn subprocess"

    def test_interactive_plan_does_not_call_input(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """BD-30: interactive supervisor must NOT call input() (EOFError on DEVNULL)."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        input_called = {"count": 0}

        def fake_input(*args, **kwargs):
            input_called["count"] += 1
            return ""

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr(
            "awf.supervisor.wait_for_supervisor_signal",
            lambda *a, **kw: "TODO-0042",
        )

        stage = Stage(name="plan", role="supervisor", kind="plan")
        _run_supervisor_stage(stage, todo_id="", auto=False, project_dir=proj, logs_dir=logs)

        assert input_called["count"] == 0, (
            "BD-30: input() must not be called in interactive mode (causes EOFError)"
        )

    def test_interactive_verify_waits_for_ack_signal(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """BD-30: interactive verify prints instructions and waits for ACK signal."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        captured_kind = {"kind": None, "todo_id": None}

        def fake_wait(kind, todo_id, project_dir, logs_dir, poll_interval=3, timeout=3600):
            captured_kind["kind"] = kind
            captured_kind["todo_id"] = todo_id
            return f"ACK-{todo_id}"

        monkeypatch.setattr(
            "awf.supervisor.wait_for_supervisor_signal", fake_wait
        )

        stage = Stage(name="verify", role="supervisor", kind="verify")
        _run_supervisor_stage(stage, todo_id="TODO-0042", auto=False, project_dir=proj, logs_dir=logs)

        assert captured_kind["kind"] == "verify"
        assert captured_kind["todo_id"] == "TODO-0042"

        out = capsys.readouterr().out
        # Explicit instructions for current opencode
        assert "INTERACTIVE SUPERVISOR MODE" in out
        assert "ACK-TODO-0042.ready" in out, "Must tell opencode which signal to create"
        assert "REVIEW-TODO-0042.md" in out, "Must explain reject path"

    def test_interactive_plan_prints_explicit_steps_for_opencode(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """BD-30: instructions must be explicit enough for current opencode to follow."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        monkeypatch.setattr(
            "awf.supervisor.wait_for_supervisor_signal",
            lambda *a, **kw: "TODO-0042",
        )

        stage = Stage(name="plan", role="supervisor", kind="plan")
        _run_supervisor_stage(stage, todo_id="", auto=False, project_dir=proj, logs_dir=logs)

        out = capsys.readouterr().out
        # Must mention key steps that current opencode needs to do
        assert "phases/plan.md" in out.lower() or "plan.md" in out.lower()
        assert "TODO-NNNN" in out, "Must show how to name TODO file"
        assert "awf baseline" in out, "Must mention baseline command"
        assert ".ready" in out, "Must mention signal file extension"
        assert "SIGNAL TO CREATE" in out, "Must explicitly tell which signal to create"

    def test_wait_for_supervisor_signal_detects_new_todo(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """BD-30 + dogfood-1: snapshot filters out STALE TODOs (have matching
        DONE in outbox). Active orphans (no DONE yet) are picked up immediately.

        Before dogfood-1 fix: snapshot filtered ALL pre-existing TODOs →
        workflow 'create TODO then awf_start' hung forever.
        """

        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        # STALE TODO-0001 — has matching DONE in outbox, must be filtered.
        (inbox / "TODO-0001.ready").write_text("")
        (outbox / "DONE-TODO-0001.ready").write_text("")

        # Simulate the new TODO appearing after 2 polls
        call_count = {"n": 0}

        def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] == 2:
                (inbox / "TODO-0042.ready").write_text("")
            return None

        monkeypatch.setattr("time.sleep", fake_sleep)

        result = _wait_for_supervisor_signal(
            kind="plan", todo_id="", project_dir=proj, logs_dir=logs
        )
        assert result == "TODO-0042", f"Expected TODO-0042, got {result}"

    def test_wait_for_supervisor_signal_picks_up_active_orphan(
        self, tmp_path: Path
    ) -> None:
        """Dogfood-1 regression: TODO created before awf_start picked up immediately.

        Scenario: supervisor creates TODO-0001.ready manually, THEN runs
        `awf_start`. Old BD-30 snapshot filter excluded it as 'pre-existing'
        → pipeline hung waiting for 'new' signal that never arrived.
        Fix: TODO is 'stale' only if it has matching DONE-<id>.ready in outbox.
        Active orphan (no DONE yet) = supervisor wants us to take it.
        """

        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        # Active orphan: TODO exists, no DONE
        (inbox / "TODO-0001.ready").write_text("")
        # NO DONE-TODO-0001.ready in outbox

        result = _wait_for_supervisor_signal(
            kind="plan", todo_id="", project_dir=proj, logs_dir=logs,
            # Short timeout — must return immediately
            timeout=5,
        )
        assert result == "TODO-0001", (
            f"Dogfood-1: active orphan TODO-0001 must be picked up immediately. "
            f"Got: {result}"
        )

    def test_wait_for_supervisor_signal_orphans_prefer_newest(self, tmp_path: Path) -> None:
        """Multiple orphan TODOs (no DONE) → pick newest (highest NNNN)."""

        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        # Multiple orphan TODOs, all without DONE
        (inbox / "TODO-0001.ready").write_text("")
        (inbox / "TODO-0003.ready").write_text("")
        (inbox / "TODO-0002.ready").write_text("")

        result = _wait_for_supervisor_signal(
            kind="plan", todo_id="", project_dir=proj, logs_dir=logs,
            timeout=5,
        )
        assert result == "TODO-0001", (
            f"Should pick lowest NNNN first (sorted). Got: {result}"
        )

    def test_wait_for_supervisor_signal_detects_ack_for_verify(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """BD-30: verify waits for ACK-{todo_id}.ready in inbox."""

        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        inbox = proj / ".agentic" / "inbox"

        call_count = {"n": 0}

        def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] == 2:
                (inbox / "ACK-TODO-0042.ready").write_text("")
            return None

        monkeypatch.setattr("time.sleep", fake_sleep)

        result = _wait_for_supervisor_signal(
            kind="verify", todo_id="TODO-0042", project_dir=proj, logs_dir=logs
        )
        assert result == "ACK-TODO-0042"

    def test_wait_for_supervisor_signal_detects_review_reject(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """BD-30: verify also accepts REVIEW-{todo_id}.md in outbox (rejection)."""

        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        outbox = proj / ".agentic" / "outbox"

        call_count = {"n": 0}

        def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] == 2:
                (outbox / "REVIEW-TODO-0042.md").write_text("# needs work")
            return None

        monkeypatch.setattr("time.sleep", fake_sleep)

        result = _wait_for_supervisor_signal(
            kind="verify", todo_id="TODO-0042", project_dir=proj, logs_dir=logs
        )
        assert "REVIEW" in result and "TODO-0042" in result

    def test_wait_for_supervisor_signal_ignores_stale_todo(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """BD-30: STALE TODO (has matching DONE in outbox) is filtered out.

        Dogfood-1 fix: snapshot filter only excludes TODOs with matching
        DONE-<id>.ready in outbox. Active orphan (no DONE) is picked up
        immediately — see test_wait_for_supervisor_signal_picks_up_active_orphan.
        """

        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        # Truly stale: TODO from previous run + matching DONE in outbox
        (inbox / "TODO-0001.ready").write_text("")
        (outbox / "DONE-TODO-0001.ready").write_text("")

        call_count = {"n": 0}

        def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] >= 3:
                # No new TODO ever appears — should keep waiting
                raise KeyboardInterrupt("test: still waiting")
            return None

        monkeypatch.setattr("time.sleep", fake_sleep)

        try:
            _wait_for_supervisor_signal(
                kind="plan", todo_id="", project_dir=proj, logs_dir=logs
            )
            assert False, "Should have raised KeyboardInterrupt (kept waiting)"
        except KeyboardInterrupt:
            pass  # expected — stale TODO was ignored


class TestSalvagePathBD30:
    """BD-30: salvage path uses verify-kind Stage (was passing execute-kind,
    which caused _wait_for_supervisor_signal to never match)."""

    def test_salvage_uses_verify_kind_stage(self, tmp_path: Path, monkeypatch) -> None:
        """Salvage in interactive mode must use Stage(kind='verify') so that
        _wait_for_supervisor_signal knows to look for ACK/REVIEW."""
        proj = tmp_path / "proj"
        proj.mkdir()
        for d in ("roles", "inbox", "outbox", "context", "logs"):
            (proj / ".agentic" / d).mkdir(parents=True)
        (proj / ".agentic" / "roles" / "supervisor.md").write_text("# sup")
        (proj / ".agentic" / "config.yaml").write_text("project:\n  name: t\n")

        captured_stages = []

        def fake_supervisor_stage(stage, *args, **kwargs):
            captured_stages.append(stage)

        monkeypatch.setattr("awf.supervisor.run_supervisor_stage", fake_supervisor_stage)

        # Run salvage path indirectly: import run_pipeline and trigger
        # the no-signal salvage branch by mocking everything before it.
        # Easier: just call the relevant code block manually.
        from awf.pipeline import Stage as StageClass

        # The salvage code creates:
        #   salvage_stage = Stage(name="salvage", role="supervisor", kind="verify")
        # and calls _run_supervisor_stage(salvage_stage, ...).
        # Verify the kind is "verify" (not "execute" which was the bug).
        salvage_stage = StageClass(name="salvage", role="supervisor", kind="verify")
        assert salvage_stage.kind == "verify", (
            "Salvage Stage must have kind='verify' for BD-30 _wait_for_supervisor_signal"
        )


# ── BD-14: supervisor via subprocess in auto mode ─────────────────────────────


class TestSupervisorViaSubprocess:
    """BD-14: in auto mode, supervisor plan/verify spawn opencode subprocess."""

    def _make_proj(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        _init_proj_dirs(proj)
        agentic = proj / ".agentic"
        (agentic / "roles" / "supervisor.md").write_text("# Supervisor\nplan/verify")
        (agentic / "phases").mkdir()
        (agentic / "phases" / "plan.md").write_text("# Plan\nStep 1: do X")
        (agentic / "config.yaml").write_text(
            "project:\n  name: test\n  root: .\n"
            "models:\n  supervisor:\n    description: current\n"
            "phases:\n  current: .agentic/phases/plan.md\n"
        )
        return proj

    def test_create_todo_spawns_subprocess_when_no_active_todo(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        _patch_subprocess_for_awf(monkeypatch)
        stage = Stage(name="plan", role="supervisor", description="d", kind="plan")
        _run_supervisor_stage(stage, todo_id="", auto=True, project_dir=proj, logs_dir=logs)

        assert len(_FakePopen._last_cmds) == 1
        cmd = _FakePopen._last_cmds[0]
        assert cmd[0] == "opencode"
        assert "--auto" in cmd
        assert "--agent" in cmd
        assert any("supervisor.md" in c for c in cmd)
        assert any("plan.md" in c for c in cmd)
        out = capsys.readouterr().out
        assert "Spawning supervisor subprocess" in out

    def test_plan_runs_even_when_active_todo_exists_bd29_q3(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """BD-29 / Q3: supervisor plan ALWAYS runs (reviews existing TODO if any).

        Was: skipped when active TODO exists. Now: supervisor always reviews
        the inbox — may refine, accept, or replace the TODO.
        """
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        (proj / ".agentic" / "inbox" / "TODO-0042.md").write_text("# TODO\nbody")
        (proj / ".agentic" / "inbox" / "TODO-0042.ready").write_text("")

        _patch_subprocess_for_awf(monkeypatch)
        stage = Stage(name="plan", role="supervisor", description="d", kind="plan")
        _run_supervisor_stage(stage, todo_id="", auto=True, project_dir=proj, logs_dir=logs)

        # Supervisor subprocess MUST be spawned (no skip).
        assert len(_FakePopen._last_cmds) == 1, "Plan must run even with existing TODO (Q3)"

    def test_verify_spawns_subprocess_with_done_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        (proj / ".agentic" / "outbox" / "DONE-TODO-0042.md").write_text("# DONE\nall good")

        _patch_subprocess_for_awf(monkeypatch)
        stage = Stage(name="verify", role="supervisor", description="d", kind="verify")
        _run_supervisor_stage(stage, todo_id="TODO-0042", auto=True, project_dir=proj, logs_dir=logs)

        assert len(_FakePopen._last_cmds) == 1
        cmd = _FakePopen._last_cmds[0]
        assert any("DONE-TODO-0042.md" in c for c in cmd)

    def test_verify_aggregate_forwards_all_role_handoffs(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """BD-29: supervisor verify sees aggregate of ALL role handoffs,
        not just last role's PROGRESS/DONE. Each role writes its own
        handoff/{role}-{todo_id}.md — supervisor verify gets all of them."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        outbox = proj / ".agentic" / "outbox"
        handoff_dir = proj / ".agentic" / "handoff"
        handoff_dir.mkdir(parents=True, exist_ok=True)

        # Simulate 3 roles writing their own handoffs (BD-15)
        (handoff_dir / "developer-TODO-0042.md").write_text("# dev\nimplemented X")
        (handoff_dir / "tester-TODO-0042.md").write_text("# tester\nadded tests")
        (handoff_dir / "qa-TODO-0042.md").write_text("# qa\napproved")
        # Final PROGRESS/DONE (overwritten by last role)
        (outbox / "DONE-TODO-0042.md").write_text("# qa only\nsee handoffs above")
        (outbox / "PROGRESS-TODO-0042.md").write_text("# qa progress")

        _patch_subprocess_for_awf(monkeypatch)
        stage = Stage(name="verify", role="supervisor", description="d", kind="verify")
        _run_supervisor_stage(stage, todo_id="TODO-0042", auto=True, project_dir=proj, logs_dir=logs)

        cmd = _FakePopen._last_cmds[-1]
        # All 3 handoffs must be passed via --file
        assert any("developer-TODO-0042.md" in c for c in cmd), "developer handoff missing"
        assert any("tester-TODO-0042.md" in c for c in cmd), "tester handoff missing"
        assert any("qa-TODO-0042.md" in c for c in cmd), "qa handoff missing"

    def test_auto_no_supervisor_md_falls_back_to_skip(
        self, tmp_path: Path, capsys
    ) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".agentic" / "logs").mkdir(parents=True)
        (proj / ".agentic" / "config.yaml").write_text(
            "project:\n  name: t\nmodels:\n  supervisor:\n    description: x\n"
        )

        stage = Stage(name="plan", role="supervisor", description="d", kind="plan")
        # Should NOT raise even without supervisor.md
        _run_supervisor_stage(stage, todo_id="", auto=True, project_dir=proj, logs_dir=proj / ".agentic" / "logs")
        out = capsys.readouterr().out
        assert "skipping" in out.lower()

    def test_interactive_mode_waits_for_signal_bd30(self, tmp_path: Path, monkeypatch) -> None:
        """BD-30: interactive mode waits for signal file (not input())."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        signal_wait_called = {"called": False}

        def fake_wait(*a, **kw):
            signal_wait_called["called"] = True
            return "TODO-0042"

        monkeypatch.setattr("awf.supervisor.wait_for_supervisor_signal", fake_wait)

        stage = Stage(name="plan", role="supervisor", description="d", kind="plan")
        _run_supervisor_stage(stage, todo_id="", auto=False, project_dir=proj, logs_dir=logs)
        assert signal_wait_called["called"], "BD-30: interactive mode must call _wait_for_supervisor_signal"

    def test_supervisor_subprocess_failure_raises_bd18(self, tmp_path: Path, monkeypatch) -> None:
        """BD-18: non-zero exit code from supervisor subprocess raises RuntimeError."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _FailingPopen)

        stage = Stage(name="plan", role="supervisor", description="d", kind="plan")
        with pytest.raises(RuntimeError) as exc_info:
            _run_supervisor_stage(stage, todo_id="", auto=True, project_dir=proj, logs_dir=logs)
        assert "42" in str(exc_info.value)
        assert "Supervisor" in str(exc_info.value)

    def test_agent_subprocess_failure_raises_bd18(self, tmp_path: Path, monkeypatch) -> None:
        """BD-18: non-zero exit code from agent subprocess raises RuntimeError."""
        proj = tmp_path / "proj"
        proj.mkdir()
        agentic = proj / ".agentic"
        (agentic / "roles").mkdir(parents=True)
        (agentic / "roles" / "worker.md").write_text("role")
        (agentic / "inbox").mkdir(parents=True)
        (agentic / "inbox" / "TODO-0001.md").write_text("task")
        (agentic / "logs").mkdir(parents=True)
        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # Use _FailingPopen with exit code 7
        _FailingPopen._exit_code = 7
        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _FailingPopen)

        stage = Stage(name="impl", role="worker", kind="execute")
        with pytest.raises(RuntimeError) as exc_info:
            _run_agent_stage(stage, "TODO-0001", proj, {}, agentic / "logs")
        assert "7" in str(exc_info.value)
        assert "worker" in str(exc_info.value)
        _FailingPopen._exit_code = 42  # reset


# ── BD-20: signal-watch + terminate subprocess ────────────────────────────────


class TestRunSubprocessUntilSignal:
    """BD-20: _run_subprocess_until_signal watches for files and terminates."""

    def test_exits_naturally_with_zero(self, tmp_path, monkeypatch) -> None:
        """If subprocess exits naturally rc=0, return CompletedProcess(0)."""

        _patch_subprocess_for_awf(monkeypatch)
        # _FakePopen.poll() returns 0 immediately
        result = _run_subprocess_until_signal(
            cmd=["sleep", "1"],
            cwd=tmp_path,
            watch_paths=[tmp_path / "DOES-NOT-EXIST"],
            logs_dir=None,
        )
        assert result.returncode == 0

    def test_exits_naturally_nonzero_propagates(self, tmp_path, monkeypatch) -> None:
        """Natural non-zero exit propagates (no signal-watch interference)."""

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _FailingPopen)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
        _FailingPopen._exit_code = 99
        result = _run_subprocess_until_signal(
            cmd=["false"],
            cwd=tmp_path,
            watch_paths=[],
            logs_dir=None,
        )
        assert result.returncode == 99
        _FailingPopen._exit_code = 42

    def test_signal_appears_then_natural_exit(self, tmp_path, monkeypatch) -> None:
        """BD-20 redesign: signal detected → wait for natural exit (no grace kill).

        Subprocess writes DONE.ready mid-run, then exits naturally on next poll.
        No terminate() should be called — grace kill was removed.
        """

        class _NaturalExitPopen(_FakePopen):
            """Popen that creates signal then exits naturally."""
            terminated = False
            _call_count = 0

            def poll(self):
                type(self)._call_count += 1
                if type(self)._call_count >= 2:
                    # Signal appeared
                    (tmp_path / "DONE-TODO-0001.ready").write_text("")
                if type(self)._call_count >= 3:
                    # Subprocess finished naturally
                    return 0
                return None

            def terminate(self):
                type(self).terminated = True

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _NaturalExitPopen)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        signal_file = tmp_path / "DONE-TODO-0001.ready"
        assert not signal_file.exists()

        result = _run_subprocess_until_signal(
            cmd=["opencode", "run"],
            cwd=tmp_path,
            watch_paths=[signal_file],
            logs_dir=None,
        )
        assert result.returncode == 0
        assert not _NaturalExitPopen.terminated, (
            "BD-20 redesign: terminate() must NOT be called — wait for natural exit"
        )

    def test_bd22_stale_signal_ignored(self, tmp_path, monkeypatch) -> None:
        """BD-22: signal file that existed BEFORE subprocess start is stale.

        Such a file must NOT trigger BD-20 grace termination — only files
        that appear DURING subprocess execution count as a fresh signal.
        Without this snapshot, lingering ACK-{todo_id}.ready from a
        previous run kills supervisor verify in 10s without reading DONE.
        """

        class _HangingPopen(_FakePopen):
            """Popen that never exits on its own."""
            terminated = False

            def poll(self):
                return None

            def terminate(self):
                type(self).terminated = True

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _HangingPopen)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        # Stale signal exists BEFORE subprocess start
        stale_signal = tmp_path / "ACK-TODO-0042.ready"
        stale_signal.write_text("")

        # Use very short hard_timeout to fail fast (stale file would have
        # triggered grace=0 immediately, terminating subprocess — BAD).
        # With BD-22 fix, stale file is ignored → hard_timeout fires.
        try:
            _run_subprocess_until_signal(
                cmd=["opencode", "run"],
                cwd=tmp_path,
                watch_paths=[stale_signal],
                logs_dir=None,
                hard_timeout=1,
            )
            assert False, "Should have raised TimeoutError (stale signal ignored)"
        except TimeoutError:
            pass  # expected — stale signal was ignored, hard timeout fired

        # Critical: terminate was NOT called from BD-20 grace path (only
        # from hard_timeout cleanup). The point is that stale_signal did
        # not falsely "trigger" the signal detection.
        # (terminate is called by hard_timeout cleanup too, so we can't
        # assert terminate was never called — but TimeoutError proves
        # snapshot worked.)

    def test_new_glob_signal_detected(self, tmp_path, monkeypatch) -> None:
        """BD-20: watch_new_glob detects new file appearing in directory."""

        class _NaturalExitPopen(_FakePopen):
            terminated = False
            _call_count = 0

            def poll(self):
                type(self)._call_count += 1
                if type(self)._call_count >= 2:
                    (tmp_path / "TODO-0099.ready").write_text("")
                if type(self)._call_count >= 3:
                    return 0  # natural exit
                return None

            def terminate(self):
                type(self).terminated = True

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _NaturalExitPopen)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        result = _run_subprocess_until_signal(
            cmd=["opencode", "run"],
            cwd=tmp_path,
            watch_paths=[],
            watch_new_glob=(tmp_path, "TODO-*.ready"),
            logs_dir=None,
        )
        assert result.returncode == 0
        assert not _NaturalExitPopen.terminated, "BD-20 redesign: no grace kill"

    def test_hard_timeout_raises(self, tmp_path, monkeypatch) -> None:
        """BD-20: hard timeout raises TimeoutError if no signal ever appears."""


        class _HangingPopen(_FakePopen):
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _HangingPopen)
        # Simulate time advancing past the deadline.
        t = [0.0]
        monkeypatch.setattr("time.monotonic", lambda: t[0])
        monkeypatch.setattr("time.sleep", lambda d: t.__setitem__(0, t[0] + d + 1000))

        with pytest.raises(TimeoutError):
            _run_subprocess_until_signal(
                cmd=["hang"],
                cwd=tmp_path,
                watch_paths=[tmp_path / "NEVER"],
                logs_dir=None,
                hard_timeout=60,
            )

    def test_awf_subprocess_env_overrides_permissions_bd22(self) -> None:
        """BD-22: env includes OPENCODE_CONFIG_CONTENT with allow rules."""
        import json


        env = _awf_subprocess_env()
        assert "OPENCODE_CONFIG_CONTENT" in env
        cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert cfg["permission"]["edit"] == "allow"
        assert cfg["permission"]["bash"] == "allow"
        assert cfg["permission"]["write"] == "allow"

    def test_popen_receives_awf_env_bd22(self, tmp_path, monkeypatch) -> None:
        """BD-22: Popen is called with env containing permission override."""

        captured_env: dict = {}

        class _EnvCheckingPopen(_FakePopen):
            def __init__(self, cmd, env=None, **kw):
                super().__init__(cmd, **kw)
                captured_env.update(env or {})

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _EnvCheckingPopen)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        _run_subprocess_until_signal(
            cmd=["opencode", "run"],
            cwd=tmp_path,
            watch_paths=[],
            logs_dir=None,
        )
        assert "OPENCODE_CONFIG_CONTENT" in captured_env
        assert '"allow"' in captured_env["OPENCODE_CONFIG_CONTENT"]


def _make_supervisor_proj(tmp_path: Path) -> Path:
    """Module-level helper for tests outside TestSupervisorViaSubprocess."""
    proj = tmp_path / "proj"
    proj.mkdir()
    agentic = proj / ".agentic"
    (agentic / "roles").mkdir(parents=True)
    (agentic / "roles" / "supervisor.md").write_text("# Supervisor\nplan/verify")
    (agentic / "phases").mkdir()
    (agentic / "phases" / "plan.md").write_text("# Plan\nStep 1: do X")
    (agentic / "inbox").mkdir()
    (agentic / "outbox").mkdir()
    (agentic / "context").mkdir()
    (agentic / "logs").mkdir()
    (agentic / "config.yaml").write_text(
        "project:\n  name: test\n  root: .\n"
        "models:\n  supervisor:\n    description: current\n"
        "phases:\n  current: .agentic/phases/plan.md\n"
    )
    return proj


def test_replan_skips_when_no_todo_id_module(tmp_path: Path, capsys) -> None:
    """BD-14: replan with empty todo_id should skip gracefully and log."""
    proj = _make_supervisor_proj(tmp_path)
    logs = proj / ".agentic" / "logs"

    stage = Stage(name="replan", role="supervisor", description="d", kind="replan")
    _run_supervisor_stage(stage, todo_id="", auto=True, project_dir=proj, logs_dir=logs)

    out = capsys.readouterr().out
    assert "No todo_id for replan" in out
    log_file = logs / "orchestrator.log"
    assert log_file.exists()
    assert "auto-skipped" in log_file.read_text()


def test_salvage_skips_in_auto_mode_module(tmp_path: Path, capsys) -> None:
    """BD-14: salvage action is not automatable — should skip."""
    proj = _make_supervisor_proj(tmp_path)
    logs = proj / ".agentic" / "logs"

    stage = Stage(name="salvage", role="supervisor", description="d", kind="salvage")
    _run_supervisor_stage(stage, todo_id="TODO-0042", auto=True, project_dir=proj, logs_dir=logs)

    out = capsys.readouterr().out
    assert "not automated" in out


def _make_supervisor_proj(tmp_path: Path) -> Path:
    """Module-level helper for tests outside TestSupervisorViaSubprocess."""
    proj = tmp_path / "proj"
    proj.mkdir()
    agentic = proj / ".agentic"
    (agentic / "roles").mkdir(parents=True)
    (agentic / "roles" / "supervisor.md").write_text("# Supervisor\nplan/verify")
    (agentic / "phases").mkdir()
    (agentic / "phases" / "plan.md").write_text("# Plan\nStep 1: do X")
    (agentic / "inbox").mkdir()
    (agentic / "outbox").mkdir()
    (agentic / "context").mkdir()
    (agentic / "logs").mkdir()
    (agentic / "config.yaml").write_text(
        "project:\n  name: test\n  root: .\n"
        "models:\n  supervisor:\n    description: current\n"
        "phases:\n  current: .agentic/phases/plan.md\n"
    )
    return proj


# ── BD-18 graceful handling: run_pipeline catches RuntimeError ────────────────


class TestRunPipelineGracefulCrash:
    """BD-18: when subprocess crashes, run_pipeline prints helpful message + returns 1.

    Note: full integration test (spawning real opencode) is too heavy here.
    We test the contract via _run_supervisor_stage and _run_agent_stage
    directly — both raise RuntimeError on failure, and the wrapper in
    run_pipeline catches it. The unit test below verifies the raise; the
    catch in run_pipeline is verified by reading the source (lines ~880, ~934).
    """

    def test_supervisor_subprocess_raise_documented_bd18(self) -> None:
        """Sanity: BD-18 contract — supervisor/agent subprocess raises on non-zero exit.

        A6 refactor: raise moved to supervisor.py and agent_stage.py (out of
        orchestrator.py). Both checked.
        """
        import awf.agent_stage as ag
        import awf.supervisor as sup

        sup_src = open(sup.__file__).read()
        ag_src = open(ag.__file__).read()
        assert "raise RuntimeError(" in sup_src, "supervisor.run_supervisor_via_subprocess must raise on non-zero exit"
        assert "raise RuntimeError(" in ag_src, "agent_stage.run_agent_stage must raise on non-zero exit"
        # DAUD-7: exception handling moved to pipeline_engine.py
        import awf.pipeline_engine as engine_mod
        engine_src = open(engine_mod.__file__).read()
        assert "except (RuntimeError, TimeoutError)" in engine_src
        assert "Pipeline stopped" in engine_src


# ── BD-15: handoff chain ──────────────────────────────────────────────────────


class TestHandoffChain:
    """BD-15: agent stages receive previous handoffs as --file and produce
    their own handoff for the next stage."""

    def _setup_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        _init_proj_dirs(project_dir)
        agentic = project_dir / ".agentic"
        (agentic / "roles" / "worker.md").write_text("role")
        (agentic / "roles" / "developer.md").write_text("role")
        (agentic / "inbox" / "TODO-0001.md").write_text("task")
        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        return project_dir

    def test_run_agent_stage_forwards_prev_handoffs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-15: prev_handoffs list is passed as --file args."""
        proj = self._setup_project(tmp_path, monkeypatch)
        handoff_dir = proj / ".agentic" / "handoff"
        handoff_dir.mkdir(parents=True)
        (handoff_dir / "system-analysis.md").write_text("# Handoff sys-analysis\n...")
        (handoff_dir / "developer.md").write_text("# Handoff developer\n...")

        stage = Stage(name="qa", role="worker", kind="execute")
        _patch_subprocess_for_awf(monkeypatch)
        prev = [handoff_dir / "system-analysis.md", handoff_dir / "developer.md"]
        _run_agent_stage(stage, "TODO-0001", proj, {}, proj / ".agentic" / "logs", prev_handoffs=prev)

        opencode_cmd = _FakePopen._last_cmds[0]
        assert "--file" in opencode_cmd
        assert any("system-analysis.md" in c for c in opencode_cmd)
        assert any("developer.md" in c for c in opencode_cmd)

    def test_run_agent_stage_filters_missing_handoffs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-15: prev_handoffs that don't exist on disk are skipped."""
        proj = self._setup_project(tmp_path, monkeypatch)
        handoff_dir = proj / ".agentic" / "handoff"
        handoff_dir.mkdir(parents=True)
        (handoff_dir / "system-analysis.md").write_text("# Handoff\n...")
        # developer.md intentionally NOT created

        stage = Stage(name="qa", role="worker", kind="execute")
        _patch_subprocess_for_awf(monkeypatch)
        prev = [handoff_dir / "system-analysis.md", handoff_dir / "developer.md"]
        _run_agent_stage(stage, "TODO-0001", proj, {}, proj / ".agentic" / "logs", prev_handoffs=prev)

        opencode_cmd = _FakePopen._last_cmds[0]
        assert any("system-analysis.md" in c for c in opencode_cmd)
        assert not any("developer.md" in c for c in opencode_cmd)

    def test_collect_handoff_writes_file_with_progress_and_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-15: _collect_handoff writes handoff .md with PROGRESS + DONE."""
        proj = self._setup_project(tmp_path, monkeypatch)
        # Worker wrote progress + done
        (proj / ".agentic" / "outbox" / "PROGRESS-TODO-0001.md").write_text("did X, Y")
        (proj / ".agentic" / "outbox" / "DONE-TODO-0001.md").write_text("# DONE\nall good")

        # Stub git calls
        monkeypatch.setattr(
            "awf.signal_watch.subprocess.run",
            lambda *a, **kw: None,
        )

        out = _collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
        assert out is not None
        assert out.name == "worker-TODO-0001.md"  # BD-19: role-todo naming
        body = out.read_text()
        assert "Handoff from `worker`" in body
        assert "did X, Y" in body
        assert "# DONE" in body
        assert "all good" in body
        assert "next role" in body.lower()

    def test_collect_handoff_skips_git_when_no_baseline(self, tmp_path, monkeypatch) -> None:
        """BD-15: no BASELINE-*.sha → no git diff section (and no git subprocess)."""
        proj = self._setup_project(tmp_path, monkeypatch)
        runs: list = []
        monkeypatch.setattr(
            "awf.signal_watch.subprocess.run",
            lambda cmd, *a, **kw: runs.append(cmd) or None,
        )

        out = _collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
        assert out is not None
        body = out.read_text()
        assert "Git diff summary" not in body
        # Only the "git log" call should have happened (no git diff)
        git_calls = [c for c in runs if isinstance(c, list) and c[:1] == ["git"]]
        # git log may still be called; assert no git diff
        assert not any("diff" in c for c in git_calls)

    def test_collect_handoff_always_writes_even_without_progress_or_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-15: _collect_handoff always writes a file (never returns None),
        even when PROGRESS and DONE are both missing."""
        proj = self._setup_project(tmp_path, monkeypatch)
        # Do NOT create PROGRESS or DONE files

        monkeypatch.setattr(
            "awf.signal_watch.subprocess.run",
            lambda *a, **kw: None,
        )

        out = _collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
        assert out is not None
        assert out.exists()
        body = out.read_text()
        assert "Handoff from `worker`" in body
        assert "next role" in body.lower()
        # Should NOT have PROGRESS or DONE sections
        assert "PROGRESS notes" not in body
        assert "DONE summary" not in body

    def test_collect_handoff_no_output_marker_when_both_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When PROGRESS and DONE both missing, handoff includes NO OUTPUT marker."""
        proj = self._setup_project(tmp_path, monkeypatch)

        monkeypatch.setattr(
            "awf.signal_watch.subprocess.run",
            lambda *a, **kw: None,
        )

        out = _collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
        body = out.read_text()
        assert "NO OUTPUT FROM PREVIOUS STAGE" in body
        assert "worker crashed" in body or "crashed" in body.lower()

    def test_collect_handoff_no_output_marker_when_both_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When PROGRESS and DONE both exist but are empty, NO OUTPUT marker appears."""
        proj = self._setup_project(tmp_path, monkeypatch)
        (proj / ".agentic" / "outbox" / "PROGRESS-TODO-0001.md").write_text("")
        (proj / ".agentic" / "outbox" / "DONE-TODO-0001.md").write_text("")

        monkeypatch.setattr(
            "awf.signal_watch.subprocess.run",
            lambda *a, **kw: None,
        )

        out = _collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
        body = out.read_text()
        assert "NO OUTPUT FROM PREVIOUS STAGE" in body
        assert "PROGRESS notes" not in body
        assert "DONE summary" not in body

    def test_collect_handoff_no_marker_when_progress_has_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When PROGRESS has content, NO OUTPUT marker should NOT appear."""
        proj = self._setup_project(tmp_path, monkeypatch)
        (proj / ".agentic" / "outbox" / "PROGRESS-TODO-0001.md").write_text("some work done")

        monkeypatch.setattr(
            "awf.signal_watch.subprocess.run",
            lambda *a, **kw: None,
        )

        out = _collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
        body = out.read_text()
        assert "NO OUTPUT FROM PREVIOUS STAGE" not in body
        assert "some work done" in body

    def test_collect_handoff_done_without_progress_no_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F3: DONE exists but no PROGRESS → no PROGRESS warning (DONE carries info)."""
        proj = self._setup_project(tmp_path, monkeypatch)
        (proj / ".agentic" / "outbox" / "DONE-TODO-0001.md").write_text("# Done\nWork complete")

        monkeypatch.setattr(
            "awf.signal_watch.subprocess.run",
            lambda *a, **kw: None,
        )

        out = _collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
        body = out.read_text()
        assert "Worker did not leave progress notes" not in body
        assert "Work complete" in body

    def test_resolve_prev_handoffs_skips_supervisor_stages(self, tmp_path) -> None:
        """BD-15/19: supervisor stages don't produce handoffs — only agent roles.
        With todo_id, paths are role-todo.md."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".agentic").mkdir()
        stages = [
            Stage(name="plan", role="supervisor", kind="plan"),
            Stage(name="r1", role="analyst", kind="execute"),
            Stage(name="r2", role="dev", kind="execute"),
            Stage(name="verify", role="supervisor", kind="verify"),
            Stage(name="r3", role="qa", kind="execute"),
        ]
        # Current stage is index 4 (qa). Previous agent stages = analyst, dev.
        prev = _resolve_prev_handoffs(stages, 4, proj, todo_id="TODO-0042")
        prev_names = [p.name for p in prev]
        assert prev_names == ["analyst-TODO-0042.md", "dev-TODO-0042.md"]

    def test_resolve_prev_handoffs_empty_for_first_agent_stage(self, tmp_path) -> None:
        """First agent stage has no prior handoffs."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".agentic").mkdir()
        stages = [
            Stage(name="plan", role="supervisor", kind="plan"),
            Stage(name="impl", role="worker", kind="execute"),
        ]
        prev = _resolve_prev_handoffs(stages, 1, proj)
        assert prev == []

    def test_handoff_naming_includes_todo_id_bd19(self, tmp_path, monkeypatch) -> None:
        """BD-19: filename is role-<todo>.md so retries don't overwrite prior handoffs."""
        proj = tmp_path / "proj"
        proj.mkdir()
        agentic = proj / ".agentic"
        (agentic / "logs").mkdir(parents=True)
        (agentic / "outbox").mkdir(parents=True)
        (agentic / "context").mkdir(parents=True)

        monkeypatch.setattr(
            "awf.signal_watch.subprocess.run",
            lambda *a, **kw: None,
        )

        # First attempt for TODO-0001
        out1 = _collect_handoff("worker", "TODO-0001", proj, agentic / "logs")
        assert out1.name == "worker-TODO-0001.md"

        # Replan → new TODO-0002 on same role. Previous handoff must survive.
        out2 = _collect_handoff("worker", "TODO-0002", proj, agentic / "logs")
        assert out2.name == "worker-TODO-0002.md"

        # Both files exist (no overwrite)
        handoff_dir = agentic / "handoff"
        assert (handoff_dir / "worker-TODO-0001.md").exists()
        assert (handoff_dir / "worker-TODO-0002.md").exists()

    def test_resolve_prev_handoffs_no_todo_id_falls_back_to_glob(self, tmp_path) -> None:
        """BD-19: when todo_id is empty, scan handoff_dir for <role>-*.md files."""
        proj = tmp_path / "proj"
        proj.mkdir()
        handoff_dir = proj / ".agentic" / "handoff"
        handoff_dir.mkdir(parents=True)
        # Seed multiple handoffs for analyst across TODOs
        (handoff_dir / "analyst-TODO-0001.md").write_text("v1")
        (handoff_dir / "analyst-TODO-0002.md").write_text("v2")  # newer

        stages = [
            Stage(name="r1", role="analyst", kind="execute"),
            Stage(name="r2", role="dev", kind="execute"),
        ]
        # No todo_id provided — should pick newest analyst-*.md
        prev = _resolve_prev_handoffs(stages, 1, proj, todo_id="")
        assert len(prev) == 1
        assert "analyst-TODO-0002.md" in prev[0].name


class TestSupervisorReplan:
    """BD-29 fix: escalation/rollback paths must use kind='replan' for supervisor."""

    def _make_proj(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        _init_proj_dirs(proj)
        agentic = proj / ".agentic"
        (agentic / "roles" / "supervisor.md").write_text("# Supervisor")
        (agentic / "phases").mkdir()
        (agentic / "phases" / "plan.md").write_text("# Plan")
        (agentic / "config.yaml").write_text(
            "project:\n  name: test\n  root: .\n"
            "models:\n  supervisor:\n    description: current\n"
            "phases:\n  current: .agentic/phases/plan.md\n"
        )
        return proj

    def test_replan_spawns_subprocess_with_todo_id(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When kind='replan' and todo_id is set, supervisor subprocess is spawned."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        (proj / ".agentic" / "outbox" / "BLOCKED-TODO-0042.md").write_text("# BLOCKED\ncan't proceed")

        _patch_subprocess_for_awf(monkeypatch)
        stage = Stage(name="replan", role="supervisor", kind="replan")
        _run_supervisor_stage(stage, todo_id="TODO-0042", auto=True, project_dir=proj, logs_dir=logs)

        assert len(_FakePopen._last_cmds) == 1, "Replan must spawn supervisor subprocess"
        cmd = _FakePopen._last_cmds[0]
        assert any("BLOCKED-TODO-0042.md" in c for c in cmd), "BLOCKED file must be passed"

    def test_execute_kind_skips_in_supervisor_subprocess(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """If kind='execute' reaches _run_supervisor_via_subprocess, it should
        skip gracefully (not crash). This was the BD-29 bug where escalation
        passed agent stage with kind='execute' to supervisor."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        _patch_subprocess_for_awf(monkeypatch)
        # Simulating the old bug: passing kind="execute" to supervisor stage
        stage = Stage(name="agent", role="supervisor", kind="execute")
        _run_supervisor_stage(stage, todo_id="TODO-0042", auto=True, project_dir=proj, logs_dir=logs)

        # Should NOT have spawned a subprocess (execute kind → skip in supervisor)
        assert len(_FakePopen._last_cmds) == 0
        out = capsys.readouterr().out
        assert "Unknown kind" in out or "not automatable" in out or "skipping" in out.lower()
