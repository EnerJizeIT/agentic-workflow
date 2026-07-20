"""Unit tests for awf.git_utils — current_sha, has_diff, untracked_files, commit_all, is_git_repo."""
import subprocess
from pathlib import Path

from awf import git_utils


class TestCurrentSha:

    def test_returns_40_char_hex(self, tmp_git_repo: Path) -> None:
        sha = git_utils.current_sha(tmp_git_repo)
        assert len(sha) == 40
        int(sha, 16)  # validates hex


class TestHasDiff:

    def test_clean_tree_no_diff(self, tmp_git_repo: Path) -> None:
        sha = git_utils.current_sha(tmp_git_repo)
        assert git_utils.has_diff(tmp_git_repo, sha) is False

    def test_modified_tracked_file(self, tmp_git_repo: Path) -> None:
        sha = git_utils.current_sha(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("modified\n")
        assert git_utils.has_diff(tmp_git_repo, sha) is True


class TestUntrackedFiles:

    def test_clean_repo_empty(self, tmp_git_repo: Path) -> None:
        assert git_utils.untracked_files(tmp_git_repo) == []

    def test_new_file(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "newfile.txt").write_text("hello\n")
        result = git_utils.untracked_files(tmp_git_repo)
        assert "newfile.txt" in result

    def test_gitignored_file_excluded(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / ".gitignore").write_text("ignored.txt\n")
        (tmp_git_repo / "ignored.txt").write_text("x\n")
        (tmp_git_repo / "notignored.txt").write_text("y\n")
        result = git_utils.untracked_files(tmp_git_repo)
        assert "ignored.txt" not in result
        assert "notignored.txt" in result


class TestCommitAll:

    def test_with_changes_returns_true(self, tmp_git_repo: Path) -> None:
        sha_before = git_utils.current_sha(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        result = git_utils.commit_all(tmp_git_repo, "test commit")
        assert result is True
        sha_after = git_utils.current_sha(tmp_git_repo)
        assert sha_after != sha_before

    def test_without_changes_returns_false(self, tmp_git_repo: Path) -> None:
        sha_before = git_utils.current_sha(tmp_git_repo)
        result = git_utils.commit_all(tmp_git_repo, "test commit")
        assert result is False
        sha_after = git_utils.current_sha(tmp_git_repo)
        assert sha_after == sha_before

    def test_commit_includes_changes(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "new.txt").write_text("content\n")
        git_utils.commit_all(tmp_git_repo, "add new")
        content = subprocess.run(
            ["git", "cat-file", "-p", "HEAD:new.txt"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout
        assert content == "content\n"


class TestIsGitRepo:

    def test_in_git_repo(self, tmp_git_repo: Path) -> None:
        assert git_utils.is_git_repo(tmp_git_repo) is True

    def test_outside_git_repo(self, tmp_path: Path) -> None:
        not_repo = tmp_path / "notgit"
        not_repo.mkdir()
        assert git_utils.is_git_repo(not_repo) is False
