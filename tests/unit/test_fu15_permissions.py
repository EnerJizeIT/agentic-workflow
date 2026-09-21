"""FU-15: 0600 permissions and atomic-write discipline.

Findings covered:
- AUD14-06 — sensitive artifacts must be born 0600 (backups, start config,
  awf-start.out, orchestrator.log, worker log, submit yaml, SKILL.md).
- AUD01-06 — atomic_write_text must not flip an existing file's mode.
- AUD07-06 — MCP block edit lives in opencode_agents (ensure_mcp_block),
  atomic write, backup only after a successful parse.
- AUD07-09 — apply backs up only when something is written; unique ts;
  propose detail mentions the ``* -> deny`` default it introduces.
- AUD09-08 — skill_installer writes SKILL.md atomically.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf import api, opencode_agents
from awf._atomic import atomic_write_text


def _mode(p: Path) -> int:
    return p.stat().st_mode & 0o777


@pytest.fixture
def awf_project(tmp_git_repo):
    """Project with .agentic/ initialized (config.yaml + supervisor.md)."""
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


def _cfg(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "opencode.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(p)


# ─── AUD01-06: atomic_write_text preserves existing mode ────────────────


class TestAtomicWriteModes:
    def test_overwrite_preserves_0644(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_text("old")
        p.chmod(0o644)
        atomic_write_text(p, "new")
        assert _mode(p) == 0o644

    def test_overwrite_preserves_0600(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_text("old")
        p.chmod(0o600)
        atomic_write_text(p, "new")
        assert _mode(p) == 0o600

    def test_new_file_is_0600(self, tmp_path):
        p = tmp_path / "new.txt"
        atomic_write_text(p, "x")
        assert _mode(p) == 0o600

    def test_explicit_mode_wins(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_text("old")
        p.chmod(0o644)
        atomic_write_text(p, "new", mode=0o600)
        assert _mode(p) == 0o600


# ─── AUD14-06: sensitive files born 0600 ─────────────────────────────────


class TestSetupBackupPermissions:
    def test_config_backups_are_0600_fresh_and_repeat(self, awf_project):
        cdir = awf_project / ".agentic"
        assert api.setup.update_config_role_mapping(
            [{"role": "developer"}], awf_project
        ) is True
        baks = list(cdir.glob("config.yaml*.bak"))
        assert baks, "expected .bak + timestamped backup"
        for b in baks:
            assert _mode(b) == 0o600, b

        # repeat run: a second change must also leave 0600 backups
        assert api.setup.update_config_role_mapping(
            [{"role": "developer", "model": "vllm/x"}, {"role": "qa"}],
            awf_project,
        ) is True
        for b in cdir.glob("config.yaml*.bak"):
            assert _mode(b) == 0o600, b


def test_fresh_init_config_is_0600(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="T")
    cfg = tmp_git_repo / ".agentic" / "config.yaml"
    assert _mode(cfg) == 0o600


def test_orchestrator_log_is_0600(tmp_path):
    from awf import _log

    _log.log(tmp_path, "hello")
    f = tmp_path / "orchestrator.log"
    assert f.is_file()
    assert _mode(f) == 0o600


class _FakeProc:
    pid = 4242


def test_awf_start_out_is_0600(tmp_git_repo, monkeypatch):
    from awf.api import _background

    monkeypatch.setattr(
        _background.subprocess, "Popen", lambda *a, **k: _FakeProc()
    )
    _background.start_in_background(
        tmp_git_repo, pipeline=None, from_stage=None, auto=False, timeout=60
    )
    log_file = tmp_git_repo / ".agentic" / "logs" / "awf-start.out"
    assert log_file.is_file()
    assert _mode(log_file) == 0o600


def test_worker_log_is_0600(tmp_path):
    from awf import signal_watch

    logs = tmp_path / "logs"
    logs.mkdir()
    cp = signal_watch.run_subprocess_until_signal(
        ["true"],
        cwd=str(tmp_path),
        watch_paths=[tmp_path / "DONE.ready"],
        logs_dir=logs,
        hard_timeout=30,
    )
    assert cp.returncode == 0
    wlog = logs / "worker-output.out"
    assert wlog.is_file()
    assert _mode(wlog) == 0o600


def test_atomic_write_yaml_is_0600(tmp_path):
    from agent_workflow_ui.http_endpoint import _atomic_write_yaml

    target = tmp_path / "FORM-x.yaml"
    _atomic_write_yaml(target, {"a": 1})
    assert _mode(target) == 0o600


def test_skill_install_is_0600(tmp_path, monkeypatch):
    from agent_workflow_ui import skill_installer

    monkeypatch.setattr(skill_installer, "xdg_config_home", lambda: tmp_path)
    assert skill_installer.ensure_skill_installed() is True
    target = tmp_path / "opencode" / "skills" / "agent-workflow-ui" / "SKILL.md"
    assert target.is_file()
    assert _mode(target) == 0o600


# ─── AUD07-06: ensure_mcp_block in opencode_agents ───────────────────────


class TestEnsureMcpBlock:
    def test_adds_block_and_backs_up(self, tmp_path):
        cfg = _cfg(tmp_path, {"agent": {}})
        result = opencode_agents.ensure_mcp_block(cfg)
        assert not result.startswith("ERR"), result
        data = json.loads(Path(cfg).read_text(encoding="utf-8"))
        assert data["mcp"]["agent-workflow-ui"]["command"] == [
            "python3",
            "-m",
            "agent_workflow_ui",
        ]
        assert any(Path(cfg).parent.glob("opencode.json.bak-*"))

    def test_broken_json_no_change_no_backup(self, tmp_path):
        cfg = tmp_path / "opencode.json"
        cfg.write_text("{not json", encoding="utf-8")
        result = opencode_agents.ensure_mcp_block(str(cfg))
        assert result.startswith("ERR"), result
        assert cfg.read_text(encoding="utf-8") == "{not json"
        assert not list(Path(cfg).parent.glob("*.bak-*"))

    def test_root_not_dict_is_err(self, tmp_path):
        cfg = tmp_path / "opencode.json"
        cfg.write_text("[1, 2]", encoding="utf-8")
        result = opencode_agents.ensure_mcp_block(str(cfg))
        assert result.startswith("ERR"), result
        assert json.loads(cfg.read_text(encoding="utf-8")) == [1, 2]

    def test_mcp_not_dict_is_err(self, tmp_path):
        cfg = _cfg(tmp_path, {"mcp": "oops"})
        result = opencode_agents.ensure_mcp_block(cfg)
        assert result.startswith("ERR"), result
        assert json.loads(Path(cfg).read_text(encoding="utf-8")) == {"mcp": "oops"}

    def test_already_present_no_backup(self, tmp_path):
        cfg = _cfg(tmp_path, {"mcp": {"agent-workflow-ui": {
            "type": "local",
            "command": ["python3", "-m", "agent_workflow_ui"],
        }}})
        result = opencode_agents.ensure_mcp_block(cfg)
        assert "already" in result
        assert not list(Path(cfg).parent.glob("*.bak-*"))


# ─── AUD07-09: apply backup discipline + propose completeness ────────────


class TestApplyBackupDiscipline:
    def test_noop_apply_creates_no_backup(self, tmp_path):
        cfg = _cfg(tmp_path, {"agent": {"worker": {
            "description": "w",
            "model": "model-a",
            "permission": {"external_directory": {
                "/tmp/opencode/**": "allow",
                "/tmp/pytest-*": "allow",
                "/tmp/pytest-*/**": "allow",
            }},
        }}})
        result = opencode_agents.apply(cfg, ["worker"], "model-a")
        assert "Nothing to write" in result
        assert not list(Path(cfg).parent.glob("*.bak-*"))

    def test_two_applies_same_second_two_backups(self, tmp_path):
        cfg = _cfg(tmp_path, {"agent": {}})
        opencode_agents.apply(cfg, ["worker"], "model-a")
        opencode_agents.apply(cfg, ["worker"], "model-b")
        baks = sorted(Path(cfg).parent.glob("opencode.json.bak-*"))
        assert len(baks) == 2, [b.name for b in baks]

    def test_propose_detail_mentions_deny_default(self, tmp_path):
        cfg = _cfg(tmp_path, {"agent": {"worker": {"description": "w"}}})
        p = opencode_agents.propose(cfg, ["worker"], "")
        assert p.kind is opencode_agents.ProposalKind.PROPOSE
        assert "deny" in p.detail
