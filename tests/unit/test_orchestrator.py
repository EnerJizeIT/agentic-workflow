"""Unit tests for awf.orchestrator — role file resolution."""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from awf.orchestrator import (
    _check_needs_normalize,
    _check_skill_drift,
    _collect_handoff,
    _consume_needs_normalize,
    _global_roles_dir,
    _maybe_commit,
    _needs_normalize,
    _resolve_prev_handoffs,
    _resolve_role_file,
    _run_agent_stage,
    _run_normalize_stage,
    _run_supervisor_stage,
)
from awf.pipeline import Stage

# BD-18: subprocess.run return value for "success" mocks.
_OK_RESULT = subprocess.CompletedProcess(args=[], returncode=0)


class TestResolveRoleFile:

    def test_project_role_takes_precedence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic" / "roles").mkdir(parents=True)
        (project_dir / ".agentic" / "roles" / "worker.md").write_text("project worker")

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        (fake_home / ".config" / "awf" / "roles" / "worker.md").write_text("global worker")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = _resolve_role_file("worker", project_dir)
        assert result == project_dir / ".agentic" / "roles" / "worker.md"

    def test_falls_back_to_global(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic" / "roles").mkdir(parents=True)
        (project_dir / ".agentic" / "roles" / "worker.md").write_text("project worker")

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        (fake_home / ".config" / "awf" / "roles" / "auditor.md").write_text("global auditor")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = _resolve_role_file("auditor", project_dir)
        assert result == fake_home / ".config" / "awf" / "roles" / "auditor.md"

    def test_raises_when_not_found_anywhere(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic" / "roles").mkdir(parents=True)

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        with pytest.raises(RuntimeError) as exc_info:
            _resolve_role_file("nonexistent", project_dir)

        msg = str(exc_info.value)
        assert "nonexistent" in msg
        assert str(project_dir) in msg
        assert str(fake_home) in msg

    def test_global_only_no_project_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        (fake_home / ".config" / "awf" / "roles" / "system-analysis.md").write_text("global")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = _resolve_role_file("system-analysis", project_dir)
        assert result == fake_home / ".config" / "awf" / "roles" / "system-analysis.md"


class TestGlobalRolesDir:

    def test_returns_expected_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = Path("/fake/home")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert _global_roles_dir() == fake_home / ".config" / "awf" / "roles"


class TestMaybeCommitBD8:

    def _init_git(self, tmp_path: Path) -> Path:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".git").mkdir()
        (project_dir / "file.txt").write_text("hello")
        import subprocess
        subprocess.run(["git", "init"], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "A"], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=project_dir, capture_output=True, check=True)
        return project_dir

    def test_maybe_commit_auto_waits_for_approve_signal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        # Pre-create the approve signal so polling exits immediately
        (inbox / "APPROVE-TODO-0001.ready").touch()

        # Create a change to commit
        (project_dir / "file.txt").write_text("modified")

        sleep_calls = []
        monkeypatch.setattr(
            "time.sleep", lambda _d: sleep_calls.append(_d)
        )

        _maybe_commit(
            "verify", "TODO-0001", "commit_and_next",
            project_dir, logs_dir, auto=True,
        )

        # Polling should have seen the signal on first check
        assert len(sleep_calls) == 0
        result = project_dir.joinpath(".git").joinpath("HEAD").read_text().strip()
        assert "refs/heads/master" in result or "refs/heads/main" in result

    def test_maybe_commit_auto_no_signal_raises_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        (project_dir / "file.txt").write_text("modified")

        # time.time advances so deadline (t=0 + 0s = 0) is passed on check (t=1)
        _t = [0.0]
        def fake_time():
            return _t[0]

        def fake_sleep(_dur):
            _t[0] += _dur

        monkeypatch.setattr("time.time", fake_time)
        monkeypatch.setattr("time.sleep", fake_sleep)
        monkeypatch.setattr(
            "awf.orchestrator.APPROVE_TIMEOUT_SECONDS", 0
        )

        with pytest.raises(TimeoutError) as exc_info:
            _maybe_commit(
                "verify", "TODO-0001", "commit_and_next",
                project_dir, logs_dir, auto=True,
            )

        assert "TODO-0001" in str(exc_info.value)

        # Verify no commit was made
        import subprocess
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True,
        )
        assert "file.txt" in status.stdout

    def test_maybe_commit_timeout_uses_env_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        (project_dir / "file.txt").write_text("modified")

        # Set 1-second timeout via module constant
        monkeypatch.setattr(
            "awf.orchestrator.APPROVE_TIMEOUT_SECONDS", 1
        )
        monkeypatch.setattr(
            "awf.orchestrator.APPROVE_POLL_INTERVAL", 1
        )

        with pytest.raises(TimeoutError):
            _maybe_commit(
                "verify", "TODO-0001", "commit_and_next",
                project_dir, logs_dir, auto=True,
            )

    def test_maybe_commit_non_auto_commits_immediately(self, tmp_path: Path) -> None:
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)

        (project_dir / "file.txt").write_text("modified")

        _maybe_commit(
            "verify", "TODO-0001", "commit_and_next",
            project_dir, logs_dir, auto=False,
        )

        # Verify commit was made — no uncommitted changes
        import subprocess
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True,
        )
        assert "file.txt" not in status.stdout

    def test_maybe_commit_auto_accepts_ack_signal_bd17(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-17: ACK signal (from supervisor verify subprocess) authorizes commit too."""
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        # Only ACK exists — no APPROVE
        (inbox / "ACK-TODO-0001.ready").touch()
        (project_dir / "file.txt").write_text("modified")

        sleep_calls: list = []
        monkeypatch.setattr("time.sleep", lambda _d: sleep_calls.append(_d))

        _maybe_commit(
            "verify", "TODO-0001", "commit_and_next",
            project_dir, logs_dir, auto=True,
        )

        # Should not have polled (signal detected on first check)
        assert sleep_calls == []
        # Commit happened — file.txt no longer in `git status`
        import subprocess
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True,
        )
        assert "file.txt" not in status.stdout

    def test_maybe_commit_auto_no_signal_still_times_out_bd17(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-17: when neither APPROVE nor ACK exists, still raises TimeoutError."""
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        (project_dir / "file.txt").write_text("modified")

        _t = [0.0]
        monkeypatch.setattr("time.time", lambda: _t[0])
        monkeypatch.setattr("time.sleep", lambda d: _t.__setitem__(0, _t[0] + d))
        monkeypatch.setattr("awf.orchestrator.APPROVE_TIMEOUT_SECONDS", 0)

        with pytest.raises(TimeoutError) as exc_info:
            _maybe_commit(
                "verify", "TODO-0001", "commit_and_next",
                project_dir, logs_dir, auto=True,
            )
        # Error message should mention both signal types
        assert "APPROVE/ACK" in str(exc_info.value)


class TestRunAgentStageLocalSkill:

    def _setup_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic" / "roles").mkdir(parents=True)
        (project_dir / ".agentic" / "roles" / "worker.md").write_text("role content")
        (project_dir / ".agentic" / "inbox").mkdir(parents=True)
        (project_dir / ".agentic" / "inbox" / "TODO-0001.md").write_text("task content")
        (project_dir / ".agentic" / "logs").mkdir(parents=True)
        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        return project_dir

    def test_run_agent_stage_passes_local_skill_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_dir = self._setup_project(tmp_path, monkeypatch)
        (project_dir / ".agentic" / "skills").mkdir(parents=True)
        local_skill = project_dir / ".agentic" / "skills" / "worker.md"
        local_skill.write_text("skill content")

        stage = Stage(name="execute", role="worker", action="execute_todo")
        captured = []

        def fake_run(cmd, **kwargs):
            captured.extend(cmd)
            return _OK_RESULT

        with patch("awf.orchestrator.subprocess.run", side_effect=fake_run):
            _run_agent_stage(stage, "TODO-0001", project_dir, {}, project_dir / ".agentic" / "logs")

        assert "--file" in captured
        skill_idx = captured.index("--file")
        assert str(local_skill) in captured
        file_indices = [i for i, x in enumerate(captured) if x == "--file"]
        assert len(file_indices) == 3

    def test_run_agent_stage_no_skill_unchanged_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_dir = self._setup_project(tmp_path, monkeypatch)

        stage = Stage(name="execute", role="worker", action="execute_todo")
        captured = []

        def fake_run(cmd, **kwargs):
            captured.extend(cmd)
            return _OK_RESULT

        with patch("awf.orchestrator.subprocess.run", side_effect=fake_run):
            _run_agent_stage(stage, "TODO-0001", project_dir, {}, project_dir / ".agentic" / "logs")

        file_indices = [i for i, x in enumerate(captured) if x == "--file"]
        assert len(file_indices) == 2

    def test_run_agent_stage_skill_dir_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_dir = self._setup_project(tmp_path, monkeypatch)
        (project_dir / ".agentic" / "skills").mkdir(parents=True)

        stage = Stage(name="execute", role="worker", action="execute_todo")
        captured = []

        def fake_run(cmd, **kwargs):
            captured.extend(cmd)
            return _OK_RESULT

        with patch("awf.orchestrator.subprocess.run", side_effect=fake_run):
            _run_agent_stage(stage, "TODO-0001", project_dir, {}, project_dir / ".agentic" / "logs")

        file_indices = [i for i, x in enumerate(captured) if x == "--file"]
        assert len(file_indices) == 2


class TestNeedsNormalize:

    def test_no_state_file(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic").mkdir()

        needed, team = _needs_normalize(project_dir)
        assert needed is False
        assert team == []

    def test_needed_true_consumes_file(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        state_dir = project_dir / ".agentic" / "state"
        state_dir.mkdir(parents=True)
        team_data = [
            {"role": "worker", "type": "primary"},
            {"role": "reviewer", "type": "secondary"},
        ]
        import yaml
        (state_dir / "needs_normalize.yaml").write_text(
            yaml.dump({"needed": True, "team": team_data})
        )

        needed, team = _needs_normalize(project_dir)
        assert needed is True
        assert len(team) == 2
        assert team[0]["role"] == "worker"
        assert not (state_dir / "needs_normalize.yaml").exists()

    def test_needed_false(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        state_dir = project_dir / ".agentic" / "state"
        state_dir.mkdir(parents=True)
        import yaml
        (state_dir / "needs_normalize.yaml").write_text(
            yaml.dump({"needed": False, "team": [{"role": "x"}]})
        )

        needed, team = _needs_normalize(project_dir)
        assert needed is False
        assert team == []

    def test_consumed_idempotent(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        state_dir = project_dir / ".agentic" / "state"
        state_dir.mkdir(parents=True)
        import yaml
        (state_dir / "needs_normalize.yaml").write_text(
            yaml.dump({"needed": True, "team": [{"role": "w"}]})
        )

        needed1, _ = _needs_normalize(project_dir)
        assert needed1 is True

        needed2, _ = _needs_normalize(project_dir)
        assert needed2 is False

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        state_dir = project_dir / ".agentic" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "needs_normalize.yaml").write_text(":::invalid{{{")

        needed, team = _needs_normalize(project_dir)
        assert needed is False
        assert team == []

    def test_team_not_list(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        state_dir = project_dir / ".agentic" / "state"
        state_dir.mkdir(parents=True)
        import yaml
        (state_dir / "needs_normalize.yaml").write_text(
            yaml.dump({"needed": True, "team": "worker"})
        )

        needed, team = _needs_normalize(project_dir)
        assert needed is True
        assert team == []


class TestCheckNeedsNormalize:

    def _setup_state_file(self, tmp_path: Path) -> Path:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        state_dir = project_dir / ".agentic" / "state"
        state_dir.mkdir(parents=True)
        import yaml
        (state_dir / "needs_normalize.yaml").write_text(
            yaml.dump({"needed": True, "team": [{"role": "worker", "type": "primary"}]})
        )
        return project_dir

    def test_check_does_not_delete_file(self, tmp_path: Path) -> None:
        project_dir = self._setup_state_file(tmp_path)
        state_file = project_dir / ".agentic" / "state" / "needs_normalize.yaml"

        needed, team, returned_path = _check_needs_normalize(project_dir)
        assert needed is True
        assert len(team) == 1
        assert returned_path == state_file
        assert state_file.exists()

    def test_check_idempotent(self, tmp_path: Path) -> None:
        project_dir = self._setup_state_file(tmp_path)

        needed1, _, _ = _check_needs_normalize(project_dir)
        assert needed1 is True

        needed2, _, _ = _check_needs_normalize(project_dir)
        assert needed2 is True

    def test_consume_deletes_file(self, tmp_path: Path) -> None:
        project_dir = self._setup_state_file(tmp_path)
        state_file = project_dir / ".agentic" / "state" / "needs_normalize.yaml"

        _consume_needs_normalize(state_file)
        assert not state_file.exists()

    def test_consume_none_safe(self, tmp_path: Path) -> None:
        _consume_needs_normalize(None)

    def test_consume_nonexistent_safe(self, tmp_path: Path) -> None:
        _consume_needs_normalize(tmp_path / "nope" / "file.yaml")

    def test_state_file_survives_normalize_failure(self, tmp_path: Path) -> None:
        project_dir = self._setup_state_file(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir()
        state_file = project_dir / ".agentic" / "state" / "needs_normalize.yaml"

        needed, team, returned_path = _check_needs_normalize(project_dir)
        assert needed is True

        with patch("builtins.input", side_effect=RuntimeError("simulated failure")):
            with patch("sys.stdin.isatty", return_value=True):
                with pytest.raises(RuntimeError):
                    _run_normalize_stage(team, project_dir, logs_dir, background=False)

        assert state_file.exists()


class TestCheckSkillDrift:

    def test_no_skills_dir(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic").mkdir()

        assert _check_skill_drift(project_dir) is False

    def test_skills_dir_no_frontmatter(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        skills_dir = project_dir / ".agentic" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "worker.md").write_text("just plain text, no frontmatter")

        assert _check_skill_drift(project_dir) is False

    def test_skills_dir_no_derived_from_global(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        skills_dir = project_dir / ".agentic" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "worker.md").write_text("---\nauthor: me\n---\ncontent")

        assert _check_skill_drift(project_dir) is False

    def test_matching_sha(self, tmp_path: Path) -> None:
        import hashlib

        import yaml

        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        skills_dir = project_dir / ".agentic" / "skills"
        skills_dir.mkdir(parents=True)

        global_dir = tmp_path / "global_skills"
        global_dir.mkdir()
        global_skill = global_dir / "worker.md"
        global_skill.write_text("global skill content")

        expected_sha = hashlib.sha256(global_skill.read_bytes()).hexdigest()
        fm = {
            "derived_from_global": True,
            "global_path": str(global_skill),
            "global_sha": expected_sha,
        }
        local_content = f"---\n{yaml.dump(fm, default_flow_style=False)}---\nlocal content"
        (skills_dir / "worker.md").write_text(local_content)

        assert _check_skill_drift(project_dir) is False

    def test_mismatched_sha(self, tmp_path: Path) -> None:
        import yaml

        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        skills_dir = project_dir / ".agentic" / "skills"
        skills_dir.mkdir(parents=True)

        global_dir = tmp_path / "global_skills"
        global_dir.mkdir()
        global_skill = global_dir / "worker.md"
        global_skill.write_text("original content")

        fm = {
            "derived_from_global": True,
            "global_path": str(global_skill),
            "global_sha": "0" * 64,
        }
        local_content = f"---\n{yaml.dump(fm, default_flow_style=False)}---\nlocal content"
        (skills_dir / "worker.md").write_text(local_content)

        assert _check_skill_drift(project_dir) is True

    def test_global_path_missing(self, tmp_path: Path) -> None:
        import yaml

        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        skills_dir = project_dir / ".agentic" / "skills"
        skills_dir.mkdir(parents=True)

        fm = {
            "derived_from_global": True,
            "global_path": "/nonexistent/path/skill.md",
            "global_sha": "abc123",
        }
        local_content = f"---\n{yaml.dump(fm, default_flow_style=False)}---\nlocal content"
        (skills_dir / "worker.md").write_text(local_content)

        assert _check_skill_drift(project_dir) is False


class TestRunNormalizeStage:

    def test_background_defers_without_exit(self, tmp_path: Path, capsys) -> None:
        """BD-13: in --background mode normalize_skills defers and returns
        (does NOT raise SystemExit). Pipeline can continue."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic").mkdir()
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir()

        # Must NOT raise
        _run_normalize_stage([], project_dir, logs_dir, background=True)

        out = capsys.readouterr().out
        assert "NORMALIZE_SKILLS STAGE" in out
        assert "deferred" in out
        assert "WARNING" in out

    def test_background_false_with_mocked_input(self, tmp_path: Path, capsys, monkeypatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        agentic = project_dir / ".agentic"
        logs_dir = agentic / "logs"
        logs_dir.mkdir(parents=True)

        monkeypatch.setattr("builtins.input", lambda: "")

        _run_normalize_stage([], project_dir, logs_dir, background=False)

        captured = capsys.readouterr()
        assert "NORMALIZE_SKILLS STAGE" in captured.out

    def test_empty_team_reads_roles_dir(self, tmp_path: Path, capsys, monkeypatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        agentic = project_dir / ".agentic"
        roles_dir = agentic / "roles"
        roles_dir.mkdir(parents=True)
        (roles_dir / "architect.md").write_text("arch role")
        (roles_dir / "worker.md").write_text("worker role")
        logs_dir = agentic / "logs"
        logs_dir.mkdir()

        monkeypatch.setattr("builtins.input", lambda: "")

        _run_normalize_stage([], project_dir, logs_dir, background=False)

        captured = capsys.readouterr()
        assert "architect" in captured.out
        assert "worker" in captured.out
        assert "(local)" in captured.out

    def test_with_team_prints_members(self, tmp_path: Path, capsys, monkeypatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        agentic = project_dir / ".agentic"
        logs_dir = agentic / "logs"
        logs_dir.mkdir(parents=True)

        team = [
            {"role": "worker", "type": "primary"},
            {"role": "reviewer", "type": "secondary"},
        ]

        monkeypatch.setattr("builtins.input", lambda: "")

        _run_normalize_stage(team, project_dir, logs_dir, background=False)

        captured = capsys.readouterr()
        assert "NORMALIZE_SKILLS STAGE" in captured.out
        assert "worker" in captured.out
        assert "reviewer" in captured.out
        assert "primary" in captured.out


# ── BD-14: supervisor via subprocess in auto mode ─────────────────────────────


class TestSupervisorViaSubprocess:
    """BD-14: in auto mode, supervisor plan/verify spawn opencode subprocess."""

    def _make_proj(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        agentic = proj / ".agentic"
        (agentic / "roles").mkdir(parents=True)
        (agentic / "roles" / "supervisor.md").write_text("# Supervisor\nplan/verify")
        (agentic / "phases").mkdir()
        (agentic / "phases" / "plan.md").write_text("# Plan\nStep 1: do X")
        (agentic / "inbox").mkdir()
        (agentic / "outbox").mkdir()
        (agentic / "context").mkdir()
        (agentic / "logs").mkdir()
        # minimal config.yaml
        (agentic / "config.yaml").write_text(
            "project:\n  name: test\n  root: .\n"
            "models:\n  supervisor:\n    description: current\n"
            "phases:\n  current: .agentic/phases/plan.md\n"
        )
        return proj

    def test_create_todo_spawns_subprocess_when_no_active_todo(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        calls: list[list[str]] = []
        monkeypatch.setattr(
            "awf.orchestrator.subprocess.run",
            lambda cmd, *a, **kw: calls.append(cmd) or _OK_RESULT,
        )

        stage = Stage(name="plan", role="supervisor", action="create_todo", description="d")
        _run_supervisor_stage(stage, todo_id="", auto=True, project_dir=proj, logs_dir=logs)

        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "opencode"
        assert "--auto" in cmd
        assert "--agent" in cmd
        # role file passed
        assert any("supervisor.md" in c for c in cmd)
        # phases file passed
        assert any("plan.md" in c for c in cmd)
        out = capsys.readouterr().out
        assert "Spawning supervisor subprocess" in out

    def test_create_todo_skips_subprocess_when_active_todo_exists(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        # Seed an active TODO
        (proj / ".agentic" / "inbox" / "TODO-0042.md").write_text("# TODO\nbody")
        (proj / ".agentic" / "inbox" / "TODO-0042.ready").write_text("")

        calls: list[list[str]] = []
        monkeypatch.setattr(
            "awf.orchestrator.subprocess.run",
            lambda cmd, *a, **kw: calls.append(cmd) or _OK_RESULT,
        )

        stage = Stage(name="plan", role="supervisor", action="create_todo", description="d")
        _run_supervisor_stage(stage, todo_id="", auto=True, project_dir=proj, logs_dir=logs)

        assert calls == [], "subprocess must NOT be spawned when active TODO exists"
        out = capsys.readouterr().out
        assert "Active TODO already exists" in out
        assert "TODO-0042" in out

    def test_verify_spawns_subprocess_with_done_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"
        (proj / ".agentic" / "outbox" / "DONE-TODO-0042.md").write_text("# DONE\nall good")

        calls: list[list[str]] = []
        monkeypatch.setattr(
            "awf.orchestrator.subprocess.run",
            lambda cmd, *a, **kw: calls.append(cmd) or _OK_RESULT,
        )

        stage = Stage(name="verify", role="supervisor", action="verify_result", description="d")
        _run_supervisor_stage(stage, todo_id="TODO-0042", auto=True, project_dir=proj, logs_dir=logs)

        assert len(calls) == 1
        cmd = calls[0]
        # DONE file is passed as --file
        assert any("DONE-TODO-0042.md" in c for c in cmd)

    def test_auto_no_supervisor_md_falls_back_to_skip(
        self, tmp_path: Path, capsys
    ) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".agentic" / "logs").mkdir(parents=True)
        (proj / ".agentic" / "config.yaml").write_text(
            "project:\n  name: t\nmodels:\n  supervisor:\n    description: x\n"
        )

        stage = Stage(name="plan", role="supervisor", action="create_todo", description="d")
        # Should NOT raise even without supervisor.md
        _run_supervisor_stage(stage, todo_id="", auto=True, project_dir=proj, logs_dir=proj / ".agentic" / "logs")
        out = capsys.readouterr().out
        assert "skipping" in out.lower()

    def test_interactive_mode_waits_for_input(self, tmp_path: Path, monkeypatch) -> None:
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        pressed: list[bool] = []
        def fake_input(*a, **kw):
            pressed.append(True)
            return ""
        monkeypatch.setattr("builtins.input", fake_input)

        stage = Stage(name="plan", role="supervisor", action="create_todo", description="d")
        _run_supervisor_stage(stage, todo_id="", auto=False, project_dir=proj, logs_dir=logs)
        assert pressed == [True], "interactive mode must call input()"

    def test_supervisor_subprocess_failure_raises_bd18(self, tmp_path: Path, monkeypatch) -> None:
        """BD-18: non-zero exit code from supervisor subprocess raises RuntimeError."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        failing = subprocess.CompletedProcess(args=[], returncode=42)
        monkeypatch.setattr(
            "awf.orchestrator.subprocess.run",
            lambda *a, **kw: failing,
        )

        stage = Stage(name="plan", role="supervisor", action="create_todo", description="d")
        with pytest.raises(RuntimeError) as exc_info:
            _run_supervisor_stage(stage, todo_id="", auto=True, project_dir=proj, logs_dir=logs)
        assert "42" in str(exc_info.value)
        assert "Supervisor" in str(exc_info.value)

    def test_agent_subprocess_failure_raises_bd18(self, tmp_path: Path, monkeypatch) -> None:
        """BD-18: non-zero exit code from agent subprocess raises RuntimeError."""
        proj = tmp_path / "proj"
        proj.mkdir()
        agentic = proj / ".agentic"
        (agentic / "roles").mkdir(parents=True)
        (agentic / "roles" / "worker.md").write_text("role")
        (agentic / "inbox").mkdir(parents=True)
        (agentic / "inbox" / "TODO-0001.md").write_text("task")
        (agentic / "logs").mkdir(parents=True)
        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        failing = subprocess.CompletedProcess(args=[], returncode=7)
        monkeypatch.setattr(
            "awf.orchestrator.subprocess.run",
            lambda *a, **kw: failing,
        )

        stage = Stage(name="impl", role="worker", action="execute_todo")
        with pytest.raises(RuntimeError) as exc_info:
            _run_agent_stage(stage, "TODO-0001", proj, {}, agentic / "logs")
        assert "7" in str(exc_info.value)
        assert "worker" in str(exc_info.value)

    def test_replan_skips_when_no_todo_id(self, tmp_path: Path, capsys) -> None:
        """BD-14: replan with empty todo_id should skip gracefully and log."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        stage = Stage(name="replan", role="supervisor", action="replan", description="d")
        _run_supervisor_stage(stage, todo_id="", auto=True, project_dir=proj, logs_dir=logs)

        out = capsys.readouterr().out
        assert "No todo_id for replan" in out
        # Verify it was logged
        log_file = logs / "orchestrator.log"
        assert log_file.exists()
        assert "auto-skipped" in log_file.read_text()

    def test_salvage_skips_in_auto_mode(self, tmp_path: Path, capsys) -> None:
        """BD-14: salvage action is not automatable — should skip."""
        proj = self._make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        stage = Stage(name="salvage", role="supervisor", action="salvage", description="d")
        _run_supervisor_stage(stage, todo_id="TODO-0042", auto=True, project_dir=proj, logs_dir=logs)

        out = capsys.readouterr().out
        assert "not automated" in out


# ── BD-15: handoff chain ──────────────────────────────────────────────────────


class TestHandoffChain:
    """BD-15: agent stages receive previous handoffs as --file and produce
    their own handoff for the next stage."""

    def _setup_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        agentic = project_dir / ".agentic"
        (agentic / "roles").mkdir(parents=True)
        (agentic / "roles" / "worker.md").write_text("role")
        (agentic / "roles" / "developer.md").write_text("role")
        (agentic / "inbox").mkdir(parents=True)
        (agentic / "inbox" / "TODO-0001.md").write_text("task")
        (agentic / "outbox").mkdir(parents=True)
        (agentic / "context").mkdir(parents=True)
        (agentic / "logs").mkdir(parents=True)
        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        return project_dir

    def test_run_agent_stage_forwards_prev_handoffs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-15: prev_handoffs list is passed as --file args."""
        proj = self._setup_project(tmp_path, monkeypatch)
        # Seed handoffs from previous stages
        handoff_dir = proj / ".agentic" / "handoff"
        handoff_dir.mkdir(parents=True)
        (handoff_dir / "system-analysis.md").write_text("# Handoff sys-analysis\n...")
        (handoff_dir / "developer.md").write_text("# Handoff developer\n...")

        stage = Stage(name="qa", role="worker", action="execute_todo")
        captured: list[list[str]] = []
        monkeypatch.setattr(
            "awf.orchestrator.subprocess.run",
            lambda cmd, *a, **kw: captured.append(cmd) or _OK_RESULT,
        )

        prev = [handoff_dir / "system-analysis.md", handoff_dir / "developer.md"]
        _run_agent_stage(stage, "TODO-0001", proj, {}, proj / ".agentic" / "logs", prev_handoffs=prev)

        # First captured cmd is the opencode run invocation
        opencode_cmd = captured[0]
        assert "--file" in opencode_cmd
        # both handoffs should appear
        assert any("system-analysis.md" in c for c in opencode_cmd)
        assert any("developer.md" in c for c in opencode_cmd)

    def test_run_agent_stage_filters_missing_handoffs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-15: prev_handoffs that don't exist on disk are skipped."""
        proj = self._setup_project(tmp_path, monkeypatch)
        handoff_dir = proj / ".agentic" / "handoff"
        handoff_dir.mkdir(parents=True)
        (handoff_dir / "system-analysis.md").write_text("# Handoff\n...")
        # developer.md intentionally NOT created

        stage = Stage(name="qa", role="worker", action="execute_todo")
        captured: list[list[str]] = []
        monkeypatch.setattr(
            "awf.orchestrator.subprocess.run",
            lambda cmd, *a, **kw: captured.append(cmd) or _OK_RESULT,
        )

        prev = [handoff_dir / "system-analysis.md", handoff_dir / "developer.md"]
        _run_agent_stage(stage, "TODO-0001", proj, {}, proj / ".agentic" / "logs", prev_handoffs=prev)

        opencode_cmd = captured[0]
        assert any("system-analysis.md" in c for c in opencode_cmd)
        assert not any("developer.md" in c for c in opencode_cmd)

    def test_collect_handoff_writes_file_with_progress_and_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-15: _collect_handoff writes handoff .md with PROGRESS + DONE."""
        proj = self._setup_project(tmp_path, monkeypatch)
        # Worker wrote progress + done
        (proj / ".agentic" / "outbox" / "PROGRESS-TODO-0001.md").write_text("did X, Y")
        (proj / ".agentic" / "outbox" / "DONE-TODO-0001.md").write_text("# DONE\nall good")

        # Stub git calls
        monkeypatch.setattr(
            "awf.orchestrator.subprocess.run",
            lambda *a, **kw: None,
        )

        out = _collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
        assert out is not None
        assert out.name == "worker-TODO-0001.md"  # BD-19: role-todo naming
        body = out.read_text()
        assert "Handoff from `worker`" in body
        assert "did X, Y" in body
        assert "# DONE" in body
        assert "all good" in body
        assert "next role" in body.lower()

    def test_collect_handoff_skips_git_when_no_baseline(self, tmp_path, monkeypatch) -> None:
        """BD-15: no BASELINE-*.sha → no git diff section (and no git subprocess)."""
        proj = self._setup_project(tmp_path, monkeypatch)
        runs: list = []
        monkeypatch.setattr(
            "awf.orchestrator.subprocess.run",
            lambda cmd, *a, **kw: runs.append(cmd) or None,
        )

        out = _collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
        assert out is not None
        body = out.read_text()
        assert "Git diff summary" not in body
        # Only the "git log" call should have happened (no git diff)
        git_calls = [c for c in runs if isinstance(c, list) and c[:1] == ["git"]]
        # git log may still be called; assert no git diff
        assert not any("diff" in c for c in git_calls)

    def test_collect_handoff_always_writes_even_without_progress_or_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BD-15: _collect_handoff always writes a file (never returns None),
        even when PROGRESS and DONE are both missing."""
        proj = self._setup_project(tmp_path, monkeypatch)
        # Do NOT create PROGRESS or DONE files

        monkeypatch.setattr(
            "awf.orchestrator.subprocess.run",
            lambda *a, **kw: None,
        )

        out = _collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
        assert out is not None
        assert out.exists()
        body = out.read_text()
        assert "Handoff from `worker`" in body
        assert "next role" in body.lower()
        # Should NOT have PROGRESS or DONE sections
        assert "PROGRESS notes" not in body
        assert "DONE summary" not in body

    def test_resolve_prev_handoffs_skips_supervisor_stages(self, tmp_path) -> None:
        """BD-15/19: supervisor stages don't produce handoffs — only agent roles.
        With todo_id, paths are role-todo.md."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".agentic").mkdir()
        stages = [
            Stage(name="plan", role="supervisor", action="create_todo"),
            Stage(name="r1", role="analyst", action="execute_todo"),
            Stage(name="r2", role="dev", action="execute_todo"),
            Stage(name="verify", role="supervisor", action="verify_result"),
            Stage(name="r3", role="qa", action="execute_todo"),
        ]
        # Current stage is index 4 (qa). Previous agent stages = analyst, dev.
        prev = _resolve_prev_handoffs(stages, 4, proj, todo_id="TODO-0042")
        prev_names = [p.name for p in prev]
        assert prev_names == ["analyst-TODO-0042.md", "dev-TODO-0042.md"]

    def test_resolve_prev_handoffs_empty_for_first_agent_stage(self, tmp_path) -> None:
        """First agent stage has no prior handoffs."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".agentic").mkdir()
        stages = [
            Stage(name="plan", role="supervisor", action="create_todo"),
            Stage(name="impl", role="worker", action="execute_todo"),
        ]
        prev = _resolve_prev_handoffs(stages, 1, proj)
        assert prev == []

    def test_handoff_naming_includes_todo_id_bd19(self, tmp_path, monkeypatch) -> None:
        """BD-19: filename is role-<todo>.md so retries don't overwrite prior handoffs."""
        proj = tmp_path / "proj"
        proj.mkdir()
        agentic = proj / ".agentic"
        (agentic / "logs").mkdir(parents=True)
        (agentic / "outbox").mkdir(parents=True)
        (agentic / "context").mkdir(parents=True)

        monkeypatch.setattr(
            "awf.orchestrator.subprocess.run",
            lambda *a, **kw: None,
        )

        # First attempt for TODO-0001
        out1 = _collect_handoff("worker", "TODO-0001", proj, agentic / "logs")
        assert out1.name == "worker-TODO-0001.md"

        # Replan → new TODO-0002 on same role. Previous handoff must survive.
        out2 = _collect_handoff("worker", "TODO-0002", proj, agentic / "logs")
        assert out2.name == "worker-TODO-0002.md"

        # Both files exist (no overwrite)
        handoff_dir = agentic / "handoff"
        assert (handoff_dir / "worker-TODO-0001.md").exists()
        assert (handoff_dir / "worker-TODO-0002.md").exists()

    def test_resolve_prev_handoffs_no_todo_id_falls_back_to_glob(self, tmp_path) -> None:
        """BD-19: when todo_id is empty, scan handoff_dir for <role>-*.md files."""
        proj = tmp_path / "proj"
        proj.mkdir()
        handoff_dir = proj / ".agentic" / "handoff"
        handoff_dir.mkdir(parents=True)
        # Seed multiple handoffs for analyst across TODOs
        (handoff_dir / "analyst-TODO-0001.md").write_text("v1")
        (handoff_dir / "analyst-TODO-0002.md").write_text("v2")  # newer

        stages = [
            Stage(name="r1", role="analyst", action="execute_todo"),
            Stage(name="r2", role="dev", action="execute_todo"),
        ]
        # No todo_id provided — should pick newest analyst-*.md
        prev = _resolve_prev_handoffs(stages, 1, proj, todo_id="")
        assert len(prev) == 1
        assert "analyst-TODO-0002.md" in prev[0].name
