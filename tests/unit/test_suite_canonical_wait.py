"""A57 (TODO-0380): ``suite-canonical.sh --wait`` replaces blind ``sleep``.

QA used to start the suite with ``--background`` and then sleep for minutes
between ``--status`` polls. ``--wait`` blocks until the run finishes, polling
every 10 seconds (``AWF_SUITE_WAIT_INTERVAL`` overrides), prints the result
and returns the suite's own exit code. ``--timeout`` (default 1800 s) ends
the wait with the separate code 124 — never with a fake suite verdict.

A run that is not started yet is started by ``--wait`` itself: one call
instead of ``--background`` plus a polling loop.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "suite-canonical.sh"
ROLE_TEMPLATE = ROOT / "awf" / "templates" / "roles" / "agent-qa-review.md"

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="setsid is a Linux tool"
)


def _project(tmp_path: Path, test_cmd: str = "echo hello") -> Path:
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
        "project:\n  name: t\nverification:\n  test_cmd: " + test_cmd + "\n",
        encoding="utf-8",
    )
    return project


def _run(
    project: Path, *args: str, interval: str = "0.2"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args, "--project", str(project)],
        capture_output=True,
        text=True,
        timeout=120,
        env=_env(interval),
    )


def _env(interval: str = "0.2") -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["AWF_SUITE_WAIT_INTERVAL"] = interval
    return env


def _context(project: Path) -> Path:
    return project / ".agentic" / "context"


def _kill_detached_run(project: Path) -> None:
    """Stop a suite left running by a timeout test (never pid 1)."""
    for pid_file in (project / ".agentic" / "context").glob("SUITE-*.json.pid"):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            continue
        if pid > 1:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass


def test_wait_returns_the_run_exit_code_without_fixed_sleeps(tmp_path):
    project = _project(tmp_path, "sleep 0.4; echo hello")
    started = time.monotonic()
    result = _run(project, "--wait")
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stdout + result.stderr
    assert "suite-canonical: done" in result.stdout
    assert "tree-sha" in result.stdout
    assert "after " in result.stdout
    # The old habit slept for minutes; a 0.4 s run must not pay that price.
    assert elapsed < 30, f"--wait took {elapsed:.1f}s for a 0.4s run"


def test_wait_returns_a_red_run_code(tmp_path):
    project = _project(tmp_path, "exit 7")
    result = _run(project, "--wait")

    assert result.returncode == 7, result.stdout + result.stderr
    assert "suite-canonical: done" in result.stdout


def test_wait_timeout_is_a_separate_code(tmp_path):
    project = _project(tmp_path, "sleep 30")
    try:
        result = _run(project, "--wait", "--timeout", "1")
        assert result.returncode == 124, result.stdout + result.stderr
        assert "wait timeout" in result.stderr
        assert "tree-sha" in result.stderr
    finally:
        _kill_detached_run(project)


def test_wait_starts_the_run_when_nothing_is_running(tmp_path):
    project = _project(tmp_path, "echo hello")
    result = _run(project, "--wait")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "suite-canonical: started" in result.stdout
    artifacts = list((project / ".agentic" / "context").glob("SUITE-*.json"))
    assert len(artifacts) == 1


def test_every_qa_surface_recommends_wait_not_polling():
    """The wait rule must reach the role a project actually gets.

    The template body alone is not enough: the managed "Политика тестов" block
    is what ``awf add-role`` appends to every materialized QA role. A block
    still saying "start --background, then poll --status" contradicts the
    template line and brings the blind-sleep habit straight back.
    """
    from awf.api.roles import _suite_canon_body

    body = _suite_canon_body("qa")
    assert "suite-canonical.sh --wait" in body
    assert "--status" not in body
    assert "раз в минуту" not in body


def test_recommended_waiting_is_wait_not_sleep(tmp_path):
    script = SCRIPT.read_text(encoding="utf-8")
    role = ROLE_TEMPLATE.read_text(encoding="utf-8")

    assert "--wait" in script
    assert "--wait" in role
    for text in (script, role):
        assert "sleep 300" not in text
        assert "sleep 240" not in text
    # The QA role must not send the worker back to manual status polling.
    assert "--status" not in role


def test_wait_force_refuses_a_second_run_on_an_unchanged_tree(tmp_path):
    """A75: ``--force`` больше не обход кэша на том же дереве.

    Раньше ``--force --wait`` молча перезапускал сьют; теперь это отказ 2 с
    путём готового артефакта, а перезапуск — только после удаления артефакта
    (или изменения дерева). Иначе один прогон на дерево не докажешь.
    """
    ticks = tmp_path / "ticks"
    project = _project(tmp_path, f"echo tick >> {ticks}; echo hello")

    first = _run(project, "--wait")
    assert first.returncode == 0, first.stdout + first.stderr

    cached = _run(project, "--wait")
    assert cached.returncode == 0
    assert "cached" in cached.stdout
    assert ticks.read_text(encoding="utf-8").count("tick") == 1

    forced = _run(project, "--force", "--wait")
    assert forced.returncode == 2, forced.stdout + forced.stderr
    assert "полный прогон на это дерево уже есть" in forced.stderr
    assert ticks.read_text(encoding="utf-8").count("tick") == 1

    artifact = next(_context(project).glob("SUITE-*.json"))
    artifact.unlink()
    rerun = _run(project, "--force", "--wait")
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "started" in rerun.stdout
    assert ticks.read_text(encoding="utf-8").count("tick") == 2


def test_wait_joins_a_run_already_in_flight(tmp_path):
    """A second waiter must not start a second suite on the same tree."""
    gate = tmp_path / "gate"
    ticks = tmp_path / "ticks"
    project = _project(
        tmp_path,
        f"echo tick >> {ticks}; while [ ! -f {gate} ]; do sleep 0.05; done",
    )
    started = _run(project, "--background")
    assert started.returncode == 3, started.stdout + started.stderr

    wait_out = tmp_path / "wait.out"
    wait_err = tmp_path / "wait.err"
    with open(wait_out, "w", encoding="utf-8") as fo, open(
        wait_err, "w", encoding="utf-8"
    ) as fe:
        waiter = subprocess.Popen(
            ["bash", str(SCRIPT), "--wait", "--timeout", "60", "--project", str(project)],
            stdout=fo,
            stderr=fe,
            env=_env(),
        )
    try:
        time.sleep(1)
        gate.write_text("go", encoding="utf-8")
        assert waiter.wait(timeout=60) == 0
    finally:
        gate.write_text("go", encoding="utf-8")
        if waiter.poll() is None:
            waiter.kill()
            _kill_detached_run(project)

    out = wait_out.read_text(encoding="utf-8")
    assert "already running" in out, out + wait_err.read_text(encoding="utf-8")
    assert "started (pid" not in out
    # One run, not two: the command ran once and left one artifact behind.
    assert ticks.read_text(encoding="utf-8").count("tick") == 1
    assert len(list(_context(project).glob("SUITE-*.json"))) == 1


def test_wait_reports_a_run_killed_before_it_wrote_an_artifact(tmp_path):
    """A dead run is a fact (rc 2), not a reason to block up to ``--timeout``."""
    project = _project(tmp_path, "sleep 30")
    wait_out = tmp_path / "wait.out"
    wait_err = tmp_path / "wait.err"
    with open(wait_out, "w", encoding="utf-8") as fo, open(
        wait_err, "w", encoding="utf-8"
    ) as fe:
        waiter = subprocess.Popen(
            ["bash", str(SCRIPT), "--wait", "--timeout", "15", "--project", str(project)],
            stdout=fo,
            stderr=fe,
            env=_env(),
        )
    try:
        deadline = time.monotonic() + 15
        pid = 0
        while time.monotonic() < deadline and not pid:
            for pid_file in _context(project).glob("SUITE-*.json.pid"):
                try:
                    pid = int(pid_file.read_text(encoding="utf-8").strip() or "0")
                except (OSError, ValueError):
                    pid = 0
            if not pid:
                time.sleep(0.1)
        assert pid > 1, "the detached run never announced its pid"
        os.killpg(pid, signal.SIGKILL)
        rc = waiter.wait(timeout=30)
    finally:
        if waiter.poll() is None:
            waiter.kill()
        _kill_detached_run(project)

    text = wait_out.read_text(encoding="utf-8") + wait_err.read_text(encoding="utf-8")
    assert rc == 2, text
    assert "run died without an artifact" in wait_err.read_text(encoding="utf-8")


def test_wait_rejects_a_timeout_that_is_not_a_positive_integer(tmp_path):
    """``--timeout 0`` would fake a "did not fit" verdict on the first poll."""
    project = _project(tmp_path)

    for value in ("0", "abc", "-5"):
        result = _run(project, "--wait", "--timeout", value)
        assert result.returncode == 2, (value, result.stdout, result.stderr)
        assert "positive integer" in result.stderr

    assert not list(_context(project).glob("SUITE-*.json")), "a run was started anyway"
