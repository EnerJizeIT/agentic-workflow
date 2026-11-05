"""A35/A41: doubles shared by the verify-grace and verify-wait tests.

``test_verify_signal_grace.py`` and ``test_verify_signal_prompt.py`` were
split to stay under the ratchet line budget; the fakes both need live here,
so one fix to a double reaches both files (the ``_gate_scripts.py``
precedent). A41 split ``test_run_verify_wait.py`` the same way — its
approve/log/spawn doubles live here too, for the same reason.
"""
from __future__ import annotations

import json
import sys
import time as _real_time
from pathlib import Path
from subprocess import CompletedProcess

import pytest


class Clock:
    """Monotonic clock driven by the fake clock's ``sleep``."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def monotonic(self) -> float:
        return self.t


#: The engine modules whose module-level ``time`` name the test replaces.
_ENGINE_TIME_MODULES = ("awf.supervisor", "awf.arbiter", "awf.signal_watch")


class FakeTime:
    """The ``time`` module the engine's wait loops see in a test.

    ``monotonic`` and ``sleep`` come from the test's :class:`Clock`; every
    other attribute (``time.time`` above all) falls through to the real
    module. A test that needs a pinned wall clock sets it on THIS object
    (``monkeypatch.setattr(fake, "time", ...)``) — it is the one the engine
    reads, whichever of the patched modules it imports ``time`` from.
    """

    def __init__(self, clock: Clock, on_sleep=None) -> None:
        self._clock = clock
        self._on_sleep = on_sleep

    def monotonic(self) -> float:
        return self._clock.monotonic()

    def sleep(self, dt: float) -> None:
        self._clock.t += dt
        if self._on_sleep is not None:
            self._on_sleep()

    def __getattr__(self, name: str):
        return getattr(_real_time, name)


def install_fake_time(
    monkeypatch, clock: Clock, on_sleep=None
) -> FakeTime:
    """Make the engine's ``time.monotonic`` advance by each fake ``sleep``.

    ``on_sleep`` runs after every tick — the hook that lets a test write a
    decision file "a second into" a window without a real sleep.

    A61: the fake clock replaces the ``time`` the engine's wait loops see,
    not the real module. ``subprocess`` waits for a real child (the ``git``
    read of the STATE block) with ``time.sleep(0.05)`` in its own poll loop;
    a global patch made those real waits tick the test clock, so under
    ``-n 8`` the clock ran far ahead of the test and a "second tick" fired
    during the spawn instead of inside the wait.

    The swap reaches further than the three modules above: anything that
    does a fresh ``import time`` during the test gets the fake too. A module
    imported for the FIRST time that way keeps the fake after the test, with
    ``monotonic`` frozen — that is why the entry in ``sys.modules`` stays a
    deliberate, documented step and not a hidden one.
    """
    fake = FakeTime(clock, on_sleep)
    for module in _ENGINE_TIME_MODULES:
        monkeypatch.setattr(f"{module}.time", fake)
    # ``wait_for_supervisor_signal`` imports ``time`` INSIDE the function
    # (``awf/supervisor.py``), so its name comes from ``sys.modules``;
    # ``subprocess`` already holds its own reference to the real module and
    # is not affected. The engine modules on the verify path are imported
    # before the tests run, so nothing keeps the fake past the test.
    monkeypatch.setitem(sys.modules, "time", fake)
    return fake


def window_clock(monkeypatch, on_sleep=None) -> Clock:
    """The human-override window's clock — one helper for both window files.

    ``on_sleep`` writes the owner's verdict "a second into" the window. The
    clock is scoped to the engine modules (see :func:`install_fake_time`):
    the real ``time.sleep`` of ``subprocess``'s poll of a ``git`` child must
    not tick it — that latency is what made the window flaky under
    ``-n auto``.
    """
    clock = Clock()
    install_fake_time(monkeypatch, clock, on_sleep)
    return clock


def make_project(tmp_path: Path) -> Path:
    """A project skeleton with the dirs and role file the supervisor needs."""
    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "logs", "roles", "context"):
        (proj / ".agentic" / d).mkdir(parents=True)
    # IMP2b: the stage text comes from the builder — the project declares
    # its stage types and owns the context dir the text is written to.
    (proj / ".agentic" / "config.yaml").write_text(
        "stage_types:\n  plan: plan\n  verify: supervisor\n  agent-dev: execute\n",
        encoding="utf-8",
    )
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
            self.args = cmd
            self.pid = 999999999  # never a real group — killpg must not run
            self.returncode = None
            self.stdout = b""
            self.stderr = b""
            # ``subprocess.run`` (the git read in the STATE block) uses the
            # same patched Popen; only the supervisor spawn writes a verdict.
            if not (cmd and str(cmd[0]).split("/")[-1] == "git"):
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

        def communicate(self, input=None, timeout=None):
            return (self.stdout, self.stderr)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

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
