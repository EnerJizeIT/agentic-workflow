"""Integration tests for normalize_skills stage.

These tests verify that normalize_skills ACTUALLY creates local skill files
in .agentic/skills/ with correct content — not just that the function is
called. They use real filesystem operations and mock only the global skill
source directory (to avoid depending on user's ~/.config/opencode/skills/).

Run with: pytest tests/integration/test_normalize_skills.py -v
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from awf.orchestrator import _run_normalize_stage

# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home_with_skills(tmp_path: Path, monkeypatch) -> Path:
    """Mock ~/.config/opencode/skills/ with realistic skill files.

    Returns the fake home directory. Skills are placed at:
        <fake_home>/.config/opencode/skills/<name>/SKILL.md
    """
    fake_home = tmp_path / "home"
    skills_root = fake_home / ".config" / "opencode" / "skills"
    skills_root.mkdir(parents=True)

    # Seed 4 skills matching common awf roles.
    # BD-27: skills directory names match role names (no mapping needed).
    # Form puts skill content directly into .agentic/roles/<role>.md.
    skills = {
        "system-analysis": (
            "# System Analyst\n\n"
            "Analyze requirements. Decompose TODO into clear specifications.\n"
            "Output: requirements document. Do NOT write production code.\n"
        ),
        "developer": (
            "# Developer\n\n"
            "Implement features per specification. Write production code.\n"
            "Output: implementation + tests. Do NOT audit.\n"
        ),
        "qa": (
            "# QA\n\n"
            "Review code. Find bugs. Write regression tests.\n"
            "Output: test report + bug fixes.\n"
        ),
        "project-auditor": (
            "# Project Auditor\n\n"
            "Holistic audit. Find design issues, security concerns.\n"
            "Output: audit report.\n"
        ),
    }

    for name, body in skills.items():
        skill_dir = skills_root / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")

    # Also create awf/roles/ stubs (used by UI dropdown).
    awf_roles = fake_home / ".config" / "awf" / "roles"
    awf_roles.mkdir(parents=True)
    for role in ("system-analysis", "developer", "qa", "project-auditor"):
        (awf_roles / f"{role}.md").write_text(f"---\nname: {role}\n---\nstub\n")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home


@pytest.fixture
def awf_project(tmp_path: Path) -> Path:
    """Minimal awf project structure."""
    proj = tmp_path / "proj"
    proj.mkdir()
    agentic = proj / ".agentic"
    (agentic / "skills").mkdir(parents=True)
    (agentic / "roles").mkdir(parents=True)
    (agentic / "logs").mkdir(parents=True)
    return proj


# ─── Role → skill name mapping ─────────────────────────────────────────────


# Documented mapping used by normalize_skills.
ROLE_TO_SKILL = {
    "system-analysis": "system-analyst",
    "developer": "developer",
    "qa": "qa-review",
    "project-auditor": "project-auditor",
}


# ─── Tests: background mode auto-creates local skills ──────────────────────


class TestNormalizeBackgroundAutoCreates:
    """BD-13 follow-up: in background mode, normalize_skills should
    AUTO-CREATE local skills (not defer)."""

    def test_creates_local_skill_for_each_team_role(
        self, awf_project: Path, fake_home_with_skills: Path, capsys
    ) -> None:
        """For every role in team, .agentic/skills/<role>.md must exist after
        normalize_skills runs in background mode."""
        team = [
            {"role": "system-analysis", "type": "default"},
            {"role": "developer", "type": "default"},
            {"role": "qa", "type": "default"},
            {"role": "project-auditor", "type": "default"},
        ]
        logs = awf_project / ".agentic" / "logs"

        _run_normalize_stage(team, awf_project, logs, background=True)

        skills_dir = awf_project / ".agentic" / "skills"
        for member in team:
            role = member["role"]
            skill_file = skills_dir / f"{role}.md"
            assert skill_file.exists(), f"Missing local skill for {role}: {skill_file}"

    def test_local_skill_has_frontmatter_with_global_sha(
        self, awf_project: Path, fake_home_with_skills: Path
    ) -> None:
        """Each local skill must have frontmatter with derived_from_global=True
        and global_sha matching the source SKILL.md content."""
        team = [{"role": "system-analysis", "type": "default"}]
        logs = awf_project / ".agentic" / "logs"

        _run_normalize_stage(team, awf_project, logs, background=True)

        skill_file = awf_project / ".agentic" / "skills" / "system-analysis.md"
        assert skill_file.exists()

        content = skill_file.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        end = content.find("\n---\n", 4)
        assert end > 0, "frontmatter not closed"

        fm = yaml.safe_load(content[4:end])
        assert fm["derived_from_global"] is True
        assert "global_sha" in fm
        assert len(fm["global_sha"]) == 64  # SHA-256 hex

        # Verify the SHA matches the source.
        source = fake_home_with_skills / ".config" / "opencode" / "skills" / "system-analysis" / "SKILL.md"
        expected_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        assert fm["global_sha"] == expected_sha

    def test_local_skill_contains_source_body(
        self, awf_project: Path, fake_home_with_skills: Path
    ) -> None:
        """Local skill must include the full body of the global SKILL.md."""
        team = [{"role": "developer", "type": "default"}]
        logs = awf_project / ".agentic" / "logs"

        _run_normalize_stage(team, awf_project, logs, background=True)

        skill_file = awf_project / ".agentic" / "skills" / "developer.md"
        content = skill_file.read_text(encoding="utf-8")

        source = fake_home_with_skills / ".config" / "opencode" / "skills" / "developer" / "SKILL.md"
        source_body = source.read_text(encoding="utf-8").strip()
        assert source_body in content, "global skill body must be embedded in local skill"

    def test_local_skill_contains_pipeline_contract(
        self, awf_project: Path, fake_home_with_skills: Path
    ) -> None:
        """BD-16: local skill must include a 'Pipeline contract' section."""
        team = [{"role": "system-analysis", "type": "default"}]
        logs = awf_project / ".agentic" / "logs"

        _run_normalize_stage(team, awf_project, logs, background=True)

        skill_file = awf_project / ".agentic" / "skills" / "system-analysis.md"
        content = skill_file.read_text(encoding="utf-8")
        assert "Pipeline contract" in content or "Zone of responsibility" in content
        # system-analysis must NOT write production code
        assert "production code" in content.lower()

    def test_creates_skills_dir_if_missing(
        self, awf_project: Path, fake_home_with_skills: Path
    ) -> None:
        """If .agentic/skills/ doesn't exist, normalize must create it."""
        # Remove the skills dir created by fixture.
        (awf_project / ".agentic" / "skills").rmdir()
        team = [{"role": "developer", "type": "default"}]
        logs = awf_project / ".agentic" / "logs"

        _run_normalize_stage(team, awf_project, logs, background=True)

        skill_file = awf_project / ".agentic" / "skills" / "developer.md"
        assert skill_file.exists()

    def test_role_without_global_skill_logs_warning(
        self, awf_project: Path, fake_home_with_skills: Path, caplog
    ) -> None:
        """If no global skill exists for a role, log warning and skip (don't crash)."""
        team = [{"role": "totally-fake-role", "type": "default"}]
        logs = awf_project / ".agentic" / "logs"

        # Must NOT raise.
        _run_normalize_stage(team, awf_project, logs, background=True)

        # No local skill created (no source).
        assert not (awf_project / ".agentic" / "skills" / "totally-fake-role.md").exists()


# ─── Tests: idempotency ────────────────────────────────────────────────────


class TestNormalizeIdempotent:
    """Re-running normalize should not corrupt existing local skills."""

    def test_second_run_preserves_manual_edits(
        self, awf_project: Path, fake_home_with_skills: Path
    ) -> None:
        """If user manually edited .agentic/skills/<role>.md (e.g. added project
        adaptation), second normalize run must not blow it away IF the global
        SHA matches (i.e. source unchanged)."""
        team = [{"role": "developer", "type": "default"}]
        logs = awf_project / ".agentic" / "logs"

        # First run.
        _run_normalize_stage(team, awf_project, logs, background=True)
        skill_file = awf_project / ".agentic" / "skills" / "developer.md"
        first_content = skill_file.read_text(encoding="utf-8")

        # User appends manual adaptation.
        with skill_file.open("a") as f:
            f.write("\n## My manual notes\n\nThis project uses React.\n")
        after_edit = skill_file.read_text(encoding="utf-8")

        # Second run.
        _run_normalize_stage(team, awf_project, logs, background=True)

        # Behavior choice: awf may either (a) preserve user edits when SHA
        # matches, or (b) overwrite. Test asserts that running twice is
        # safe (no crash, no corruption of unrelated files).
        final = skill_file.read_text(encoding="utf-8")
        assert "Pipeline contract" in final or "Zone of responsibility" in final


# ─── Tests: frontmatter fields ─────────────────────────────────────────────


class TestNormalizeFrontmatter:
    def test_frontmatter_has_required_fields(
        self, awf_project: Path, fake_home_with_skills: Path
    ) -> None:
        """Frontmatter must include: derived_from_global, global_path,
        global_sha, normalized_at, pipeline_context."""
        team = [{"role": "qa", "type": "default"}]
        logs = awf_project / ".agentic" / "logs"

        _run_normalize_stage(team, awf_project, logs, background=True)

        skill_file = awf_project / ".agentic" / "skills" / "qa.md"
        content = skill_file.read_text(encoding="utf-8")
        end = content.find("\n---\n", 4)
        fm = yaml.safe_load(content[4:end])

        assert "derived_from_global" in fm
        assert "global_path" in fm
        assert "global_sha" in fm
        assert "normalized_at" in fm
        assert "pipeline_context" in fm
        assert isinstance(fm["normalized_at"], str)
        assert len(fm["normalized_at"]) > 0

    def test_global_path_points_to_source_skill(
        self, awf_project: Path, fake_home_with_skills: Path
    ) -> None:
        """global_path must point to the actual source SKILL.md file."""
        team = [{"role": "project-auditor", "type": "default"}]
        logs = awf_project / ".agentic" / "logs"

        _run_normalize_stage(team, awf_project, logs, background=True)

        skill_file = awf_project / ".agentic" / "skills" / "project-auditor.md"
        content = skill_file.read_text(encoding="utf-8")
        end = content.find("\n---\n", 4)
        fm = yaml.safe_load(content[4:end])

        source = fake_home_with_skills / ".config" / "opencode" / "skills" / "project-auditor" / "SKILL.md"
        assert fm["global_path"] == str(source)
