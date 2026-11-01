"""``commit_plan.preexisting_approval_status`` — A23 fingerprint rule.

The verify wait validates a pre-existing ``APPROVE-<id>.ready`` against the
current attempt before consuming it. A23 adds the fingerprint-only rule used
outside a run: the signal's ``verified_sha`` must match the recorded
``VERIFIED-<id>.sha`` and the current tree fingerprint.
"""
from __future__ import annotations

import json

from awf import api, commit_plan, git_utils

TODO = "TODO-0001"


def _project(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="CommitPlan")
    return tmp_git_repo


def _approve(proj) -> str:
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, TODO, verified_sha=fp)
    return fp


def test_matching_fingerprint_is_valid(tmp_git_repo):
    proj = _project(tmp_git_repo)
    _approve(proj)
    assert commit_plan.preexisting_approval_status(proj, TODO) == ("valid", "")


def test_missing_fingerprint_is_stale(tmp_git_repo):
    """A hand-made/legacy marker stays stale (AUD04-04 unchanged)."""
    proj = _project(tmp_git_repo)
    sig = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
    sig.write_text("", encoding="utf-8")
    status, reason = commit_plan.preexisting_approval_status(proj, TODO)
    assert status == "stale"
    assert reason


def test_verified_file_mismatch_is_stale(tmp_git_repo):
    proj = _project(tmp_git_repo)
    _approve(proj)
    (proj / ".agentic" / "context" / f"VERIFIED-{TODO}.sha").write_text(
        "1" * 64 + "\n", encoding="utf-8",
    )
    status, reason = commit_plan.preexisting_approval_status(proj, TODO)
    assert status == "stale"
    assert "verified" in reason.lower()


def test_moved_tree_is_stale(tmp_git_repo):
    proj = _project(tmp_git_repo)
    _approve(proj)
    (proj / "late.txt").write_text("moved\n", encoding="utf-8")
    status, reason = commit_plan.preexisting_approval_status(proj, TODO)
    assert status == "stale"
    assert "tree" in reason.lower()


def test_absent_signal(tmp_git_repo):
    proj = _project(tmp_git_repo)
    assert commit_plan.preexisting_approval_status(proj, TODO) == ("absent", "")


def test_retaken_baseline_is_stale(tmp_git_repo):
    """A23/QA: the same tree on a re-taken baseline is a different diff scope.

    ``tree_fingerprint`` contains HEAD but not ``BASELINE-<id>.sha`` (it
    lives under gitignored ``.agentic/``), so a fingerprint-only match must
    not consume an approval made against another baseline.
    """
    proj = _project(tmp_git_repo)
    baseline = proj / ".agentic" / "context" / f"BASELINE-{TODO}.sha"
    baseline.write_text("a" * 40 + "\n", encoding="utf-8")
    _approve(proj)
    baseline.write_text("b" * 40 + "\n", encoding="utf-8")

    status, reason = commit_plan.preexisting_approval_status(
        proj, TODO, commit_plan.read_baseline_sha(proj, TODO),
    )
    assert status == "stale"
    assert "baseline" in reason.lower()


def test_run_binding_outside_a_run_is_stale(tmp_git_repo):
    """A dead cycle's binding must not be revived by a matching fingerprint.

    Before A23 any binding was "only honored inside an active run"; the
    outside-run shape has no ``generation``. A run binding that lacks
    ``baseline_sha`` has no attempt identity left to check once the run is
    gone, so it stays stale.
    """
    proj = _project(tmp_git_repo)
    fp = _approve(proj)
    sig = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
    sig.write_text(
        json.dumps(
            {"generation": 7, "verified_sha": fp, "files_digest": "a" * 64},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    status, reason = commit_plan.preexisting_approval_status(proj, TODO)
    assert status == "stale"
    assert "run" in reason.lower()


def test_binding_without_baseline_is_stale(tmp_git_repo):
    """A hand-made signal that names only the tree has no attempt identity."""
    proj = _project(tmp_git_repo)
    fp = _approve(proj)
    sig = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
    sig.write_text(
        json.dumps({"verified_sha": fp}, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    status, reason = commit_plan.preexisting_approval_status(proj, TODO)
    assert status == "stale"
    assert "baseline" in reason.lower()
