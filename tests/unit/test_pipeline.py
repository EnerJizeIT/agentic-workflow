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
        # AUD12-12: this test used to load stages and assert NOTHING — a
        # load_stages regression (silent fallback, dropped stage, lost
        # action) stayed green. Now every stage's name/kind/on_* is pinned.
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "simple.yaml")
        assert [s.name for s in stages] == ["plan", "implement", "verify"]
        assert [s.kind for s in stages] == ["plan", "execute", "verify"]
        assert stages[0].on_blocked == "escalate"
        assert stages[1].on_blocked == "escalate"
        assert stages[1].max_retries == 3
        assert stages[2].on_approved == "commit_and_next"
        assert stages[2].on_rejected == "replan"

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
        """A-06: on_failed is a reserved policy — the fixture carries a
        plain allowed word (rollback_to: on on_failed is refused now)."""
        tmp_path, _ = tmp_pipeline_file
        stages = load_stages(tmp_path / ".agentic" / "pipelines" / "full.yaml")
        test = stages[3]
        assert test.name == "test"
        assert test.role == "tester"
        assert test.on_failed == "escalate"

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
        # AUD16-03: on_passed is gone (dead TEST-PASSED signal); on_failed
        # stays as a reserved policy (loader accepts it, nothing drives it).
        assert not hasattr(s, "on_passed")
        assert s.on_failed == "escalate"
        assert s.max_retries == 1
        assert s.max_rollbacks == 3

    def test_loader_rejects_legacy_on_passed(self, tmp_path) -> None:
        """A-06: load_stages does not accept on_passed — a YAML key with
        that name is an unknown key and the load is refused (the same rule
        as awf_write_pipeline, AUD13-04)."""
        from awf.api._errors import AwfApiError

        pipe = tmp_path / "pipeline.yaml"
        pipe.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: implement\n    role: worker\n    on_passed: commit_and_next\n"
            "  - name: verify\n    role: supervisor\n",
            encoding="utf-8",
        )
        with pytest.raises(AwfApiError, match="on_passed"):
            load_stages(pipe)

    def test_invalid_policy_word_rejected_at_load(self, tmp_path) -> None:
        """A-06: an unknown policy word is refused at load (AUD04-02 used to
        warn; typos like 'on_blocked: halt' now fail instead of being
        silently reinterpreted by the resolver)."""
        from awf.api._errors import AwfApiError

        pipe = tmp_path / "pipeline.yaml"
        pipe.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: implement\n    role: worker\n    on_blocked: halt\n"
            "  - name: verify\n    role: supervisor\n",
            encoding="utf-8",
        )
        with pytest.raises(AwfApiError, match="on_blocked='halt'"):
            load_stages(pipe)

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

    def test_negative_max_retries_rejected_at_load(self, tmp_path) -> None:
        """A-06: max_retries: -3 is refused at load (AUD03-06 used to warn;
        a negative budget is meaningless, so the file fails)."""
        from awf.api._errors import AwfApiError

        pipe = tmp_path / "pipeline.yaml"
        pipe.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: implement\n    role: worker\n    max_retries: -3\n"
            "  - name: verify\n    role: supervisor\n",
            encoding="utf-8",
        )
        with pytest.raises(AwfApiError, match="max_retries must be non-negative"):
            load_stages(pipe)

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

    def test_explicit_missing_name_is_clear_error(self, tmp_pipeline_file) -> None:
        """RUN3 #1: an explicit name that does not exist fails with the list
        of available pipelines — no silent fallback to default.yaml (that
        would run the wrong pipeline)."""
        tmp_path, _ = tmp_pipeline_file
        from awf.api._errors import AwfApiError

        with pytest.raises(AwfApiError) as exc:
            resolve_pipeline_file(tmp_path, "audit-llm")

        msg = str(exc.value)
        assert "audit-llm" in msg
        # all existing files are listed — the next action is obvious
        assert "default" in msg and "simple" in msg and "full" in msg




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
    """A-06: non-dict entries in stages list are refused (BD-29 used to skip
    them — a typo'd stage silently vanished and the pipeline misbehaved)."""

    def test_non_dict_entries_rejected(self, tmp_path: Path) -> None:
        """A non-dict entry mixed into stages is a form error."""
        from awf.api._errors import AwfApiError

        agentic = tmp_path / ".agentic" / "pipelines"
        agentic.mkdir(parents=True)
        pipeline = agentic / "test.yaml"
        pipeline.write_text(
            "stages:\n"
            "  - \"invalid\"\n"
            "  - name: plan\n    role: supervisor\n\n"
            "  - name: execute\n    role: worker\n\n"
            "  - name: verify\n    role: supervisor\n"
        )
        with pytest.raises(AwfApiError, match="stage #0 must be a mapping"):
            load_stages(pipeline)

    def test_all_non_dict_rejected(self, tmp_path: Path) -> None:
        """Pipeline with only non-dict entries is refused, not 'empty'."""
        from awf.api._errors import AwfApiError

        agentic = tmp_path / ".agentic" / "pipelines"
        agentic.mkdir(parents=True)
        pipeline = agentic / "test.yaml"
        pipeline.write_text("stages:\n  - \"a\"\n  - 42\n  - null\n")
        with pytest.raises(AwfApiError, match="stage #0 must be a mapping"):
            load_stages(pipeline)


class TestRollbackToKeyScope:
    """FU-19 (AUD03-02 tail): ``rollback_to:<stage>`` is only resolved on
    on_blocked/on_rejected. On any other policy key the resolver ignores it,
    so the loader must say so instead of accepting it silently."""

    def test_rollback_to_on_on_approved_rejected(self, tmp_path: Path) -> None:
        """A-06: rollback_to: on a key the resolver does not read is refused
        at load (FU-19 used to warn — dead config now fails the file)."""
        from awf.api._errors import AwfApiError

        pipeline = tmp_path / "pipeline.yaml"
        pipeline.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: implement\n    role: worker\n"
            "  - name: verify\n    role: supervisor\n"
            "    on_approved: rollback_to:implement\n",
            encoding="utf-8",
        )
        with pytest.raises(AwfApiError, match="on_approved"):
            load_stages(pipeline)

    def test_rollback_to_on_on_failed_rejected(self, tmp_path: Path) -> None:
        """on_failed is a reserved key (AUD16-03) — nothing drives it, so a
        rollback_to: there is dead config and is refused at load."""
        from awf.api._errors import AwfApiError

        pipeline = tmp_path / "pipeline.yaml"
        pipeline.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: implement\n    role: worker\n"
            "  - name: test\n    role: tester\n    on_failed: rollback_to:implement\n",
            encoding="utf-8",
        )
        with pytest.raises(AwfApiError, match="on_failed"):
            load_stages(pipeline)

    def test_rollback_to_on_supported_key_no_warning(self, tmp_path: Path, capsys) -> None:
        pipeline = tmp_path / "pipeline.yaml"
        pipeline.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: implement\n    role: worker\n"
            "  - name: verify\n    role: supervisor\n"
            "    on_rejected: rollback_to:implement\n",
            encoding="utf-8",
        )
        stages = load_stages(pipeline)
        err = capsys.readouterr().err
        assert "rollback_to" not in err, err
        assert stages[2].on_rejected == "rollback_to:implement"

    def test_bad_target_on_supported_key_still_warns(self, tmp_path: Path, capsys) -> None:
        """Existing target-existence check is untouched: on_rejected pointing
        at a nonexistent stage still warns."""
        pipeline = tmp_path / "pipeline.yaml"
        pipeline.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: implement\n    role: worker\n"
            "  - name: verify\n    role: supervisor\n"
            "    on_rejected: rollback_to:ghost\n",
            encoding="utf-8",
        )
        load_stages(pipeline)
        err = capsys.readouterr().err
        assert "ghost" in err
