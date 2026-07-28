"""Unit tests for awf.orchestrator — role file resolution."""
from pathlib import Path
from unittest.mock import patch

import pytest

from awf.orchestrator import (
    _check_needs_normalize,
    _check_skill_drift,
    _consume_needs_normalize,
    _global_roles_dir,
    _maybe_commit,
    _needs_normalize,
    _resolve_role_file,
    _run_agent_stage,
    _run_normalize_stage,
)
from awf.pipeline import Stage


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

    def test_raises_systemexit_background(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic").mkdir()
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir()

        with pytest.raises(SystemExit) as exc_info:
            _run_normalize_stage([], project_dir, logs_dir, background=True)
        assert exc_info.value.code == 1

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
