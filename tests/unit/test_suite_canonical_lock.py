"""A75 (TODO-0399): один полный прогон на дерево и на пайплайн.

Замок ``.agentic/context/SUITE.lock`` держится весь прогон через ``flock``:
pid переиспользуется, поэтому pid-файл не доказывает, что прогон жив.

* ``--background`` на занятом замке — rc 3 («идёт»);
* ``--wait`` ждёт освобождения и возвращает код прогона, а не падает;
* ``--force`` при живом прогоне — rc 2 с pid владельца;
* ``--force`` при готовом артефакте того же дерева — rc 2 с его путём;
* замок убитого прогона освобождается автоматически;
* ``runs`` в артефакте и в ``--status`` — сколько стартов этой команды на
  этот tree-sha (узкий прогон полным не считается).

Прогоны здесь поддельные: вместо сьюта — скрипт pytest, который спит или
ждёт файл-ворота. Реальный сьют не запускается.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "suite-canonical.sh"
TEMPLATE = ROOT / "awf" / "templates" / "scripts" / "suite-canonical.sh"

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="setsid is a Linux tool"
)


def _env(cmd: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["AWF_SUITE_WAIT_INTERVAL"] = "0.2"
    if cmd is not None:
        env["AWF_SUITE_CMD"] = cmd
    return env


def _setup(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """Временный проект и поддельный pytest; счётчик запусков — вне репо."""
    counter = tmp_path / "counter.txt"
    fake = tmp_path / "pytest"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "run\\n" >> "{counter}"\n' + body,
        encoding="utf-8",
    )
    fake.chmod(0o755)

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
        "project:\n  name: t\nverification:\n  test_cmd: " + str(fake) + "\n",
        encoding="utf-8",
    )
    return project, counter


def _run(
    project: Path, *args: str, timeout: int = 120, cmd: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args, "--project", str(project)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env(cmd),
    )


def _context(project: Path) -> Path:
    return project / ".agentic" / "context"


def _artifact(project: Path, timeout: float = 30) -> Path | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        artifacts = list(_context(project).glob("SUITE-*.json"))
        if artifacts:
            return artifacts[0]
        time.sleep(0.05)
    return None


def _lock_pid(project: Path, timeout: float = 15) -> int:
    lock = _context(project) / "SUITE.lock"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pid = json.loads(lock.read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError):
            pid = None
        if isinstance(pid, int) and pid > 1:
            return pid
        time.sleep(0.05)
    return 0


def _kill_group(pid: int) -> None:
    """Убить прогон вместе с группой; pid 1 не трогаем (safety invariant)."""
    if pid <= 1:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _kill_leftover(project: Path) -> None:
    pids: set[int] = set()
    for pid_file in _context(project).glob("SUITE-*.json.pid"):
        try:
            pids.add(int(pid_file.read_text(encoding="utf-8").strip() or "0"))
        except (OSError, ValueError):
            continue
    try:
        pids.add(int(json.loads((_context(project) / "SUITE.lock").read_text()).get("pid")))
    except (OSError, ValueError, TypeError):
        pass
    for pid in pids:
        _kill_group(pid)


def _gate_body(gate: Path) -> str:
    return (
        f'while [ ! -f "{gate}" ]; do sleep 0.05; done\n'
        'echo "1 passed in 0.01s"\n'
        "exit 0\n"
    )


def _released(project: Path, gate: Path) -> None:
    gate.write_text("go", encoding="utf-8")
    assert _artifact(project) is not None, "the detached run never wrote an artifact"


def test_second_background_on_a_live_run_returns_3(tmp_path):
    """Второй ``--background`` не запускает второй сьют на том же дереве."""
    project, counter = _setup(tmp_path, "sleep 2\necho '1 passed in 2.00s'\nexit 0\n")

    first = _run(project, "--background")
    assert first.returncode in (0, 3), first.stdout + first.stderr
    assert "started" in first.stdout

    second = _run(project, "--background")
    assert second.returncode == 3, second.stdout + second.stderr
    assert "already running" in second.stdout

    assert _artifact(project) is not None
    assert counter.read_text(encoding="utf-8").count("run") == 1


def test_wait_joins_a_live_run_and_returns_its_code(tmp_path):
    """``--wait`` при занятом замке ждёт и возвращает код прогона."""
    gate = tmp_path / "gate"
    project, counter = _setup(tmp_path, _gate_body(gate))
    assert _run(project, "--background").returncode == 3

    waiter = subprocess.Popen(
        ["bash", str(SCRIPT), "--wait", "--project", str(project)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(),
    )
    try:
        time.sleep(0.5)
        gate.write_text("go", encoding="utf-8")
        out, err = waiter.communicate(timeout=60)
    finally:
        gate.write_text("go", encoding="utf-8")
        if waiter.poll() is None:
            waiter.kill()
        _kill_leftover(project)

    assert waiter.returncode == 0, out + err
    assert "already running" in out
    assert "done" in out
    assert counter.read_text(encoding="utf-8").count("run") == 1


def test_force_is_refused_while_a_run_is_live(tmp_path):
    """``--force`` не должен становиться вторым прогоном поверх живого."""
    gate = tmp_path / "gate"
    project, counter = _setup(tmp_path, _gate_body(gate))
    try:
        assert _run(project, "--background").returncode == 3
        pid = _lock_pid(project)
        assert pid > 1, "the lock never named its owner"

        forced = _run(project, "--force")
        assert forced.returncode == 2, forced.stdout + forced.stderr
        assert "полный прогон уже идёт" in forced.stderr
        assert str(pid) in forced.stderr
    finally:
        _released(project, gate)
        _kill_leftover(project)
    assert counter.read_text(encoding="utf-8").count("run") == 1


def test_force_is_refused_when_the_tree_already_has_an_artifact(tmp_path):
    """Готовый артефакт на этот tree-sha — перезапуск не нужен."""
    project, counter = _setup(tmp_path, "echo '1 passed in 0.01s'\nexit 0\n")

    first = _run(project, "--wait")
    assert first.returncode == 0, first.stdout + first.stderr
    artifact = _artifact(project)
    assert artifact is not None

    forced = _run(project, "--force")
    assert forced.returncode == 2, forced.stdout + forced.stderr
    assert "полный прогон на это дерево уже есть" in forced.stderr
    assert artifact.name in forced.stderr
    assert counter.read_text(encoding="utf-8").count("run") == 1


def test_lock_of_a_killed_run_is_released(tmp_path):
    """Замок убитого прогона не блокирует следующий: его снимает ядро."""
    project, _counter = _setup(tmp_path, "sleep 30\necho '1 passed'\nexit 0\n")
    try:
        assert _run(project, "--background").returncode == 3
        pid = _lock_pid(project)
        assert pid > 1
        _kill_group(pid)
        time.sleep(0.5)

        status = _run(project, "--status")
        assert status.returncode == 4, status.stdout + status.stderr

        again = _run(project, "--wait", cmd="echo '1 passed in 0.01s'")
        assert again.returncode == 0, again.stdout + again.stderr
        assert "started" in again.stdout
    finally:
        _kill_leftover(project)


def test_runs_counts_starts_per_tree_sha(tmp_path):
    """``runs`` — цифра, а не «один прогон» на слово: 1, затем 2."""
    project, counter = _setup(tmp_path, "echo '1 passed in 0.01s'\nexit 0\n")

    before = _run(project, "--status")
    assert before.returncode == 4
    assert "runs 0" in before.stdout

    first = _run(project, "--wait")
    assert first.returncode == 0, first.stdout + first.stderr
    artifact = _artifact(project)
    assert artifact is not None
    assert json.loads(artifact.read_text(encoding="utf-8"))["runs"] == 1
    status = _run(project, "--status")
    assert "runs 1" in status.stdout

    artifact.unlink()
    second = _run(project, "--wait")
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(artifact.read_text(encoding="utf-8"))["runs"] == 2
    status = _run(project, "--status")
    assert "runs 2" in status.stdout
    assert counter.read_text(encoding="utf-8").count("run") == 2


def test_runs_counter_does_not_count_a_narrow_command(tmp_path):
    """``runs`` — про эту команду, а не про любой старт скрипта на дереве.

    Узкий прогон через ``AWF_SUITE_CMD`` — тоже старт, но не полный прогон:
    иначе канонический артефакт показывал бы ``runs 2`` после одного полного
    прогона, и вердикт читал бы это как двойной прогон.
    """
    project, counter = _setup(tmp_path, "echo '1 passed in 0.01s'\nexit 0\n")
    fake = str(tmp_path / "pytest")
    narrow_cmd = f"{fake} tests/unit/test_one.py"

    narrow = _run(project, "--wait", cmd=narrow_cmd)
    assert narrow.returncode == 0, narrow.stdout + narrow.stderr
    canonical = _run(project, "--wait")
    assert canonical.returncode == 0, canonical.stdout + canonical.stderr

    artifacts = {}
    for path in _context(project).glob("SUITE-*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        artifacts[data["command"]] = data

    assert artifacts[narrow_cmd]["runs"] == 1
    assert artifacts[fake]["runs"] == 1, "узкий старт не делает полный прогон вторым"
    assert counter.read_text(encoding="utf-8").count("run") == 2
    assert "runs 1" in _run(project, "--status").stdout


def test_template_ships_the_same_lock_implementation():
    """Шаблон для чужих проектов обязан совпадать со скриптом в репо."""
    assert TEMPLATE.read_bytes() == SCRIPT.read_bytes()


def test_force_in_every_mode_is_refused_while_a_run_is_live(tmp_path):
    """A75 #1: ``--force`` не обходит живой прогон ни в одном режиме."""
    gate = tmp_path / "gate"
    project, counter = _setup(tmp_path, _gate_body(gate))
    try:
        assert _run(project, "--background").returncode == 3
        pid = _lock_pid(project)
        assert pid > 1, "the lock never named its owner"
        for args in (("--background", "--force"), ("--wait", "--force")):
            forced = _run(project, *args)
            assert forced.returncode == 2, (args, forced.stdout + forced.stderr)
            assert "полный прогон уже идёт" in forced.stderr, args
            assert str(pid) in forced.stderr, args
    finally:
        _released(project, gate)
        _kill_leftover(project)
    assert counter.read_text(encoding="utf-8").count("run") == 1


def test_foreground_run_joins_the_live_one_instead_of_starting_a_second(tmp_path):
    """A75 #1 на историческом режиме: без флагов второй прогон не стартует."""
    gate = tmp_path / "gate"
    project, counter = _setup(tmp_path, _gate_body(gate))
    try:
        assert _run(project, "--background").returncode == 3
        gate.write_text("go", encoding="utf-8")
        foreground = _run(project)
        assert foreground.returncode == 0, foreground.stdout + foreground.stderr
        assert "cached" in foreground.stdout, foreground.stdout
    finally:
        gate.write_text("go", encoding="utf-8")
        _kill_leftover(project)
    assert counter.read_text(encoding="utf-8").count("run") == 1


def test_status_and_the_final_line_carry_the_run_counter(tmp_path):
    """A75 #3: ``runs`` видно и в ``--status`` живого прогона, и в итоговой строке."""
    gate = tmp_path / "gate"
    project, _counter = _setup(tmp_path, _gate_body(gate))
    try:
        assert _run(project, "--background").returncode == 3
        status = _run(project, "--status")
        deadline = time.monotonic() + 10
        while "runs 1" not in status.stdout and time.monotonic() < deadline:
            time.sleep(0.05)
            status = _run(project, "--status")
        assert status.returncode == 3, status.stdout + status.stderr
        assert "runs 1" in status.stdout, status.stdout
        gate.write_text("go", encoding="utf-8")
        artifact = _artifact(project)
    finally:
        gate.write_text("go", encoding="utf-8")
        _kill_leftover(project)
    assert artifact is not None
    assert "runs 1" in Path(f"{artifact}.log").read_text(encoding="utf-8")


def test_a_child_left_by_the_suite_does_not_hold_the_lock(tmp_path):
    """A75: fd замка не уезжает в команду сьюта — демон теста не запирает дерево."""
    daemon = tmp_path / "daemon.pid"
    body = "echo '1 passed in 0.01s'\nsleep 30 >/dev/null 2>&1 &\n"
    project, _counter = _setup(tmp_path, body + f"echo $! > {daemon}\nexit 0\n")
    try:
        assert _run(project, "--wait").returncode == 0
        # Другая команда промахивается мимо кэша и спрашивает замок: демон,
        # унаследовавший fd 9, держал бы дерево «занятым» навсегда.
        probe = _run(project, "--status", cmd="echo probe")
        assert probe.returncode == 4, probe.stdout + probe.stderr
    finally:
        try:
            os.kill(int(daemon.read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def test_background_force_is_refused_on_a_tree_that_already_has_an_artifact(tmp_path):
    """A75 #2 держится и в ``--background``, не только в историческом режиме."""
    project, counter = _setup(tmp_path, "echo '1 passed in 0.01s'\nexit 0\n")
    first = _run(project, "--wait")
    assert first.returncode == 0, first.stdout + first.stderr
    artifact = _artifact(project)
    assert artifact is not None
    forced = _run(project, "--background", "--force")
    assert forced.returncode == 2, forced.stdout + forced.stderr
    assert artifact.name in forced.stderr
    assert counter.read_text(encoding="utf-8").count("run") == 1
