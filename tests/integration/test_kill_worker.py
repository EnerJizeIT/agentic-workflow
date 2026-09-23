"""RUN8 #2 (TODO-0064): awf_kill kills the stage's worker.

Incident (topic-trainer, awf 1.2.0 fae1421): awf_kill killed the pipeline,
but the opencode worker — its own process group, so the pipeline-only
SIGTERM/SIGKILL missed it and PR_SET_PDEATHSIG only reaches the DIRECT
child, not its children (the opencode server) — kept working ~2 minutes
after the stop and wrote DONE-TODO-0037.* + 7 docs that landed in the
NEXT unit's commit (TODO-0034).

Now: kill_pipeline collects the worker BEFORE the pipeline dies (state
worker_pid + /proc direct children, ppid re-check as PID-reuse defense),
kills the worker's process group (TERM → grace → KILL, pid > 1 guard),
names every pid in the answer, records last-kill.json — and the next
start/continue warns when a recorded worker is still alive.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from awf import _proc
from awf.api import _liveness
from awf.api.pipeline import (
    LAST_KILL_FILE,
    kill_pipeline,
    orphan_worker_warning,
)
from awf.pipeline_state import write_state

AWF_ARGV = "python\x00-m\x00awf\x00start\x00--project-dir\x00/x\x00"
OPENCODE_ARGV = "opencode\x00run\x00--auto\x00--agent\x00worker\x00"
NGINX_ARGV = "nginx\x00worker\x00"


@pytest.fixture
def project(tmp_git_repo):
    return tmp_git_repo


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _start_pipeline_with_worker():
    """A real 'pipeline' process with a real worker child.

    Topology mirrors production: the worker is a direct child of the
    pipeline, started in its own session (its own process group, pgid ==
    worker pid) — exactly like opencode in run_subprocess_until_signal.
    Returns (pipeline_proc, worker_pid).
    """
    code = (
        "import subprocess, sys, time\n"
        "worker = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(300)'],\n"
        "    start_new_session=True,\n"
        ")\n"
        "print(worker.pid, flush=True)\n"
        "time.sleep(300)\n"
    )
    pipe = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        wpid = int(pipe.stdout.readline())
    except ValueError:
        pipe.kill()
        pipe.wait()
        raise
    return pipe, wpid


def _cleanup(pipe, wpid: int) -> None:
    if pipe.poll() is None:
        pipe.kill()
        pipe.wait()
    if pipe.stdout is not None:
        try:
            pipe.stdout.close()
        except OSError:
            pass
    if _alive(wpid):
        try:
            os.kill(wpid, 9)
        except OSError:
            pass


def _write_last_kill(project, workers: dict) -> None:
    state_dir = project / ".agentic" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / LAST_KILL_FILE).write_text(
        json.dumps(
            {"at": "2026-09-23T00:00:00Z", "pipeline_pid": 4242, "workers": workers}
        ),
        encoding="utf-8",
    )


class TestKillKillsWorker:
    """Part A: the kill reaches the worker (both discovery sources)."""

    def test_kill_pipeline_kills_recorded_worker(self, project, monkeypatch):
        """state worker_pid — the worker's group dies with the pipeline."""
        pipe, wpid = _start_pipeline_with_worker()
        try:
            write_state(
                project, pipeline_pid=pipe.pid, worker_pid=wpid,
                worker_role="agent-implementer", worker_todo="TODO-0064",
            )
            monkeypatch.setattr(_liveness, "read_cmdline", lambda pid: AWF_ARGV)

            res = kill_pipeline(project)

            assert res["killed"] is True
            assert res["pid"] == pipe.pid
            assert res["workers"] == {str(wpid): "killed"}
            assert f"PID {wpid} killed" in res["message"]

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and (
                pipe.poll() is None or _alive(wpid)
            ):
                time.sleep(0.2)
            assert pipe.poll() is not None, "pipeline survived"
            assert not _alive(wpid), "worker survived kill_pipeline (the incident)"
        finally:
            _cleanup(pipe, wpid)

    def test_kill_pipeline_finds_worker_via_child_scan(self, project, monkeypatch):
        """No state record (older awf / failed write) — the /proc direct-
        children scan still finds the worker."""
        pipe, wpid = _start_pipeline_with_worker()
        try:
            write_state(project, pipeline_pid=pipe.pid)
            monkeypatch.setattr(_liveness, "read_cmdline", lambda pid: AWF_ARGV)

            res = kill_pipeline(project)

            assert res["killed"] is True
            assert res["workers"] == {str(wpid): "killed"}

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and _alive(wpid):
                time.sleep(0.2)
            assert not _alive(wpid), "worker survived (child-scan path)"
        finally:
            _cleanup(pipe, wpid)


class TestSurvivorWarning:
    """Part A.3: a worker that outlives TERM + KILL is named — no silence."""

    def test_surviving_worker_is_named(self, project, monkeypatch):
        pipe, wpid = _start_pipeline_with_worker()
        try:
            write_state(project, pipeline_pid=pipe.pid, worker_pid=wpid)
            monkeypatch.setattr(_liveness, "read_cmdline", lambda pid: AWF_ARGV)
            monkeypatch.setattr(
                "awf.api.pipeline.kill_pid_tree", lambda pid, **kw: "survived"
            )

            res = kill_pipeline(project)

            assert res["workers"] == {str(wpid): "survived"}
            assert f"worker PID {wpid} still alive" in res["message"]
            assert "stop it manually" in res["message"]
        finally:
            _cleanup(pipe, wpid)

    def test_unverified_recorded_worker_is_not_signaled(self, project, monkeypatch):
        """The recorded pid is alive but NOT a child of the pipeline
        (reparented after the pipeline died / recycled number) — never
        signaled, only tracked and warned."""
        orphan = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # resolve returns a pid that is dead by the time we signal it
            monkeypatch.setattr(
                _liveness, "resolve", lambda d: (True, 2**31, "state")
            )
            write_state(project, worker_pid=orphan.pid)

            signaled: list[int] = []
            real_kill = os.kill

            def spy_kill(pid, sig, *a, **kw):
                if pid == orphan.pid and sig != 0:
                    # a REAL signal to the orphan = the bug; signal 0 is a
                    # liveness probe and must pass
                    signaled.append(pid)
                    raise ProcessLookupError
                return real_kill(pid, sig, *a, **kw)

            monkeypatch.setattr(os, "kill", spy_kill)

            res = kill_pipeline(project)

            assert res["killed"] is False
            assert res["workers"] == {str(orphan.pid): "unverified"}
            assert f"PID {orphan.pid} unverified" in res["message"]
            assert "possible orphan" in res["message"]
            assert signaled == [], "an unverified pid must never be signaled"
        finally:
            orphan.kill()
            orphan.wait()


class TestPidOneGuard:
    """Part A/B hard rule: pid 1 is never signaled (killpg(1) == kill(-1))."""

    def test_kill_pid_tree_skips_pid_1(self, monkeypatch):
        calls: list[tuple] = []

        def _boom(target, sig, *a, **kw):
            calls.append((target, sig))
            raise AssertionError(f"signaled pid/pgid {target}")

        monkeypatch.setattr(os, "kill", _boom)
        monkeypatch.setattr(os, "killpg", _boom)

        assert _proc.kill_pid_tree(1) == "skipped"
        assert _proc.kill_pid_tree(0) == "skipped"
        assert _proc.kill_pid_tree(-5) == "skipped"
        assert calls == []

    def test_kill_pipeline_with_worker_pid_1(self, project, monkeypatch):
        """A corrupt state pointing worker_pid at 1 signals nothing broad."""
        pipe = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            write_state(project, pipeline_pid=pipe.pid, worker_pid=1)
            monkeypatch.setattr(_liveness, "read_cmdline", lambda pid: AWF_ARGV)

            res = kill_pipeline(project)

            assert res["killed"] is True
            assert res["workers"] == {}
            assert "Worker: not found." in res["message"]
        finally:
            pipe.kill()
            pipe.wait()


class TestKillPidTree:
    """kill_pid_tree on real processes — the kill semantics themselves."""

    def test_kills_own_session_tree(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _proc.kill_pid_tree(proc.pid) == "killed"
            assert proc.poll() is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_non_group_leader_never_uses_killpg(self, monkeypatch):
        """An inherited group belongs to someone else — only the pid is
        signaled, the group is never (the AUD04-06 rule, pid-based form)."""
        killpg_calls: list[tuple] = []
        monkeypatch.setattr(os, "killpg", lambda *a: killpg_calls.append(a))

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _proc.kill_pid_tree(proc.pid) == "killed"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        assert killpg_calls == [], f"killpg on an inherited group: {killpg_calls}"
        assert not _alive(proc.pid)

    def test_dead_pid(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        assert _proc.kill_pid_tree(proc.pid) == "dead"

    def test_child_pids_and_ppid(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _proc.ppid_of(proc.pid) == os.getpid()
            assert proc.pid in _proc.child_pids(os.getpid())
        finally:
            proc.kill()
            proc.wait()


class TestOrphanWarningAtStart:
    """Part B: the next launch warns when a killed worker is still alive."""

    def test_live_orphan_warned(self, project, monkeypatch):
        orphan = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _write_last_kill(project, {str(orphan.pid): "survived"})
            monkeypatch.setattr(
                _liveness, "read_cmdline", lambda pid: OPENCODE_ARGV
            )

            warn = orphan_worker_warning(project)

            assert str(orphan.pid) in warn
            assert "orphaned" in warn
        finally:
            orphan.kill()
            orphan.wait()

    def test_dead_worker_no_warning(self, project):
        orphan = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        orphan.kill()
        orphan.wait()
        _write_last_kill(project, {str(orphan.pid): "killed"})
        assert orphan_worker_warning(project) == ""

    def test_no_record_no_warning(self, project):
        assert orphan_worker_warning(project) == ""

    def test_recycled_pid_no_false_alarm(self, project, monkeypatch):
        """A live pid whose cmdline is not opencode — the number was
        recycled for a foreign process. No alarm."""
        orphan = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _write_last_kill(project, {str(orphan.pid): "survived"})
            monkeypatch.setattr(
                _liveness, "read_cmdline", lambda pid: NGINX_ARGV
            )
            assert orphan_worker_warning(project) == ""
        finally:
            orphan.kill()
            orphan.wait()

    def test_start_message_carries_warning(self, project, monkeypatch):
        """awf_start's answer names the orphan (launch mocked out)."""
        import awf.api.pipeline as ap

        orphan = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            (project / ".agentic").mkdir(exist_ok=True)
            inbox = project / ".agentic" / "inbox"
            inbox.mkdir(parents=True)
            (inbox / "TODO-0064.md").write_text(
                "# TODO-0064\n\nKill the worker with the pipeline.\n",
                encoding="utf-8",
            )
            (inbox / "TODO-0064.ready").touch()
            _write_last_kill(project, {str(orphan.pid): "survived"})
            monkeypatch.setattr(
                _liveness, "read_cmdline", lambda pid: OPENCODE_ARGV
            )
            logs = project / ".agentic" / "logs"
            logs.mkdir(parents=True)
            monkeypatch.setattr(
                ap, "start_in_background",
                lambda *a, **kw: (
                    99999, logs / "awf-start.out", logs / "awf-start.pid",
                ),
            )
            monkeypatch.setattr(ap, "_verify_child_alive", lambda *a, **kw: True)

            res = ap.start_pipeline(project, background=True)

            assert res.run_mode == "background"
            assert str(orphan.pid) in res.message
            assert "orphaned" in res.message
        finally:
            orphan.kill()
            orphan.wait()
