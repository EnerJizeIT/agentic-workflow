"""A65: a claim that cannot be repeated from the root names the next step.

The rule is deliberately small (round 3): a hint is cosmetics for a gate
that keeps failing, so it must not try to read a command like a shell. Two
exact conditions only — ``rc=5`` (pytest collected nothing) and one of the
four named forms that declares a working directory outside the project.
Every other path is data: ``--junitxml /tmp/x.xml`` and ``cp x /tmp`` do
not make a repeatable run a demonstration.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from awf import verify_pack


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".agentic" / "outbox").mkdir(parents=True)
    return project


def _done(project: Path, tests_run: list[dict], todo: str = "TODO-0001") -> None:
    (project / ".agentic" / "outbox" / f"DONE-{todo}.json").write_text(
        json.dumps({"tests_run": tests_run}), encoding="utf-8"
    )


def _red_project(tmp_path: Path) -> Path:
    project = _project(tmp_path)
    demo = project / "demo"
    demo.mkdir()
    (demo / "red.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    return project


# --- rc=5: pytest collected nothing ----------------------------------------


def test_pytest_with_nothing_collected_points_at_probes(tmp_path):
    """rc=5 means "no tests collected": the command proves nothing."""
    project = _project(tmp_path)
    _done(project, [{
        "cmd": f"{sys.executable} -m pytest -q -p no:cacheprovider",
        "result": "1 passed",
    }])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(report.mismatches) == 1
    assert "rc=5" in report.mismatches[0]
    assert "PROBES-" in report.mismatches[0]
    assert "демонстрация" in report.mismatches[0]


@pytest.mark.parametrize("wrapper", ["time", "nice", "command", "timeout 30"])
def test_exit_five_points_at_probes_behind_any_wrapper(tmp_path, wrapper):
    """The rule reads the exit code, not the command: a wrapper cannot hide 5."""
    project = _project(tmp_path)
    cmd = f"{wrapper} {sys.executable} -m pytest -q"

    assert "PROBES-" in verify_pack._claim_hint(cmd, 5, project)


def test_exit_five_is_read_without_any_program_analysis(tmp_path):
    """Round 3: rc=5 is the whole condition — no pytest guess, no false negative."""
    project = _project(tmp_path)

    assert "PROBES-" in verify_pack._claim_hint("some-program --flag", 5, project)


# --- declared working directory outside the project ------------------------


@pytest.mark.parametrize(
    "form",
    [
        "cd {dir} && pytest -q",
        "env -C {dir} pytest -q",
        "uv run --directory {dir} pytest -q",
        "make -C {dir} test",
    ],
)
def test_a_declared_cwd_outside_the_project_points_at_probes(tmp_path, form):
    """The four named forms, and only the directory position of each."""
    project = _project(tmp_path)
    outside = tmp_path / "archive"
    outside.mkdir()

    assert "PROBES-" in verify_pack._claim_hint(form.format(dir=outside), 1, project)


@pytest.mark.parametrize(
    "form",
    [
        "cd {dir} && pytest -q",
        "env -C {dir} pytest -q",
        "uv run --directory {dir} pytest -q",
        "make -C {dir} test",
    ],
)
def test_a_declared_cwd_inside_the_project_is_replayable(tmp_path, form):
    """Same forms, a directory the root can run in: no hint."""
    project = _project(tmp_path)

    assert verify_pack._claim_hint(form.format(dir=project), 1, project) == ""


def test_a_cd_outside_the_project_reaches_the_skipped_claims(tmp_path):
    """A shell line is skipped, not rerun — the hint still has to be there."""
    project = _project(tmp_path)
    outside = tmp_path / "archive"
    outside.mkdir()
    _done(project, [{"cmd": f"cd {outside} && pytest -q", "result": "1 passed"}])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.skipped == {"shell": 1}
    assert any("PROBES-" in line for line in report.hints)
    assert any("PROBES-" in line for line in report.lines())


def test_a_symlink_loop_is_outside_without_raising(tmp_path):
    """A loop cannot be a working directory: hint, never an exception."""
    project = _project(tmp_path)
    loop = tmp_path / "loop"
    os.symlink(loop, loop)

    assert "PROBES-" in verify_pack._claim_hint(f"cd {loop} && pytest -q", 1, project)


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest --junitxml /tmp/opencode/report.xml -q",
        "cp report.txt /tmp/opencode",
        "cat /tmp/opencode/suite.log",
        f"{sys.executable} -m pytest tests/unit -q -o /tmp/opencode",
        f"{sys.executable} -m pytest tests/unit -q > /tmp/opencode/run.log 2>&1",
    ],
)
def test_other_paths_are_data_not_a_demonstration(tmp_path, cmd):
    """A path in the command is not a claim about where the run happens."""
    project = _project(tmp_path)

    assert verify_pack._claim_hint(cmd, 1, project) == ""


def test_a_redirected_claim_keeps_the_hint_out_of_the_report(tmp_path):
    """A log after ``>`` is data, not a declared cwd: shell skip, no hint."""
    project = _project(tmp_path)
    log = tmp_path / "archive" / "run.log"
    _done(project, [{
        "cmd": f"{sys.executable} -c 'print(1)' > {log} 2>&1",
        "result": "1 passed",
    }])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.skipped == {"shell": 1}
    assert report.hints == []


# --- the old behaviour that must stay --------------------------------------


def test_an_ordinary_red_command_keeps_the_old_message(tmp_path):
    """A real red run inside the project is a contradiction, not a demo."""
    project = _red_project(tmp_path)
    _done(project, [{
        "cmd": f"{sys.executable} demo/red.py",
        "result": "1 passed",
    }])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(report.mismatches) == 1
    assert "claimed '1 passed' but rc=1" in report.mismatches[0]
    assert "PROBES-" not in report.mismatches[0]
    assert report.hints == []


def test_the_interpreter_outside_the_project_is_not_a_demonstration(tmp_path):
    """The shared venv python lives outside: it is the program, not the data."""
    project = _project(tmp_path)
    _done(project, [{
        "cmd": f"{sys.executable} -c 'import sys; sys.exit(1)'",
        "result": "1 passed",
    }])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(report.mismatches) == 1
    assert "PROBES-" not in report.mismatches[0]


def test_an_env_wrapped_interpreter_stays_the_program(tmp_path):
    """``env VAR=val /venv/python …`` is repeatable: no declared cwd, no hint."""
    project = _project(tmp_path)
    _done(project, [{
        "cmd": f"env PYTHONPATH=. {sys.executable} -c 'import sys; sys.exit(1)'",
        "result": "1 passed",
    }])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(report.mismatches) == 1
    assert "rc=1" in report.mismatches[0]
    assert "PROBES-" not in report.mismatches[0]
    assert report.hints == []


@pytest.mark.parametrize("prefix", ["timeout 30", "env -u PYTHONPATH"])
def test_a_wrapped_red_run_stays_a_contradiction(tmp_path, prefix):
    """R1 regression: a wrapper does not turn a repeatable red run into a demo."""
    project = _red_project(tmp_path)
    _done(project, [{
        "cmd": f"{prefix} {sys.executable} demo/red.py",
        "result": "1 passed",
    }])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(report.mismatches) == 1
    assert "rc=1" in report.mismatches[0]
    assert "PROBES-" not in report.mismatches[0]
    assert report.hints == []


def test_a_bare_foreign_path_is_data_not_a_declared_cwd(tmp_path):
    """Round 3: a lone ``/tmp/…`` line is not one of the four forms."""
    project = _project(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    _done(project, [{"cmd": str(archive / "suite.log"), "result": "5 passed"}])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.skipped == {"prose": 1}
    assert report.hints == []


def test_a_hint_never_breaks_the_rerun(tmp_path, monkeypatch):
    """Cosmetics: any failure inside the rule is an empty hint, not a crash."""
    project = _project(tmp_path)

    def boom(_words):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(verify_pack.command_parse, "split_argv", boom)

    assert verify_pack._claim_hint("pytest -q", 1, project) == ""


def test_a_probe_is_not_told_to_move_into_probes(tmp_path):
    """A probe already lives in PROBES: the hint would point at itself."""
    project = _project(tmp_path)
    outside = tmp_path / "scratch"
    outside.mkdir()
    (project / ".agentic" / "outbox" / "PROBES-TODO-0001.json").write_text(
        json.dumps({"tests_run": [{
            "cmd": f"env -C {outside} pytest -q",
            "result": "3 passed",
        }]}),
        encoding="utf-8",
    )

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.probes_skipped == {"probe": 1}
    assert report.hints == []
    assert not any("PROBES-" in line for line in report.lines())


def test_the_gate_still_fails_a_demonstration(tmp_path):
    """The hint is advice; the section keeps its FAIL."""
    project = _project(tmp_path)
    outside = tmp_path / "archive"
    outside.mkdir()
    _done(project, [{
        "cmd": (
            f"env -C {outside} {sys.executable} -c 'import sys; sys.exit(1)'"
        ),
        "result": "1 passed",
    }])

    section = verify_pack._done_json_section(project, "TODO-0001", project / ".agentic")

    assert section.status == "fail"
    assert any("PROBES-" in line for line in section.lines)


@pytest.mark.parametrize(
    "form",
    ["cd ../archive && pytest -q", "env -C ../archive pytest -q"],
)
def test_a_relative_declared_cwd_is_anchored_to_the_project(tmp_path, form):
    """A relative directory is read from the root, the way the rerun reads it."""
    project = _project(tmp_path)
    (tmp_path / "archive").mkdir()

    assert "PROBES-" in verify_pack._claim_hint(form, 1, project)
