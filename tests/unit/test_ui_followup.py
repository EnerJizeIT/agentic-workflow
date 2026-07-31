"""Tests for BD-31 fixes (zone classification, disambiguation) + UI-2/UI-3 (context/instructions)."""
from __future__ import annotations

from pathlib import Path

# ── BD-31a: zone misclassification fix ───────────────────────────────────────


class TestBD31aZoneClassification:

    def test_system_analyst_classified_as_requirements(self):
        """BD-31a fix: system-analyst → 'writes requirements', not 'writes code'."""
        from awf.cmd_analyze_roles import _infer_zone
        # Even if skill.md content contains "implement", slug must win
        zone = _infer_zone("system-analyst", "# Skill\nYou implement features and write code")
        assert "requirements" in zone, f"Expected requirements, got {zone!r}"

    def test_dev_classified_as_code(self):
        from awf.cmd_analyze_roles import _infer_zone
        zone = _infer_zone("dev", "# Developer\nWrites code")
        assert "code" in zone

    def test_qa_review_classified_as_verify(self):
        from awf.cmd_analyze_roles import _infer_zone
        zone = _infer_zone("qa-review", "# QA\nVerifies work")
        assert "verif" in zone

    def test_project_auditor_classified_as_code_health(self):
        from awf.cmd_analyze_roles import _infer_zone
        zone = _infer_zone("project-auditor", "# Auditor\nAudits code")
        assert "code health" in zone or "quality" in zone

    def test_unknown_role_generalist(self):
        from awf.cmd_analyze_roles import _infer_zone
        zone = _infer_zone("totally-unknown", "random content")
        assert "generalist" in zone

    def test_slug_wins_over_content(self):
        """Slug match takes priority over content match."""
        from awf.cmd_analyze_roles import _infer_zone
        # 'architect' in slug, but content says 'writes code' → architect wins
        zone = _infer_zone("architect", "writes code, implements features")
        assert "designs" in zone or "architect" in zone.lower()


# ── BD-31b: disambiguation not empty ─────────────────────────────────────────


class TestBD31bDisambiguation:

    def test_dev_gets_specific_disambiguation(self):
        """BD-31b fix: dev role gets specific disambiguation (was empty)."""
        from awf.cmd_analyze_roles import _build_disambiguation_addendum
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
        from awf.cmd_analyze_roles import _build_disambiguation_addendum
        result = _build_disambiguation_addendum(
            role="project-auditor",
            zone="verifies code health / project quality",
            overlaps=[("qa-review", "project-auditor", "verify")],
            pipeline_roles=["dev", "qa-review", "project-auditor"],
        )
        assert "code health" in result.lower()
        assert "qa-review" in result  # mentions the other role

    def test_qa_gets_todo_requirements_instruction(self):
        from awf.cmd_analyze_roles import _build_disambiguation_addendum
        result = _build_disambiguation_addendum(
            role="qa-review",
            zone="verifies implementation against requirements",
            overlaps=[("qa-review", "project-auditor", "verify")],
            pipeline_roles=["dev", "qa-review", "project-auditor"],
        )
        assert "TODO" in result or "requirements" in result.lower()

    def test_system_analyst_gets_requirements_instruction(self):
        from awf.cmd_analyze_roles import _build_disambiguation_addendum
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
        from awf.cmd_analyze_roles import _detect_overlaps
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
        from awf.cmd_analyze_roles import _zone_family
        assert _zone_family("verifies implementation against requirements") == "verify"
        assert _zone_family("verifies code health / project quality") == "verify"

    def test_different_families_no_overlap(self):
        from awf.cmd_analyze_roles import _detect_overlaps
        zones = {
            "dev": "writes code",
            "qa-review": "verifies implementation against requirements",
        }
        overlaps = _detect_overlaps(zones)
        assert overlaps == []

    def test_generalist_not_flagged(self):
        from awf.cmd_analyze_roles import _detect_overlaps
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
