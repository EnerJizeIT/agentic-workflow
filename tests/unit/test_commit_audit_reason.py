"""IMP3 (TODO-0343): ``COMMIT-AUDIT.reason`` for every refusal (T10).

Before this unit only the silent plan-skip carried a cause (``skip_reason``);
a refused/error commit left it in the log alone, so the brief could say
"refused" but not why. The artifact is the one place the fact must survive.
"""
from __future__ import annotations

from pathlib import Path

from awf import api, commit_gate, paths
from awf.commit_plan import (
    OUTCOME_COMMITTED,
    OUTCOME_ERROR,
    OUTCOME_REFUSED,
    OUTCOME_SKIPPED,
    CommitOutcome,
)

TODO = "TODO-0326"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "repo"
    (project / ".agentic" / "context").mkdir(parents=True)
    (project / ".agentic" / "logs").mkdir(parents=True)
    return project


def test_reason_is_written_for_every_status(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outcomes = [
        CommitOutcome(OUTCOME_COMMITTED, "", "abc1234"),
        CommitOutcome(OUTCOME_SKIPPED, "no changes since baseline"),
        CommitOutcome(OUTCOME_REFUSED, "sha mismatch"),
        CommitOutcome(OUTCOME_ERROR, "git command failed: timeout"),
    ]

    for outcome in outcomes:
        record = commit_gate._audit_record(TODO, outcome)
        assert record["reason"] == outcome.reason, (
            f"AC43: {outcome.status} must carry its own reason in COMMIT-AUDIT"
        )


def test_plan_skip_keeps_skip_reason_and_reason(tmp_path: Path) -> None:
    outcome = CommitOutcome(
        OUTCOME_REFUSED, "plan-skip-inconsistent: files match HEAD"
    )

    record = commit_gate._audit_record(TODO, outcome, skip_inconsistent=True)

    # Two fields, two readers: the RUN-REPORT keeps ``skip_reason``; the
    # brief reads ``reason``. One must not overwrite the other (RR54).
    assert record["reason"] == outcome.reason
    assert record["skip_reason"] == outcome.reason
    assert record["skip_inconsistent"] is True


def test_other_refusals_leave_skip_reason_empty() -> None:
    outcome = CommitOutcome(OUTCOME_REFUSED, "sha mismatch")

    record = commit_gate._audit_record(TODO, outcome)

    assert record["reason"] == "sha mismatch"
    assert record["skip_reason"] == ""


def test_artifact_round_trip_through_the_public_reader(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outcome = CommitOutcome(OUTCOME_REFUSED, "sha mismatch")
    record = commit_gate._audit_record(TODO, outcome)

    commit_gate._write_commit_audit_record(
        project, TODO, record, project / ".agentic" / "logs"
    )

    path = paths.context_dir(project) / f"COMMIT-AUDIT-{TODO}.json"
    assert path.is_file()
    read = api.read_commit_audit(project, TODO)
    assert read is not None
    assert read["status"] == "refused"
    assert read["reason"] == "sha mismatch"
    # The RUN-REPORT keeps reading the same artifact through the same reader.
    assert commit_gate.read_commit_audit(project, TODO) == read


def test_no_artifact_and_broken_artifact_degrade_to_none(tmp_path: Path) -> None:
    project = _project(tmp_path)

    assert api.read_commit_audit(project, TODO) is None

    path = paths.context_dir(project) / f"COMMIT-AUDIT-{TODO}.json"
    path.write_text("{not json", encoding="utf-8")
    assert api.read_commit_audit(project, TODO) is None
