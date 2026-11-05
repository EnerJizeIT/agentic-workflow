"""A35/A41: doubles shared by the verify-grace and verify-wait tests.

``test_verify_signal_grace.py`` and ``test_verify_signal_prompt.py`` were
split to stay under the ratchet line budget; the fakes both need live here,
so one fix to a double reaches both files (the ``_gate_scripts.py``
precedent). A41 split ``test_run_verify_wait.py`` the same way — its
approve/log/spawn doubles live here too, for the same reason.
"""
from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest


class Clock:
    """Monotonic clock driven by the patched ``time.sleep``."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def monotonic(self) -> float:
        return self.t


def install_fake_time(monkeypatch, clock: Clock, on_sleep=None) -> None:
    """Make ``time.monotonic`` advance exactly by each fake ``sleep``.

    ``on_sleep`` runs after every tick — the hook that lets a test write a
    decision file "a second into" a window without a real sleep.
    """

    def fake_sleep(dt: float) -> None:
        clock.t += dt
        if on_sleep is not None:
            on_sleep()

    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)


def make_project(tmp_path: Path) -> Path:
    """A project skeleton with the dirs and role file the supervisor needs."""
    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "logs", "roles"):
        (proj / ".agentic" / d).mkdir(parents=True)
    (proj / ".agentic" / "roles" / "supervisor.md").write_text("# Role\n")
    phases = proj / ".agentic" / "phases"
    phases.mkdir()
    (phases / "plan.md").write_text("# Plan\n")
    return proj


def approve(proj: Path, todo: str) -> Path:
    """The owner's verdict on disk: an APPROVE signal for ``todo``."""
    path = proj / ".agentic" / "inbox" / f"APPROVE-{todo}.ready"
    path.write_text(json.dumps({"source": "human"}), encoding="utf-8")
    return path


def log_text(proj: Path) -> str:
    """The project's orchestrator log — the surface the wait reports on."""
    return (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )


def run_verify(proj: Path, monkeypatch, todo: str, config: dict | None = None,
               run=None, **kwargs):
    """Run the auto verify stage with a subprocess that exits 0 and no signal.

    ``config`` is the stage config the supervisor reads (``wait.*``
    windows); ``run`` replaces the default no-signal subprocess double
    (a subprocess that writes the verdict itself).
    """
    from awf import supervisor

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal",
        run or (lambda cmd, cwd=None, **kw: CompletedProcess(cmd, 0)),
        raising=True,
    )
    return supervisor.run_supervisor_via_subprocess(
        "verify",
        todo,
        proj,
        {**(config or {}), "models": {"supervisor": {"model": "p/test"}}},
        str(proj / ".agentic" / "phases" / "plan.md"),
        proj / ".agentic" / "logs",
        **kwargs,
    )


@pytest.fixture(autouse=True)
def opencode_ok(monkeypatch):
    """Every test here spawns the supervisor: the version gate must pass."""
    from awf import opencode_version as ov

    monkeypatch.setattr(
        "awf.opencode_version.probe_version",
        lambda *a, **kw: ov.Probe("ok", (1, 19, 4)),
    )


def hanging_popen(signal_file: Path):
    """Popen stand-in: writes the verdict in ``__init__``, then never exits."""

    class _Hanging:
        killed = False

        def __init__(self, cmd, cwd=None, **kwargs):
            self.cmd = cmd
            self.pid = 999999999  # never a real group — killpg must not run
            self.returncode = None
            signal_file.write_text("", encoding="utf-8")

        def poll(self):
            return None

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            type(self).killed = True
            self.returncode = -15

        def terminate(self):
            self.kill()

    return _Hanging


def silent_popen():
    """Popen stand-in that never writes a signal and never exits."""

    class _Silent:
        killed = False

        def __init__(self, cmd, cwd=None, **kwargs):
            self.cmd = cmd
            self.pid = 999999999
            self.returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            type(self).killed = True
            self.returncode = -15

        def terminate(self):
            self.kill()

    return _Silent
