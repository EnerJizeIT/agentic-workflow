"""REVIEW-0337: an auditor probe is evidence, not a claim about this repo.

A probe is a demonstration inside a scratch directory the auditor built
(``doctor --project-dir /tmp/…``). Rerunning it from the repository root
contradicts a claim nobody made. The optional ``cwd`` in a probe entry makes
it reproducible: an existing directory means "run it there"; anything else
keeps it out of the rerun and names it as a demonstration. The author's own
``tests_run`` stays strict — a false green claim is still a contradiction.
"""
from __future__ import annotations

import json
from pathlib import Path

from awf import verify_pack


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".agentic" / "outbox").mkdir(parents=True)
    (project / ".agentic" / "context").mkdir(parents=True)
    return project


def _claims(project: Path, name: str, tests_run: list[dict]) -> None:
    (project / ".agentic" / "outbox" / name).write_text(
        json.dumps({"tests_run": tests_run}), encoding="utf-8"
    )


def _done(project: Path, tests_run: list[dict]) -> None:
    _claims(project, "DONE-TODO-0001.json", tests_run)


def _probes(project: Path, tests_run: list[dict]) -> None:
    _claims(project, "PROBES-TODO-0001.json", tests_run)


def test_a_probe_without_cwd_is_named_not_rerun(tmp_path: Path) -> None:
    """The auditor's demo fails when rerun anywhere; it is not a lie."""
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'print(1)'", "result": "1 passed"}])
    _probes(project, [
        {"cmd": "python3 -c 'import sys; sys.exit(3)'", "result": "9 passed"},
    ])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.probe_mismatches == []
    assert report.probes_checked == 0
    assert report.probes_skipped == {"probe": 1}
    assert any("not rerun (demonstrations)" in line for line in report.lines())


def test_a_probe_with_a_missing_cwd_is_not_rerun(tmp_path: Path) -> None:
    """A scratch directory from another run is gone — still not a lie."""
    project = _project(tmp_path)
    _probes(project, [{
        "cmd": "python3 -c 'import sys; sys.exit(4)'",
        "result": "5 passed",
        "cwd": str(tmp_path / "gone-scratch-project"),
    }])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.probe_mismatches == []
    assert report.probes_skipped == {"probe": 1}
    section = verify_pack._done_json_section(project, "TODO-0001", project / ".agentic")
    assert section.status == "skipped"


def test_a_probe_with_an_existing_cwd_runs_there(tmp_path: Path) -> None:
    """``cwd`` is the opt-in: the command sees that directory, not the root."""
    project = _project(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "marker.txt").write_text("here", encoding="utf-8")
    _probes(project, [{
        "cmd": "python3 -c \"import pathlib, sys; "
               "sys.exit(0 if pathlib.Path('marker.txt').exists() else 1)\"",
        "result": "1 passed",
        "cwd": str(scratch),
    }])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.probe_mismatches == []
    assert report.probes_checked == 1


def test_a_probe_with_an_existing_cwd_that_lies_still_fails(
    tmp_path: Path,
) -> None:
    """Opting into the rerun brings the old strictness with it."""
    project = _project(tmp_path)
    _probes(project, [{
        "cmd": "python3 -c 'import sys; sys.exit(1)'",
        "result": "9 passed",
        "cwd": str(tmp_path),
    }])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(report.probe_mismatches) == 1
    assert report.probes_checked == 1


def test_the_author_failing_claim_is_still_a_contradiction(tmp_path: Path) -> None:
    """The relaxation is about probes only; the author stays accountable."""
    project = _project(tmp_path)
    _done(project, [
        {"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "12 passed"},
    ])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(report.mismatches) == 1
    assert "rc=1" in report.mismatches[0]


def test_a_claim_naming_a_directory_is_prose_not_a_lie(tmp_path: Path) -> None:
    """A path token that is a folder is a fact, not a program.

    ``_is_command`` accepted any existing path, so a claims line naming a
    directory (``tests/unit/ — 164 passed``) went to ``subprocess.run`` and
    the report accused the author of a contradiction the run never made:
    ``PermissionError: … the command could not run``. A folder is prose, the
    same as a word from a sentence.

    Lives here, not in ``test_done_claims.py``: that file already sits at the
    400-line ratchet budget (REVIEW-1 made the same move for the gate copy).
    """
    project = _project(tmp_path)
    (project / "tests" / "unit").mkdir(parents=True)
    _done(project, [{"cmd": "tests/unit/", "result": "164 passed"}])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.mismatches == []
    assert report.skipped == {"prose": 1}
