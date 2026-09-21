"""U11 Part A: verified-sha — «что проверили = что коммитим».

The supervisor records the working-tree fingerprint at verify time
(``awf tree-sha`` → ``git_utils.tree_fingerprint``) and passes it to
``approve_commit(verified_sha=...)``. If the tree moved (new commit,
edited tracked file, new/changed untracked file) approve must refuse.
Without ``verified_sha`` the behavior is exactly as before (compat).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from awf import api, git_utils
from awf.api._errors import AwfApiError

TID = "TODO-0001"


def _commit(repo: Path, msg: str = "wip") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=repo, check=True)


class TestTreeFingerprint:
    def test_stable_across_time(self, tmp_git_repo: Path) -> None:
        fp1 = git_utils.tree_fingerprint(tmp_git_repo)
        time.sleep(0.3)
        fp2 = git_utils.tree_fingerprint(tmp_git_repo)
        assert fp1 == fp2, "same tree state must give the same fingerprint"

    def test_hex64(self, tmp_git_repo: Path) -> None:
        fp = git_utils.tree_fingerprint(tmp_git_repo)
        assert len(fp) == 64
        int(fp, 16)

    def test_changes_on_tracked_edit(self, tmp_git_repo: Path) -> None:
        fp1 = git_utils.tree_fingerprint(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("tampered\n")
        fp2 = git_utils.tree_fingerprint(tmp_git_repo)
        assert fp1 != fp2

    def test_changes_on_untracked_add(self, tmp_git_repo: Path) -> None:
        fp1 = git_utils.tree_fingerprint(tmp_git_repo)
        (tmp_git_repo / "new_file.py").write_text("print('hi')\n")
        fp2 = git_utils.tree_fingerprint(tmp_git_repo)
        assert fp1 != fp2

    def test_changes_on_untracked_content(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "new_file.py").write_text("v1\n")
        fp1 = git_utils.tree_fingerprint(tmp_git_repo)
        (tmp_git_repo / "new_file.py").write_text("v2\n")
        fp2 = git_utils.tree_fingerprint(tmp_git_repo)
        assert fp1 != fp2

    def test_changes_on_new_commit(self, tmp_git_repo: Path) -> None:
        fp1 = git_utils.tree_fingerprint(tmp_git_repo)
        (tmp_git_repo / "extra.txt").write_text("x\n")
        _commit(tmp_git_repo)
        fp2 = git_utils.tree_fingerprint(tmp_git_repo)
        assert fp1 != fp2

    def test_not_a_repo_raises(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(RuntimeError):
            git_utils.tree_fingerprint(plain)


def _make_agentic(repo: Path) -> None:
    """approve_commit requires .agentic/ — create the minimum."""
    (repo / ".agentic").mkdir(parents=True, exist_ok=True)


class TestApproveWithVerifiedSha:
    def test_matching_sha_ok_and_file_written(self, tmp_git_repo: Path) -> None:
        _make_agentic(tmp_git_repo)
        fp = git_utils.tree_fingerprint(tmp_git_repo)

        result = api.approve_commit(tmp_git_repo, TID, verified_sha=fp)

        assert (tmp_git_repo / ".agentic" / "inbox" / f"APPROVE-{TID}.ready").is_file()
        verified = tmp_git_repo / ".agentic" / "context" / f"VERIFIED-{TID}.sha"
        assert verified.is_file()
        assert verified.read_text(encoding="utf-8").strip() == fp
        assert result.verified_sha_file == str(verified)

    def test_tracked_edit_after_verify_refused(self, tmp_git_repo: Path) -> None:
        _make_agentic(tmp_git_repo)
        fp = git_utils.tree_fingerprint(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed after verify\n")

        with pytest.raises(AwfApiError, match="tree changed after verification"):
            api.approve_commit(tmp_git_repo, TID, verified_sha=fp)

        assert not (tmp_git_repo / ".agentic" / "inbox" / f"APPROVE-{TID}.ready").exists()
        assert not (tmp_git_repo / ".agentic" / "context" / f"VERIFIED-{TID}.sha").exists()

    def test_new_commit_after_verify_refused(self, tmp_git_repo: Path) -> None:
        _make_agentic(tmp_git_repo)
        fp = git_utils.tree_fingerprint(tmp_git_repo)
        (tmp_git_repo / "sneaky.txt").write_text("new work after verify\n")
        _commit(tmp_git_repo)

        with pytest.raises(AwfApiError, match="tree changed after verification"):
            api.approve_commit(tmp_git_repo, TID, verified_sha=fp)

        assert not (tmp_git_repo / ".agentic" / "inbox" / f"APPROVE-{TID}.ready").exists()

    def test_untracked_add_after_verify_refused(self, tmp_git_repo: Path) -> None:
        """The worker's new files are untracked — a swap must be caught."""
        _make_agentic(tmp_git_repo)
        fp = git_utils.tree_fingerprint(tmp_git_repo)
        (tmp_git_repo / "tests" / "unit").mkdir(parents=True, exist_ok=True)
        (tmp_git_repo / "tests" / "unit" / "swapped.py").write_text("print('nope')\n")

        with pytest.raises(AwfApiError, match="tree changed after verification"):
            api.approve_commit(tmp_git_repo, TID, verified_sha=fp)

    def test_garbage_sha_refused(self, tmp_git_repo: Path) -> None:
        _make_agentic(tmp_git_repo)
        with pytest.raises(AwfApiError, match="invalid verified_sha"):
            api.approve_commit(tmp_git_repo, TID, verified_sha="not-a-sha")
        with pytest.raises(AwfApiError, match="invalid verified_sha"):
            api.approve_commit(tmp_git_repo, TID, verified_sha="abc123")

    def test_no_sha_legacy_behavior_git_repo(self, tmp_git_repo: Path) -> None:
        """Compat: without verified_sha nothing is checked or written."""
        _make_agentic(tmp_git_repo)
        result = api.approve_commit(tmp_git_repo, TID)
        assert (tmp_git_repo / ".agentic" / "inbox" / f"APPROVE-{TID}.ready").is_file()
        assert not (tmp_git_repo / ".agentic" / "context" / f"VERIFIED-{TID}.sha").exists()
        assert result.verified_sha_file == ""

    def test_no_sha_legacy_behavior_non_git_dir(self, tmp_path: Path) -> None:
        """Compat: a non-git project dir works exactly as before."""
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".agentic").mkdir()

        result = api.approve_commit(project, TID)

        assert (project / ".agentic" / "inbox" / f"APPROVE-{TID}.ready").is_file()
        assert result.verified_sha_file == ""

    def test_sha_passed_but_not_git_repo_refused(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".agentic").mkdir()
        with pytest.raises(AwfApiError, match="fingerprint"):
            api.approve_commit(project, TID, verified_sha="a" * 64)
