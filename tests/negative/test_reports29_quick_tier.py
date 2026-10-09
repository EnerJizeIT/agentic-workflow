"""TODO-0173 (quick tier): the setup form materializes a second pipeline.

Invariants:
1. apply_project_setup writes .agentic/pipelines/quick.yaml next to the
   normal pipeline — the same team, minus the QA role stages
   (agent-qa-review): plan(supervisor) → worker stages → verify(supervisor).
2. No worker stage left after the QA removal → quick is NOT written, a
   warning names the reason (team with QA roles only).
3. The normal pipeline and default_pipeline are untouched.
4. quick.yaml is valid for the engine (document validator + loader).
"""
from __future__ import annotations

import pytest
import yaml

from awf import api
from awf.pipeline import load_stages, validate_pipeline_document

QA = "agent-qa-review"
DEV = "agent-implementer"
ARCH = "agent-architector"


@pytest.fixture
def awf_project(tmp_git_repo):
    """Project with .agentic/ initialized (config.yaml + supervisor.md)."""
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


def _pipes(project):
    return project / ".agentic" / "pipelines"


def _stages(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))["stages"]


def _worker_roles(stages):
    return [s["role"] for s in stages if s["role"] != "supervisor"]


class TestQuickPipelineWritten:
    """Invariant 1 + 4: quick.yaml is written, QA-free, engine-valid."""

    def test_apply_writes_quick_next_to_normal(self, awf_project):
        result = api.apply_project_setup(
            awf_project, team=[{"agent": DEV}, {"agent": QA}]
        )
        quick = _pipes(awf_project) / "quick.yaml"
        assert (_pipes(awf_project) / "default.yaml").is_file()
        assert quick.is_file()
        assert result.quick_pipeline_file == str(quick)

        stages = _stages(quick)
        assert stages[0]["name"] == "plan"
        assert stages[0]["role"] == "supervisor"
        assert stages[-1]["name"] == "verify"
        assert stages[-1]["role"] == "supervisor"
        # QA stage dropped, worker stage in place
        assert _worker_roles(stages) == [DEV]

    def test_quick_keeps_all_worker_stages_in_order(self, awf_project):
        """Three-role team: both workers survive, QA does not, order kept."""
        api.apply_project_setup(
            awf_project, team=[{"agent": DEV}, {"agent": QA}, {"agent": ARCH}]
        )
        stages = _stages(_pipes(awf_project) / "quick.yaml")
        assert _worker_roles(stages) == [DEV, ARCH]
        assert QA not in _worker_roles(stages)

    def test_quick_preserves_stage_policies(self, awf_project):
        """Worker stages keep their on_blocked/on_failed/retries; verify keeps its."""
        api.apply_project_setup(awf_project, team=[{"agent": DEV}, {"agent": QA}])
        stages = _stages(_pipes(awf_project) / "quick.yaml")
        worker = next(s for s in stages if s["role"] == DEV)
        assert worker["on_blocked"] == "escalate"
        assert worker["on_failed"] == "escalate"
        assert worker["max_retries"] == 3
        verify = stages[-1]
        assert verify["on_approved"] == "commit_and_next"
        assert verify["on_rejected"] == "replan"

    def test_quick_is_valid_for_engine(self, awf_project):
        """The engine's own document validator + loader accept the file."""
        api.apply_project_setup(awf_project, team=[{"agent": DEV}, {"agent": QA}])
        quick = _pipes(awf_project) / "quick.yaml"
        data = yaml.safe_load(quick.read_text(encoding="utf-8"))
        validated = validate_pipeline_document(data, "quick.yaml")
        assert [s["role"] for s in validated] == ["supervisor", DEV, "supervisor"]
        loaded = load_stages(quick)
        assert [s.role for s in loaded] == ["supervisor", DEV, "supervisor"]
        assert [s.kind for s in loaded] == ["plan", "execute", "verify"]

    def test_quick_name_is_quick(self, awf_project):
        api.apply_project_setup(awf_project, team=[{"agent": DEV}, {"agent": QA}])
        quick = _pipes(awf_project) / "quick.yaml"
        assert yaml.safe_load(quick.read_text(encoding="utf-8"))["name"] == "quick"


class TestNormalPipelineUntouched:
    """Invariant 3: the normal pipeline and default_pipeline do not change."""

    def test_normal_pipeline_keeps_qa_stage(self, awf_project):
        api.apply_project_setup(awf_project, team=[{"agent": DEV}, {"agent": QA}])
        stages = _stages(_pipes(awf_project) / "default.yaml")
        assert stages[0]["role"] == "supervisor"
        assert stages[-1]["role"] == "supervisor"
        assert _worker_roles(stages) == [DEV, QA]
        assert yaml.safe_load(
            (_pipes(awf_project) / "default.yaml").read_text(encoding="utf-8")
        )["name"] == "default"

    def test_config_default_pipeline_untouched(self, awf_project):
        api.apply_project_setup(awf_project, team=[{"agent": DEV}, {"agent": QA}])
        config = yaml.safe_load(
            (awf_project / ".agentic" / "config.yaml").read_text(encoding="utf-8")
        )
        assert config.get("default_pipeline", "default") == "default"

    def test_custom_active_pipeline_quick_is_separate_file(self, awf_project):
        """default_pipeline: custom → normal goes to custom.yaml (with QA),
        quick still lands in quick.yaml (without QA), config unchanged."""
        config_path = awf_project / ".agentic" / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["default_pipeline"] = "custom"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

        result = api.apply_project_setup(awf_project, team=[{"agent": DEV}, {"agent": QA}])

        pipes = _pipes(awf_project)
        assert (pipes / "custom.yaml").is_file()
        assert _worker_roles(_stages(pipes / "custom.yaml")) == [DEV, QA]
        assert (pipes / "quick.yaml").is_file()
        assert _worker_roles(_stages(pipes / "quick.yaml")) == [DEV]
        # no quick content leaked into the active file, and vice versa
        assert QA not in _worker_roles(_stages(pipes / "quick.yaml"))
        assert result.pipeline_file == str(pipes / "custom.yaml")
        assert result.quick_pipeline_file == str(pipes / "quick.yaml")
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config["default_pipeline"] == "custom"

    def test_resubmit_backs_up_existing_quick(self, awf_project):
        quick = _pipes(awf_project) / "quick.yaml"
        quick.parent.mkdir(parents=True, exist_ok=True)
        quick.write_text("old: content\n", encoding="utf-8")
        api.apply_project_setup(awf_project, team=[{"agent": DEV}, {"agent": QA}])
        backup = _pipes(awf_project) / "quick.yaml.bak"
        assert backup.is_file()
        assert backup.read_text(encoding="utf-8") == "old: content\n"
        # and the new file is the QA-free one
        assert _worker_roles(_stages(quick)) == [DEV]


class TestQuickSkipped:
    """Invariant 2: a skip is a named warning, never a submit failure."""

    def test_qa_only_team_no_quick_warning(self, awf_project):
        result = api.apply_project_setup(awf_project, team=[{"agent": QA}])
        # the normal pipeline IS written (QA is a valid worker stage there)
        assert result.pipeline_file is not None
        assert _worker_roles(
            _stages(_pipes(awf_project) / "default.yaml")
        ) == [QA]
        # quick is skipped with a warning that names the reason
        assert not (_pipes(awf_project) / "quick.yaml").exists()
        assert result.quick_pipeline_file is None
        assert any(
            "quick" in w and QA in w for w in result.warnings
        ), f"expected a quick-skip warning naming {QA}, got: {result.warnings}"

    def test_active_pipeline_named_quick_collision(self, awf_project):
        """default_pipeline: quick → the active file IS quick.yaml; the
        variant must not clobber it."""
        config_path = awf_project / ".agentic" / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["default_pipeline"] = "quick"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

        result = api.apply_project_setup(awf_project, team=[{"agent": DEV}, {"agent": QA}])

        quick = _pipes(awf_project) / "quick.yaml"
        # the ACTIVE pipeline (with QA) owns quick.yaml
        assert _worker_roles(_stages(quick)) == [DEV, QA]
        assert result.pipeline_file == str(quick)
        assert result.quick_pipeline_file is None
        assert any(
            "quick" in w and "active" in w for w in result.warnings
        ), f"expected a collision warning, got: {result.warnings}"


class TestBuildPipelineStagesExclude:
    """The exclusion knob itself: unit-level regression guard."""

    def test_exclude_roles_drops_qa(self):
        stages = api.setup.build_pipeline_stages(
            [{"agent": DEV}, {"agent": QA}, {"agent": ARCH}],
            exclude_roles=frozenset({QA}),
        )
        assert _worker_roles(stages) == [DEV, ARCH]

    def test_default_call_keeps_everything(self):
        """No exclude_roles → exactly the pre-existing behavior (QA stays)."""
        stages = api.setup.build_pipeline_stages([{"agent": DEV}, {"agent": QA}])
        assert _worker_roles(stages) == [DEV, QA]
        assert stages[0]["role"] == "supervisor"
        assert stages[-1]["role"] == "supervisor"


class TestQuickWriteResilience:
    """TODO-0173 QA: a hard write failure of the quick variant must not
    fail the whole submit (the docstring promises best-effort; the normal
    pipeline's write failure is a named warning, not a raise)."""

    def test_quick_write_error_warns_not_raises(self, awf_project, monkeypatch):
        import awf.api.setup as setup_mod

        real = setup_mod.atomic_write_text

        def flaky(path, content, *args, **kwargs):
            if path.name == "quick.yaml":
                raise OSError("disk on fire")
            return real(path, content, *args, **kwargs)

        monkeypatch.setattr(setup_mod, "atomic_write_text", flaky)
        result = api.apply_project_setup(
            awf_project, team=[{"agent": DEV}, {"agent": QA}]
        )

        # the normal pipeline and the config update survived
        assert (_pipes(awf_project) / "default.yaml").is_file()
        assert result.pipeline_file is not None
        assert not (_pipes(awf_project) / "quick.yaml").exists()
        assert result.quick_pipeline_file is None
        assert any(
            "quick" in w and "disk on fire" in w for w in result.warnings
        ), f"expected a quick-failure warning, got: {result.warnings}"
