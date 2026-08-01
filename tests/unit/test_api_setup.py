"""Unit tests for awf.api.setup — project-setup materialization.

Validates that form submit data correctly materializes into:
- .agentic/pipelines/default.yaml (BD-9)
- .agentic/config.yaml role mapping (BD-12) + model preservation (BD-32)
- .agentic/roles/supervisor.md context/instructions sections (UI-2/UI-3)
"""
from __future__ import annotations

import pytest
import yaml

from awf import api


@pytest.fixture
def awf_project(tmp_git_repo):
    """Project with .agentic/ initialized (config.yaml + supervisor.md)."""
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


# ─── build_pipeline_stages ──────────────────────────────────────────────


class TestBuildPipelineStages:
    def test_wraps_team_with_supervisor_plan_verify(self):
        stages = api.setup.build_pipeline_stages([
            {"agent": "developer", "type": "default"},
            {"agent": "qa", "type": "default"},
        ])
        # plan(supervisor) + developer + qa + verify(supervisor)
        assert len(stages) == 4
        assert stages[0]["name"] == "plan"
        assert stages[0]["role"] == "supervisor"
        assert stages[-1]["name"] == "verify"
        assert stages[-1]["role"] == "supervisor"
        assert stages[1]["role"] == "developer"
        assert stages[2]["role"] == "qa"

    def test_empty_team_raises(self):
        """Empty team = plan→verify with no work — useless, must raise."""
        with pytest.raises(ValueError, match="zero agent roles"):
            api.setup.build_pipeline_stages([])

    def test_team_with_only_empty_roles_raises(self):
        """Whitespace-only role names should not sneak through."""
        with pytest.raises(ValueError, match="zero agent roles"):
            api.setup.build_pipeline_stages([{"agent": "  "}, {"agent": ""}])

    def test_duplicate_role_deduped(self):
        """Same role twice → only one stage, warning logged."""
        stages = api.setup.build_pipeline_stages([
            {"agent": "developer"},
            {"agent": "developer"},
        ])
        # plan + developer + verify (developer deduped)
        execute_stages = [s for s in stages if s["role"] != "supervisor"]
        assert len(execute_stages) == 1

    def test_agent_key_takes_precedence_over_role(self):
        """Member can use 'agent' or 'role' key — agent wins if both present."""
        stages = api.setup.build_pipeline_stages([{"role": "qa", "agent": "developer"}])
        execute = [s for s in stages if s["role"] != "supervisor"]
        assert execute[0]["role"] == "developer"


# ─── write_pipeline ─────────────────────────────────────────────────────


class TestWritePipeline:
    def test_writes_default_yaml(self, awf_project):
        path = api.setup.write_pipeline(
            [{"agent": "developer"}, {"agent": "qa"}], awf_project
        )
        assert path is not None
        assert path.name == "default.yaml"
        assert path.is_file()

        data = yaml.safe_load(path.read_text())
        assert data["name"] == "default"
        assert len(data["stages"]) == 4
        assert data["stages"][0]["role"] == "supervisor"
        assert data["stages"][-1]["role"] == "supervisor"

    def test_backs_up_existing(self, awf_project):
        """Existing default.yaml backed up to .bak before overwrite."""
        target = awf_project / ".agentic" / "pipelines" / "default.yaml"
        target.write_text("old: content\n")
        api.setup.write_pipeline([{"agent": "developer"}], awf_project)
        backup = awf_project / ".agentic" / "pipelines" / "default.yaml.bak"
        assert backup.is_file()
        assert backup.read_text() == "old: content\n"

    def test_no_action_field_in_stages(self, awf_project):
        """BD-29: pipeline.yaml has no 'action' field — kind computed from position."""
        api.setup.write_pipeline([{"agent": "developer"}], awf_project)
        data = yaml.safe_load(
            (awf_project / ".agentic" / "pipelines" / "default.yaml").read_text()
        )
        for stage in data["stages"]:
            assert "action" not in stage


# ─── update_config_role_mapping ─────────────────────────────────────────


class TestUpdateConfigRoleMapping:
    def test_adds_agent_name_for_team_roles(self, awf_project):
        """BD-12: each non-supervisor role gets models.<role>.agent_name=worker."""
        result = api.setup.update_config_role_mapping(
            [{"role": "developer"}, {"role": "qa"}], awf_project
        )
        assert result is True
        config = yaml.safe_load(
            (awf_project / ".agentic" / "config.yaml").read_text()
        )
        assert config["models"]["developer"]["agent_name"] == "worker"
        assert config["models"]["qa"]["agent_name"] == "worker"

    def test_skips_supervisor_role(self, awf_project):
        """supervisor is current session, not a subprocess — never gets agent_name."""
        api.setup.update_config_role_mapping(
            [{"role": "supervisor"}, {"role": "developer"}], awf_project
        )
        config = yaml.safe_load(
            (awf_project / ".agentic" / "config.yaml").read_text()
        )
        # supervisor block exists (from init_project) but no agent_name added
        assert "agent_name" not in config["models"].get("supervisor", {})

    def test_preserves_existing_agent_name(self, awf_project):
        """If user already set agent_name, don't overwrite."""
        config_path = awf_project / ".agentic" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config.setdefault("models", {})["developer"] = {"agent_name": "custom-agent"}
        config_path.write_text(yaml.safe_dump(config))

        api.setup.update_config_role_mapping([{"role": "developer"}], awf_project)
        config = yaml.safe_load(config_path.read_text())
        assert config["models"]["developer"]["agent_name"] == "custom-agent"

    def test_bd32_preserves_model_from_form(self, awf_project):
        """BD-32: model selection from form reaches config.yaml."""
        api.setup.update_config_role_mapping(
            [{"role": "developer", "model": "claude-sonnet-4-20250514"}],
            awf_project,
        )
        config = yaml.safe_load(
            (awf_project / ".agentic" / "config.yaml").read_text()
        )
        assert config["models"]["developer"]["model"] == "claude-sonnet-4-20250514"

    def test_bd32_clears_model_when_empty_string(self, awf_project):
        """BD-32: explicit empty string in form clears existing model."""
        config_path = awf_project / ".agentic" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config.setdefault("models", {})["developer"] = {
            "agent_name": "worker", "model": "gpt-4"
        }
        config_path.write_text(yaml.safe_dump(config))

        api.setup.update_config_role_mapping(
            [{"role": "developer", "model": ""}], awf_project
        )
        config = yaml.safe_load(config_path.read_text())
        assert "model" not in config["models"]["developer"]

    def test_bd32_leaves_model_when_key_absent(self, awf_project):
        """BD-32: missing model key in form → existing model untouched."""
        config_path = awf_project / ".agentic" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config.setdefault("models", {})["developer"] = {
            "agent_name": "worker", "model": "gpt-4"
        }
        config_path.write_text(yaml.safe_dump(config))

        api.setup.update_config_role_mapping([{"role": "developer"}], awf_project)
        config = yaml.safe_load(config_path.read_text())
        assert config["models"]["developer"]["model"] == "gpt-4"

    def test_no_changes_returns_false(self, awf_project):
        """Idempotent: re-applying same team with no model changes → no write."""
        # First call sets agent_name
        api.setup.update_config_role_mapping([{"role": "developer"}], awf_project)
        # Second call: agent_name already set, no model → no change
        result = api.setup.update_config_role_mapping([{"role": "developer"}], awf_project)
        assert result is False


# ─── save_context_and_instructions ──────────────────────────────────────


class TestSaveContextAndInstructions:
    def test_context_message_saved_to_config(self, awf_project):
        """UI-2: context_message → config.yaml context.message."""
        api.setup.save_context_and_instructions(
            awf_project, context_message="Focus on security"
        )
        config = yaml.safe_load(
            (awf_project / ".agentic" / "config.yaml").read_text()
        )
        assert config["context"]["message"] == "Focus on security"

    def test_supervisor_instructions_saved_to_config(self, awf_project):
        """UI-3: supervisor_instructions → config.yaml supervisor.instructions."""
        api.setup.save_context_and_instructions(
            awf_project, supervisor_instructions="Prefer Strategy pattern"
        )
        config = yaml.safe_load(
            (awf_project / ".agentic" / "config.yaml").read_text()
        )
        assert config["supervisor"]["instructions"] == "Prefer Strategy pattern"

    def test_context_appended_to_supervisor_md(self, awf_project):
        """UI-2: context_message appended as section to supervisor.md."""
        api.setup.save_context_and_instructions(
            awf_project, context_message="Important project context"
        )
        content = (awf_project / ".agentic" / "roles" / "supervisor.md").read_text()
        assert "Project context (from user, UI-2)" in content
        assert "Important project context" in content

    def test_instruction_appended_to_supervisor_md(self, awf_project):
        """UI-3: supervisor_instructions appended as section to supervisor.md."""
        api.setup.save_context_and_instructions(
            awf_project, supervisor_instructions="Use TDD always"
        )
        content = (awf_project / ".agentic" / "roles" / "supervisor.md").read_text()
        assert "Additional supervisor instructions (from user, UI-3)" in content
        assert "Use TDD always" in content

    def test_idempotent_resubmit_replaces_not_duplicates(self, awf_project):
        """Re-submitting context replaces existing UI-2 section (no duplicates)."""
        api.setup.save_context_and_instructions(
            awf_project, context_message="First version"
        )
        api.setup.save_context_and_instructions(
            awf_project, context_message="Second version"
        )
        content = (awf_project / ".agentic" / "roles" / "supervisor.md").read_text()
        assert content.count("Project context") == 1
        assert "Second version" in content
        assert "First version" not in content

    def test_empty_inputs_no_changes(self, awf_project):
        """Empty context_message + supervisor_instructions → no file changes."""
        result = api.setup.save_context_and_instructions(
            awf_project, context_message="", supervisor_instructions=""
        )
        assert result is False


# ─── apply_project_setup (integration) ──────────────────────────────────


class TestApplyProjectSetup:
    def test_full_submit_writes_all_files(self, awf_project):
        """End-to-end: team + context + instructions all materialize."""
        result = api.apply_project_setup(
            awf_project,
            team=[{"agent": "developer", "model": "claude-sonnet-4-20250514"}],
            context_message="Build a CLI tool",
            supervisor_instructions="Prefer simplicity",
        )
        assert isinstance(result, api.ApplyProjectSetupResult)
        assert result.pipeline_file is not None
        assert result.config_updated is True
        assert result.supervisor_md_updated is True

        # Verify pipeline.yaml
        pipeline = yaml.safe_load(
            (awf_project / ".agentic" / "pipelines" / "default.yaml").read_text()
        )
        assert any(s["role"] == "developer" for s in pipeline["stages"])

        # Verify config.yaml role mapping
        config = yaml.safe_load(
            (awf_project / ".agentic" / "config.yaml").read_text()
        )
        assert config["models"]["developer"]["agent_name"] == "worker"
        assert config["models"]["developer"]["model"] == "claude-sonnet-4-20250514"
        assert config["context"]["message"] == "Build a CLI tool"

        # Verify supervisor.md
        sup_md = (awf_project / ".agentic" / "roles" / "supervisor.md").read_text()
        assert "Build a CLI tool" in sup_md
        assert "Prefer simplicity" in sup_md

    def test_context_only_without_team(self, awf_project):
        """Standalone context_message submit (no team change)."""
        result = api.apply_project_setup(
            awf_project,
            team=[],
            context_message="Updated focus",
        )
        # No pipeline written (team empty), but config + supervisor.md patched
        assert result.pipeline_file is None
        assert result.config_updated is True
        assert result.supervisor_md_updated is True
        assert any("team is empty" in w for w in result.warnings)

    def test_empty_team_warning(self, awf_project):
        """Empty team emits warning but doesn't fail."""
        result = api.apply_project_setup(
            awf_project, team=[], context_message="x"
        )
        assert "team is empty" in result.warnings[0]

    def test_missing_agentic_raises(self, tmp_git_repo):
        """Precondition: .agentic/ must exist."""
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            api.apply_project_setup(tmp_git_repo, team=[{"agent": "x"}])

    def test_as_dict_serializable(self, awf_project):
        """Result.as_dict() must be JSON-serializable for MCP response."""
        import json
        result = api.apply_project_setup(
            awf_project, team=[{"agent": "developer"}]
        )
        d = result.as_dict()
        json.dumps(d)  # must not raise
