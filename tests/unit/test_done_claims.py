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

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(report.mismatches) == 1
    assert "claimed '12 passed'" in report.mismatches[0]
    assert "rc=1" in report.mismatches[0]


def test_a_true_green_claim_passes(tmp_path):
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'print(\"ok\")'", "result": "1 passed"}])

    assert verify_pack.check_done_claims(project, "TODO-0001").mismatches == []


def test_a_red_claim_is_not_a_contradiction(tmp_path):
    """A worker reporting a failure is honest; verification is not a rerun."""
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "3 failed"}])

    assert verify_pack.check_done_claims(project, "TODO-0001").mismatches == []


def test_an_unclear_claim_is_not_checked(tmp_path):
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "see log"}])

    assert verify_pack.check_done_claims(project, "TODO-0001").mismatches == []


def test_a_shell_line_is_skipped(tmp_path):
    """argv only: a pipeline cannot be replayed as one argv."""
    project = _project(tmp_path)
    _done(project, [{"cmd": "false && echo ok", "result": "1 passed"}])

    assert verify_pack.check_done_claims(project, "TODO-0001").mismatches == []


def test_a_missing_or_broken_file_is_silent(tmp_path):
    project = _project(tmp_path)
    assert verify_pack.check_done_claims(project, "TODO-0001").mismatches == []
    (project / ".agentic" / "outbox" / "DONE-TODO-0001.json").write_text(
        "{not json", encoding="utf-8"
    )
    assert verify_pack.check_done_claims(project, "TODO-0001").mismatches == []


def test_only_the_first_claims_are_rerun(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _done(
        project,
        [{"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "1 passed"}] * 8,
    )
    report = verify_pack.check_done_claims(project, "TODO-0001", limit=2)
    assert len(report.mismatches) == 2


def test_a_timeout_is_not_a_contradiction(tmp_path, monkeypatch):
    """AUD4: a claim that outlived the budget is "not verified", not a fail."""
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'import time; time.sleep(5)'", "result": "1 passed"}])
    report = verify_pack.check_done_claims(project, "TODO-0001", timeout=0.2)
    assert report.mismatches == []
    assert any("not verified" in line for line in report.unverified)


def test_the_report_counts_checked_and_skipped_claims(tmp_path):
    """AUD6: silence about skipped claims hides the unchecked half."""
    project = _project(tmp_path)
    _done(
        project,
        [
            {"cmd": "python3 -c 'print(1)'", "result": "1 passed"},
            {"cmd": "false && echo ok", "result": "1 passed"},
            {"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "see log"},
            {"cmd": "python3 -c 'print(2)'", "result": "1 passed"},
            {"cmd": "python3 -c 'print(3)'", "result": "1 passed"},
            {"cmd": "python3 -c 'print(4)'", "result": "1 passed"},
        ],
    )
    report = verify_pack.check_done_claims(project, "TODO-0001", limit=4)
    assert report.mismatches == []
    assert report.checked == 2
    summary = report.summary()
    assert "checked 2" in summary
    assert "1 shell line" in summary
    assert "1 claim that is not green" in summary
    assert "2 claims beyond the limit" in summary


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
        verify_pack,
        "check_done_claims",
        lambda *a, **kw: verify_pack.ClaimsReport(
            mismatches=["claimed '1 passed' but rc=1"], checked=1
        ),
    )

    section = verify_pack._done_json_section(project, "TODO-0001", project / ".agentic")

    assert section.status == "fail"
    assert any("do not survive" in line for line in section.lines)


def test_the_section_names_checked_and_skipped_claims(tmp_path):
    """AUD6: the section says how many claims were rerun and what was skipped."""
    project = _project(tmp_path)
    _done(
        project,
        [
            {"cmd": "python3 -c 'print(1)'", "result": "1 passed"},
            {"cmd": "false && echo ok", "result": "1 passed"},
        ],
    )

    section = verify_pack._done_json_section(project, "TODO-0001", project / ".agentic")

    assert section.status == "pass"
    assert any("checked 1" in line for line in section.lines)
    assert any("shell" in line for line in section.lines)


def test_a_contradicted_claim_is_counted_as_checked(tmp_path):
    """A claim the rerun answered is checked even when it contradicts.

    The count answers "how many claims were verified", not "how many were
    green": a report that says `checked 0` while printing a contradiction
    undercounts the work it actually did.
    """
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "1 passed"}])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(report.mismatches) == 1
    assert report.checked == 1
    assert "checked 1" in report.summary()


def test_an_unknown_skip_reason_is_refused_at_the_call():
    """The reason set is closed: a new reason needs a wording, not a KeyError."""
    report = verify_pack.ClaimsReport()

    try:
        report.skip("no-such-reason")
    except KeyError as e:
        assert "no-such-reason" in str(e)
    else:  # pragma: no cover - the assertion below explains the failure
        raise AssertionError("an unworded skip reason must be refused")
    assert report.skipped == {}


def test_the_claim_budget_is_the_owners_decision():
    """Owner decision (TODO-0321, A8): 120 s per claim, 600 s for the check.

    Both numbers bound the supervisor's wait: five claims at the old 300 s
    each kept verify blocked for 25 minutes, and the owner capped the whole
    check at ten. A budget that lives only in a report is not a budget.
    """
    assert verify_pack.CLAIM_TIMEOUT == 120
    assert verify_pack.CLAIM_TOTAL_TIMEOUT == 600


def test_a_claim_beyond_the_total_budget_is_skipped_not_failed(tmp_path):
    """A8: no budget left is "not checked", never a contradiction."""
    project = _project(tmp_path)
    _done(
        project,
        [
            {"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "1 passed"},
            {"cmd": "python3 -c 'print(2)'", "result": "1 passed"},
        ],
    )

    report = verify_pack.check_done_claims(project, "TODO-0001", total_timeout=0)

    assert report.mismatches == []
    assert report.checked == 0
    assert len(report.unverified) == 0
    assert report.skipped == {"budget": 2}
    assert "2 claims beyond the time budget" in report.summary()


def test_the_total_budget_stops_the_rerun_early(tmp_path):
    """The total budget caps the wait, not just each command.

    Two claims that each sleep past the remaining budget must not spend
    their own timeout twice: the check returns in about the total budget
    and names what it did not verify.
    """
    import time

    project = _project(tmp_path)
    _done(
        project,
        [
            {"cmd": "python3 -c 'import time; time.sleep(5)'", "result": "1 passed"},
            {"cmd": "python3 -c 'import time; time.sleep(5)'", "result": "1 passed"},
        ],
    )

    started = time.monotonic()
    report = verify_pack.check_done_claims(
        project, "TODO-0001", timeout=5, total_timeout=1
    )
    elapsed = time.monotonic() - started

    # Two 5 s commands under a 1 s budget: without the total budget the
    # check would spend 10 s, one per-command timeout after another.
    assert elapsed < 3, f"the check waited {elapsed:.1f}s for a 1s budget"
    assert report.mismatches == []
    assert report.checked == 0
    assert len(report.unverified) + report.skipped.get("budget", 0) == 2


def test_a_line_of_prose_is_skipped_not_blamed(tmp_path):
    """A27: "reused canonical suite artifact …" is a fact, not a command.

    It used to be rerun from the project root: FileNotFoundError, and the
    honest unit landed in "does not reproduce".
    """
    project = _project(tmp_path)
    _done(
        project,
        [
            {
                "cmd": "reused canonical suite artifact .agentic/context/SUITE-abc-1234.json (QA stage)",
                "result": "exit 0, 6494 passed (not rerun)",
            }
        ],
    )

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.mismatches == []
    assert report.skipped == {"prose": 1}
    assert "line that is not a command" in report.summary()


def test_a_real_failing_command_is_still_a_mismatch(tmp_path):
    """A27 did not soften the check: an existing command with rc≠0 lies."""
    project = _project(tmp_path)
    _done(
        project,
        [{"cmd": "python3 -c 'import sys; sys.exit(3)'", "result": "5 passed"}],
    )

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(report.mismatches) == 1
    assert report.skipped == {}
    assert report.checked == 1


def _probes(project: Path, tests_run: list[dict]) -> None:
    (project / ".agentic" / "outbox" / "PROBES-TODO-0001.json").write_text(
        json.dumps({"tests_run": tests_run}), encoding="utf-8"
    )


def test_auditor_probes_are_checked_and_counted_apart(tmp_path):
    """A27: PROBES is read too, and the report names it separately."""
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'print(1)'", "result": "1 passed"}])
    _probes(project, [{"cmd": "python3 -c 'print(2)'", "result": "1 passed"}])

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.mismatches == []
    assert report.probe_mismatches == []
    assert report.checked == 1
    assert report.probes_checked == 1
    assert "auditor probes — checked 1" in report.probes_summary()
    assert any("auditor probes" in line for line in report.lines())


def test_a_false_probe_is_a_contradiction_too(tmp_path):
    """A green probe that no longer reproduces fails the pack section."""
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'print(1)'", "result": "1 passed"}])
    _probes(project, [{"cmd": "python3 -c 'import sys; sys.exit(1)'", "result": "9 passed"}])

    report = verify_pack.check_done_claims(project, "TODO-0001")
    section = verify_pack._done_json_section(project, "TODO-0001", project / ".agentic")

    assert len(report.probe_mismatches) == 1
    assert report.mismatches == []
    assert section.status == "fail"
    assert any("probe:" in line for line in section.lines)


def test_the_pack_passes_a_unit_with_honest_auditor_probes(tmp_path):
    """Acceptance: DONE + a separate PROBES file give ``done_json: PASS``."""
    project = _project(tmp_path)
    _done(project, [{"cmd": "python3 -c 'print(1)'", "result": "1 passed"}])
    _probes(project, [{"cmd": "python3 -c 'print(2)'", "result": "1 passed"}])

    section = verify_pack._done_json_section(project, "TODO-0001", project / ".agentic")

    assert section.status == "pass"
    assert any("auditor probes — checked 1" in line for line in section.lines)


def test_an_audit_only_unit_still_checks_its_probes(tmp_path):
    """A unit with PROBES and no DONE: the probes are rerun and named; the
    section does not claim a DONE it never had."""
    project = _project(tmp_path)
    _probes(project, [{"cmd": "python3 -c 'print(1)'", "result": "1 passed"}])

    section = verify_pack._done_json_section(project, "TODO-0001", project / ".agentic")

    assert section.status == "skipped"
    assert any("no DONE-TODO-0001.json" in line for line in section.lines)
    assert any("auditor probes — checked 1" in line for line in section.lines)


def test_an_audit_only_unit_with_a_lying_probe_fails_the_section(tmp_path):
    """No author DONE is not a hiding place for a probe that does not reproduce."""
    project = _project(tmp_path)
    _probes(project, [{"cmd": "python3 -c 'import sys; sys.exit(2)'", "result": "7 passed"}])

    section = verify_pack._done_json_section(project, "TODO-0001", project / ".agentic")

    assert section.status == "fail"
    assert any("probe:" in line for line in section.lines)


def test_a_shell_builtin_line_is_named_a_shell_line_not_prose(tmp_path):
    """``cd x && pytest`` has no program as its first token, but it is a
    shell line — calling it "not a command" would misname an honest entry."""
    project = _project(tmp_path)
    _done(
        project,
        [{"cmd": "cd . && python3 -c 'print(1)'", "result": "1 passed"}],
    )

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.mismatches == []
    assert report.skipped == {"shell": 1}
