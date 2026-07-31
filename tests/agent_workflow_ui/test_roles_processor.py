"""Tests for roles_processor.py: extract role save/delete logic from HTTP handler."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_workflow_ui.opencode_config import _slugify
from agent_workflow_ui.roles_processor import (
    _delete_from_project,
    process_role_deletions,
    process_role_saves,
)

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


# ── BD-9: process_role_saves triggers pipeline write ──────────────────────────


def test_bd9_pipeline_written_with_team_and_project(isolated_roles_dir, reset_project_dir, tmp_path):
    """process_role_saves with team_config + project_dir → writes pipeline.yaml."""
    proj = _make_project(tmp_path)
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "worker"},
            {"type": "default", "agent": "tester"},
        ]),
    }
    process_role_saves(data, project_dir=proj)

    target = proj / ".agentic" / "pipelines" / "default.yaml"
    assert target.exists()
    import yaml as _yaml

    parsed = _yaml.safe_load(target.read_text())
    assert len(parsed["stages"]) == 4
    assert [s["name"] for s in parsed["stages"]] == ["plan", "worker", "tester", "verify"]


def test_bd9_pipeline_skipped_no_project_dir(isolated_roles_dir, reset_project_dir, tmp_path):
    """process_role_saves with project_dir=None → no pipeline written."""
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "worker"},
        ]),
    }
    process_role_saves(data, project_dir=None)

    # Should not crash and should not try to write pipeline
    # (no .agentic/ in tmp_path so nothing to check)


def test_bd9_pipeline_skipped_malformed_json(isolated_roles_dir, reset_project_dir, tmp_path):
    """process_role_saves with malformed team_config → no crash, no pipeline."""
    proj = _make_project(tmp_path)
    state._project_dir = None

    data = {
        "team_config": "not valid json at all",
    }
    saved = process_role_saves(data, project_dir=proj)
    assert saved == 0
    assert not (proj / ".agentic" / "pipelines").exists()


# ── BD-10-B: process_role_saves triggers normalize state ──────────────────────


def test_bd10b_normalize_state_removed_legacy(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-13 legacy removed: process_role_saves no longer writes needs_normalize.yaml.

    mark_needs_normalize was deleted (BD-13 normalize stage removed).
    This test verifies the file is NOT created — protects against regression.
    """
    proj = _make_project(tmp_path)
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "worker"},
            {"type": "custom", "agent": "auditor", "skill_content": "# A", "save": True},
        ]),
    }
    process_role_saves(data, project_dir=proj)

    target = proj / ".agentic" / "state" / "needs_normalize.yaml"
    assert not target.exists(), (
        "BD-13 legacy: needs_normalize.yaml should NOT be written "
        "(normalize stage was removed)"
    )


def test_bd10b_normalize_state_no_project_dir(isolated_roles_dir, reset_project_dir, tmp_path):
    """process_role_saves with project_dir=None → no error."""
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "worker"},
        ]),
    }
    process_role_saves(data, project_dir=None)


def test_bd10b_normalize_state_no_team_config(isolated_roles_dir, reset_project_dir, tmp_path):
    """process_role_saves without team_config → no error."""
    proj = _make_project(tmp_path)
    state._project_dir = None

    data = {
        "supervisor_content": "# Lead\n...",
        "save_supervisor": "true",
    }
    process_role_saves(data, project_dir=proj)

    target = proj / ".agentic" / "state" / "needs_normalize.yaml"
    assert not target.exists()


def test_bd10b_normalize_state_no_agentic(isolated_roles_dir, reset_project_dir, tmp_path):
    """project_dir without .agentic/ → no state file written (back-compat)."""
    proj = tmp_path / "project"
    proj.mkdir()
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "worker"},
        ]),
    }
    process_role_saves(data, project_dir=proj)


# ── BD-12: config.yaml role→agent_name mapping ────────────────────────────────


def _make_project_with_config(tmp_path: Path, config_yaml: str = "") -> Path:
    """Project with .agentic/roles/ + config.yaml pre-populated."""
    import yaml as _yaml

    proj = tmp_path / "project"
    proj.mkdir()
    (proj / ".agentic" / "roles").mkdir(parents=True)
    cfg = proj / ".agentic" / "config.yaml"
    if config_yaml:
        cfg.write_text(config_yaml)
    else:
        cfg.write_text(_yaml.safe_dump({
            "project": {"name": "test", "root": "."},
            "models": {
                "supervisor": {"description": "current session"},
                "worker": {"agent_name": "worker", "model": "vllm/llm"},
            },
        }, allow_unicode=True, sort_keys=False))
    return proj


def test_bd12_maps_team_roles_to_worker_agent(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-12: each team role gets agent_name=worker if not already set."""
    proj = _make_project_with_config(tmp_path)
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "system-analysis"},
            {"type": "default", "agent": "developer"},
            {"type": "default", "agent": "qa"},
        ]),
    }
    process_role_saves(data, project_dir=proj)

    import yaml as _yaml

    cfg = _yaml.safe_load((proj / ".agentic" / "config.yaml").read_text())
    models = cfg["models"]
    assert models["system-analysis"]["agent_name"] == "worker"
    assert models["developer"]["agent_name"] == "worker"
    assert models["qa"]["agent_name"] == "worker"
    # supervisor and worker untouched
    assert "agent_name" not in models["supervisor"]
    assert models["worker"]["agent_name"] == "worker"


def test_bd12_preserves_existing_agent_name(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-12: existing agent_name is NOT overwritten."""
    import yaml as _yaml

    initial = {
        "models": {
            "supervisor": {"description": "x"},
            "worker": {"agent_name": "worker"},
            "qa": {"agent_name": "custom-qa-agent", "model": "vllm/llm"},
        },
    }
    proj = _make_project_with_config(tmp_path, _yaml.safe_dump(initial, sort_keys=False))
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "qa"},
            {"type": "default", "agent": "developer"},
        ]),
    }
    process_role_saves(data, project_dir=proj)

    cfg = _yaml.safe_load((proj / ".agentic" / "config.yaml").read_text())
    assert cfg["models"]["qa"]["agent_name"] == "custom-qa-agent"
    assert cfg["models"]["qa"]["model"] == "vllm/llm"
    assert cfg["models"]["developer"]["agent_name"] == "worker"


def test_bd12_creates_config_backup(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-12: original config.yaml is backed up to config.yaml.bak."""
    proj = _make_project_with_config(tmp_path)
    original = (proj / ".agentic" / "config.yaml").read_text()
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "developer"},
        ]),
    }
    process_role_saves(data, project_dir=proj)

    backup = proj / ".agentic" / "config.yaml.bak"
    assert backup.exists()
    assert backup.read_text() == original


def test_bd12_idempotent_on_resubmit(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-12: re-submitting same team doesn't create churn (config identical)."""

    proj = _make_project_with_config(tmp_path)
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "developer"},
        ]),
    }
    process_role_saves(data, project_dir=proj)
    first = (proj / ".agentic" / "config.yaml").read_text()

    # Second submit with same team — should be identical (no double backup churn)
    process_role_saves(data, project_dir=proj)
    second = (proj / ".agentic" / "config.yaml").read_text()

    assert first == second


def test_bd12_no_config_yaml_logs_and_skips(isolated_roles_dir, reset_project_dir, tmp_path, caplog):
    """BD-12: missing config.yaml — log warning, no crash."""
    proj = _make_project(tmp_path)  # no config.yaml
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "developer"},
        ]),
    }
    # Should not raise
    process_role_saves(data, project_dir=proj)
    # No config.yaml created
    assert not (proj / ".agentic" / "config.yaml").exists()


def test_bd12_skips_supervisor_role(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-12: supervisor role is never mapped (it's the current session)."""
    import yaml as _yaml

    proj = _make_project_with_config(tmp_path)
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "supervisor"},
            {"type": "default", "agent": "developer"},
        ]),
    }
    process_role_saves(data, project_dir=proj)

    cfg = _yaml.safe_load((proj / ".agentic" / "config.yaml").read_text())
    assert "agent_name" not in cfg["models"]["supervisor"]
    assert cfg["models"]["developer"]["agent_name"] == "worker"


# ── _delete_from_project path traversal (F2) ─────────────────────────────────


def test_delete_from_project_path_traversal_dotdot(isolated_roles_dir, tmp_path):
    """_delete_from_project with ../.. is rejected."""
    proj = _make_project(tmp_path)
    secret = tmp_path / "secret.md"
    secret.write_text("secret")
    _delete_from_project("../../secret.md", project_dir=proj)
    assert secret.exists()


def test_delete_from_project_path_traversal_slash(isolated_roles_dir, tmp_path):
    """_delete_from_project with / is rejected."""
    proj = _make_project(tmp_path)
    secret = tmp_path / "secret.md"
    secret.write_text("secret")
    _delete_from_project(f"../{tmp_path.name}/secret.md", project_dir=proj)
    assert secret.exists()


def test_delete_from_project_path_traversal_backslash(isolated_roles_dir, tmp_path):
    """_delete_from_project with \\ is rejected."""
    proj = _make_project(tmp_path)
    _delete_from_project("..\\..\\secret.md", project_dir=proj)


def test_delete_from_project_dotdot_alone(isolated_roles_dir, tmp_path):
    """_delete_from_project with '..' is rejected."""
    proj = _make_project(tmp_path)
    _delete_from_project("..", project_dir=proj)


def test_delete_from_project_dot_alone(isolated_roles_dir, tmp_path):
    """_delete_from_project with '.' is rejected."""
    proj = _make_project(tmp_path)
    _delete_from_project(".", project_dir=proj)


def test_delete_from_project_normal_file(isolated_roles_dir, tmp_path):
    """_delete_from_project with a normal filename works."""
    proj = _make_project(tmp_path)
    role_file = proj / ".agentic" / "roles" / "normal.md"
    role_file.write_text("content")
    _delete_from_project("normal.md", project_dir=proj)
    assert not role_file.exists()


def test_delete_from_project_normal_missing(isolated_roles_dir, tmp_path):
    """_delete_from_project with a normal filename that doesn't exist — no error."""
    proj = _make_project(tmp_path)
    _delete_from_project("nonexistent.md", project_dir=proj)


# ── BD-32: per-role model assignment from form ────────────────────────────────


def test_bd32_model_from_form_saved_to_config(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-32: model selection from form is persisted to config.yaml.

    Before fix: form sent {agent: 'project-auditor', model: 'anthropic/claude'}
    but update_config_role_mapping only saved agent_name. Model was dropped
    silently → orchestrator's _get_role_model returned None → role used
    worker's default model regardless of user choice.
    """
    import yaml as _yaml

    proj = _make_project_with_config(tmp_path)
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "project-auditor", "model": "anthropic/claude-sonnet-4"},
            {"type": "default", "agent": "developer", "model": "vllm/llm"},
        ]),
    }
    process_role_saves(data, project_dir=proj)

    cfg = _yaml.safe_load((proj / ".agentic" / "config.yaml").read_text())
    assert cfg["models"]["project-auditor"]["model"] == "anthropic/claude-sonnet-4", (
        "BD-32: chosen model must reach config.yaml"
    )
    assert cfg["models"]["developer"]["model"] == "vllm/llm"
    # agent_name still set (BD-12)
    assert cfg["models"]["project-auditor"]["agent_name"] == "worker"


def test_bd32_no_model_in_form_does_not_add(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-32: if form doesn't include model, config.yaml stays unchanged
    (no empty model field added)."""
    import yaml as _yaml

    proj = _make_project_with_config(tmp_path)
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "developer"},
        ]),
    }
    process_role_saves(data, project_dir=proj)

    cfg = _yaml.safe_load((proj / ".agentic" / "config.yaml").read_text())
    # developer was just added — should have agent_name but NO model field
    assert cfg["models"]["developer"]["agent_name"] == "worker"
    assert "model" not in cfg["models"]["developer"], (
        "BD-32: empty model must not produce empty model field"
    )


def test_bd32_empty_model_in_form_clears_existing(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-32: if form sends model='' explicitly, existing model is removed
    (user cleared the dropdown)."""
    import yaml as _yaml

    initial = {
        "models": {
            "supervisor": {"description": "x"},
            "qa": {"agent_name": "worker", "model": "vllm/llm"},
        },
    }
    proj = _make_project_with_config(tmp_path, _yaml.safe_dump(initial, sort_keys=False))
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "qa", "model": ""},
        ]),
    }
    process_role_saves(data, project_dir=proj)

    cfg = _yaml.safe_load((proj / ".agentic" / "config.yaml").read_text())
    assert "model" not in cfg["models"]["qa"], (
        "BD-32: empty model from form must clear existing model field"
    )


def test_bd32_idempotent_same_model_no_churn(isolated_roles_dir, reset_project_dir, tmp_path):
    """BD-32: re-submitting same model doesn't rewrite config (no backup churn)."""
    proj = _make_project_with_config(tmp_path)
    state._project_dir = None

    data = {
        "team_config": json.dumps([
            {"type": "default", "agent": "developer", "model": "vllm/llm"},
        ]),
    }
    process_role_saves(data, project_dir=proj)
    first = (proj / ".agentic" / "config.yaml").read_text()

    process_role_saves(data, project_dir=proj)
    second = (proj / ".agentic" / "config.yaml").read_text()

    assert first == second, "BD-32: same model should not produce different config"


# ── BD-32: _collect_opencode_models (form dropdown source) ────────────────────


def test_bd32_collect_opencode_models_from_agents(tmp_path, monkeypatch):
    """BD-32: models collected from agent.<name>.model in opencode.json."""
    import json as _json

    from agent_workflow_ui.tools.forms import _collect_opencode_models

    fake_home = tmp_path / "home"
    oc_dir = fake_home / ".config" / "opencode"
    oc_dir.mkdir(parents=True)
    (oc_dir / "opencode.json").write_text(_json.dumps({
        "agent": {
            "worker": {"model": "vllm/llm"},
            "build": {"model": "anthropic/claude-sonnet-4"},
            "explore": {"model": "vllm/llm"},  # duplicate
        }
    }))

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    models = _collect_opencode_models()
    assert sorted(models) == ["anthropic/claude-sonnet-4", "vllm/llm"]


def test_bd32_collect_opencode_models_from_providers(tmp_path, monkeypatch):
    """BD-32: models collected from provider.<name>.models as <name>/<id>."""
    import json as _json

    from agent_workflow_ui.tools.forms import _collect_opencode_models

    fake_home = tmp_path / "home"
    oc_dir = fake_home / ".config" / "opencode"
    oc_dir.mkdir(parents=True)
    (oc_dir / "opencode.json").write_text(_json.dumps({
        "provider": {
            "vllm": {"models": {"llm": {}}},
            "anthropic": {"models": ["claude-sonnet-4", "claude-haiku"]},
        }
    }))

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    models = _collect_opencode_models()
    assert "vllm/llm" in models
    assert "anthropic/claude-sonnet-4" in models
    assert "anthropic/claude-haiku" in models


def test_bd32_collect_opencode_models_missing_file(tmp_path, monkeypatch):
    """BD-32: no opencode.json → empty list (no crash)."""
    from agent_workflow_ui.tools.forms import _collect_opencode_models

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nonexistent")
    assert _collect_opencode_models() == []


def test_bd32_collect_opencode_models_invalid_json(tmp_path, monkeypatch):
    """BD-32: malformed opencode.json → empty list (no crash)."""
    from agent_workflow_ui.tools.forms import _collect_opencode_models

    fake_home = tmp_path / "home"
    oc_dir = fake_home / ".config" / "opencode"
    oc_dir.mkdir(parents=True)
    (oc_dir / "opencode.json").write_text(":::not valid json:::")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert _collect_opencode_models() == []
