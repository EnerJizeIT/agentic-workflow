"""Unit tests for awf.verify — detect_work_evidence, run_verify_commands, attempt_auto_done."""
import subprocess
from pathlib import Path

from awf import verify


class TestVerifyCmdTimeout:
    """QA 2026-08-03: hung verify command must not block orchestrator.

    Before fix: subprocess.run had no timeout. A watcher (`pytest --watch`)
    or stdin-prompt would freeze `attempt_auto_done` and the whole pipeline.
    """

    def test_hung_command_returns_false(self, monkeypatch) -> None:
        """`sleep 30` with AWF_VERIFY_TIMEOUT=1 → TimeoutExpired caught, returns False."""
        monkeypatch.setenv("AWF_VERIFY_TIMEOUT", "1")
        cfg = {"verification": {"test_cmd": "sleep 30"}}
        # Must return quickly (within a few seconds), not hang for 30s.
        result = verify.run_verify_commands(cfg)
        assert result is False

    def test_hung_command_writes_log(self, tmp_path: Path, monkeypatch) -> None:
        """Timeout produces TEST-RESULTS log with TIMEOUT marker."""
        monkeypatch.setenv("AWF_VERIFY_TIMEOUT", "1")
        agentic = tmp_path / ".agentic" / "outbox"
        agentic.mkdir(parents=True)
        cfg = {"verification": {"test_cmd": "sleep 30"}}
        result = verify.run_verify_commands(cfg, project_dir=tmp_path, todo_id="TODO-X")
        assert result is False
        log = (agentic / "TEST-RESULTS-TODO-X.log").read_text()
        assert "TIMEOUT" in log

    def test_invalid_env_falls_back_to_default(self, monkeypatch) -> None:
        """AWF_VERIFY_TIMEOUT='abc' (typo) → default 600, no crash."""
        monkeypatch.setenv("AWF_VERIFY_TIMEOUT", "abc")
        assert verify._verify_cmd_timeout() == verify.DEFAULT_VERIFY_TIMEOUT

    def test_zero_env_disables_timeout(self, monkeypatch) -> None:
        """AWF_VERIFY_TIMEOUT=0 → legacy behavior (no timeout)."""
        monkeypatch.setenv("AWF_VERIFY_TIMEOUT", "0")
        assert verify._verify_cmd_timeout() == 0


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

    def test_pre_existing_untracked_filtered(self, tmp_git_repo: Path) -> None:
        """AUD-1: pre-existing untracked files must NOT count as work evidence."""
        (tmp_git_repo / ".gitignore").write_text(".agentic/\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
        subprocess.run(["git", "commit", "-qm", "gitignore"], cwd=tmp_git_repo, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        # Pre-existing untracked file (was there before worker started)
        (tmp_git_repo / "stale.txt").write_text("was here before\n")
        # Create baseline snapshot recording it as pre-existing
        ctx_dir = tmp_git_repo / ".agentic" / "context"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        (ctx_dir / "BASELINE-TODO-0001.untracked").write_text("stale.txt\n")
        # With todo_id → snapshot filters out stale.txt → no evidence
        assert verify.detect_work_evidence(tmp_git_repo, sha, "TODO-0001") is False
        # Without todo_id → backward compat → counts as evidence
        assert verify.detect_work_evidence(tmp_git_repo, sha) is True

    def test_new_untracked_not_filtered(self, tmp_git_repo: Path) -> None:
        """AUD-1: worker-created files still count as evidence even with snapshot."""
        (tmp_git_repo / ".gitignore").write_text(".agentic/\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
        subprocess.run(["git", "commit", "-qm", "gitignore"], cwd=tmp_git_repo, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True,
        ).stdout.strip()
        (tmp_git_repo / "stale.txt").write_text("was here before\n")
        (tmp_git_repo / "worker_created.py").write_text("new code\n")
        ctx_dir = tmp_git_repo / ".agentic" / "context"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        (ctx_dir / "BASELINE-TODO-0002.untracked").write_text("stale.txt\n")
        # stale.txt filtered, but worker_created.py remains → evidence
        assert verify.detect_work_evidence(tmp_git_repo, sha, "TODO-0002") is True


class TestRunVerifyCommands:

    def test_no_commands_returns_false(self) -> None:
        assert verify.run_verify_commands({}) is False

    def test_empty_verification_section(self) -> None:
        cfg = {"verification": {}}
        assert verify.run_verify_commands(cfg) is False

    def test_one_passing_command(self) -> None:
        cfg = {"verification": {"test_cmd": "/bin/true"}}
        assert verify.run_verify_commands(cfg) is True

    def test_one_failing_command(self) -> None:
        cfg = {"verification": {"test_cmd": "/bin/false"}}
        assert verify.run_verify_commands(cfg) is False

    def test_mix_passing_and_failing(self) -> None:
        cfg = {
            "verification": {
                "test_cmd": "/bin/true",
                "lint_cmd": "/bin/false",
            }
        }
        assert verify.run_verify_commands(cfg) is False

    def test_all_passing(self) -> None:
        cfg = {
            "verification": {
                "test_cmd": "/bin/true",
                "lint_cmd": "/bin/true",
                "typecheck_cmd": "/bin/true",
            }
        }
        assert verify.run_verify_commands(cfg) is True

    def test_empty_string_cmd_skipped(self) -> None:
        cfg = {
            "verification": {
                "test_cmd": "/bin/true",
                "lint_cmd": "",
                "typecheck_cmd": "",
                "build_cmd": "",
            }
        }
        assert verify.run_verify_commands(cfg) is True

    def test_shell_injection_not_executed(self) -> None:
        """shell=False: semicolon becomes part of executable name → FileNotFoundError → False."""
        cfg = {"verification": {"test_cmd": "/bin/true; /bin/false"}}
        assert verify.run_verify_commands(cfg) is False

    def test_shell_injection_semicolon_as_argument(self) -> None:
        """echo hello; /bin/true → shlex splits to ['echo', 'hello;', '/bin/true'].
        echo succeeds with those args; /bin/true is NOT run as separate command."""
        cfg = {"verification": {"test_cmd": "echo hello; /bin/true"}}
        assert verify.run_verify_commands(cfg) is True

    def test_empty_cmd_after_split(self) -> None:
        """Command that is only whitespace → shlex.split returns [] → False."""
        cfg = {"verification": {"test_cmd": "   "}}
        assert verify.run_verify_commands(cfg) is False

    def test_project_dir_passed_to_subprocess(self, tmp_path: Path) -> None:
        """project_dir is forwarded as cwd to subprocess.run."""
        cfg = {"verification": {"test_cmd": "pwd"}}
        result = verify.run_verify_commands(cfg, project_dir=tmp_path)
        # pwd always succeeds (returncode 0)
        assert result is True

    def test_project_dir_none_uses_cwd(self) -> None:
        """project_dir=None → runs in current CWD (legacy behavior)."""
        cfg = {"verification": {"test_cmd": "/bin/true"}}
        assert verify.run_verify_commands(cfg, project_dir=None) is True


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
            "verification": {"typecheck_cmd": "/bin/true"},
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
            "verification": {"typecheck_cmd": "/bin/false"},
            "automation": {"auto_done": True},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0002", cfg, sha)
        assert result is False
        assert not (outbox / "DONE-TODO-0002.ready").exists()

    def test_no_work_no_done(self, tmp_git_repo: Path) -> None:
        outbox, sha = self._setup_repo(tmp_git_repo)
        cfg = {
            "verification": {"typecheck_cmd": "/bin/true"},
            "automation": {"auto_done": True},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0003", cfg, sha)
        assert result is False
        assert not (outbox / "DONE-TODO-0003.ready").exists()

    def test_auto_done_disabled(self, tmp_git_repo: Path) -> None:
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "verification": {"typecheck_cmd": "/bin/true"},
            "automation": {"auto_done": False},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0004", cfg, sha)
        assert result is False
        assert not (outbox / "DONE-TODO-0004.ready").exists()

    def test_auto_done_string_false(self, tmp_git_repo: Path) -> None:
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "verification": {"typecheck_cmd": "/bin/true"},
            "automation": {"auto_done": "false"},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0005", cfg, sha)
        assert result is False

    def test_auto_done_bool_true_works_regression(self, tmp_git_repo: Path) -> None:
        """Auditor HIGH bug: YAML `auto_done: true` parses to Python bool True
        (not string 'true'). Old code: `True not in ('true', '1')` → returned
        False → auto-DONE silently disabled even though user enabled it.
        Must work after fix."""
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "verification": {"typecheck_cmd": "/bin/true"},
            "automation": {"auto_done": True},  # YAML `true` → Python bool
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0006", cfg, sha)
        assert result is True, "Bool True must enable auto-DONE (was HIGH bug)"
        assert (outbox / "DONE-TODO-0006.ready").exists()

    def test_auto_done_int_one_works(self, tmp_git_repo: Path) -> None:
        """YAML `auto_done: 1` parses to int 1. Must enable."""
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "verification": {"typecheck_cmd": "/bin/true"},
            "automation": {"auto_done": 1},  # YAML `1` → Python int
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0007", cfg, sha)
        assert result is True

    def test_no_verify_cmds_auto_done_with_work(self, tmp_git_repo: Path) -> None:
        """DF5-3: no verify cmds + work evidence → auto-DONE (greenfield projects).

        Previously returned False (blocked all greenfield/doc-heavy projects).
        Now: no verify commands = no blocking checks. Work evidence alone
        is sufficient. Supervisor review stage still acts as safety net.
        """
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "automation": {"auto_done": True},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0006", cfg, sha)
        assert result is True
        assert (outbox / "DONE-TODO-0006.ready").exists()

    def test_no_verify_cmds_no_work_evidence(self, tmp_git_repo: Path) -> None:
        """DF5-3: no verify cmds + NO work → still False (no evidence)."""
        outbox, sha = self._setup_repo(tmp_git_repo)
        cfg = {
            "automation": {"auto_done": True},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0006", cfg, sha)
        assert result is False

    def test_auto_done_string_true(self, tmp_git_repo: Path) -> None:
        outbox, sha = self._setup_repo(tmp_git_repo)
        (tmp_git_repo / "README.md").write_text("changed\n")
        cfg = {
            "verification": {"test_cmd": "/bin/true"},
            "automation": {"auto_done": "true"},
        }
        result = verify.attempt_auto_done(tmp_git_repo, "TODO-0007", cfg, sha)
        assert result is True
        assert (outbox / "DONE-TODO-0007.ready").exists()


class TestDiffStatForTodo:
    """Day-4 live fix: `git diff` is blind to new files — verify must see them."""

    def _repo(self, tmp_path: Path) -> Path:
        proj = tmp_path / "repo"
        proj.mkdir()
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@t.t"],
            ["git", "config", "user.name", "tester"],
        ):
            subprocess.run(cmd, cwd=proj, check=True)
        (proj / "README.md").write_text("init\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)
        return proj

    def _baseline(self, proj: Path, sha: str | None = None) -> str:
        from awf.git_utils import current_sha

        sha = sha or current_sha(proj)
        ctx = proj / ".agentic" / "context"
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "BASELINE-TODO-0001.sha").write_text(sha + "\n", encoding="utf-8")
        return sha

    def test_untracked_files_listed(self, tmp_path):
        proj = self._repo(tmp_path)
        self._baseline(proj)
        (proj / "scripts").mkdir()
        (proj / "scripts" / "run.sh").write_text("echo\n", encoding="utf-8")

        text = verify.diff_stat_for_todo(proj, "TODO-0001")

        assert "new (untracked) files:" in text
        assert "scripts/" in text

    def test_tracked_and_untracked_together(self, tmp_path):
        proj = self._repo(tmp_path)
        self._baseline(proj)
        (proj / "README.md").write_text("changed\n", encoding="utf-8")
        (proj / "new_file.py").write_text("x = 1\n", encoding="utf-8")

        text = verify.diff_stat_for_todo(proj, "TODO-0001")

        assert "README.md" in text          # tracked diff
        assert "new_file.py" in text        # untracked section

    def test_pre_existing_untracked_excluded(self, tmp_path):
        proj = self._repo(tmp_path)
        self._baseline(proj)
        # Snapshot records a file that existed BEFORE the task
        ctx = proj / ".agentic" / "context"
        (ctx / "BASELINE-TODO-0001.untracked").write_text("old.txt\n", encoding="utf-8")
        (proj / "old.txt").write_text("old\n", encoding="utf-8")
        (proj / "fresh.py").write_text("x\n", encoding="utf-8")

        text = verify.diff_stat_for_todo(proj, "TODO-0001")

        assert "fresh.py" in text
        assert "old.txt" not in text

    def test_no_baseline_empty(self, tmp_path):
        proj = self._repo(tmp_path)
        assert verify.diff_stat_for_todo(proj, "TODO-0001") == ""
