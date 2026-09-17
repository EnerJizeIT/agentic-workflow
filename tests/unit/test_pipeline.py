"""Unit tests for awf.pipeline — Stage, load_stages, resolve_pipeline_file."""

from pathlib import Path

import pytest

from awf.pipeline import Stage, _compute_kind, load_stages, resolve_pipeline_file


class TestLoadStages:

    def test_simple_pipeline_count(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "simple.yaml")
        assert len(stages) == 3

    def test_full_pipeline_count(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "full.yaml")
        assert len(stages) == 5

    def test_simple_stage_names(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "simple.yaml")
        names = [s.name for s in stages]
        assert names == ["plan", "implement", "verify"]

    def test_simple_stage_roles(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "simple.yaml")
        assert stages[0].role == "supervisor"
        assert stages[1].role == "worker"
        assert stages[2].role == "supervisor"

    def test_simple_stage_actions(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "simple.yaml")

    def test_simple_verify_on_approved(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "simple.yaml")
        assert stages[2].on_approved == "commit_and_next"

    def test_simple_verify_on_rejected(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "simple.yaml")
        assert stages[2].on_rejected == "replan"

    def test_simple_implement_max_retries(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "simple.yaml")
        assert stages[1].max_retries == 3

    def test_simple_plan_default_policies(self, tmp_pipeline_file) -> None:
        """Stage without explicit policies gets defaults (NEG-2: escalate, not
        a phantom rollback target)."""
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "simple.yaml")
        plan = stages[0]
        assert plan.on_blocked == "escalate"
        assert plan.on_approved == "next"
        assert plan.on_rejected == "escalate"
        assert plan.max_retries == 1

    def test_full_review_stage(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "full.yaml")
        review = stages[2]
        assert review.name == "review"
        assert review.role == "reviewer"
        assert review.on_rejected == "rollback_to:implement"
        assert review.max_retries == 2

    def test_full_test_stage(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "full.yaml")
        test = stages[3]
        assert test.name == "test"
        assert test.role == "tester"
        assert test.on_failed == "rollback_to:implement"

    def test_full_finalize_stage(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "full.yaml")
        fin = stages[4]
        assert fin.on_approved == "commit_and_report"

    def test_description_preserved(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "simple.yaml")
        assert stages[1].description == "Worker executes the TODO"

    def test_empty_description(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "full.yaml")
        assert stages[0].description == ""

    def test_stage_dataclass_defaults(self) -> None:
        s = Stage(name="x", role="r")
        assert s.description == ""
        assert s.on_blocked == "escalate"
        assert s.on_approved == "next"
        # NEG-2: escalate is the only safe default — a fixed stage name
        # ('implement') does not exist in role-named generated pipelines.
        assert s.on_rejected == "escalate"
        assert s.on_passed == "next"
        assert s.on_failed == "escalate"
        assert s.max_retries == 1


class TestResolvePipelineFile:

    def test_explicit_name(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        result = resolve_pipeline_file(tmp_path, "simple")
        assert result.name == "simple.yaml"

    def test_default_from_config(self, tmp_pipeline_file) -> None:
        tmp_path, _ = tmp_pipeline_file
        result = resolve_pipeline_file(tmp_path)
        assert result.name == "default.yaml"

    def test_fallback_to_default_yaml(self, tmp_pipeline_file) -> None:
        tmp_path, cfg = tmp_pipeline_file
        cfg["default_pipeline"] = "nonexistent"
        agentic = tmp_path / ".agentic"
        (agentic / "config.yaml").write_text("default_pipeline: nonexistent\n")
        result = resolve_pipeline_file(tmp_path, config=cfg)
        assert result.name == "default.yaml"

    def test_file_not_found(self, tmp_pipeline_file) -> None:
        tmp_path, cfg = tmp_pipeline_file
        cfg["default_pipeline"] = "nonexistent"
        default_file = tmp_path / ".agentic" / "pipelines" / "default.yaml"
        default_file.unlink()
        with pytest.raises(FileNotFoundError):
            resolve_pipeline_file(tmp_path, config=cfg)


class TestComputeKind:
    """BD-29: _compute_kind edge cases."""

    def test_single_stage(self) -> None:
        assert _compute_kind(0, 1) == "plan"

    def test_two_stages_first(self) -> None:
        assert _compute_kind(0, 2) == "plan"

    def test_two_stages_last(self) -> None:
        assert _compute_kind(1, 2) == "verify"

    def test_three_stages(self) -> None:
        assert _compute_kind(0, 3) == "plan"
        assert _compute_kind(1, 3) == "execute"
        assert _compute_kind(2, 3) == "verify"

    def test_four_stages(self) -> None:
        assert _compute_kind(0, 4) == "plan"
        assert _compute_kind(1, 4) == "execute"
        assert _compute_kind(2, 4) == "execute"
        assert _compute_kind(3, 4) == "verify"

    def test_total_zero(self) -> None:
        """Degenerate: empty pipeline."""
        assert _compute_kind(0, 0) == "plan"


class TestLoadStagesWithNonDict:
    """BD-29: load_stages handles non-dict entries in stages list."""

    def test_non_dict_entries_skipped(self, tmp_path: Path) -> None:
        """Non-dict entries in stages list are skipped; kind computed from
        dict-only position."""
        agentic = tmp_path / ".agentic" / "pipelines"
        agentic.mkdir(parents=True)
        pipeline = agentic / "test.yaml"
        # YAML with a non-dict entry (string) mixed in
        pipeline.write_text(
            "stages:\n"
            "  - \"invalid\"\n"
            "  - name: plan\n    role: supervisor\n\n"
            "  - name: execute\n    role: worker\n\n"
            "  - name: verify\n    role: supervisor\n"
        )
        stages = load_stages(pipeline)
        assert len(stages) == 3
        assert stages[0].kind == "plan"
        assert stages[1].kind == "execute"
        assert stages[2].kind == "verify"

    def test_all_non_dict(self, tmp_path: Path) -> None:
        """Pipeline with only non-dict entries returns empty list."""
        agentic = tmp_path / ".agentic" / "pipelines"
        agentic.mkdir(parents=True)
        pipeline = agentic / "test.yaml"
        pipeline.write_text("stages:\n  - \"a\"\n  - 42\n  - null\n")
        stages = load_stages(pipeline)
        assert stages == []
