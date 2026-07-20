"""Unit tests for awf.verify — detect_work_evidence, run_verify_commands, attempt_auto_done."""
import subprocess
from pathlib import Path

from awf import config as cfg_mod
from awf import verify


class TestDetectWorkEvidence:

    def test_clean_tree_no_evidence(self, tmp_git_repo: Path) -> None:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        assert verify.detect_work_evidence(tmp_git_repo, sha) is False

    def test_modified_tracked_file(self, tmp_git_repo: Path) -> None:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        (tmp_git_repo / "README.md").write_text("modified\n")
        assert verify.detect_work_evidence(tmp_git_repo, sha) is True

    def test_untracked_file_only(self, tmp_git_repo: Path) -> None:
        """Bug fix: untracked files must count as work evidence."""
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        (tmp_git_repo / "newfile.txt").write_text("new\n")
        assert verify.detect_work_evidence(tmp_git_repo, sha) is True

    def test_missing_baseline_sha(self, tmp_git_repo: Path) -> None:
        """Non-existent baseline SHA — git diff treats all files as new → True."""
        fake_sha = "0" * 40
        assert verify.detect_work_evidence(tmp_git_repo, fake_sha) is True

    def test_not_a_git_repo(self, tmp_path: Path) -> None:
        not_repo = tmp_path / "notgit"
        not_repo.mkdir()
        assert verify.detect_work_evidence(not_repo, "abc") is False


class TestRunVerifyCommands:

    def test_no_commands_returns_false(self) -> None:
        assert verify.run_verify_commands({}) is False

    def test_empty_verification_section(self) -> None:
        cfg = {"verification": {}}
        assert verify.run_verify_commands(cfg) is False

    def test_one_passing_command(self) -> None:
        cfg = {"verification": {"test_cmd": "true"}}
        assert verify.run_verify_commands(cfg) is True

    def test_one_failing_command(self) -> None:
        cfg = {"verification": {"test_cmd": "false"}}
        assert verify.run_verify_commands(cfg) is False

    def test_mix_passing_and_failing(self) -> None:
        cfg = {
            "verification": {
                "test_cmd": "true",
                "lint_cmd": "false",
            }
        }
        assert verify.run_verify_commands(cfg) is False

    def test_all_passing(self) -> None:
        cfg = {
            "verification": {
                "test_cmd": "true",
                "lint_cmd": "true",
                "typecheck_cmd": "true",
            }
        }
        assert verify.run_verify_commands(cfg) is True

    def test_empty_string_cmd_skipped(self) -> None:
        cfg = {
            "verification": {
                "test_cmd": "true",
                "lint_cmd": "",
                "typecheck_cmd": "",
                "build_cmd": "",
            }
        }
        assert verify.run_verify_commands(cfg) is True


class TestAttemptAutoDone:

    def _setup_repo(self, tmp_git_repo: Path) -> tuple[Path, str]:
        """Set up .agentic dirs and return (outbox, baseline_sha)."""
        agentic = tmp_git_repo / ".agentic"
        outbox = agentic / "outbox"
        context = agentic / "context"
        outbox.mkdir(parents=True)
        context.mkdir(parents=True)

        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        return outbox, sha

    def test_verify_pass_work_present_auto_done_true(self, tmp_git_repo: Path) -> None:
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "verification": {"typecheck_cmd": "true"},
            "automation": {"auto_done": "true"},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0001", cfg, sha)
        assert result is True
        assert (outbox / "DONE-TODO-0001.ready").exists()
        done_md = (outbox / "DONE-TODO-0001.md").read_text()
        assert "AUTO-GENERATED" in done_md

    def test_verify_fails_no_done(self, tmp_git_repo: Path) -> None:
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "verification": {"typecheck_cmd": "false"},
            "automation": {"auto_done": True},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0002", cfg, sha)
        assert result is False
        assert not (outbox / "DONE-TODO-0002.ready").exists()

    def test_no_work_no_done(self, tmp_git_repo: Path) -> None:
        outbox, sha = self._setup_repo(tmp_git_repo)
        cfg = {
            "verification": {"typecheck_cmd": "true"},
            "automation": {"auto_done": True},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0003", cfg, sha)
        assert result is False
        assert not (outbox / "DONE-TODO-0003.ready").exists()

    def test_auto_done_disabled(self, tmp_git_repo: Path) -> None:
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "verification": {"typecheck_cmd": "true"},
            "automation": {"auto_done": False},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0004", cfg, sha)
        assert result is False
        assert not (outbox / "DONE-TODO-0004.ready").exists()

    def test_auto_done_string_false(self, tmp_git_repo: Path) -> None:
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "verification": {"typecheck_cmd": "true"},
            "automation": {"auto_done": "false"},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0005", cfg, sha)
        assert result is False

    def test_no_verify_cmds_no_done(self, tmp_git_repo: Path) -> None:
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "automation": {"auto_done": True},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0006", cfg, sha)
        assert result is False
        assert not (outbox / "DONE-TODO-0006.ready").exists()

    def test_auto_done_string_true(self, tmp_git_repo: Path) -> None:
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "verification": {"test_cmd": "true"},
            "automation": {"auto_done": "true"},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0007", cfg, sha)
        assert result is True
        assert (outbox / "DONE-TODO-0007.ready").exists()
