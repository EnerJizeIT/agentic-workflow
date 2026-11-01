"""A23: approve must work without a manual ``touch``.

Incident 2026-11-01: outside a run ``awf_approve(verified_sha=...)`` published
an EMPTY ``APPROVE-<id>.ready`` marker. The verify wait accepts a decision
signal only when it is newer than the wait start (AUD04-04) and validates a
pre-existing one against the current attempt
(``commit_plan.preexisting_approval_status``). An empty marker carries no
binding, so a signal written before the wait started was ignored and the
supervisor had to ``touch`` the file by hand.

The fix: the approve writes the verified fingerprint into the signal, and the
validator accepts it while the recorded ``VERIFIED-<id>.sha`` and the current
tree fingerprint still match — no mtime requirement, no manual touch.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from awf import api, git_utils
from awf.supervisor import wait_for_supervisor_signal

TODO = "TODO-0001"


def _project(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="A23")
    return tmp_git_repo


def _approve(proj) -> str:
    """Approve the current tree and return the fingerprint it pinned."""
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, TODO, verified_sha=fp)
    return fp


def _age_signal(proj) -> None:
    """Make the signal look like it was written before the wait started."""
    sig = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
    old = time.time() - 3600
    os.utime(sig, (old, old))


def _wait(proj, timeout: int = 10) -> str:
    return wait_for_supervisor_signal(
        "verify", TODO, proj, proj / ".agentic" / "logs", timeout=timeout,
    )


class TestApproveCarriesVerifiedFingerprint:
    def test_approve_writes_verified_fingerprint_into_signal(self, tmp_git_repo):
        """The signal itself names the tree it approved (outside a run too)."""
        proj = _project(tmp_git_repo)
        fp = _approve(proj)
        sig = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
        binding = json.loads(sig.read_text(encoding="utf-8"))
        assert binding.get("verified_sha") == fp


class TestPreexistingApproveBeforeWait:
    def test_approve_written_before_wait_is_accepted(self, tmp_git_repo, monkeypatch):
        """Red on the old code: an empty marker was always stale."""
        proj = _project(tmp_git_repo)
        _approve(proj)
        _age_signal(proj)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        assert _wait(proj) == f"APPROVE-{TODO}"

    def test_foreign_verified_sha_still_refused(self, tmp_git_repo, monkeypatch):
        """A signal pinned to another tree must not unlock the commit."""
        proj = _project(tmp_git_repo)
        _approve(proj)
        verified = proj / ".agentic" / "context" / f"VERIFIED-{TODO}.sha"
        verified.write_text("0" * 64 + "\n", encoding="utf-8")
        _age_signal(proj)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        with pytest.raises(TimeoutError):
            _wait(proj, timeout=1)

    def test_tree_moved_after_approve_still_refused(self, tmp_git_repo, monkeypatch):
        """A new file after the approve invalidates the decision."""
        proj = _project(tmp_git_repo)
        _approve(proj)
        (proj / "late.txt").write_text("appeared after verify\n", encoding="utf-8")
        _age_signal(proj)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        with pytest.raises(TimeoutError):
            _wait(proj, timeout=1)



class TestEndToEndWithoutTouch:
    def test_pre_existing_approve_commits_without_touch(self, tmp_git_repo, monkeypatch):
        """Приёмка A23: approve до старта ожидания → коммит без `touch`."""
        from awf.commit_gate import maybe_commit

        proj = _project(tmp_git_repo)
        inbox = proj / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{TODO}.md").write_text("# Task\n", encoding="utf-8")
        (proj / "file.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=proj, check=True)
        baseline = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True,
            text=True, check=True,
        ).stdout.strip()
        (proj / "file.txt").write_text("unit work\n", encoding="utf-8")
        context = proj / ".agentic" / "context"
        context.mkdir(parents=True, exist_ok=True)
        (context / f"BASELINE-{TODO}.sha").write_text(baseline + "\n", encoding="utf-8")

        _approve(proj)
        _age_signal(proj)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        assert _wait(proj) == f"APPROVE-{TODO}"
        outcome = maybe_commit(
            "verify", TODO, "commit_and_next", proj,
            proj / ".agentic" / "logs", auto=True, baseline_sha=baseline,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True,
            text=True, check=True,
        ).stdout.strip()
        assert outcome.status == "committed", outcome.reason
        assert head != baseline, "the unit was not committed"
        assert subprocess.run(
            ["git", "status", "--porcelain"], cwd=proj, capture_output=True,
            text=True, check=True,
        ).stdout.strip() == ""
