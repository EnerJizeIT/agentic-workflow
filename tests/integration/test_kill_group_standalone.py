"""R3 (T9, AC14–AC16): the group kill reaches the private ``serve`` child.

IMP1 probe of 2.2.3: ``--standalone`` starts ``opencode serve --stdio`` as a
CHILD of the worker (it does move to its own session/process group, but it
lives and dies with the worker's stdio). The kill path stays group-based —
this test proves no descendant survives, and never widens the signal to a
foreign pid (INV4).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from awf import _proc


def _pid_alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


def test_group_kill_leaves_no_serve_child(tmp_path: Path):
    pid_file = tmp_path / "serve.pid"
    script = tmp_path / "worker.sh"
    script.write_text(
        "#!/bin/bash\n"
        '( exec -a "opencode serve --stdio --port 0" sleep 300 ) &\n'
        f'echo $! > "{pid_file}"\n'
        "sleep 300\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        ["bash", str(script)], start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            time.sleep(0.05)
        serve = int(pid_file.read_text().strip())
        assert os.getpgid(serve) == proc.pid          # same group (AC14)
        assert _pid_alive(serve)

        _proc.kill_process_tree(proc, grace=1.0)

        for _ in range(40):
            if not _pid_alive(serve) and not _pid_alive(proc.pid):
                break
            time.sleep(0.05)
        assert not _pid_alive(serve), "serve survived the group kill"
        assert not _pid_alive(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_guard_and_tripwire_are_untouched():
    """INV4 canary: the two safety lines still exist verbatim."""
    root = Path(__file__).resolve().parents[2]
    proc_src = (root / "awf" / "_proc.py").read_text(encoding="utf-8")
    conftest_src = (root / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "pid > 1" in proc_src
    assert "_forbid_session_kill" in conftest_src
