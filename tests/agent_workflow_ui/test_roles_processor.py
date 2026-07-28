"""Tests for roles_processor.py: extract role save/delete logic from HTTP handler."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_workflow_ui.opencode_config import _slugify
from agent_workflow_ui.roles_processor import process_role_deletions, process_role_saves

from agent_workflow_ui import opencode_config, state


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


# ── project copy (BD-3-B) ─────────────────────────────────────────────────────


@pytest.fixture
def reset_project_dir():
    """Reset project_dir after each test."""
    original = state._project_dir
    state._project_dir = None
    yield
    state._project_dir = original


def _make_project(tmp_path: Path) -> Path:
    """Create a project dir with .agentic/roles/ structure."""
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / ".agentic" / "roles").mkdir(parents=True)
    return proj


def test_save_copies_to_project(isolated_roles_dir, reset_project_dir, tmp_path):
    """Save custom agent → copied to project .agentic/roles/."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    data = {
        "team_config": json.dumps([
            {"type": "custom", "agent": "Auditor", "skill_content": "# Auditor\nCheck code.", "save": True},
        ]),
    }
    saved = process_role_saves(data)
    assert saved == 1
    assert (isolated_roles_dir / "auditor.md").exists()
    assert (proj / ".agentic" / "roles" / "auditor.md").exists()
    assert (proj / ".agentic" / "roles" / "auditor.md").read_text() == "# Auditor\nCheck code."


def test_save_supervisor_copies_to_project(isolated_roles_dir, reset_project_dir, tmp_path):
    """Save supervisor → copied to project .agentic/roles/."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    data = {
        "supervisor_content": "# Strict Lead\nBe strict.",
        "save_supervisor": "true",
    }
    saved = process_role_saves(data)
    assert saved == 1
    assert (isolated_roles_dir / "supervisor-strict-lead.md").exists()
    assert (proj / ".agentic" / "roles" / "supervisor-strict-lead.md").exists()


def test_save_no_project_dir(isolated_roles_dir, reset_project_dir):
    """project_dir=None → save succeeds globally, no copy, no error."""
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "custom", "agent": "Worker", "skill_content": "# W", "save": True},
        ]),
    }
    saved = process_role_saves(data)
    assert saved == 1
    assert (isolated_roles_dir / "worker.md").exists()


def test_save_no_agentic_roles(isolated_roles_dir, reset_project_dir, tmp_path, monkeypatch):
    """project_dir set but .agentic/roles/ missing → save globally, no copy."""
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / ".agentic").mkdir()  # .agentic exists but no roles/ subdir
    state._project_dir = proj

    data = {
        "team_config": json.dumps([
            {"type": "custom", "agent": "Tester", "skill_content": "# T", "save": True},
        ]),
    }
    saved = process_role_saves(data)
    assert saved == 1
    assert (isolated_roles_dir / "tester.md").exists()
    assert not (proj / ".agentic" / "roles").exists()


def test_delete_copies_to_project(isolated_roles_dir, reset_project_dir, tmp_path):
    """Delete custom role → also deleted from project .agentic/roles/."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    # Create role in both global and project
    content = "# Worker\nDo work."
    (isolated_roles_dir / "worker.md").write_text(content)
    (proj / ".agentic" / "roles" / "worker.md").write_text(content)

    data = {"delete_agent": "worker"}
    deleted = process_role_deletions(data)
    assert deleted == 1
    assert not (isolated_roles_dir / "worker.md").exists()
    assert not (proj / ".agentic" / "roles" / "worker.md").exists()


def test_delete_project_only_global_exists(isolated_roles_dir, reset_project_dir, tmp_path):
    """Delete when project copy doesn't exist → no error."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    (isolated_roles_dir / "worker.md").write_text("x")
    data = {"delete_agent": "worker"}
    deleted = process_role_deletions(data)
    assert deleted == 1
    assert not (isolated_roles_dir / "worker.md").exists()


def test_delete_no_project_dir(isolated_roles_dir, reset_project_dir):
    """project_dir=None → delete succeeds globally, no project side-effect."""
    state._project_dir = None
    (isolated_roles_dir / "worker.md").write_text("x")

    data = {"delete_agent": "worker"}
    deleted = process_role_deletions(data)
    assert deleted == 1
    assert not (isolated_roles_dir / "worker.md").exists()


# ── BD-5-B: copy existing supervisor variant ──────────────────────────────────


def test_bd5b_existing_supervisor_variant_copied(isolated_roles_dir, reset_project_dir, tmp_path):
    """supervisor_role set, no new content → copy existing variant to project."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    (isolated_roles_dir / "supervisor-architect.md").write_text("# Architect\nBe smart.")

    data = {
        "supervisor_role": "supervisor-architect",
        "supervisor_content": "",
    }
    saved = process_role_saves(data)
    assert saved == 1
    assert (proj / ".agentic" / "roles" / "supervisor-architect.md").exists()
    assert (proj / ".agentic" / "roles" / "supervisor-architect.md").read_text() == "# Architect\nBe smart."


def test_bd5b_supervisor_default_no_copy(isolated_roles_dir, reset_project_dir, tmp_path):
    """supervisor_role='default' → no copy."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    (isolated_roles_dir / "supervisor-architect.md").write_text("# Architect")

    data = {
        "supervisor_role": "default",
        "supervisor_content": "",
    }
    saved = process_role_saves(data)
    assert saved == 0
    assert not list((proj / ".agentic" / "roles").iterdir())


def test_bd5b_supervisor_nonexistent_no_error(isolated_roles_dir, reset_project_dir, tmp_path):
    """supervisor_role points to missing file → no error, no copy."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    data = {
        "supervisor_role": "nonexistent-variant",
        "supervisor_content": "",
    }
    saved = process_role_saves(data)
    assert saved == 0
    assert not list((proj / ".agentic" / "roles").iterdir())


def test_bd5b_new_content_takes_precedence(isolated_roles_dir, reset_project_dir, tmp_path):
    """When supervisor_content is provided with save_supervisor, BD-5-B path is skipped."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    (isolated_roles_dir / "supervisor-architect.md").write_text("# Old Architect")

    data = {
        "supervisor_role": "supervisor-architect",
        "supervisor_content": "# New Architect\nFresh content.",
        "save_supervisor": "true",
    }
    saved = process_role_saves(data)
    assert saved == 1
    proj_role = proj / ".agentic" / "roles" / "supervisor-new-architect.md"
    assert proj_role.exists()
    assert "Fresh content" in proj_role.read_text()


# ── BD-5-C: copy existing custom agents from agent[] ──────────────────────────


def test_bd5c_custom_agent_copied(isolated_roles_dir, reset_project_dir, tmp_path):
    """agent=['auditor'] where auditor.md exists globally → copy to project."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    (isolated_roles_dir / "auditor.md").write_text("# Auditor\nCheck code.")

    data = {
        "agent": ["worker", "tester", "auditor"],
        "team_config": "[]",
    }
    saved = process_role_saves(data)
    assert saved == 1
    assert (proj / ".agentic" / "roles" / "auditor.md").exists()
    assert not (proj / ".agentic" / "roles" / "worker.md").exists()
    assert not (proj / ".agentic" / "roles" / "tester.md").exists()


def test_bd5c_single_string_agent(isolated_roles_dir, reset_project_dir, tmp_path):
    """agent='auditor' (single string, not list) → handled correctly."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    (isolated_roles_dir / "auditor.md").write_text("# Auditor")

    data = {
        "agent": "auditor",
        "team_config": "[]",
    }
    saved = process_role_saves(data)
    assert saved == 1
    assert (proj / ".agentic" / "roles" / "auditor.md").exists()


def test_bd5c_only_default_agents_no_copy(isolated_roles_dir, reset_project_dir, tmp_path):
    """agent=['worker', 'tester'] (all default) → no copies."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    (isolated_roles_dir / "worker.md").write_text("# Worker")
    (isolated_roles_dir / "tester.md").write_text("# Tester")

    data = {
        "agent": ["worker", "tester"],
        "team_config": "[]",
    }
    saved = process_role_saves(data)
    assert saved == 0
    assert not list((proj / ".agentic" / "roles").iterdir())


def test_bd5c_no_double_copy_with_team_config(isolated_roles_dir, reset_project_dir, tmp_path):
    """Custom agent saved via team_config AND in agent[] → copied only once."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    (isolated_roles_dir / "auditor.md").write_text("# Old Auditor")

    slug = _slugify("Auditor")
    data = {
        "agent": ["worker", "auditor"],
        "team_config": json.dumps([
            {"type": "custom", "agent": "Auditor", "skill_content": "# New Auditor\nFresh.", "save": True},
        ]),
    }
    saved = process_role_saves(data)
    assert saved == 1
    assert (proj / ".agentic" / "roles" / "auditor.md").read_text() == "# New Auditor\nFresh."


def test_bd5c_multiple_custom_agents(isolated_roles_dir, reset_project_dir, tmp_path):
    """Multiple custom agents in agent[] → all copied."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    (isolated_roles_dir / "auditor.md").write_text("# Auditor")
    (isolated_roles_dir / "system-analysis.md").write_text("# SA")

    data = {
        "agent": ["worker", "auditor", "system-analysis"],
        "team_config": "[]",
    }
    saved = process_role_saves(data)
    assert saved == 2
    assert (proj / ".agentic" / "roles" / "auditor.md").exists()
    assert (proj / ".agentic" / "roles" / "system-analysis.md").exists()


def test_bd5c_missing_custom_agent_skipped(isolated_roles_dir, reset_project_dir, tmp_path):
    """agent=['phantom'] where phantom.md doesn't exist globally → no error."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    data = {
        "agent": ["phantom"],
        "team_config": "[]",
    }
    saved = process_role_saves(data)
    assert saved == 0
    assert not list((proj / ".agentic" / "roles").iterdir())


# ── BD-6: explicit project_dir parameter (overrides cwd-based state) ──────────


def _make_project(tmp_path: Path) -> Path:
    """Create a project dir with .agentic/roles/ structure."""
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / ".agentic" / "roles").mkdir(parents=True)
    return proj


def test_bd6_saves_with_explicit_project_dir(isolated_roles_dir, reset_project_dir, tmp_path):
    """process_role_saves with project_dir= copies to that project, not cwd-based."""
    proj = _make_project(tmp_path)
    # Ensure global state says NO project — but we pass project_dir explicitly
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "custom", "agent": "Auditor", "skill_content": "# Auditor\nCheck code.", "save": True},
        ]),
    }
    saved = process_role_saves(data, project_dir=proj)
    assert saved == 1
    assert (isolated_roles_dir / "auditor.md").exists()
    assert (proj / ".agentic" / "roles" / "auditor.md").exists()
    assert (proj / ".agentic" / "roles" / "auditor.md").read_text() == "# Auditor\nCheck code."


def test_bd6_saves_supervisor_with_explicit_project_dir(isolated_roles_dir, reset_project_dir, tmp_path):
    """process_role_saves with project_dir= copies supervisor to that project."""
    proj = _make_project(tmp_path)
    state._project_dir = None

    data = {
        "supervisor_content": "# Strict Lead\nBe strict.",
        "save_supervisor": "true",
    }
    saved = process_role_saves(data, project_dir=proj)
    assert saved == 1
    assert (isolated_roles_dir / "supervisor-strict-lead.md").exists()
    assert (proj / ".agentic" / "roles" / "supervisor-strict-lead.md").exists()


def test_bd6_saves_no_project_dir_fallback(isolated_roles_dir, reset_project_dir, tmp_path):
    """process_role_saves with project_dir=None falls back to state._project_dir."""
    proj = _make_project(tmp_path)
    state._project_dir = proj

    data = {
        "team_config": json.dumps([
            {"type": "custom", "agent": "Worker", "skill_content": "# W", "save": True},
        ]),
    }
    saved = process_role_saves(data, project_dir=None)
    assert saved == 1
    assert (isolated_roles_dir / "worker.md").exists()
    assert (proj / ".agentic" / "roles" / "worker.md").exists()


def test_bd6_deletions_with_explicit_project_dir(isolated_roles_dir, reset_project_dir, tmp_path):
    """process_role_deletions with project_dir= deletes from that project."""
    proj = _make_project(tmp_path)
    state._project_dir = None

    content = "# Worker\nDo work."
    (isolated_roles_dir / "worker.md").write_text(content)
    (proj / ".agentic" / "roles" / "worker.md").write_text(content)

    data = {"delete_agent": "worker"}
    deleted = process_role_deletions(data, project_dir=proj)
    assert deleted == 1
    assert not (isolated_roles_dir / "worker.md").exists()
    assert not (proj / ".agentic" / "roles" / "worker.md").exists()


def test_bd6_saves_project_dir_no_agentic_roles(isolated_roles_dir, reset_project_dir, tmp_path):
    """project_dir set but .agentic/roles/ missing → save globally, no copy."""
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / ".agentic").mkdir()
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "custom", "agent": "Tester", "skill_content": "# T", "save": True},
        ]),
    }
    saved = process_role_saves(data, project_dir=proj)
    assert saved == 1
    assert (isolated_roles_dir / "tester.md").exists()
    assert not (proj / ".agentic" / "roles").exists()


def test_bd6_bd5b_explicit_project_dir(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-5-B supervisor_role copy uses explicit project_dir, not state."""
    proj = _make_project(tmp_path)
    state._project_dir = None

    (isolated_roles_dir / "supervisor-architect.md").write_text("# Architect\nBe smart.")

    data = {
        "supervisor_role": "supervisor-architect",
        "supervisor_content": "",
    }
    saved = process_role_saves(data, project_dir=proj)
    assert saved == 1
    assert (proj / ".agentic" / "roles" / "supervisor-architect.md").exists()


def test_bd6_bd5c_explicit_project_dir(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-5-C agent[] copy uses explicit project_dir, not state."""
    proj = _make_project(tmp_path)
    state._project_dir = None

    (isolated_roles_dir / "auditor.md").write_text("# Auditor")

    data = {
        "agent": ["auditor"],
        "team_config": "[]",
    }
    saved = process_role_saves(data, project_dir=proj)
    assert saved == 1
    assert (proj / ".agentic" / "roles" / "auditor.md").exists()
