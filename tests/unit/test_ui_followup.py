"""Tests for BD-31 fixes (zone classification, disambiguation) + UI-2/UI-3 (context/instructions)."""
from __future__ import annotations

from pathlib import Path

# ── BD-31a: zone misclassification fix ───────────────────────────────────────


class TestBD31aZoneClassification:

    def test_system_analyst_classified_as_requirements(self):
        """BD-31a fix: system-analyst → 'writes requirements', not 'writes code'."""
        from awf.api.roles import _infer_zone
        # Even if skill.md content contains "implement", slug must win
        zone = _infer_zone("system-analyst", "# Skill\nYou implement features and write code")
        assert "requirements" in zone, f"Expected requirements, got {zone!r}"

    def test_dev_classified_as_code(self):
        from awf.api.roles import _infer_zone
        zone = _infer_zone("dev", "# Developer\nWrites code")
        assert "code" in zone

    def test_qa_review_classified_as_verify(self):
        from awf.api.roles import _infer_zone
        zone = _infer_zone("qa-review", "# QA\nVerifies work")
        assert "verif" in zone

    def test_project_auditor_classified_as_code_health(self):
        from awf.api.roles import _infer_zone
        zone = _infer_zone("project-auditor", "# Auditor\nAudits code")
        assert "code health" in zone or "quality" in zone

    def test_unknown_role_generalist(self):
        from awf.api.roles import _infer_zone
        zone = _infer_zone("totally-unknown", "random content")
        assert "generalist" in zone

    def test_slug_wins_over_content(self):
        """Slug match takes priority over content match."""
        from awf.api.roles import _infer_zone
        # 'architect' in slug, but content says 'writes code' → architect wins
        zone = _infer_zone("architect", "writes code, implements features")
        assert "designs" in zone or "architect" in zone.lower()


# ── BD-31b: disambiguation not empty ─────────────────────────────────────────


class TestBD31bDisambiguation:

    def test_dev_gets_specific_disambiguation(self):
        """BD-31b fix: dev role gets specific disambiguation (was empty)."""
        from awf.api.roles import _build_disambiguation_addendum
        result = _build_disambiguation_addendum(
            role="dev",
            zone="writes code",
            overlaps=[],  # no overlaps — still should have pipeline contract
            pipeline_roles=["system-analyst", "dev", "qa-review", "project-auditor"],
        )
        assert "BD-31" in result
        assert "position 2/4" in result

    def test_auditor_gets_code_health_instruction(self):
        """BD-31b fix: project-auditor gets 'Focus on code health' (was fallback)."""
        from awf.api.roles import _build_disambiguation_addendum
        result = _build_disambiguation_addendum(
            role="project-auditor",
            zone="verifies code health / project quality",
            overlaps=[("qa-review", "project-auditor", "verify")],
            pipeline_roles=["dev", "qa-review", "project-auditor"],
        )
        assert "code health" in result.lower()
        assert "qa-review" in result  # mentions the other role

    def test_qa_gets_todo_requirements_instruction(self):
        from awf.api.roles import _build_disambiguation_addendum
        result = _build_disambiguation_addendum(
            role="qa-review",
            zone="verifies implementation against requirements",
            overlaps=[("qa-review", "project-auditor", "verify")],
            pipeline_roles=["dev", "qa-review", "project-auditor"],
        )
        assert "TODO" in result or "requirements" in result.lower()

    def test_system_analyst_gets_requirements_instruction(self):
        from awf.api.roles import _build_disambiguation_addendum
        result = _build_disambiguation_addendum(
            role="system-analyst",
            zone="writes requirements / vision",
            overlaps=[],
            pipeline_roles=["system-analyst", "dev"],
        )
        assert "BD-31" in result
        assert "FIRST" in result  # position 0


# ── BD-31c: broader overlap detection ────────────────────────────────────────


class TestBD31cBroaderOverlap:

    def test_qa_auditor_overlap_detected(self):
        """BD-31c fix: qa-review + project-auditor overlap detected via zone family."""
        from awf.api.roles import _detect_overlaps
        zones = {
            "qa-review": "verifies implementation against requirements",
            "project-auditor": "verifies code health / project quality",
        }
        overlaps = _detect_overlaps(zones)
        assert len(overlaps) == 1
        assert ("qa-review", "project-auditor") in [
            (a, b) for a, b, _ in overlaps
        ]

    def test_zone_family_groups_verify(self):
        from awf.api.roles import _zone_family
        assert _zone_family("verifies implementation against requirements") == "verify"
        assert _zone_family("verifies code health / project quality") == "verify"

    def test_different_families_no_overlap(self):
        from awf.api.roles import _detect_overlaps
        zones = {
            "dev": "writes code",
            "qa-review": "verifies implementation against requirements",
        }
        overlaps = _detect_overlaps(zones)
        assert overlaps == []

    def test_generalist_not_flagged(self):
        from awf.api.roles import _detect_overlaps
        zones = {
            "role-a": "generalist (undefined zone)",
            "role-b": "generalist (undefined zone)",
        }
        assert _detect_overlaps(zones) == []


# ── UI-2/UI-3: context_message + supervisor_instruction backend ─────────────


class TestUI2UI3ContextInstructions:

    def _make_project(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        (proj / ".agentic" / "roles").mkdir(parents=True)
        (proj / ".agentic" / "roles" / "supervisor.md").write_text("# Supervisor\n\nBase content.")
        (proj / ".agentic" / "config.yaml").write_text(
            'project:\n  name: test\nphases:\n  current: ".agentic/phases/plan.md"\n'
        )
        return proj

    def test_context_message_saved_to_config(self, tmp_path: Path):
        """UI-2: context_message saved to config.yaml as context.message."""
        import yaml as _yaml
        from agent_workflow_ui.roles_processor import process_role_saves

        proj = self._make_project(tmp_path)
        data = {"context_message": "Focus on security, test everything"}
        process_role_saves(data, project_dir=proj)

        config = _yaml.safe_load((proj / ".agentic" / "config.yaml").read_text())
        assert config["context"]["message"] == "Focus on security, test everything"

    def test_supervisor_instruction_saved_to_config(self, tmp_path: Path):
        """UI-3: supervisor_instruction saved to config.yaml."""
        import yaml as _yaml
        from agent_workflow_ui.roles_processor import process_role_saves

        proj = self._make_project(tmp_path)
        data = {"supervisor_instruction": "Prefer Strategy pattern, commit in Russian"}
        process_role_saves(data, project_dir=proj)

        config = _yaml.safe_load((proj / ".agentic" / "config.yaml").read_text())
        assert config["supervisor"]["instructions"] == "Prefer Strategy pattern, commit in Russian"

    def test_context_appended_to_supervisor_md(self, tmp_path: Path):
        """UI-2: context_message appended as section to supervisor.md."""
        from agent_workflow_ui.roles_processor import process_role_saves

        proj = self._make_project(tmp_path)
        data = {"context_message": "Important project context here"}
        process_role_saves(data, project_dir=proj)

        content = (proj / ".agentic" / "roles" / "supervisor.md").read_text()
        assert "Project context" in content
        assert "Important project context here" in content

    def test_instruction_appended_to_supervisor_md(self, tmp_path: Path):
        """UI-3: supervisor_instruction appended as section to supervisor.md."""
        from agent_workflow_ui.roles_processor import process_role_saves

        proj = self._make_project(tmp_path)
        data = {"supervisor_instruction": "Be extra careful with tests"}
        process_role_saves(data, project_dir=proj)

        content = (proj / ".agentic" / "roles" / "supervisor.md").read_text()
        assert "Additional supervisor instructions" in content
        assert "Be extra careful with tests" in content

    def test_empty_context_no_changes(self, tmp_path: Path):
        """Empty context_message and supervisor_instruction → no changes."""
        from agent_workflow_ui.roles_processor import process_role_saves

        proj = self._make_project(tmp_path)
        original_config = (proj / ".agentic" / "config.yaml").read_text()
        original_sv = (proj / ".agentic" / "roles" / "supervisor.md").read_text()

        data = {"context_message": "", "supervisor_instruction": ""}
        process_role_saves(data, project_dir=proj)

        assert (proj / ".agentic" / "config.yaml").read_text() == original_config
        assert (proj / ".agentic" / "roles" / "supervisor.md").read_text() == original_sv

    def test_idempotent_resubmit_replaces_not_duplicates(self, tmp_path: Path):
        """Re-submitting context replaces old section, doesn't duplicate."""
        from agent_workflow_ui.roles_processor import process_role_saves

        proj = self._make_project(tmp_path)

        process_role_saves({"context_message": "First context"}, project_dir=proj)
        process_role_saves({"context_message": "Second context"}, project_dir=proj)

        content = (proj / ".agentic" / "roles" / "supervisor.md").read_text()
        assert content.count("Project context") == 1
        assert "Second context" in content
        assert "First context" not in content


# ── UI-2/UI-3: build_prompt reads context/instructions from config ──────────


class TestBuildPromptWithContext:

    def test_plan_prompt_includes_context(self):
        """UI-2: build_prompt for plan includes context.message from config."""
        from awf.supervisor import build_prompt
        config = {"context": {"message": "Security-first project"}}
        prompt = build_prompt("plan", "TODO-0001", config=config)
        assert "Security-first project" in prompt

    def test_verify_prompt_includes_instructions(self):
        """UI-3: build_prompt for verify includes supervisor.instructions."""
        from awf.supervisor import build_prompt
        config = {"supervisor": {"instructions": "Check all edge cases"}}
        prompt = build_prompt("verify", "TODO-0001", config=config)
        assert "Check all edge cases" in prompt

    def test_execute_prompt_no_context(self):
        """Execute prompt does NOT include context (agents don't need it)."""
        from awf.supervisor import build_prompt
        config = {"context": {"message": "Secret context"}}
        prompt = build_prompt("execute", "TODO-0001", config=config)
        assert "Secret context" not in prompt

    def test_no_config_no_context(self):
        """Without config, prompt works normally (backward compat)."""
        from awf.supervisor import build_prompt
        prompt = build_prompt("plan", "TODO-0001")
        assert "TODO-{NNNN}" in prompt


# ── UI-2/UI-3 end-to-end: form submit → config → supervisor.md → build_prompt ──


class TestUI2UI3EndToEnd:

    def test_full_flow_submit_to_build_prompt(self, tmp_path: Path):
        """UI-2/UI-3 end-to-end: submit data → config updated → supervisor.md updated → build_prompt reads both.

        Simulates the full pipeline:
        1. Form submit with context_message + supervisor_instruction
        2. process_role_saves writes to config.yaml and supervisor.md
        3. build_prompt("plan") includes context.message
        4. build_prompt("verify") includes supervisor.instructions
        """

        import yaml as _yaml
        from agent_workflow_ui.roles_processor import process_role_saves

        from awf.supervisor import build_prompt

        proj = tmp_path / "proj"
        (proj / ".agentic" / "roles").mkdir(parents=True)
        (proj / ".agentic" / "roles" / "supervisor.md").write_text(
            "# Supervisor\n\nBase supervisor instructions.\n"
        )
        (proj / ".agentic" / "config.yaml").write_text(
            'project:\n  name: e2e-test\nphases:\n  current: ".agentic/phases/plan.md"\n'
        )

        # Step 1: Simulate form submit
        data = {
            "context_message": "This is a security-critical project. All code must pass audit.",
            "supervisor_instruction": "Always check for SQL injection and XSS vulnerabilities.",
        }
        saved = process_role_saves(data, project_dir=proj)
        assert saved == 0  # no custom roles saved, but context/instructions processed

        # Step 2: Verify config.yaml updated
        config = _yaml.safe_load((proj / ".agentic" / "config.yaml").read_text())
        assert config["context"]["message"] == "This is a security-critical project. All code must pass audit."
        assert config["supervisor"]["instructions"] == "Always check for SQL injection and XSS vulnerabilities."

        # Step 3: Verify supervisor.md updated
        sv_content = (proj / ".agentic" / "roles" / "supervisor.md").read_text()
        assert "Project context" in sv_content
        assert "security-critical project" in sv_content
        assert "Additional supervisor instructions" in sv_content
        assert "SQL injection" in sv_content

        # Step 4: build_prompt("plan") includes context.message
        plan_prompt = build_prompt("plan", "TODO-0001", config=config)
        assert "security-critical project" in plan_prompt
        assert "Project context" in plan_prompt

        # Step 5: build_prompt("verify") includes supervisor.instructions
        verify_prompt = build_prompt("verify", "TODO-0001", config=config)
        assert "SQL injection" in verify_prompt
        assert "Additional instructions" in verify_prompt

        # Step 6: execute prompt does NOT include context (agents don't see it)
        exec_prompt = build_prompt("execute", "TODO-0001", config=config)
        assert "security-critical" not in exec_prompt
        assert "SQL injection" not in exec_prompt


# ── UI-1: project_roles in template ──────────────────────────────────────────


class TestUI1ProjectRolesDropdown:

    def test_project_setup_template_renders_project_roles(self, tmp_path: Path):
        """UI-1: project-setup template renders project_roles section when project_dir passed.

        When a project has existing role files in .agentic/roles/, the template
        should render them in the projectRoles JavaScript array for the dropdown.
        """
        from agent_workflow_ui.render.engine import create_default_env, render_template

        env = create_default_env()

        # Full context matching what the server provides
        context = {
            "form_id": "FORM-test-123",
            "submit_url": "http://127.0.0.1:9999/submit/FORM-test-123",
            "roles": [
                {"id": "worker", "title": "Worker", "description": "Default worker"},
            ],
            "available_models": ["claude-4", "gpt-4"],
            "recent_models": ["claude-4"],
            "custom_agents": [],
            "project_roles": [
                {"id": "custom-dev", "title": "Custom Developer"},
                {"id": "my-qa", "title": "My QA Role"},
            ],
            "global_skills": [],
            "existing_supervisor_slugs": [],
            "existing_agent_slugs": [],
        }

        html = render_template(env, "project-setup", context)

        # Verify project_roles are rendered in the JavaScript array
        assert "projectRoles" in html
        assert "custom-dev" in html
        assert "Custom Developer" in html
        assert "my-qa" in html
        assert "My QA Role" in html

    def test_project_setup_template_empty_project_roles(self, tmp_path: Path):
        """UI-1: when no project_roles, the JS array is empty (no crash)."""
        from agent_workflow_ui.render.engine import create_default_env, render_template

        env = create_default_env()

        context = {
            "form_id": "FORM-test-456",
            "submit_url": "http://127.0.0.1:9999/submit/FORM-test-456",
            "roles": [],
            "available_models": [],
            "recent_models": [],
            "custom_agents": [],
            "global_skills": [],
            "existing_supervisor_slugs": [],
            "existing_agent_slugs": [],
            # No project_roles key — template should handle missing gracefully
        }

        html = render_template(env, "project-setup", context)

        # Should render without error
        assert "projectRoles" in html
        # With no data, the array should be empty
        assert "[]" in html or "projectRoles = []" in html or "projectRoles = [" in html
