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
        assert s.max_rollbacks == 3

    def test_invalid_policy_word_warns_at_load(self, tmp_path, capsys) -> None:
        """AUD04-02: an unknown policy word must warn at load time instead of
        being silently reinterpreted by the resolver (typos like
        'on_blocked: halt' used to behave as 'escalate' with no hint)."""
        pipe = tmp_path / "pipeline.yaml"
        pipe.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n    kind: plan\n"
            "  - name: implement\n    role: worker\n    kind: execute\n"
            "    on_blocked: halt\n"
            "  - name: verify\n    role: supervisor\n    kind: verify\n",
            encoding="utf-8",
        )
        stages = load_stages(pipe)
        err = capsys.readouterr().err
        assert "on_blocked" in err and "halt" in err, (
            f"invalid policy word must be flagged at load, got stderr:\n{err}"
        )
        # the value is still loaded verbatim — the resolver fallback applies
        assert stages[1].on_blocked == "halt"

    def test_duplicate_stage_names_warn_at_load(self, tmp_path, capsys) -> None:
        """AUD03-06: two stages named 'impl' used to load silently — the
        engine always resolves the first one, the second is unreachable."""
        pipe = tmp_path / "pipeline.yaml"
        pipe.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: impl\n    role: worker\n"
            "  - name: impl\n    role: qa\n"
            "  - name: verify\n    role: supervisor\n",
            encoding="utf-8",
        )
        stages = load_stages(pipe)
        assert len(stages) == 4  # still loaded — warning, not rejection
        err = capsys.readouterr().err
        assert "impl" in err and "duplicate" in err.lower(), (
            f"duplicate stage name must be flagged at load, got stderr:\n{err}"
        )

    def test_negative_max_retries_warns_at_load(self, tmp_path, capsys) -> None:
        """AUD03-06: max_retries: -3 was accepted without a hint."""
        pipe = tmp_path / "pipeline.yaml"
        pipe.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: implement\n    role: worker\n    max_retries: -3\n"
            "  - name: verify\n    role: supervisor\n",
            encoding="utf-8",
        )
        load_stages(pipe)
        err = capsys.readouterr().err
        assert "max_retries" in err and "-3" in err, (
            f"negative max_retries must be flagged at load, got stderr:\n{err}"
        )

    def test_valid_pipeline_stays_quiet_on_new_checks(self, tmp_path, capsys) -> None:
        """AUD03-06: a clean pipeline must not trigger the new warnings."""
        pipe = tmp_path / "pipeline.yaml"
        pipe.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: implement\n    role: worker\n    max_retries: 2\n"
            "  - name: verify\n    role: supervisor\n",
            encoding="utf-8",
        )
        load_stages(pipe)
        err = capsys.readouterr().err
        assert "duplicate" not in err.lower()
        assert "max_retries" not in err


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
