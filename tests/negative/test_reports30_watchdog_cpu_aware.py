"""REPORTS30 (TODO-0180): the U6c watchdog must be CPU-aware.

Incident 09.10: the watchdog killed worker TODO-0175 for "silent 15 min"
in the middle of the final FULL test-suite run — a quiet long command that
burns CPU for tens of minutes without a log line. The project's threshold
was raised 900→1800s as a band-aid; the real fix is to account for the
worker process tree's activity.

Invariant:
- log silent AND the tree's total CPU (utime+stime of all descendants)
  frozen between two readings → hung → kill, and the log/exception text
  names BOTH facts (silence + no CPU progress);
- log silent but the tree burns CPU → a quiet long command, NOT a hang →
  the effective silence is refreshed and the watchdog keeps watching (the
  stage hard timeout stays the last resort);
- existing behavior is untouched: signal seen → never kill; fresh log →
  alive; the production threshold mechanics are unchanged.

All tests use REAL worker subprocesses (the defect is about real /proc CPU
accounting, not state fakes). The poll interval is shrunk to 0.5s so the
whole file runs in ~15s.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from awf import _proc
from awf.signal_watch import run_subprocess_until_signal

POLL = 0.5  # patched over awf.signal_watch.BD20_POLL_INTERVAL
SILENCE = 1.0  # no_output_timeout for all cases: 1s is enough to be "hung"


# ─── worker scripts ─────────────────────────────────────────────────────────


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / "workers" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body, encoding="utf-8")
    return script


#: (a) the worker is silent; its child BURNS CPU (tight loop). The pidfile
#: hands the child's pid to the test for the cleanup and the aliveness check.
BUSY_CHILD = """\
import subprocess, sys, time

child = subprocess.Popen([sys.executable, "-c", "while True: pass"])
with open(sys.argv[1], "w") as f:
    f.write(str(child.pid))
time.sleep(3600)
"""

#: (b) the worker is silent and the WHOLE tree is frozen (both sleep).
STALLED_TREE = """\
import subprocess, sys, time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
time.sleep(3600)
"""

#: (c) the worker is chatty: a log line every 0.2s for ~4s, then a clean exit.
CHATTY = """\
import time

for i in range(20):
    print(f"tick {i}", flush=True)
    time.sleep(0.2)
"""


def _pid_alive(pid: int) -> bool:
    """signal-0 probe (the suite tripwire allows probes of pid > 1)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _spawn_watch_thread(**kwargs) -> tuple[threading.Thread, dict]:
    """Run the (blocking) watchdog loop in a daemon thread; capture outcome."""
    box: dict = {}

    def target() -> None:
        try:
            box["result"] = run_subprocess_until_signal(**kwargs)
        except BaseException as exc:  # noqa: BLE001 — report what the loop raised
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


# ─── the three acceptance cases ─────────────────────────────────────────────


class TestWatchdogCpuAware:

    def test_silent_worker_with_busy_child_not_killed(
        self, tmp_path, monkeypatch
    ) -> None:
        """(a) log silent beyond the threshold, but a child burns CPU → the
        tree is busy, not hung: NOT killed. (On the baseline this worker is
        killed the moment the silence crosses the threshold.)"""
        monkeypatch.setattr("awf.signal_watch.BD20_POLL_INTERVAL", POLL)
        script = _write_script(tmp_path, "busy_child.py", BUSY_CHILD)
        pidfile = tmp_path / "child.pid"
        pids: dict[str, int] = {}

        thread, box = _spawn_watch_thread(
            cmd=[sys.executable, str(script), str(pidfile)],
            cwd=tmp_path,
            logs_dir=tmp_path,
            no_output_timeout=SILENCE,
            on_spawn=lambda pid: pids.update(worker=pid),
        )
        try:
            # the worker hands over its child's pid right after spawn
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not pidfile.exists():
                time.sleep(0.05)
            assert pidfile.exists(), "worker never reported its child pid"
            child_pid = int(pidfile.read_text(encoding="utf-8"))

            # let the baseline's kill moment pass with room to spare:
            # the baseline kills on the first poll past the threshold
            time.sleep(3.0)

            assert "error" not in box, (
                f"watchdog killed a BUSY worker: {box['error']!r}"
            )
            assert _pid_alive(pids["worker"]), (
                "worker with a CPU-burning child was killed by the watchdog"
            )
            assert _pid_alive(child_pid), (
                "the CPU-burning child was killed by the watchdog"
            )
        finally:
            # the worker survived — clean the tree up ourselves (the worker
            # is a session leader: its own group takes the child with it)
            if pids:
                _proc.kill_pid_tree(pids["worker"])
            thread.join(timeout=10)

    def test_silent_worker_with_stalled_tree_killed_with_both_facts(
        self, tmp_path, monkeypatch
    ) -> None:
        """(b) log silent AND the tree's CPU frozen → hung → killed, and the
        text names BOTH facts: the silence AND the no-progress CPU."""
        monkeypatch.setattr("awf.signal_watch.BD20_POLL_INTERVAL", POLL)
        script = _write_script(tmp_path, "stalled_tree.py", STALLED_TREE)
        pids: dict[str, int] = {}

        try:
            with pytest.raises(TimeoutError) as exc_info:
                run_subprocess_until_signal(
                    cmd=[sys.executable, str(script)],
                    cwd=tmp_path,
                    logs_dir=tmp_path,
                    no_output_timeout=SILENCE,
                    on_spawn=lambda pid: pids.update(worker=pid),
                )
        finally:
            if pids:
                _proc.kill_pid_tree(pids["worker"])

        assert pids.get("worker") is not None, "on_spawn never fired"
        assert not _pid_alive(pids["worker"]), "the stalled tree survived"

        msg = str(exc_info.value)
        assert "no output" in msg, f"silence fact missing from: {msg!r}"
        assert "CPU" in msg, f"CPU fact missing from: {msg!r}"

        log_text = (tmp_path / "orchestrator.log").read_text(encoding="utf-8")
        assert "U6c" in log_text
        assert "CPU" in log_text, "the awf log must name the frozen tree CPU"

    def test_fresh_log_keeps_worker_alive(self, tmp_path, monkeypatch) -> None:
        """(c) regression pin: a chatty log is always fresh → the watchdog
        never fires; the worker lives past the silence threshold and exits
        naturally."""
        monkeypatch.setattr("awf.signal_watch.BD20_POLL_INTERVAL", POLL)
        script = _write_script(tmp_path, "chatty.py", CHATTY)

        start = time.monotonic()
        result = run_subprocess_until_signal(
            cmd=[sys.executable, str(script)],
            cwd=tmp_path,
            logs_dir=tmp_path,
            no_output_timeout=SILENCE,
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, "the chatty worker must exit naturally"
        # it survived well past the silence threshold on a fresh log
        assert elapsed >= 2.5, (
            f"worker exited after {elapsed:.1f}s — the watchdog may have "
            "killed a fresh-log worker"
        )


# ─── the CPU helper (skipped while the helper itself is not implemented) ────


_HAS_TREE_CPU = hasattr(_proc, "cpu_time_of_tree")


@pytest.mark.skipif(
    not _HAS_TREE_CPU, reason="cpu_time_of_tree not implemented yet"
)
class TestCpuTimeOfTree:

    def test_refuses_pid_le_1(self) -> None:
        """Safety pin: init's tree is never measured (the 2026-09-20 rule —
        nothing may ever target pid 1)."""
        assert _proc.cpu_time_of_tree(1) is None
        assert _proc.cpu_time_of_tree(0) is None
        assert _proc.cpu_time_of_tree(-5) is None
        assert _proc.cpu_time_of_tree(None) is None  # type: ignore[arg-type]

    def test_grows_for_busy_tree(self) -> None:
        root = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', 'while True: pass'])\n"
                "time.sleep(60)\n",
            ],
            start_new_session=True,
        )
        try:
            time.sleep(0.2)  # let the child start burning
            t0 = _proc.cpu_time_of_tree(root.pid)
            time.sleep(0.5)
            t1 = _proc.cpu_time_of_tree(root.pid)
            assert t0 is not None and t1 is not None
            assert t1 > t0, "a burning tree must accumulate CPU"
        finally:
            _proc.kill_pid_tree(root.pid)
            root.wait(timeout=5)

    def test_frozen_for_stalled_tree(self) -> None:
        root = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        try:
            time.sleep(0.3)  # let startup CPU settle into the sleep
            t0 = _proc.cpu_time_of_tree(root.pid)
            time.sleep(0.5)
            t1 = _proc.cpu_time_of_tree(root.pid)
            assert t0 is not None and t1 is not None
            assert t1 == t0, "a stalled tree must not accumulate CPU"
        finally:
            _proc.kill_pid_tree(root.pid)
            root.wait(timeout=5)
