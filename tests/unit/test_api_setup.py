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


# ─── D9: role disambiguation refresh on pipeline rebuild ────────────────────


class TestDisambiguationRefresh:
    """D9 (topic-trainer spec): rebuilding the pipeline must refresh BD-31 markers.

    The addenda embed stage positions ("position 2/3"); after a form rebuild
    the positions shift and stale markers used to linger until someone
    remembered to re-run awf_analyze_roles.
    """

    def _roles(self, project, names=("developer", "qa")):
        roles = project / ".agentic" / "roles"
        roles.mkdir(parents=True, exist_ok=True)
        for n in names:
            (roles / f"{n}.md").write_text(f"# {n}\n\nRole body.\n", encoding="utf-8")

    def test_markers_written_by_apply_project_setup(self, awf_project):
        self._roles(awf_project)
        api.setup.apply_project_setup(
            awf_project, team=[{"agent": "developer"}, {"agent": "qa"}]
        )
        dev = (awf_project / ".agentic" / "roles" / "developer.md").read_text(encoding="utf-8")
        assert "BD-31" in dev
        assert "FIRST agent" in dev

    def test_rebuild_refreshes_positions(self, awf_project):
        self._roles(awf_project)
        api.setup.apply_project_setup(
            awf_project, team=[{"agent": "developer"}, {"agent": "qa"}]
        )
        dev_path = awf_project / ".agentic" / "roles" / "developer.md"
        assert "FIRST agent" in dev_path.read_text(encoding="utf-8")

        # Rebuild with the roles swapped → developer is now LAST.
        api.setup.apply_project_setup(
            awf_project, team=[{"agent": "qa"}, {"agent": "developer"}]
        )
        dev = dev_path.read_text(encoding="utf-8")
        assert "LAST agent" in dev
        assert "FIRST agent" not in dev


class TestRoleSlugify:
    """Day-2 spec: stage roles must match the saved role-file slugs.

    The plugin saves custom roles as ``auditor.md`` / ``my-agent.md``; a
    pipeline carrying the raw "Auditor" fails at runtime with an opaque
    "role file not found".
    """

    def test_custom_role_slugified(self):
        stages = api.setup.build_pipeline_stages(
            [{"agent": "Auditor"}, {"agent": "My Agent"}]
        )
        roles = [s["role"] for s in stages]
        assert "auditor" in roles
        assert "my-agent" in roles

    def test_lowercase_roles_unchanged(self):
        stages = api.setup.build_pipeline_stages([{"agent": "agent-qa-review"}])
        assert "agent-qa-review" in [s["role"] for s in stages]

    def test_cyrillic_transliterated(self):
        """FU-14: core transliterates Cyrillic like the plugin (one slug)."""
        from awf.api.setup import _slugify_role

        assert _slugify_role("Аудитор") == "auditor"


# ─── AUD06-02/03: corrupted config.yaml must survive a submit ─────────────


class TestCorruptedConfigSurvivesSubmit:
    """A broken config.yaml (non-UTF-8, broken YAML, non-dict root) must not
    crash the submit and must not be wiped — the damage goes to warnings."""

    def test_non_utf8_config_does_not_crash_submit(self, awf_project):
        config_path = awf_project / ".agentic" / "config.yaml"
        config_path.write_bytes(b"\xff\xfe\x00broken\n")
        result = api.apply_project_setup(
            awf_project,
            team=[{"agent": "developer"}],
            context_message="ctx",
        )
        # pipeline still written, config damage surfaced in warnings
        assert result.pipeline_file is not None
        assert any("config.yaml" in w for w in result.warnings)

    def test_broken_yaml_config_does_not_crash_submit(self, awf_project):
        config_path = awf_project / ".agentic" / "config.yaml"
        config_path.write_text("key: [unclosed\n  bad: : :\n", encoding="utf-8")
        result = api.apply_project_setup(
            awf_project,
            team=[{"agent": "developer"}],
            context_message="ctx",
        )
        assert result.pipeline_file is not None
        assert any("config.yaml" in w for w in result.warnings)

    def test_non_dict_config_is_not_wiped(self, awf_project):
        """AUD06-03: non-dict root survives the submit byte-for-byte."""
        config_path = awf_project / ".agentic" / "config.yaml"
        original = b"- a\n- b\n"
        config_path.write_bytes(original)
        api.apply_project_setup(
            awf_project, team=[], context_message="hello ctx"
        )
        assert config_path.read_bytes() == original

    def test_non_dict_config_warns(self, awf_project):
        config_path = awf_project / ".agentic" / "config.yaml"
        config_path.write_bytes(b"- a\n- b\n")
        result = api.apply_project_setup(
            awf_project, team=[], context_message="hello ctx"
        )
        assert any("config.yaml" in w for w in result.warnings)


# ─── AUD06-01: models.<role> keys must match the pipeline slugs ────────────


class TestRoleSlugConsistency:
    """AUD06-01: build_pipeline_stages slugifies role names, so config.yaml
    must key models.<slug> the same way. With the raw name the runtime
    lookup models.<slug> misses and BD-12/BD-32 silently do not apply."""

    def test_raw_role_name_slugified_in_config(self, awf_project):
        api.setup.update_config_role_mapping(
            [{"role": "QA Lead", "model": "anthropic/claude-3"}],
            awf_project,
        )
        config = yaml.safe_load(
            (awf_project / ".agentic" / "config.yaml").read_text()
        )
        assert "qa-lead" in config["models"]
        assert "QA Lead" not in config["models"]
        assert config["models"]["qa-lead"]["agent_name"] == "worker"
        assert config["models"]["qa-lead"]["model"] == "anthropic/claude-3"

    def test_runtime_lookup_finds_slug_keyed_mapping(self, awf_project):
        """Runtime reads models.<slug> — the config key must be the slug."""
        from awf.supervisor import get_agent_name, get_role_model

        api.setup.update_config_role_mapping(
            [{"role": "My Auditor", "model": "vllm/audit-model"}],
            awf_project,
        )
        config = yaml.safe_load(
            (awf_project / ".agentic" / "config.yaml").read_text()
        )
        assert get_agent_name(config, "my-auditor") == "worker"
        assert get_role_model(config, "my-auditor") == "vllm/audit-model"


# ─── AUD06-04: write_pipeline targets the active pipeline file ─────────────


class TestWritePipelineActiveFile:
    """AUD06-04: with default_pipeline: custom the runtime uses custom.yaml —
    writing default.yaml would silently ignore the form's team (repro S9)."""

    def _set_default_pipeline(self, project, name):
        config_path = project / ".agentic" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["default_pipeline"] = name
        config_path.write_text(yaml.safe_dump(config))

    def test_writes_active_custom_pipeline(self, awf_project):
        pipes = awf_project / ".agentic" / "pipelines"
        (pipes / "custom.yaml").write_text("name: custom\nstages: []\n")
        (pipes / "default.yaml").write_text("name: default\nstages: []\n")
        self._set_default_pipeline(awf_project, "custom")

        path = api.setup.write_pipeline([{"agent": "developer"}], awf_project)
        assert path.name == "custom.yaml"
        data = yaml.safe_load(path.read_text())
        assert any(s["role"] == "developer" for s in data["stages"])
        # the non-active file is left untouched
        default_stages = yaml.safe_load(
            (pipes / "default.yaml").read_text()
        )["stages"]
        assert default_stages == []

    def test_custom_declared_without_file_creates_custom(self, awf_project):
        """Config declares custom → the team goes to custom.yaml, the file
        the runtime will use from then on."""
        self._set_default_pipeline(awf_project, "custom")
        path = api.setup.write_pipeline([{"agent": "developer"}], awf_project)
        assert path.name == "custom.yaml"
        assert path.is_file()

    def test_invalid_declared_name_falls_back_to_default(self, awf_project):
        """default_pipeline is a value that becomes a path — validate it like
        resolve_pipeline_file does (AUD14-05)."""
        self._set_default_pipeline(awf_project, "../../evil")
        path = api.setup.write_pipeline([{"agent": "developer"}], awf_project)
        assert path.name == "default.yaml"
        assert not (awf_project / "evil.yaml").exists()
        assert not (awf_project / ".agentic" / "evil.yaml").exists()

    def test_custom_pipeline_backed_up(self, awf_project):
        pipes = awf_project / ".agentic" / "pipelines"
        (pipes / "custom.yaml").write_text("old: content\n")
        self._set_default_pipeline(awf_project, "custom")
        api.setup.write_pipeline([{"agent": "developer"}], awf_project)
        backup = pipes / "custom.yaml.bak"
        assert backup.is_file()
        assert backup.read_text() == "old: content\n"


# ─── AUD06-08: UI-2/UI-3 sections delimited by sentinels ───────────────────


class TestUiSectionSentinels:
    """AUD06-08: sections are delimited by machine-readable sentinels, not by
    the first '## ' inside user text — re-submits must leave no orphans
    (repro S4)."""

    def test_resubmit_with_inner_heading_leaves_no_orphans(self, awf_project):
        api.setup.save_context_and_instructions(
            awf_project, context_message="line one\n\n## Inner Heading\nlost part"
        )
        api.setup.save_context_and_instructions(
            awf_project, context_message="new context only"
        )
        content = (awf_project / ".agentic" / "roles" / "supervisor.md").read_text()
        assert content.count("Project context (from user, UI-2)") == 1
        assert "new context only" in content
        assert "line one" not in content
        assert "Inner Heading" not in content
        assert "lost part" not in content

    def test_sentinel_markers_present(self, awf_project):
        api.setup.save_context_and_instructions(awf_project, context_message="x")
        content = (awf_project / ".agentic" / "roles" / "supervisor.md").read_text()
        assert "<!-- awf:ui2:start -->" in content
        assert "<!-- awf:ui2:end -->" in content

    def test_legacy_section_without_sentinels_stripped(self, awf_project):
        """Back-compat: files written before sentinels carry bare '## '
        headings — the old section must be stripped whole, '## ' headings
        inside user text included."""
        sup = awf_project / ".agentic" / "roles" / "supervisor.md"
        sup.write_text(
            "# supervisor\n\n"
            "## Project context (from user, UI-2)\n\n"
            "legacy ctx\n\n"
            "## Inner Heading\nlost part\n",
            encoding="utf-8",
        )
        api.setup.save_context_and_instructions(awf_project, context_message="fresh")
        content = sup.read_text(encoding="utf-8")
        assert content.count("Project context (from user, UI-2)") == 1
        assert "fresh" in content
        assert "legacy ctx" not in content
        assert "lost part" not in content
        assert "# supervisor" in content


# ─── AUD06-15: team roles that do not resolve to a role file ───────────────


class TestUnresolvedRoleWarning:
    """AUD06-15: a typo'd role used to surface only at runtime (RuntimeError
    from resolve_role_file at stage spawn). Setup must report which roles do
    not resolve — project .agentic/roles/ first, then the global fallback."""

    def test_missing_role_file_warns(self, awf_project, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
        (tmp_path / "xdg-empty").mkdir()
        result = api.apply_project_setup(
            awf_project, team=[{"agent": "ghost-role"}]
        )
        assert result.pipeline_file is not None
        assert any("ghost-role" in w for w in result.warnings)

    def test_project_role_file_no_warning(self, awf_project, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
        (tmp_path / "xdg-empty").mkdir()
        (awf_project / ".agentic" / "roles" / "developer.md").write_text("# dev\n")
        result = api.apply_project_setup(
            awf_project, team=[{"agent": "developer"}]
        )
        assert not any("developer" in w for w in result.warnings)

    def test_global_role_fallback_no_warning(self, awf_project, monkeypatch, tmp_path):
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        (xdg / "awf" / "roles").mkdir(parents=True)
        (xdg / "awf" / "roles" / "globally-installed.md").write_text("# g\n")
        result = api.apply_project_setup(
            awf_project, team=[{"agent": "globally-installed"}]
        )
        assert not any("globally-installed" in w for w in result.warnings)
