"""AUD14-02/03/07, AUD04-06/10/11, AUD01-02: process, timeout, quoting, env.

Each test reproduces a real audit finding
(~/Desktop/awf-audit/reports/) and asserts the SAFE behavior. On the
current code the process-group / timeout / quoting / env tests are RED —
that is the point of the suite: the audited bug must be caught by the
test suite, not by a live incident.

Findings covered (TODO-0008 / FU-10):
- AUD14-03 / AUD04-06: timeout/kill must kill the WHOLE process group
  (grandchildren of a hung worker/verify command are orphans today)
- AUD14-02: shlex.split ValueError on bad test_cmd quoting = traceback
- AUD04-11: commit_gate git calls have no timeout (hung hook hangs verify)
- AUD01-02: git_utils has_diff/commit_all/is_git_repo have no timeout
- AUD14-07: preexec_fn does dlopen (CDLL) after fork in a threaded parent
- AUD04-10: AWF_SUPERVISOR_TIMEOUT env flag not restored on early returns

Findings covered (TODO-0009 / FU-11):
- AUD14-04: worker log name derived from cmd arg (config agent_name)
  without sanitization — "../agent-pwn" escapes logs/
- AUD14-05: pipeline_name (public arg) read outside .agentic/pipelines/
  via "../evil" — resolve_pipeline_file must validate the name
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

# ── process-group helpers ─────────────────────────────────────────────────

# Spawner that starts a grandchild `sleep` and records its pid. Stands in
# for a real worker (opencode): the grandchild is what survives a
# direct-child-only kill (MCP servers, xdist workers, node).
_GRANDCHILD = [sys.executable, "-c", "import time; time.sleep(60)"]

HANG_SPAWNER = f"""\
import subprocess, sys, time
grand = subprocess.Popen({_GRANDCHILD!r})
open(sys.argv[1], "w").write(str(grand.pid))
time.sleep(60)
"""

SIGNAL_HANG_SPAWNER = f"""\
import subprocess, sys, time
grand = subprocess.Popen({_GRANDCHILD!r})
open(sys.argv[1], "w").write(str(grand.pid))
open(sys.argv[2], "w").write("done")
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.2)
    return not _alive(pid)


LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="process groups are Linux-only")


# ── AUD14-03 / AUD04-06: timeout must kill the whole process group ───────


@LINUX_ONLY
class TestTimeoutKillsProcessGroup:
    def test_verify_timeout_kills_grandchildren(self, tmp_path, monkeypatch):
        """Hung test_cmd: grandchildren must not survive as orphans."""
        from awf import verify

        spawner = tmp_path / "spawner.py"
        spawner.write_text(HANG_SPAWNER)
        pid_file = tmp_path / "grand.pid"
        monkeypatch.setenv("AWF_VERIFY_TIMEOUT", "2")
        cfg = {"verification": {"test_cmd": f"{sys.executable} {spawner} {pid_file}"}}

        t0 = time.monotonic()
        result = verify.run_verify_commands(cfg, project_dir=tmp_path)
        elapsed = time.monotonic() - t0

        assert result is False
        assert elapsed < 30, f"verify did not return in bounded time: {elapsed:.0f}s"
        grand = int(pid_file.read_text())
        assert _wait_dead(grand), "grandchild survived the verify timeout kill"

    def test_run_tree_helper_kills_grandchildren(self, tmp_path):
        """awf._proc.run_tree: TimeoutExpired after the whole group is dead."""
        from awf import _proc

        spawner = tmp_path / "spawner.py"
        spawner.write_text(HANG_SPAWNER)
        pid_file = tmp_path / "grand.pid"

        with pytest.raises(subprocess.TimeoutExpired):
            _proc.run_tree(
                [sys.executable, str(spawner), str(pid_file)],
                timeout=2,
                capture_output=True,
            )
        grand = int(pid_file.read_text())
        assert _wait_dead(grand), "grandchild survived the run_tree timeout kill"

    def test_kill_process_tree_never_targets_pid_1(self, monkeypatch):
        """Fake Popen pid=1 must NOT be killed via killpg(1, …).

        2026-09-20 incident: getpgid(1) == 1 (init leads its own group),
        the pgid == pid guard passed, and os.killpg(1, sig) is literally
        kill(-1, sig) — SIGTERM/SIGKILL to every signalable process of the
        uid (the whole desktop session died, 3/3 full pytest runs). The
        pid > 1 guard must route pid-1 fakes to proc.kill() instead.
        """
        from awf import _proc

        calls = []

        class _FakeInitPopen:
            pid = 1

            def kill(self):
                calls.append(("proc.kill",))

            def wait(self, timeout=None):
                calls.append(("wait", timeout))
                return 0

        monkeypatch.setattr(_proc.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(_proc.os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))

        _proc.kill_process_tree(_FakeInitPopen(), grace=0.1)

        assert ("killpg", 1, _proc.signal.SIGTERM) not in calls
        assert ("killpg", 1, _proc.signal.SIGKILL) not in calls
        assert ("proc.kill",) in calls, "pid=1 must fall back to proc.kill(), not killpg"

    def test_signal_watch_hard_timeout_kills_grandchildren(self, tmp_path):
        """AUD04-06: worker hangs, no signal — group must die with it."""
        from awf.signal_watch import run_subprocess_until_signal

        spawner = tmp_path / "spawner.py"
        spawner.write_text(HANG_SPAWNER)
        pid_file = tmp_path / "grand.pid"

        with pytest.raises(TimeoutError):
            run_subprocess_until_signal(
                cmd=[sys.executable, str(spawner), str(pid_file)],
                cwd=tmp_path,
                watch_paths=[tmp_path / "NOPE.ready"],
                hard_timeout=2,
            )
        grand = int(pid_file.read_text())
        assert _wait_dead(grand), "worker grandchild survived the hard-timeout kill"

    def test_signal_seen_then_hang_consumes_signal(self, tmp_path):
        """AUD04-06: signal on disk + hung process → stage counts as done.

        The work is logically finished (signal file exists); killing the
        hung tree must not also lose the run via TimeoutError.
        """
        from awf.signal_watch import run_subprocess_until_signal

        spawner = tmp_path / "spawner.py"
        spawner.write_text(SIGNAL_HANG_SPAWNER)
        pid_file = tmp_path / "grand.pid"
        signal_file = tmp_path / "DONE-TODO-0001.ready"

        result = run_subprocess_until_signal(
            cmd=[sys.executable, str(spawner), str(pid_file), str(signal_file)],
            cwd=tmp_path,
            watch_paths=[signal_file],
            hard_timeout=2,
        )
        assert result.returncode == 0, "signal was detected — stage must be treated as done"
        grand = int(pid_file.read_text())
        assert _wait_dead(grand), "hung worker tree survived after signal consumption"


# ── AUD14-02: bad quoting in test_cmd must not traceback ──────────────────


class TestBadQuoting:
    def test_verify_bad_quoting_returns_false_and_logs(self, tmp_path):
        from awf import verify

        (tmp_path / ".agentic" / "outbox").mkdir(parents=True)
        cfg = {"verification": {"test_cmd": 'pytest -k "x and (y'}}
        result = verify.run_verify_commands(cfg, project_dir=tmp_path, todo_id="TODO-X")
        assert result is False
        log = (tmp_path / ".agentic" / "outbox" / "TEST-RESULTS-TODO-X.log").read_text()
        assert "bad quoting" in log

    def test_baseline_bad_quoting_raises_api_error(self, tmp_git_repo):
        from awf import api
        from awf.api._errors import AwfApiError

        res = api.init_project(tmp_git_repo, project_name="Quoting")
        proj = Path(res.project_dir)
        cfg_path = proj / ".agentic" / "config.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        data.setdefault("verification", {})["test_cmd"] = 'pytest -k "x and (y'
        cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

        with pytest.raises(AwfApiError) as ei:
            api.create_baseline(proj, "TODO-0001")
        assert "не парсится" in str(ei.value)


# ── AUD04-11: commit_gate git calls must have timeouts ────────────────────


class TestCommitGateTimeout:
    def test_commit_specific_files_timeout_returns_false(self, tmp_git_repo, monkeypatch):
        """TimeoutExpired inside _commit_specific_files → False, not a hang."""
        from awf import commit_gate

        def _raise(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="git commit", timeout=30)

        monkeypatch.setattr(commit_gate.subprocess, "run", _raise)
        assert commit_gate._commit_specific_files(tmp_git_repo, ["README.md"], "m") is False

    def test_commit_gate_git_calls_carry_timeout(self, tmp_git_repo, monkeypatch):
        from awf import commit_gate, git_utils

        real_run = subprocess.run
        seen: list[tuple[list[str], object]] = []

        def _recording(cmd, *a, **k):
            names = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
            seen.append((names, k.get("timeout", "MISSING")))
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(commit_gate.subprocess, "run", _recording)
        sha = git_utils.current_sha(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        changed = commit_gate._files_changed_since_baseline(tmp_git_repo, sha, todo_id="TODO-0001")
        assert "README.md" in changed
        assert commit_gate._commit_specific_files(tmp_git_repo, changed, "t") is True

        assert seen, "no git calls recorded"
        bad = [n for n, t in seen if t in (None, "MISSING")]
        assert not bad, f"git calls without timeout: {bad}"


# ── AUD01-02: git_utils raw calls must have timeouts, fail closed ─────────


class TestGitUtilsTimeout:
    def test_has_diff_timeout_returns_false(self, tmp_git_repo, monkeypatch):
        from awf import git_utils

        def _raise(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="git diff", timeout=30)

        monkeypatch.setattr(git_utils.subprocess, "run", _raise)
        assert git_utils.has_diff(tmp_git_repo, "abc") is False

    def test_is_git_repo_timeout_returns_false(self, tmp_git_repo, monkeypatch):
        from awf import git_utils

        def _raise(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="git rev-parse", timeout=30)

        monkeypatch.setattr(git_utils.subprocess, "run", _raise)
        assert git_utils.is_git_repo(tmp_git_repo) is False

    def test_commit_all_timeout_returns_false(self, tmp_git_repo, monkeypatch):
        from awf import git_utils

        def _raise(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="git commit", timeout=30)

        monkeypatch.setattr(git_utils.subprocess, "run", _raise)
        assert git_utils.commit_all(tmp_git_repo, "m") is False

    def test_git_utils_calls_carry_timeout(self, tmp_git_repo, monkeypatch):
        import awf.git_utils as gu

        real_run = subprocess.run
        seen: list[tuple[list[str], object]] = []

        def _recording(cmd, *a, **k):
            names = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
            seen.append((names, k.get("timeout", "MISSING")))
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(gu.subprocess, "run", _recording)
        sha = gu.current_sha(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        assert gu.has_diff(tmp_git_repo, sha) is True
        assert gu.is_git_repo(tmp_git_repo) is True
        assert gu.commit_all(tmp_git_repo, "test") is True

        assert seen, "no git calls recorded"
        bad = [n for n, t in seen if t in (None, "MISSING")]
        assert not bad, f"git calls without timeout: {bad}"


# ── AUD14-07: preexec_fn must not dlopen after fork ───────────────────────


@LINUX_ONLY
class TestPreexecDlopen:
    def test_preexec_does_not_load_shared_libs(self, monkeypatch):
        """CDLL (dlopen) must happen at import, not inside preexec_fn."""
        from awf import _env

        calls: list[str] = []
        real_cdll = ctypes.CDLL

        def _recording(name, *a, **k):
            calls.append(name)
            return real_cdll(name, *a, **k)

        monkeypatch.setattr(ctypes, "CDLL", _recording)
        _env._pdeathsig_preexec()
        assert calls == [], f"preexec_fn loaded shared libs: {calls}"

    def test_libc_handle_loaded_at_module_level(self):
        """The libc handle must exist right after import (parent process)."""
        from awf import _env

        assert getattr(_env, "_libc", None) is not None, (
            "awf._env must load libc at module level, not inside preexec_fn"
        )


# ── AUD04-10: env flag must be restored on every exit path ────────────────


def _pipeline_args(project_dir: Path, **overrides) -> SimpleNamespace:
    base = dict(
        project_dir=str(project_dir),
        pipeline=None,
        from_stage=None,
        auto=False,
        timeout=1234,
        todo_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _proj_with_pipeline(tmp_path: Path) -> Path:
    """Minimal project with one agent stage — enough to reach the stage loop.

    The AWF_SUPERVISOR_TIMEOUT flag is set right before the loop, so a
    meaningful AUD04-10 repro needs a project that gets PAST pipeline-file
    resolution and fails inside the loop (stage rc != 0).
    """
    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "context", "logs", "state", "roles"):
        (proj / ".agentic" / d).mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text("default_pipeline: default\n")
    (proj / ".agentic" / "roles" / "worker.md").write_text("# Worker\n")
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
        'name: "default"\n'
        'stages:\n'
        '  - name: "worker"\n    role: "worker"\n    kind: "execute"\n',
    )
    return proj


class TestEnvRestoreOnEarlyReturn:
    """AUD04-10: env flag set before the stage loop must be restored on
    the early ``return rc`` inside the loop (in-process callers like MCP
    continue_pipeline would otherwise inherit a polluted os.environ)."""

    def _run_with_failing_stage(self, tmp_path, monkeypatch) -> int:
        import awf.orchestrator
        from awf.orchestrator import run_pipeline

        proj = _proj_with_pipeline(tmp_path)
        # Stage fails → orchestrator does `return rc` (early exit AFTER the
        # env flag was set). No real worker subprocess is spawned.
        monkeypatch.setattr(
            awf.orchestrator, "execute_agent_stage",
            lambda *a, **k: ("TODO-X", 0, 1),
        )
        return run_pipeline(_pipeline_args(proj))

    def test_timeout_env_not_leaked(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AWF_SUPERVISOR_TIMEOUT", raising=False)

        rc = self._run_with_failing_stage(tmp_path, monkeypatch)
        assert rc == 1
        assert "AWF_SUPERVISOR_TIMEOUT" not in os.environ

    def test_timeout_env_restored_to_previous_value(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AWF_SUPERVISOR_TIMEOUT", "999")

        rc = self._run_with_failing_stage(tmp_path, monkeypatch)
        assert rc == 1
        assert os.environ.get("AWF_SUPERVISOR_TIMEOUT") == "999"


# ── AUD14-04: worker log name must be sanitized (config-derived) ─────────


class TestWorkerLogNameSanitized:
    """AUD14-04: ``models.<role>.agent_name`` from config.yaml reaches
    run_subprocess_until_signal as a cmd arg (``--agent <name>``). The log
    name is derived from that arg — an unsanitized "../agent-pwn" used to
    create ``logs/../agent-pwn.out``, one level above logs/."""

    def test_traversal_agent_name_stays_in_logs_dir(self, tmp_path):
        from awf.signal_watch import run_subprocess_until_signal

        logs_dir = tmp_path / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        cmd = [sys.executable, "-c", "print('worker')", "--agent", "../agent-pwn"]

        run_subprocess_until_signal(cmd=cmd, cwd=tmp_path, logs_dir=logs_dir)

        # no file escaped above logs/
        assert not (logs_dir.parent / "agent-pwn.out").exists()
        # a log WAS created (worker output must not be lost), strictly inside
        logs = sorted(logs_dir.glob("*.out"))
        assert logs, "worker log missing — output must not be dropped"
        for p in logs:
            assert p.resolve().is_relative_to(logs_dir.resolve())
            assert not p.name.startswith("..")
        # the run marker is in one of the created logs
        contents = "\n".join(p.read_text(encoding="utf-8") for p in logs)
        assert "===== awf run" in contents

    def test_plain_agent_name_unchanged(self, tmp_path):
        """Sanitization must not rename a well-formed slug."""
        from awf.signal_watch import run_subprocess_until_signal

        logs_dir = tmp_path / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        cmd = [sys.executable, "-c", "print('worker')", "--agent", "agent-implementer"]

        run_subprocess_until_signal(cmd=cmd, cwd=tmp_path, logs_dir=logs_dir)

        assert (logs_dir / "agent-implementer.out").is_file()


# ── AUD14-05: pipeline name must not escape .agentic/pipelines/ ──────────


class TestPipelineNameValidation:
    """AUD14-05: ``pipeline`` is a public arg (awf_start / awf_continue /
    CLI --pipeline). resolve_pipeline_file used to do
    ``pipelines_dir / f"{name}.yaml"`` verbatim, so ``"../../evil"`` loaded
    a YAML from outside the project."""

    def _project(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        (proj / ".agentic" / "pipelines").mkdir(parents=True)
        (proj / ".agentic" / "config.yaml").write_text("default_pipeline: default\n")
        (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
            "name: default\nstages:\n  - name: plan\n    role: supervisor\n"
        )
        return proj

    def test_dotdot_name_rejected(self, tmp_path):
        from awf.api._errors import AwfApiError
        from awf.pipeline import resolve_pipeline_file

        proj = self._project(tmp_path)
        # the file a traversal WOULD have reached — must never be loadable
        (tmp_path / "evil.yaml").write_text("name: evil\nstages: []\n")
        with pytest.raises(AwfApiError):
            resolve_pipeline_file(proj, "../evil")
        with pytest.raises(AwfApiError):
            resolve_pipeline_file(proj, "..")

    def test_slash_name_rejected(self, tmp_path):
        from awf.api._errors import AwfApiError
        from awf.pipeline import resolve_pipeline_file

        proj = self._project(tmp_path)
        with pytest.raises(AwfApiError):
            resolve_pipeline_file(proj, "a/b")

    def test_valid_names_still_resolve(self, tmp_path):
        from awf.pipeline import resolve_pipeline_file

        proj = self._project(tmp_path)
        (proj / ".agentic" / "pipelines" / "custom-name.yaml").write_text(
            "name: custom-name\nstages: []\n"
        )
        assert resolve_pipeline_file(proj, "default").name == "default.yaml"
        assert resolve_pipeline_file(proj, "custom-name").name == "custom-name.yaml"
        # AUD14-05 acceptance: a resolved pipeline file is always inside
        # .agentic/pipelines/ — pin the invariant directly.
        pipelines = (proj / ".agentic" / "pipelines").resolve()
        for name in ("default", "custom-name"):
            assert resolve_pipeline_file(proj, name).resolve().is_relative_to(pipelines)

    def test_start_with_traversal_name_fails_cleanly(self, tmp_path, monkeypatch):
        """Public entry (run_pipeline) must fail cleanly (rc 1), not crash.

        Pre-fix behavior: "../evil" loaded the external evil.yaml and the
        pipeline RAN it (rc 0 with the stage neutralized below) — the
        assertion is that a foreign pipeline must never be executed.
        """
        import awf.orchestrator
        from awf.orchestrator import run_pipeline

        proj = self._project(tmp_path)
        (tmp_path / "evil.yaml").write_text(
            "name: evil\nstages:\n  - name: plan\n    role: supervisor\n"
        )
        # Neutralize the supervisor stage: if the foreign pipeline is loaded,
        # the run "completes" (rc 0) instead of failing.
        monkeypatch.setattr(
            awf.orchestrator, "execute_supervisor_stage",
            lambda *a, **k: ("TODO-X", 1, 0),
        )
        rc = run_pipeline(_pipeline_args(proj, pipeline="../evil"))
        assert rc == 1
