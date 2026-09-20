"""AUD14-01 / AUD15-05: dispatch concurrency + pre-check grep scope.

Incident (audit 01): two parallel ``awf_dispatch_todo`` calls both picked
the same TODO-NNNN — the ``exists()`` check passed for both before either
wrote — and the second atomic write silently overwrote the first's
content. The pre-check grep scanned ``.git``/``node_modules``: up to ~25s
of dispatch on a 25k-file tree.

RED-first contract: the parallel test fails on the pre-fix code (all
workers land on the same id / content lost).
"""
from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

from awf import api
from awf.api.dispatch import dispatch_todo


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="DispatchConc")
    return tmp_git_repo


def _dispatch_worker(
    project_dir: str, content: str, out_file: str, start_gate: str
) -> None:
    """Worker: wait at the gate (all workers ready), then dispatch one TODO."""
    while not Path(start_gate).exists():
        time.sleep(0.01)
    try:
        res = dispatch_todo(project_dir, content)
        payload = res.todo_id
    except Exception as e:  # noqa: BLE001 — surface any failure in the test
        payload = f"ERROR:{e}"
    Path(out_file).write_text(payload, encoding="utf-8")


class TestParallelDispatch:
    """AUD14-01: N parallel dispatches → N unique ids, no lost content."""

    def test_parallel_dispatch_unique_ids_and_content(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        n = 8
        logs = proj / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        gate = logs / "gate"
        outs = [logs / f"out-{i}.txt" for i in range(n)]

        ctx = mp.get_context("fork")
        procs = [
            ctx.Process(
                target=_dispatch_worker,
                args=(str(proj), f"# T{i}\n\nmarker-{i}\n", str(o), str(gate)),
            )
            for i, o in enumerate(outs)
        ]
        for p in procs:
            p.start()
        time.sleep(0.5)  # let every worker reach the gate
        gate.touch()
        for p in procs:
            p.join(timeout=180)

        ids = []
        for i, o in enumerate(outs):
            assert o.is_file(), f"worker {i} produced no result"
            payload = o.read_text(encoding="utf-8").strip()
            assert not payload.startswith("ERROR:"), payload
            ids.append(payload)

        assert len(set(ids)) == n, f"ID collision — parallel dispatch raced: {ids}"
        inbox = proj / ".agentic" / "inbox"
        for tid, i in zip(ids, range(n)):
            text = (inbox / f"{tid}.md").read_text(encoding="utf-8")
            assert f"marker-{i}" in text, f"{tid}: content of worker {i} was lost"


class TestPrecheckGrepScope:
    """AUD15-05: the pre-check grep must not scan VCS/dependency dirs."""

    def test_grep_argv_has_exclude_dirs(self, tmp_git_repo, monkeypatch):
        import subprocess

        proj = _project(tmp_git_repo)
        captured: dict = {}

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "grep":
                captured.setdefault("cmds", []).append(cmd)
                captured["timeout"] = kwargs.get("timeout")
            return type("R", (), {"stdout": "", "stderr": "", "returncode": 1})()

        monkeypatch.setattr(subprocess, "run", fake_run)

        dispatch_todo(
            proj,
            "# task\n\n`unique_ident` snake_case_marker\n",
        )

        assert captured.get("cmds"), "pre-check grep was not executed"
        for cmd in captured["cmds"]:
            for d in (".git", "node_modules", ".venv", "venv", "vendor"):
                assert f"--exclude-dir={d}" in cmd, f"missing --exclude-dir={d}: {cmd}"
        assert captured["timeout"] is not None
        assert captured["timeout"] <= 2, f"grep timeout too long: {captured['timeout']}"
