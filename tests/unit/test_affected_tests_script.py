"""A21 wrapper: `scripts/affected-tests.sh` asks the project, not the shell.

The selector needs the interpreter that has pytest's plugins, and that
interpreter lives in the project's `verification.test_cmd`. The script used
to hardcode `python3`; a wrong interpreter means "empty selection" and a
skipped regression, so the lookup is pinned here with a fake interpreter:
its marker proves which binary the script actually ran.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.unit._gate_scripts import copy_scripts

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "affected-tests.sh"


def _project_with_fake_interpreter(tmp_path: Path, command: str) -> Path:
    project = tmp_path / "proj"
    (project / ".agentic").mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "verification:\n  test_cmd: " + command + "\n", encoding="utf-8"
    )
    fake = project / "bin" / "fake-python"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$FAKE_PYTHON_LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return project


def _run(project: Path, tmp_path: Path, cwd: Path) -> subprocess.CompletedProcess:
    """Run the script as a foreign caller would: `python3` is the test venv.

    The script asks `python3 -m awf suite-command` about the project, so the
    shim keeps that question answerable wherever `awf` is installed.
    """
    log = tmp_path / "fake.log"
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    (shim / "python3").symlink_to(sys.executable)
    return subprocess.run(
        ["bash", str(SCRIPT), "--project", str(project), "--explain"],
        cwd=cwd,
        env={"PATH": f"{shim}:/usr/bin:/bin", "FAKE_PYTHON_LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_relative_interpreter_is_resolved_against_the_project(tmp_path):
    """`./bin/fake-python -m pytest` runs from any caller directory.

    `--project` exists precisely because the caller and the project differ;
    a relative interpreter resolved against the caller would silently pick
    nothing to run.
    """
    project = _project_with_fake_interpreter(
        tmp_path, "./bin/fake-python -m pytest tests/ -q"
    )

    result = _run(project, tmp_path, cwd=REPO_ROOT)

    log = tmp_path / "fake.log"
    assert log.is_file(), result.stderr
    assert "-m awf.test_selection" in log.read_text(encoding="utf-8")
    # The fake prints no files: the script must refuse, not pretend.
    assert result.returncode == 2
    assert "empty selection" in result.stderr


def test_an_absolute_interpreter_is_used_as_written(tmp_path):
    """A config path is not re-rooted: `/…/python` stays `/…/python`."""
    fake = tmp_path / "python-with-dots"
    fake.write_text(
        '#!/bin/sh\necho "$@" >> "$FAKE_PYTHON_LOG"\nexit 0\n', encoding="utf-8"
    )
    fake.chmod(0o755)
    project = _project_with_fake_interpreter(
        tmp_path, f"{fake} -m pytest tests/ -q"
    )

    result = _run(project, tmp_path, cwd=REPO_ROOT)

    log = tmp_path / "fake.log"
    assert log.is_file(), result.stderr
    assert "-m awf.test_selection" in log.read_text(encoding="utf-8")
    assert result.returncode == 2


def test_a_missing_interpreter_names_the_reason(tmp_path):
    """AUD8: a missing interpreter is not an "empty selection"."""
    project = _project_with_fake_interpreter(
        tmp_path, "./bin/missing-python -m pytest tests/ -q"
    )

    result = _run(project, tmp_path, cwd=REPO_ROOT)

    assert result.returncode == 2
    assert "missing-python" in result.stderr
    assert "не найден" in result.stderr
    assert "empty selection" not in result.stderr


def _project_with_tests(tmp_path: Path) -> Path:
    """A real git project with one passing test — enough for `--run`."""
    project = tmp_path / "runproj"
    (project / ".agentic").mkdir(parents=True)
    (project / "tests" / "unit").mkdir(parents=True)
    (project / "tests" / "unit" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (project / ".agentic" / "config.yaml").write_text(
        "verification:\n  test_cmd: ./bin/fake-python -m pytest tests/ -q\n",
        encoding="utf-8",
    )
    fake = project / "bin" / "fake-python"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$FAKE_PYTHON_LOG"\n'
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
    (project / "tests" / "unit" / "test_ok.py").write_text(
        "def test_ok():\n    assert 1\n", encoding="utf-8"
    )
    return project


def _run_with_args(project: Path, tmp_path: Path, args: list[str]) -> subprocess.CompletedProcess:
    log = tmp_path / "fake.log"
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    (shim / "python3").symlink_to(sys.executable)
    return subprocess.run(
        ["bash", str(SCRIPT), "--project", str(project), *args],
        cwd=REPO_ROOT,
        env={"PATH": f"{shim}:/usr/bin:/bin", "FAKE_PYTHON_LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=120,
    )


def _pytest_line(log: Path) -> str:
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if "-m pytest" in ln]
    assert lines, log.read_text(encoding="utf-8")
    return lines[-1]


def test_run_adds_parallelism_when_the_caller_passed_none(tmp_path):
    """A21: the full suite in one thread is 25 minutes and over budget."""
    project = _project_with_tests(tmp_path)

    result = _run_with_args(project, tmp_path, ["--run", "--", "-q"])

    assert result.returncode == 0, result.stderr
    assert "-n auto" in _pytest_line(tmp_path / "fake.log")


def test_run_keeps_the_callers_parallelism(tmp_path):
    project = _project_with_tests(tmp_path)

    result = _run_with_args(project, tmp_path, ["--run", "--", "-q", "-n", "0"])

    assert result.returncode == 0, result.stderr
    line = _pytest_line(tmp_path / "fake.log")
    assert "-n 0" in line
    assert "auto" not in line


def test_no_awf_cli_still_asks_through_python_dash_m(tmp_path):
    """A28: a project with the package but no `awf` script keeps its interpreter.

    The selector used to fall back to `python -m awf` when no CLI answered;
    skipping that branch demoted a configured interpreter to `python3` from
    PATH — the exact failure the contract gate just stopped having.
    """
    project = _project_with_fake_interpreter(
        tmp_path, "./bin/fake-python -m pytest tests/ -q"
    )
    copied = tmp_path / "copy" / "scripts"
    copy_scripts(copied, "affected-tests.sh")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").symlink_to(sys.executable)
    log = tmp_path / "fake.log"

    result = subprocess.run(
        ["bash", str(copied / "affected-tests.sh"), "--project", str(project)],
        cwd=project,
        env={"PATH": f"{shim}:/usr/bin:/bin", "FAKE_PYTHON_LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert "-m awf.test_selection" in log.read_text(encoding="utf-8")
    assert result.returncode == 2, result.stderr  # empty selection, not a crash


def test_a_missing_awf_python_names_the_interpreter_not_the_cli(tmp_path):
    """AUD8: a typo in AWF_PYTHON must not read as a missing `awf` CLI.

    The CLI probe runs the interpreter too, so it has to come after the
    interpreter check: otherwise the real reason (a bad path) hides behind
    "awf CLI not found" and the caller fixes the wrong thing.
    """
    project = _project_with_fake_interpreter(
        tmp_path, "./bin/fake-python -m pytest tests/ -q"
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "--project", str(project), "--explain"],
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "AWF_PYTHON": "./bin/missing-python"},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 2, result.stderr
    assert "missing-python" in result.stderr
    assert "AWF_PYTHON" in result.stderr
    assert "не найден" in result.stderr
    assert "CLI not found" not in result.stderr


def test_run_leaves_no_selection_stderr_file_behind(tmp_path):
    """AUD8 regression: ``--run`` execs pytest, so an EXIT trap never fires.

    The selector's stderr scratch file exists to be read once; after the
    exec it has no owner and no cleanup — one stray file per run in the
    caller's TMPDIR.
    """
    project = _project_with_tests(tmp_path)
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    log = tmp_path / "fake.log"
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    (shim / "python3").symlink_to(sys.executable)

    result = subprocess.run(
        ["bash", str(SCRIPT), "--project", str(project), "--run", "--", "-q"],
        cwd=REPO_ROOT,
        env={
            "PATH": f"{shim}:/usr/bin:/bin",
            "FAKE_PYTHON_LOG": str(log),
            "TMPDIR": str(tmpdir),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert list(tmpdir.glob("awf-affected.*")) == []
