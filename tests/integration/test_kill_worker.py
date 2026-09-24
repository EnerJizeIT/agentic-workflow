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
from pathlib import Path

import pytest

from awf import _proc
from awf.api import _liveness
from awf.api.pipeline import (
    LAST_KILL_FILE,
    kill_pipeline,
    orphan_worker_warning,
)
from awf.pipeline_state import read_state, write_state

AWF_ARGV = "python\x00-m\x00awf\x00start\x00--project-dir\x00/x\x00"
OPENCODE_ARGV = "opencode\x00run\x00--auto\x00--agent\x00worker\x00"
NGINX_ARGV = "nginx\x00worker\x00"
STUBS_DIR = Path(__file__).resolve().parents[2] / "tests" / "stubs"


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


class TestKillSelfPipelineRefusal:
    """RUN10 #5 Part A (TODO-0075): kill_pipeline refuses when the CALLER
    is inside the pipeline it is trying to kill.

    Dogfood incident: worker 0065 called the kill from inside a live
    project and took down its own pipeline and itself. The supervisor is
    never an ancestor of the pipeline, so a legitimate stop is never
    blocked; a caller inside it is, by definition, part of what is being
    stopped.
    """

    def test_refusal_from_inside_pipeline(self, project):
        """A child of the fake pipeline calls kill_pipeline → refusal;
        the pipeline and the caller survive, the state is untouched."""
        child_code = (
            "import json, sys, time\n"
            "from awf.api import _liveness\n"
            "_liveness.read_cmdline = lambda pid: chr(0).join(\n"
            "    ['python', '-m', 'awf', 'start', '--project-dir', '/x'])\n"
            "from awf.api.pipeline import kill_pipeline\n"
            "time.sleep(3)  # the parent writes state (worker_pid = us) first\n"
            "res = kill_pipeline(sys.argv[1])\n"
            "print(json.dumps(res), flush=True)\n"
            "time.sleep(30)\n"
        )
        pipe_code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', sys.argv[2], sys.argv[1]],\n"
            "    stdout=subprocess.PIPE,\n"
            "    stderr=subprocess.DEVNULL,\n"
            ")\n"
            "print('CHILD', child.pid, flush=True)\n"
            "line = child.stdout.readline()\n"
            "print('RESULT', line.decode(), flush=True)\n"
            "time.sleep(300)\n"
        )
        pipe = subprocess.Popen(
            [sys.executable, "-c", pipe_code, str(project), child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        child_pid = None
        try:
            try:
                line = pipe.stdout.readline().decode().split()
                child_pid = int(line[1])
            except (ValueError, IndexError):
                pipe.kill()
                pipe.wait()
                raise
            write_state(project, pipeline_pid=pipe.pid, worker_pid=child_pid)
            line = pipe.stdout.readline().decode().split(None, 1)
            assert line[0] == "RESULT"
            res = json.loads(line[1].strip())

            assert res["killed"] is False
            assert res.get("reason") == "ancestry"
            assert res["pid"] == pipe.pid
            assert "refusing" in res["message"]
            assert "supervisor session" in res["message"]

            assert pipe.poll() is None, "the pipeline died despite the refusal"
            assert _alive(child_pid), "the caller died with the pipeline"
            state = read_state(project)
            assert state is not None
            assert int(state["pipeline_pid"]) == pipe.pid
        finally:
            if pipe.poll() is None:
                pipe.kill()
                pipe.wait()
            if pipe.stdout is not None:
                try:
                    pipe.stdout.close()
                except OSError:
                    pass
            if child_pid is not None and _alive(child_pid):
                try:
                    os.kill(child_pid, 9)
                except OSError:
                    pass

    def test_kill_from_outside_pipeline_still_works(self, project, monkeypatch):
        """The refusal must not over-block: a caller that is NOT an
        ancestor of the pipeline (the supervisor's case — here the pytest
        process, the pipeline's parent) still kills."""
        pipe, wpid = _start_pipeline_with_worker()
        try:
            write_state(project, pipeline_pid=pipe.pid, worker_pid=wpid)
            monkeypatch.setattr(_liveness, "read_cmdline", lambda pid: AWF_ARGV)

            res = kill_pipeline(project)

            assert res["killed"] is True
            assert res.get("reason") is None
            assert res["workers"] == {str(wpid): "killed"}
        finally:
            _cleanup(pipe, wpid)

    def test_ancestry_degrades_without_proc(self, project, monkeypatch):
        """/proc unreadable → the kill still proceeds; the answer carries
        the 'ancestry check unavailable' note (degradation, not a block)."""
        pipe, wpid = _start_pipeline_with_worker()
        try:
            write_state(project, pipeline_pid=pipe.pid, worker_pid=wpid)
            monkeypatch.setattr(_liveness, "read_cmdline", lambda pid: AWF_ARGV)
            monkeypatch.setattr(_proc, "ppid_of", lambda pid: None)

            res = kill_pipeline(project)

            assert res["killed"] is True
            assert "ancestry check unavailable" in res["message"]
            assert res.get("reason") is None
        finally:
            _cleanup(pipe, wpid)


class TestLateWorkerSweep:
    """RUN10 #5 Part B (TODO-0075): a worker spawned during the kill
    window is caught by the second sweep.

    Incident: the kill landed in a network pause; the worker spawned a
    second before the kill outlived it and the supervisor stopped it by
    hand. Now: after the pipeline dies, kill_pipeline re-reads state and
    /proc, terminates the late worker, names it in the answer and in
    last-kill.json.
    """

    def test_late_worker_spawned_during_kill_is_terminated(self, project, monkeypatch):
        env = os.environ.copy()
        env["PATH"] = f"{STUBS_DIR}:{env.get('PATH', '')}"
        env["AWF_TEST_OPENCODE_BEHAVIOR"] = "sleep"
        env["AWF_TEST_OPENCODE_SLEEP_SECONDS"] = "300"
        pipe_code = (
            "import signal, subprocess, sys, time\n"
            "from awf.pipeline_state import write_state\n"
            "project = sys.argv[1]\n"
            "old = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(300)'],\n"
            "    start_new_session=True,\n"
            "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            ")\n"
            "write_state(project, worker_pid=old.pid,\n"
            "             worker_role='worker', worker_todo='TODO-0075')\n"
            "print('READY', old.pid, flush=True)\n"
            "def on_term(signum, frame):\n"
            "    late = subprocess.Popen(\n"
            "        ['opencode', 'run', '--auto', '--agent', 'worker', '--', 'TODO-0075'],\n"
            "        start_new_session=True,\n"
            "        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            "        cwd=project,\n"
            "    )\n"
            "    write_state(project, worker_pid=late.pid,\n"
            "                 worker_role='worker', worker_todo='TODO-0075')\n"
            "    print('LATE', late.pid, flush=True)\n"
            "    time.sleep(2.0)\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, on_term)\n"
            "time.sleep(300)\n"
        )
        pipe = subprocess.Popen(
            [sys.executable, "-c", pipe_code, str(project)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        late_pid = None
        try:
            line = pipe.stdout.readline().decode().split()
            assert line[0] == "READY"
            old_pid = int(line[1])
            write_state(project, pipeline_pid=pipe.pid)
            real_cmdline = _liveness.read_cmdline
            monkeypatch.setattr(
                _liveness, "read_cmdline",
                lambda pid: AWF_ARGV if pid == pipe.pid else real_cmdline(pid),
            )

            res = kill_pipeline(project)

            line = pipe.stdout.readline().decode().split()
            assert line[0] == "LATE"
            late_pid = int(line[1])

            assert res["killed"] is True
            assert res["pid"] == pipe.pid
            assert res["workers"].get(str(old_pid)) in ("killed", "dead")
            assert res["workers"].get(str(late_pid)) == "killed"
            assert f"worker PID {late_pid} spawned during kill" in res["message"]

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and _alive(late_pid):
                time.sleep(0.2)
            assert not _alive(late_pid), (
                "the late worker survived the second sweep (the incident)"
            )
            rec = json.loads(
                (project / ".agentic" / "state" / LAST_KILL_FILE).read_text(
                    encoding="utf-8"
                )
            )
            assert rec["workers"].get(str(late_pid)) == "killed"
        finally:
            if pipe.poll() is None:
                pipe.kill()
                pipe.wait()
            if pipe.stdout is not None:
                try:
                    pipe.stdout.close()
                except OSError:
                    pass
            if late_pid is not None and _alive(late_pid):
                try:
                    os.kill(late_pid, 9)
                except OSError:
                    pass


class TestCallerAncestry:
    """The /proc ancestor walk itself (RUN10 #5 Part A helper)."""

    def test_walk_reaches_init_without_hit(self):
        from awf._proc import caller_ancestry

        hit, available = caller_ancestry({2**30})
        assert hit is None
        assert available is True

    def test_own_pid_is_hit(self):
        from awf._proc import caller_ancestry

        hit, available = caller_ancestry({os.getpid()})
        assert hit == os.getpid()
        assert available is True

    def test_parent_seen_from_child(self):
        code = (
            "import os\n"
            "from awf._proc import caller_ancestry\n"
            "print(caller_ancestry({os.getppid()})[0])\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert int(out) == os.getpid()
