"""BD-31: awf analyze-roles — skill-aware role normalization tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from awf import cmd_analyze_roles
from awf.cmd_analyze_roles import (
    _build_disambiguation_addendum,
    _detect_overlaps,
    _infer_zone,
    _read_pipeline_roles,
    _read_role_files,
)


def _make_project(
    tmp_path: Path,
    roles: dict[str, str] | None = None,
    pipeline_yaml: str = "",
) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".agentic").mkdir()
    (proj / ".agentic" / "roles").mkdir()
    (proj / ".agentic" / "pipelines").mkdir()

    roles = roles or {}
    # Always have a supervisor (skipped from analysis)
    (proj / ".agentic" / "roles" / "supervisor.md").write_text("# supervisor")
    for slug, content in roles.items():
        (proj / ".agentic" / "roles" / f"{slug}.md").write_text(content)

    (proj / ".agentic" / "config.yaml").write_text(
        'project:\n  name: t\nphases:\n  current: ".agentic/phases/plan.md"\n'
        'default_pipeline: "default"\n'
    )

    if pipeline_yaml:
        (proj / ".agentic" / "pipelines" / "default.yaml").write_text(pipeline_yaml)
    else:
        # Default pipeline if not specified
        role_names = list(roles.keys())
        stages_yaml = '- name: plan\n  role: supervisor\n'
        for r in role_names:
            stages_yaml += f'- name: {r}\n  role: {r}\n'
        stages_yaml += '- name: verify\n  role: supervisor\n'
        (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
            f'name: default\nstages:\n{stages_yaml}'
        )

    return proj


# ── _read_role_files / _read_pipeline_roles ───────────────────────────────────


class TestReadRoles:

    def test_reads_all_md_files(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, roles={
            "developer": "# dev",
            "qa": "# qa",
        })
        result = _read_role_files(proj)
        assert "developer" in result
        assert "qa" in result
        assert "supervisor" in result  # supervisor also read
        assert result["developer"] == "# dev"

    def test_no_roles_dir_returns_empty(self, tmp_path: Path) -> None:
        proj = tmp_path / "empty"
        proj.mkdir()
        (proj / ".agentic").mkdir()
        assert _read_role_files(proj) == {}

    def test_pipeline_roles_excludes_supervisor(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, roles={"dev": "# d", "qa": "# q"})
        config: dict = {}
        roles = _read_pipeline_roles(proj, config)
        assert roles == ["dev", "qa"]
        assert "supervisor" not in roles

    def test_no_pipeline_returns_empty(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, roles={"dev": "# d"})
        # Remove pipeline file
        (proj / ".agentic" / "pipelines" / "default.yaml").unlink()
        config: dict = {}
        assert _read_pipeline_roles(proj, config) == []


# ── _infer_zone ───────────────────────────────────────────────────────────────


class TestInferZone:

    def test_developer_slug(self) -> None:
        assert "code" in _infer_zone("developer", "")

    def test_qa_slug(self) -> None:
        assert "verifies" in _infer_zone("qa", "")

    def test_system_analyst_content(self) -> None:
        # Even with generic slug, content drives inference
        zone = _infer_zone("custom-role", "# Аналитик\nТы пишешь требования")
        assert "requirements" in zone

    def test_unknown_role_generalist(self) -> None:
        assert "generalist" in _infer_zone("totally-unknown", "random content")


# ── _detect_overlaps ──────────────────────────────────────────────────────────


class TestDetectOverlaps:

    def test_finds_overlap(self) -> None:
        zones = {"qa": "verifies implementation", "project-auditor": "verifies implementation"}
        overlaps = _detect_overlaps(zones)
        assert len(overlaps) == 1
        assert ("qa", "project-auditor", "verifies implementation") in overlaps

    def test_no_overlap_for_different_zones(self) -> None:
        zones = {"dev": "writes code", "qa": "verifies"}
        assert _detect_overlaps(zones) == []

    def test_generalist_not_flagged_as_overlap(self) -> None:
        zones = {"role-a": "generalist (undefined zone)", "role-b": "generalist (undefined zone)"}
        assert _detect_overlaps(zones) == []


# ── _build_disambiguation_addendum ────────────────────────────────────────────


class TestBuildAddendum:

    def test_includes_bd31_marker(self) -> None:
        addendum = _build_disambiguation_addendum(
            role="qa",
            zone="verifies implementation",
            overlaps=[("qa", "project-auditor", "verifies implementation")],
            pipeline_roles=["qa", "project-auditor"],
        )
        assert "BD-31: Pipeline-specific disambiguation" in addendum

    def test_lists_overlapping_roles(self) -> None:
        addendum = _build_disambiguation_addendum(
            role="qa",
            zone="verifies implementation",
            overlaps=[("qa", "project-auditor", "verifies implementation")],
            pipeline_roles=["qa", "project-auditor"],
        )
        assert "`project-auditor`" in addendum

    def test_first_position_hint(self) -> None:
        addendum = _build_disambiguation_addendum(
            role="dev",
            zone="writes code",
            overlaps=[],
            pipeline_roles=["dev", "qa"],
        )
        assert "FIRST" in addendum

    def test_last_position_hint(self) -> None:
        addendum = _build_disambiguation_addendum(
            role="qa",
            zone="verifies",
            overlaps=[],
            pipeline_roles=["dev", "qa"],
        )
        assert "LAST" in addendum

    def test_qa_vs_auditor_disambiguation(self) -> None:
        addendum = _build_disambiguation_addendum(
            role="qa",
            zone="verifies implementation against requirements",
            overlaps=[("qa", "project-auditor", "verifies implementation against requirements")],
            pipeline_roles=["qa", "project-auditor"],
        )
        assert "TODO requirements" in addendum
        assert "audit" in addendum.lower()


# ── cmd_analyze_roles.run integration ─────────────────────────────────────────


class TestAnalyzeRolesRun:

    def test_no_roles_dir_returns_1(self, tmp_path: Path) -> None:
        proj = tmp_path / "empty"
        proj.mkdir()
        args = SimpleNamespace(project_dir=str(proj), dry_run=False)
        assert cmd_analyze_roles.run(args) == 1

    def test_dry_run_does_not_modify_files(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, roles={
            "qa": "# QA — verifies implementation",
            "project-auditor": "# Auditor — verifies implementation",
        })
        original_qa = (proj / ".agentic" / "roles" / "qa.md").read_text()

        args = SimpleNamespace(project_dir=str(proj), dry_run=True)
        cmd_analyze_roles.run(args)

        # Files unchanged
        assert (proj / ".agentic" / "roles" / "qa.md").read_text() == original_qa

    def test_applies_patches_to_overlapping_roles(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, roles={
            "qa": "# QA — verifies implementation against requirements",
            "project-auditor": "# Auditor — verifies code health",
        })
        args = SimpleNamespace(project_dir=str(proj), dry_run=False)
        result = cmd_analyze_roles.run(args)
        assert result == 0

        qa_content = (proj / ".agentic" / "roles" / "qa.md").read_text()
        auditor_content = (proj / ".agentic" / "roles" / "project-auditor.md").read_text()
        # Both should have BD-31 addendum
        assert "BD-31: Pipeline-specific disambiguation" in qa_content
        assert "BD-31: Pipeline-specific disambiguation" in auditor_content

    def test_idempotent_replacing_existing_patch(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, roles={
            "qa": "# QA\n\n---\n\n## BD-31: Pipeline-specific disambiguation\n\nOLD CONTENT",
            "project-auditor": "# Auditor — verifies",
        })
        args = SimpleNamespace(project_dir=str(proj), dry_run=False)
        cmd_analyze_roles.run(args)

        qa_content = (proj / ".agentic" / "roles" / "qa.md").read_text()
        # OLD CONTENT should be replaced, not duplicated
        assert qa_content.count("BD-31: Pipeline-specific disambiguation") == 1
        assert "OLD CONTENT" not in qa_content

    def test_no_overlaps_still_reports_clean(self, tmp_path: Path, capsys) -> None:
        proj = _make_project(tmp_path, roles={
            "developer": "# Developer — writes code",
            "tester": "# Tester — writes tests",
        })
        args = SimpleNamespace(project_dir=str(proj), dry_run=False)
        result = cmd_analyze_roles.run(args)
        out = capsys.readouterr().out

        # No overlap → either nothing to patch OR clean message
        assert result == 0
