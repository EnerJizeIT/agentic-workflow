"""A28: the contract gate runs `Gate:` commands with the project interpreter.

`docs/contracts/*.md` write `python3 -m pytest …`. Whatever `python3` PATH
names first used to answer — often the system interpreter without
pytest-timeout — so the `contracts` gate went red on a green tree and hid real
failures. The gate now resolves the interpreter the project names
(`verification.test_cmd`, or `AWF_PYTHON`) and substitutes that path into the
command. The tests below pin both halves: the foreign `python3` never answers,
and a missing project interpreter is a loud rc=2, not a red gate.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from awf import doctor
from tests.unit._gate_scripts import copy_gate_scripts

CONTRACT = "# Contract: x\n\nOwns: x\nPath: `x.py`\nNever: no eval\nGate: {gate}\n"


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _project(tmp_path: Path, gate: str, test_cmd: str) -> Path:
    """A minimal project the gate can run: one contract, one config, one awf."""
    project = tmp_path / "proj"
    (project / "docs" / "contracts").mkdir(parents=True)
    (project / ".agentic").mkdir()
    copy_gate_scripts(project)
    (project / "x.py").write_text("x = 1\n", encoding="utf-8")
    (project / "docs" / "contracts" / "x.md").write_text(
        CONTRACT.format(gate=gate), encoding="utf-8"
    )
    (project / ".agentic" / "config.yaml").write_text(
        yaml.safe_dump({"verification": {"test_cmd": test_cmd}}),
        encoding="utf-8",
    )
    # The gate finds `bin/awf` and asks it for the canonical suite command.
    _script(
        project / "bin" / "awf",
        f'#!/bin/sh\nexec "{sys.executable}" -m awf "$@"\n',
    )
    return project


def _gate_run(
    project: Path, path: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": path, "HOME": os.environ.get("HOME", "/tmp")}
    env.update(extra_env or {})
    return subprocess.run(
        ["sh", "scripts/check-contracts.sh"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture()
def foreign_python3(tmp_path: Path) -> tuple[Path, Path]:
    """A `python3` that claims the gate failed: it must never answer."""
    log = tmp_path / "foreign.log"
    shim = tmp_path / "foreign"
    _script(
        shim / "python3",
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pytest" ]; then exit 1; fi\n'
        "exit 1\n",
    )
    return shim, log


def _project_interpreter(project: Path, log: Path) -> str:
    _script(
        project / "bin" / "pybin",
        "#!/bin/sh\n" f'echo "$@" >> "{log}"\n' "exit 0\n",
    )
    return "./bin/pybin -m pytest -q"


def test_gate_uses_the_project_interpreter_not_the_first_python3(
    tmp_path: Path, foreign_python3: tuple[Path, Path]
) -> None:
    shim, foreign_log = foreign_python3
    project = _project(
        tmp_path, "python3 -m pytest -q", "./bin/pybin -m pytest -q"
    )
    log = tmp_path / "project.log"
    _project_interpreter(project, log)

    cp = _gate_run(project, f"{shim}:/usr/bin:/bin")

    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "-m pytest -q" in log.read_text(encoding="utf-8")
    assert not foreign_log.exists(), "the gate ran the foreign python3"


def test_hostile_python3_does_not_silence_the_source_cli(
    tmp_path: Path, foreign_python3: tuple[Path, Path]
) -> None:
    """`bin/awf` runs under `python3` from PATH — a hostile one hides it.

    The gate must fall through to the installed `awf` (absolute interpreter in
    its shebang) instead of silently dropping to the same hostile `python3`.
    """
    shim, foreign_log = foreign_python3
    project = _project(
        tmp_path, "python3 -m pytest -q", "./bin/pybin -m pytest -q"
    )
    log = tmp_path / "project.log"
    _project_interpreter(project, log)
    # The repo's own wrapper, not the fixture's absolute-interpreter one.
    _script(project / "bin" / "awf", "#!/bin/sh\nexec python3 -m awf \"$@\"\n")
    _script(
        shim / "awf",
        f'#!/bin/sh\nexec "{sys.executable}" -m awf "$@"\n',
    )

    cp = _gate_run(project, f"{shim}:/usr/bin:/bin")

    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "-m pytest -q" in log.read_text(encoding="utf-8")
    # The probe may run it for `bin/awf --help`; it must never run a gate.
    assert "pytest" not in foreign_log.read_text(encoding="utf-8")


def test_gate_asks_through_python_dash_m_when_no_awf_cli(
    tmp_path: Path,
) -> None:
    """No CLI on PATH, package importable: the gate still asks the project.

    Falling straight to `python3` here would re-open the A28 hole in a project
    that runs awf as `python3 -m awf`.
    """
    project = _project(
        tmp_path, "python3 -m pytest -q", "./bin/pybin -m pytest -q"
    )
    log = tmp_path / "project.log"
    _project_interpreter(project, log)
    (project / "bin" / "awf").unlink()
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").symlink_to(sys.executable)

    cp = _gate_run(project, f"{shim}:/usr/bin:/bin")

    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "-m pytest -q" in log.read_text(encoding="utf-8")


def test_a_missing_project_interpreter_is_rc_2_with_the_reason(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path, "python3 -m pytest -q", "/nonexistent/python -m pytest -q"
    )

    cp = _gate_run(
        project, "/usr/bin:/bin", {"AWF_PYTHON": "/nonexistent/python"}
    )

    assert cp.returncode == 2, cp.stdout + cp.stderr
    assert "интерпретатор" in cp.stderr
    assert "/nonexistent/python" in cp.stderr


def test_a_missing_interpreter_from_test_cmd_is_named(tmp_path: Path) -> None:
    project = _project(
        tmp_path, "python3 -m pytest -q", "./bin/missing -m pytest -q"
    )

    cp = _gate_run(project, "/usr/bin:/bin")

    assert cp.returncode == 2, cp.stdout + cp.stderr
    assert str(project / "bin" / "missing") in cp.stderr


def test_doctor_names_the_interpreter_the_gate_will_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, "true", "./bin/pybin -m pytest -q")
    monkeypatch.setenv("AWF_PYTHON", "/opt/py")

    report = doctor.run_doctor(project)

    lines = [line for line in report.lines if line.startswith(("ok", "WARN"))]
    gate_lines = [line for line in lines if "contracts gate" in line]
    assert len(gate_lines) == 1, "\n".join(report.lines)
    assert "/opt/py" in gate_lines[0]


def test_doctor_has_nothing_to_say_without_a_gate_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project without ``scripts/check-contracts.sh`` has no gate to describe.

    ``awf init`` does not ship the gate, so most projects are in this state.
    A line promising "Gate: commands run through …" would describe a script
    that never runs.
    """
    project = _project(tmp_path, "true", "./bin/pybin -m pytest -q")
    (project / "scripts" / "check-contracts.sh").unlink()
    monkeypatch.setenv("AWF_PYTHON", "/opt/py")

    report = doctor.run_doctor(project)

    assert not [line for line in report.lines if "contracts gate" in line]


def test_doctor_resolves_a_relative_interpreter_against_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate runs from the project root; the doctor may run from anywhere.

    Checking ``./bin/py`` against the doctor's cwd warned "not found" while
    the gate accepted the same relative path — the false WARN the doctor
    exists to prevent.
    """
    project = _project(tmp_path, "true", "true")
    interp = _script(project / "bin" / "py", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("AWF_PYTHON", "./bin/py")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    report = doctor.run_doctor(project)

    gate_lines = [line for line in report.lines if "contracts gate" in line]
    assert len(gate_lines) == 1, "\n".join(report.lines)
    assert "WARN" not in gate_lines[0]
    assert str(interp) in gate_lines[0]


def test_interpreter_path_with_a_space_is_one_shell_word(tmp_path: Path) -> None:
    """A28: the substituted path must survive `sh -c`, not only `sed`.

    Gate commands run through `sh -c`. Escaping `&`/`#` for the sed
    replacement is not enough: an unquoted word splits at the space, so a
    project under "My Projects" (a venv inside the tree is normal) runs
    `/home/u/My` and the gate goes red on a green tree — the failure A28
    removes.
    """
    project = _project(
        tmp_path, "python3 -m pytest -q", "python3 -m pytest -q"
    )
    spaced = tmp_path / "My Projects" / "a&b#c"
    log = tmp_path / "spaced.log"
    interp = _script(
        spaced / "py",
        "#!/bin/sh\n" f'echo "$@" >> "{log}"\n' "exit 0\n",
    )

    cp = _gate_run(
        project, "/usr/bin:/bin", {"AWF_PYTHON": str(interp)}
    )

    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "-m pytest -q" in log.read_text(encoding="utf-8")


def test_doctor_warns_when_the_named_interpreter_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doctor must not promise `ok` for a gate that will refuse (rc=2)."""
    project = _project(tmp_path, "true", "./bin/pybin -m pytest -q")
    monkeypatch.setenv("AWF_PYTHON", "/nonexistent/python")

    report = doctor.run_doctor(project)

    gate_lines = [line for line in report.lines if "contracts gate" in line]
    assert len(gate_lines) == 1, "\n".join(report.lines)
    assert "WARN" in gate_lines[0]
    assert "rc=2" in gate_lines[0]
    assert report.rc == 1


def test_a_directory_named_as_the_interpreter_is_rc_2(tmp_path: Path) -> None:
    """A30: `-x` is true for a directory, so a config typo passed the check.

    `AWF_PYTHON=/path/to/venv/bin` (the folder, not the interpreter) used to
    reach the gates: `sh: /path: Permission denied`, and the run blamed the
    contract. "Not an interpreter" must be the loud rc=2, not a red gate.
    """
    project = _project(tmp_path, "python3 -m pytest -q", "true")
    folder = tmp_path / "venv-bin"
    folder.mkdir()

    cp = _gate_run(project, "/usr/bin:/bin", {"AWF_PYTHON": str(folder)})

    assert cp.returncode == 2, cp.stdout + cp.stderr
    assert "интерпретатор" in cp.stderr
    assert str(folder) in cp.stderr
    assert "FAILED" not in cp.stdout


def test_doctor_warns_when_the_path_is_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doctor's predicate is the gate's predicate — a folder is not one."""
    project = _project(tmp_path, "true", "./bin/pybin -m pytest -q")
    folder = tmp_path / "venv-bin"
    folder.mkdir()
    monkeypatch.setenv("AWF_PYTHON", str(folder))

    report = doctor.run_doctor(project)

    gate_lines = [line for line in report.lines if "contracts gate" in line]
    assert len(gate_lines) == 1, "\n".join(report.lines)
    assert "WARN" in gate_lines[0]
