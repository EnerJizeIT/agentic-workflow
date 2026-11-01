"""A8: DONE.json claims a green run — verify reruns it instead of trusting.

The narrow promise: a report may not say "12 passed" about a command that
does not pass. Red claims, unclear claims, shell lines and a missing file are
declarations, not contradictions, and keep today's behaviour.
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


def _done(project: Path, tests_run: list[dict], todo: str = "TODO-0001") -> None:
    (project / ".agentic" / "outbox" / f"DONE-{todo}.json").write_text(
        json.dumps({"tests_run": tests_run}), encoding="utf-8"
    )


def test_a_false_green_claim_is_caught(tmp_path):
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "12 passed"}])

    mismatches = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(mismatches) == 1
    assert "claimed '12 passed'" in mismatches[0]
    assert "rc=1" in mismatches[0]


def test_a_true_green_claim_passes(tmp_path):
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'print(\"ok\")'", "result": "1 passed"}])

    assert verify_pack.check_done_claims(project, "TODO-0001") == []


def test_a_red_claim_is_not_a_contradiction(tmp_path):
    """A worker reporting a failure is honest; verification is not a rerun."""
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "3 failed"}])

    assert verify_pack.check_done_claims(project, "TODO-0001") == []


def test_an_unclear_claim_is_not_checked(tmp_path):
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "see log"}])

    assert verify_pack.check_done_claims(project, "TODO-0001") == []


def test_a_shell_line_is_skipped(tmp_path):
    """argv only: a pipeline cannot be replayed as one argv."""
    project = _project(tmp_path)
    _done(project, [{"cmd": "false && echo ok", "result": "1 passed"}])

    assert verify_pack.check_done_claims(project, "TODO-0001") == []


def test_a_missing_or_broken_file_is_silent(tmp_path):
    project = _project(tmp_path)
    assert verify_pack.check_done_claims(project, "TODO-0001") == []
    (project / ".agentic" / "outbox" / "DONE-TODO-0001.json").write_text(
        "{not json", encoding="utf-8"
    )
    assert verify_pack.check_done_claims(project, "TODO-0001") == []


def test_only_the_first_claims_are_rerun(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _done(
        project,
        [{"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "1 passed"}] * 8,
    )
    mismatches = verify_pack.check_done_claims(project, "TODO-0001", limit=2)
    assert len(mismatches) == 2


def test_a_timeout_is_a_contradiction(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'import time; time.sleep(5)'", "result": "1 passed"}])
    mismatches = verify_pack.check_done_claims(project, "TODO-0001", timeout=0.2)
    assert len(mismatches) == 1
    assert "did not finish" in mismatches[0]


def test_approve_refuses_a_false_claim(tmp_path):
    from awf.api import pipeline as api_pipeline

    project = _project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text("project:\n  name: x\n")
    _done(project, [{"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "7 passed"}])

    result = api_pipeline.approve_commit(project, "TODO-0001")

    assert result.signal_file == ""
    assert "does not reproduce" in result.message
    assert not (project / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready").exists()


def test_the_section_marks_a_contradiction_as_fail(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _done(project, [{"cmd": "true", "result": "1 passed"}])
    monkeypatch.setattr(
        verify_pack, "check_done_claims", lambda *a, **kw: ["claimed '1 passed' but rc=1"]
    )

    section = verify_pack._done_json_section(project, "TODO-0001", project / ".agentic")

    assert section.status == "fail"
    assert any("do not survive" in line for line in section.lines)
