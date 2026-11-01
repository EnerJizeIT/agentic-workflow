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
