"""TODO-0306 (4): kill isolation — never signal a group we did not start.

The 2026-09-20 incident (``os.killpg(1, sig)`` ≡ ``kill(-1, sig)``) is the
loud case. The quiet one is the same rule: a child WITHOUT
``start_new_session`` inherits the caller's process group, so ``killpg``
would reach the owner's shell/desktop. Both kill helpers must fall back to
the bare pid and never touch the inherited group.

These are canaries over the real decision points (``os.killpg`` /
``os.kill`` are monkeypatched): a regression that drops the group-identity
check fails here without signaling anything.
"""
from __future__ import annotations

import signal


def test_kill_process_tree_never_signals_inherited_group(monkeypatch):
    """``kill_process_tree``: pgid != pid (inherited group) → proc.kill only."""
    from awf import _proc

    calls: list[tuple] = []

    class _FakePopen:
        pid = 4242

        def kill(self):
            calls.append(("proc.kill",))

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            return 0

    # The child's group is the caller's (e.g. 999), not its own pid.
    monkeypatch.setattr(_proc.os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(
        _proc.os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig))
    )

    _proc.kill_process_tree(_FakePopen(), grace=0.01)

    assert not [c for c in calls if c[0] == "killpg"], calls
    assert ("proc.kill",) in calls


def test_kill_pid_tree_never_signals_inherited_group(monkeypatch):
    """``kill_pid_tree``: non-group-leader pid → only ``os.kill``, no group."""
    from awf import _proc

    alive = {4242}
    singles: list[tuple] = []
    groups: list[tuple] = []

    def _kill(pid, sig):
        singles.append((pid, sig))
        if sig in (signal.SIGTERM, signal.SIGKILL):
            alive.discard(pid)

    monkeypatch.setattr(_proc, "_pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(_proc.os, "getpgid", lambda pid: 777)  # someone else
    monkeypatch.setattr(_proc.os, "kill", _kill)
    monkeypatch.setattr(
        _proc.os, "killpg", lambda pgid, sig: groups.append((pgid, sig))
    )

    assert _proc.kill_pid_tree(4242, grace=0.05, poll=0.01) == "killed"
    assert groups == [], f"killpg on an inherited group: {groups}"
    assert (4242, signal.SIGTERM) in singles
