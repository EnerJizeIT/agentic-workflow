"""A35: doubles shared by the verify-grace tests.

``test_verify_signal_grace.py`` and ``test_verify_signal_prompt.py`` were
split to stay under the ratchet line budget; the fakes both need live here,
so one fix to a double reaches both files (the ``_gate_scripts.py``
precedent).
"""
from __future__ import annotations

from pathlib import Path


class Clock:
    """Monotonic clock driven by the patched ``time.sleep``."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def monotonic(self) -> float:
        return self.t


def install_fake_time(monkeypatch, clock: Clock) -> None:
    """Make ``time.monotonic`` advance exactly by each fake ``sleep``."""

    def fake_sleep(dt: float) -> None:
        clock.t += dt

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
