"""Tests for roles_processor.py: extract role save/delete logic from HTTP handler."""
from __future__ import annotations

import json

import pytest
from agent_workflow_ui.roles_processor import process_role_deletions, process_role_saves

from agent_workflow_ui import opencode_config


@pytest.fixture
def isolated_roles_dir(tmp_path, monkeypatch):
    """Redirect GLOBAL_ROLES_DIR to tmp_path/roles (pre-created)."""
    roles = tmp_path / "roles"
    roles.mkdir(parents=True)
    monkeypatch.setattr(opencode_config, "GLOBAL_ROLES_DIR", roles)
    return roles


# ── process_role_saves ────────────────────────────────────────────────────────


def test_saves_no_data(isolated_roles_dir):
    """Empty data dict → no-op, returns 0."""
    assert process_role_saves({}) == 0
    assert not isolated_roles_dir.exists() or not list(isolated_roles_dir.iterdir())


def test_saves_custom_agent_with_save_flag(isolated_roles_dir):
    """Custom agent in team_config with save=true is saved."""
    data = {
        "team_config": json.dumps([
            {"type": "custom", "agent": "ML Engineer", "skill_content": "# ML\n...", "save": True},
        ]),
    }
    saved = process_role_saves(data)
    assert saved == 1
    assert (isolated_roles_dir / "ml-engineer.md").exists()


def test_saves_custom_agent_skips_without_save_flag(isolated_roles_dir):
    """Custom agent with save=false is NOT saved."""
    data = {
        "team_config": json.dumps([
            {"type": "custom", "agent": "ML Engineer", "skill_content": "# ML\n...", "save": False},
        ]),
    }
    assert process_role_saves(data) == 0
    assert not (isolated_roles_dir / "ml-engineer.md").exists()


def test_saves_default_agent_not_saved(isolated_roles_dir):
    """Default (non-custom) agent entries are not saved as roles."""
    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "worker", "model": "x", "save": True},
        ]),
    }
    assert process_role_saves(data) == 0


def test_saves_supervisor_variant(isolated_roles_dir):
    """Supervisor content + save_supervisor=true → saved with supervisor- prefix."""
    data = {
        "supervisor_content": "# Strict Lead\nYou are strict.",
        "save_supervisor": "true",
    }
    saved = process_role_saves(data)
    assert saved == 1
    assert (isolated_roles_dir / "supervisor-strict-lead.md").exists()


def test_saves_supervisor_skipped_without_flag(isolated_roles_dir):
    """Supervisor content without save flag → not saved."""
    data = {
        "supervisor_content": "# Strict Lead\n...",
        "save_supervisor": "false",
    }
    assert process_role_saves(data) == 0


def test_saves_supervisor_empty_content(isolated_roles_dir):
    """Empty supervisor_content → not saved (no .md heading to derive name from)."""
    data = {"supervisor_content": "", "save_supervisor": "true"}
    assert process_role_saves(data) == 0


def test_saves_malformed_team_json(isolated_roles_dir):
    """Malformed team_config JSON → no crash, no saves."""
    data = {"team_config": "not valid json"}
    assert process_role_saves(data) == 0


def test_saves_team_json_not_list(isolated_roles_dir):
    """team_config JSON that parses to non-list → no crash."""
    data = {"team_config": json.dumps({"not": "a list"})}
    assert process_role_saves(data) == 0


def test_saves_team_member_not_dict(isolated_roles_dir):
    """team_config with non-dict entries → no crash."""
    data = {"team_config": json.dumps(["string", 42, None])}
    assert process_role_saves(data) == 0


def test_saves_multiple_agents(isolated_roles_dir):
    """Multiple custom agents in one submit — all saved."""
    data = {
        "team_config": json.dumps([
            {"type": "custom", "agent": "Backend Dev", "skill_content": "# B", "save": True},
            {"type": "custom", "agent": "Frontend Dev", "skill_content": "# F", "save": True},
            {"type": "custom", "agent": "QA", "skill_content": "# Q", "save": False},  # skipped
        ]),
    }
    saved = process_role_saves(data)
    assert saved == 2
    assert (isolated_roles_dir / "backend-dev.md").exists()
    assert (isolated_roles_dir / "frontend-dev.md").exists()
    assert not (isolated_roles_dir / "qa.md").exists()


# ── process_role_deletions ────────────────────────────────────────────────────


def test_deletions_empty_string(isolated_roles_dir):
    """Empty delete_agent field → no-op."""
    assert process_role_deletions({}) == 0
    assert process_role_deletions({"delete_agent": ""}) == 0


def test_deletions_single(isolated_roles_dir):
    """Single name in delete_agent → deletes if file exists."""
    (isolated_roles_dir / "worker.md").write_text("x")
    data = {"delete_agent": "worker"}
    assert process_role_deletions(data) == 1
    assert not (isolated_roles_dir / "worker.md").exists()


def test_deletions_comma_separated(isolated_roles_dir):
    """Comma-separated names → all processed."""
    (isolated_roles_dir / "a.md").write_text("x")
    (isolated_roles_dir / "b.md").write_text("x")
    data = {"delete_agent": "a, b"}
    assert process_role_deletions(data) == 2


def test_deletions_missing_no_error(isolated_roles_dir):
    """Missing files don't error, return 0."""
    data = {"delete_agent": "nonexistent"}
    assert process_role_deletions(data) == 0


def test_deletions_path_traversal_rejected(isolated_roles_dir):
    """Path traversal attempts in delete_agent are rejected."""
    parent = isolated_roles_dir.parent
    (parent / "secret.md").write_text("secret")
    data = {"delete_agent": "../secret"}
    assert process_role_deletions(data) == 0
    assert (parent / "secret.md").exists()


def test_deletions_mixed_existing_and_missing(isolated_roles_dir):
    """Mix of existing and missing names → only existing counted."""
    (isolated_roles_dir / "x.md").write_text("x")
    data = {"delete_agent": "x, nope1, nope2"}
    assert process_role_deletions(data) == 1
