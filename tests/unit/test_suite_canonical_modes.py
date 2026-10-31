"""TODO-0316: the canonical suite can run detached and be polled.

The full suite (~12 min) outlives the worker's command budget (10 min), so
QA used to block, time out and loop. ``--background`` starts the same
content-addressed run and ``--status`` answers instantly:

* rc 4 — not started (or the artifact is for another tree/command);
* rc 3 — running;
* rc 0/1 — the artifact exists and carries the suite's exit code.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "suite-canonical.sh"

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="setsid is a Linux tool"
)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
    (project / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
    (project / ".agentic").mkdir()
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: t\nverification:\n  test_cmd: echo hello\n",
        encoding="utf-8",
    )
    return project


def _run(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        ["bash", str(SCRIPT), *args, "--project", str(project)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_status_reports_not_started(tmp_path):
    result = _run(_project(tmp_path), "--status")
    assert result.returncode == 4, result.stderr
    assert "not started" in result.stdout


def test_background_runs_and_status_reports_the_result(tmp_path):
    project = _project(tmp_path)
    started = _run(project, "--background")
    assert started.returncode == 3, started.stderr
    assert "started" in started.stdout

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = _run(project, "--status")
        if status.returncode in (0, 1):
            break
        assert status.returncode == 3, status.stdout + status.stderr
        time.sleep(0.2)
    else:  # pragma: no cover - only on a hung suite
        pytest.fail("the detached suite never finished")

    assert '"exit": 0' in status.stdout
    artifacts = list((project / ".agentic" / "context").glob("SUITE-*.json"))
    assert len(artifacts) == 1


def test_status_rejects_an_artifact_of_another_command(tmp_path):
    project = _project(tmp_path)
    assert _run(project, "--background").returncode == 3
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _run(project, "--status").returncode in (0, 1):
            break
        time.sleep(0.2)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: t\nverification:\n  test_cmd: echo other\n",
        encoding="utf-8",
    )
    status = _run(project, "--status")
    assert status.returncode == 4, status.stdout + status.stderr
