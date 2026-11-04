"""TODO-0338 (A35): the verify supervisor must not wait for the engine.

The engine waits for a natural exit after a signal; the verify subprocess
waits for the engine's commit. After the verdict the engine now grants a
bounded grace, then TERMs the subprocess's own group and proceeds.
"""
from __future__ import annotations

from pathlib import Path

from awf import opencode_version as ov
from awf import supervisor
from awf.signal_watch import run_subprocess_until_signal
from tests.unit._signal_fakes import (
    Clock,
    hanging_popen,
    install_fake_time,
    make_project,
)


def _run_verify(proj: Path, monkeypatch, config: dict):
    install_fake_time(monkeypatch, Clock())
    return supervisor.run_supervisor_via_subprocess(
        "verify", "TODO-0001", proj,
        {**config, "models": {"supervisor": {"model": "p/test"}}},
        str(proj / ".agentic" / "phases" / "plan.md"),
        proj / ".agentic" / "logs",
    )


def test_hung_supervisor_is_termed_after_grace_and_signal_returned(
    tmp_path, monkeypatch
):
    """Sleeping longer than the grace → log line, TERM, no RuntimeError."""
    proj = make_project(tmp_path)
    signal_file = proj / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready"

    class _SignalThenHang:
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

    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _SignalThenHang)
    monkeypatch.setattr(
        "awf.opencode_version.probe_version",
        lambda *a, **kw: ov.Probe("ok", (1, 19, 4)),
    )
    install_fake_time(monkeypatch, Clock())

    config = {
        "wait": {"verify_grace_seconds": 6},
        "models": {"supervisor": {"model": "p/test"}},
    }
    result = supervisor.run_supervisor_via_subprocess(
        "verify", "TODO-0001", proj, config,
        str(proj / ".agentic" / "phases" / "plan.md"),
        proj / ".agentic" / "logs",
    )

    assert result == "APPROVE-TODO-0001"
    assert _SignalThenHang.killed is True
    text = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    assert "verify subprocess did not exit within 6s" in text
    assert "decision already recorded, proceeding to commit" in text
    assert "terminating its process group" in text


def test_supervisor_exits_within_grace_is_not_termed(tmp_path, monkeypatch):
    """Natural exit inside the window keeps the old behavior — no TERM."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    signal_file = outbox / "APPROVE-TODO-0001.ready"

    class _SignalThenExit:
        killed = False

        def __init__(self, cmd, cwd=None, **kwargs):
            self.cmd = cmd
            self.pid = 999999999
            self.returncode = None
            self.polls = 0
            signal_file.write_text("", encoding="utf-8")

        def poll(self):
            self.polls += 1
            if self.polls >= 2:
                self.returncode = 0
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            type(self).killed = True

        def terminate(self):
            self.kill()

    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _SignalThenExit)
    install_fake_time(monkeypatch, Clock())

    result = run_subprocess_until_signal(
        ["fake-supervisor"], cwd=tmp_path,
        watch_paths=[signal_file],
        logs_dir=tmp_path,
        signal_exit_grace=90,
        hard_timeout=3600,
    )

    assert result.returncode == 0
    assert _SignalThenExit.killed is False
    text = (tmp_path / "orchestrator.log").read_text(encoding="utf-8")
    assert "proceeding to commit" not in text


def test_zero_grace_terms_as_soon_as_the_signal_is_seen(tmp_path, monkeypatch):
    """0 means "no window": the verdict ends the wait on the same poll."""
    proj = make_project(tmp_path)
    signal_file = proj / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready"
    fake = hanging_popen(signal_file)
    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", fake)
    monkeypatch.setattr(
        "awf.opencode_version.probe_version",
        lambda *a, **kw: ov.Probe("ok", (1, 19, 4)),
    )

    result = _run_verify(proj, monkeypatch, {"wait": {"verify_grace_seconds": 0}})

    assert result == "APPROVE-TODO-0001"
    assert fake.killed is True
    text = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    assert "did not exit within 0s" in text
    assert "U6b: signal present (APPROVE-TODO-0001)" in text


def test_negative_grace_falls_back_to_the_default(tmp_path, monkeypatch):
    """A negative window is nonsense — the 90s default applies, not "forever"."""
    proj = make_project(tmp_path)
    signal_file = proj / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready"
    fake = hanging_popen(signal_file)
    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", fake)
    monkeypatch.setattr(
        "awf.opencode_version.probe_version",
        lambda *a, **kw: ov.Probe("ok", (1, 19, 4)),
    )

    result = _run_verify(
        proj, monkeypatch, {"wait": {"verify_grace_seconds": -5}}
    )

    assert result == "APPROVE-TODO-0001"
    assert fake.killed is True
    text = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    assert "is negative — using 90s" in text
    assert "did not exit within 90s" in text


def test_grace_records_the_signal_name_without_a_holder(tmp_path, monkeypatch):
    """No signal_holder: the shared kill-path helper re-reads and names it."""
    signal_file = tmp_path / "APPROVE-TODO-0001.ready"
    fake = hanging_popen(signal_file)
    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", fake)
    install_fake_time(monkeypatch, Clock())

    result = run_subprocess_until_signal(
        ["fake-supervisor"], cwd=tmp_path,
        watch_paths=[signal_file],
        logs_dir=tmp_path,
        signal_exit_grace=0,
        hard_timeout=30,
    )

    assert result.returncode == 0
    text = (tmp_path / "orchestrator.log").read_text(encoding="utf-8")
    assert "U6b: signal present (APPROVE-TODO-0001)" in text


def test_grace_terms_the_real_process_group(tmp_path):
    """The TERM path is real: a live child must receive SIGTERM and exit.

    The Popen fakes above use an impossible pid, so ``kill_process_tree``
    takes the ``proc.kill()`` fallback — a broken group-TERM would still
    pass them. Here the child is a real process in its own session: it
    writes the verdict, then sleeps; the marker proves SIGTERM arrived
    (a SIGKILL would leave no marker).
    """
    import sys

    sig = tmp_path / "APPROVE-TODO-0001.ready"
    marker = tmp_path / "got-term.txt"
    code = (
        "import signal, sys, time\n"
        "def on_term(*_a):\n"
        "    open(sys.argv[2], 'w').write('term')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        "open(sys.argv[1], 'w').close()\n"
        "time.sleep(60)\n"
    )

    result = run_subprocess_until_signal(
        [sys.executable, "-c", code, str(sig), str(marker)],
        cwd=tmp_path,
        watch_paths=[sig],
        logs_dir=tmp_path,
        signal_exit_grace=0,
        hard_timeout=60,
    )

    assert result.returncode == 0
    assert marker.read_text(encoding="utf-8") == "term"


def test_unparseable_grace_falls_back_to_the_default(tmp_path, monkeypatch):
    """A typo must not silently restore the endless wait."""
    proj = make_project(tmp_path)
    signal_file = proj / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready"
    fake = hanging_popen(signal_file)
    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", fake)
    monkeypatch.setattr(
        "awf.opencode_version.probe_version",
        lambda *a, **kw: ov.Probe("ok", (1, 19, 4)),
    )

    result = _run_verify(
        proj, monkeypatch, {"wait": {"verify_grace_seconds": "soon"}}
    )

    assert result == "APPROVE-TODO-0001"
    text = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    assert "is not a number — using 90s" in text
    assert "did not exit within 90s" in text
