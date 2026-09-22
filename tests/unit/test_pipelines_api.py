"""RUN3 #1: named pipelines — API surface (write/list/resolve-by-name).

The pain: a project holds several pipelines (audit-llm, audit-rules, ...)
and the only entry point was the setup form, which writes the ACTIVE
pipeline and patches config.yaml/supervisor.md. Now: write one file,
touch nothing else, run by name, clear error on a miss.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf import api
from awf.api._errors import AwfApiError
from awf.pipeline import resolve_pipeline_file


def _project(tmp_path: Path) -> Path:
    """Minimal awf project: .agentic/ + config + default pipeline."""
    proj = tmp_path / "proj"
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: TestProject\n", encoding="utf-8"
    )
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
        "name: default\nstages:\n  - name: plan\n    role: supervisor\n",
        encoding="utf-8",
    )
    return proj


def _snapshot(proj: Path) -> dict[str, str]:
    """Content of the files the API must NOT touch."""
    ag = proj / ".agentic"
    return {
        "config": (ag / "config.yaml").read_text(encoding="utf-8"),
        "supervisor": (ag / "supervisor.md").read_text(encoding="utf-8")
        if (ag / "supervisor.md").is_file()
        else "",
    }


class TestWritePipeline:
    def test_writes_only_the_pipeline_file(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "supervisor.md").write_text("# sup\n")
        before = _snapshot(proj)

        result = api.write_pipeline(
            proj,
            "audit-llm",
            [
                {"role": "supervisor", "name": "plan"},
                {"role": "agent-implementer"},
                {"role": "supervisor", "name": "verify"},
            ],
        )

        assert result.name == "audit-llm"
        assert result.stages == 3
        assert result.overwritten is False
        target = proj / ".agentic" / "pipelines" / "audit-llm.yaml"
        assert Path(result.file) == target
        assert target.is_file()
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert data["name"] == "audit-llm"
        assert [s["role"] for s in data["stages"]] == [
            "supervisor",
            "agent-implementer",
            "supervisor",
        ]
        # contract: nothing else was touched
        after = _snapshot(proj)
        assert after == before, "write_pipeline touched config.yaml/supervisor.md"

    def test_name_defaults_to_role_slug(self, tmp_path):
        proj = _project(tmp_path)

        result = api.write_pipeline(proj, "p", [{"role": "Agent Implementer"}])

        data = yaml.safe_load(
            (proj / ".agentic" / "pipelines" / "p.yaml").read_text(encoding="utf-8")
        )
        assert data["stages"][0]["name"] == "agent-implementer"
        assert data["stages"][0]["role"] == "agent-implementer"
        assert result.stages == 1

    def test_creates_pipelines_dir_when_absent(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "pipelines" / "default.yaml").unlink()
        (proj / ".agentic" / "pipelines").rmdir()

        result = api.write_pipeline(proj, "fresh", [{"role": "worker"}])

        assert (proj / ".agentic" / "pipelines" / "fresh.yaml").is_file()
        assert result.file.endswith("fresh.yaml")

    def test_refuses_existing_without_force(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "pipelines" / "audit-llm.yaml").write_text(
            "name: audit-llm\nstages: []\n", encoding="utf-8"
        )
        original = (proj / ".agentic" / "pipelines" / "audit-llm.yaml").read_text()

        with pytest.raises(AwfApiError) as exc:
            api.write_pipeline(proj, "audit-llm", [{"role": "worker"}])

        assert "already exists" in str(exc.value)
        assert "force" in str(exc.value)
        # the file survived byte-identical
        assert (proj / ".agentic" / "pipelines" / "audit-llm.yaml").read_text() == original

    def test_force_overwrites(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "pipelines" / "audit-llm.yaml").write_text(
            "name: audit-llm\nstages: []\n", encoding="utf-8"
        )

        result = api.write_pipeline(
            proj, "audit-llm", [{"role": "worker"}], force=True
        )

        assert result.overwritten is True
        data = yaml.safe_load(
            (proj / ".agentic" / "pipelines" / "audit-llm.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert data["stages"][0]["role"] == "worker"

    @pytest.mark.parametrize(
        "bad_name",
        ["../evil", "..", ".", "a/b", "bad name", "", "name/with/slashes"],
    )
    def test_invalid_names_rejected(self, tmp_path, bad_name):
        proj = _project(tmp_path)

        with pytest.raises(AwfApiError) as exc:
            api.write_pipeline(proj, bad_name, [{"role": "worker"}])

        assert "Invalid pipeline name" in str(exc.value)
        # traversal targets never materialize
        assert not (proj / "evil.yaml").exists()
        assert not (tmp_path / "evil.yaml").exists()
        assert not (proj / ".agentic" / "pipelines" / "a").exists()

    def test_empty_stages_rejected(self, tmp_path):
        proj = _project(tmp_path)
        with pytest.raises(AwfApiError) as exc:
            api.write_pipeline(proj, "empty", [])
        assert "non-empty" in str(exc.value)

    def test_stage_without_role_rejected(self, tmp_path):
        proj = _project(tmp_path)
        with pytest.raises(AwfApiError) as exc:
            api.write_pipeline(proj, "p", [{"name": "no-role"}])
        assert "role" in str(exc.value)

    def test_unknown_stage_key_rejected(self, tmp_path):
        """AUD13-04 class: a typo'd key must not be written silently."""
        proj = _project(tmp_path)
        with pytest.raises(AwfApiError) as exc:
            api.write_pipeline(
                proj, "p", [{"role": "worker", "on_done": "commit"}]
            )
        assert "on_done" in str(exc.value)
        assert not (proj / ".agentic" / "pipelines" / "p.yaml").exists()

    def test_stage_not_a_mapping_rejected(self, tmp_path):
        proj = _project(tmp_path)
        with pytest.raises(AwfApiError):
            api.write_pipeline(proj, "p", ["just-a-string"])

    def test_no_agentic_rejected(self, tmp_path):
        bare = tmp_path / "bare"
        bare.mkdir()
        with pytest.raises(AwfApiError) as exc:
            api.write_pipeline(bare, "p", [{"role": "worker"}])
        assert ".agentic" in str(exc.value)


class TestListPipelines:
    def test_lists_and_marks_active(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "pipelines" / "audit-llm.yaml").write_text(
            "name: audit-llm\nstages: []\n", encoding="utf-8"
        )
        (proj / ".agentic" / "pipelines" / "audit-rules.yaml").write_text(
            "name: audit-rules\nstages: []\n", encoding="utf-8"
        )
        (proj / ".agentic" / "config.yaml").write_text(
            "default_pipeline: audit-rules\n", encoding="utf-8"
        )

        result = api.list_pipelines(proj)

        assert result.pipelines == ["audit-llm", "audit-rules", "default"]
        assert result.active == "audit-rules"
        assert result.active_exists is True

    def test_active_falls_back_to_default(self, tmp_path):
        proj = _project(tmp_path)

        result = api.list_pipelines(proj)

        assert result.active == "default"
        assert result.active_exists is True

    def test_invalid_config_name_falls_back(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "config.yaml").write_text(
            "default_pipeline: ../evil\n", encoding="utf-8"
        )

        result = api.list_pipelines(proj)

        assert result.active == "default"

    def test_bak_files_are_not_listed(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "pipelines" / "default.yaml.bak").write_text(
            "old\n", encoding="utf-8"
        )

        result = api.list_pipelines(proj)

        assert result.pipelines == ["default"]

    def test_empty_dir(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "pipelines" / "default.yaml").unlink()

        result = api.list_pipelines(proj)

        assert result.pipelines == []
        assert result.active_exists is False

    def test_no_agentic_rejected(self, tmp_path):
        bare = tmp_path / "bare"
        bare.mkdir()
        with pytest.raises(AwfApiError):
            api.list_pipelines(bare)


class TestResolveByName:
    """Part B: start-by-name reads the named file; a miss is a clear error."""

    def test_explicit_name_reads_named_file(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "pipelines" / "audit-llm.yaml").write_text(
            "name: audit-llm\nstages:\n  - name: plan\n    role: supervisor\n",
            encoding="utf-8",
        )

        result = resolve_pipeline_file(proj, "audit-llm")

        assert result.name == "audit-llm.yaml"

    def test_explicit_missing_name_is_clear_error(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "pipelines" / "audit-llm.yaml").write_text(
            "name: audit-llm\nstages: []\n", encoding="utf-8"
        )

        with pytest.raises(AwfApiError) as exc:
            resolve_pipeline_file(proj, "no-such-pipeline")

        msg = str(exc.value)
        assert "no-such-pipeline" in msg
        # the available list is in the error — the next action is obvious
        assert "audit-llm" in msg and "default" in msg

    def test_explicit_missing_name_with_no_pipelines_at_all(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "pipelines" / "default.yaml").unlink()

        with pytest.raises(AwfApiError) as exc:
            resolve_pipeline_file(proj, "nope")

        assert "(none)" in str(exc.value)

    def test_config_declared_name_keeps_fallback(self, tmp_path):
        """Legacy behavior pinned: a MISSING default_pipeline from config
        still falls back to default.yaml (only explicit names error)."""
        proj = _project(tmp_path)
        (proj / ".agentic" / "config.yaml").write_text(
            "default_pipeline: gone\n", encoding="utf-8"
        )

        result = resolve_pipeline_file(proj)

        assert result.name == "default.yaml"

    def test_write_then_resolve_roundtrip(self, tmp_path):
        proj = _project(tmp_path)
        api.write_pipeline(
            proj, "audit-llm", [{"role": "supervisor", "name": "plan"}]
        )

        result = resolve_pipeline_file(proj, "audit-llm")

        assert result.name == "audit-llm.yaml"
        assert result.resolve().is_relative_to(
            (proj / ".agentic" / "pipelines").resolve()
        )


class TestNameHelpers:
    """RUN3 #1: active_pipeline_name / list_pipeline_names — the helpers
    awf_status and run_brief use. Same rules as the resolver, plus the
    available names (no .bak noise)."""

    def _project(self, tmp_path: Path) -> Path:
        proj = _project(tmp_path)
        (proj / ".agentic" / "pipelines" / "simple.yaml").write_text(
            "name: simple\nstages: []\n", encoding="utf-8"
        )
        return proj

    def test_listing_sorted_no_bak(self, tmp_path):
        from awf.pipeline import list_pipeline_names

        proj = self._project(tmp_path)
        (proj / ".agentic" / "pipelines" / "default.yaml.bak").write_text(
            "old\n", encoding="utf-8"
        )

        assert list_pipeline_names(proj) == ["default", "simple"]

    def test_active_name_rules(self, tmp_path):
        from awf.pipeline import active_pipeline_name

        proj = self._project(tmp_path)
        assert active_pipeline_name(proj) == "default"

        (proj / ".agentic" / "config.yaml").write_text(
            "default_pipeline: simple\n", encoding="utf-8"
        )
        assert active_pipeline_name(proj) == "simple"

        # an invalid declared name falls back to "default" (AUD14-05 rule)
        (proj / ".agentic" / "config.yaml").write_text(
            "default_pipeline: ../evil\n", encoding="utf-8"
        )
        assert active_pipeline_name(proj) == "default"


class TestStatusAndBriefShowPipelines:
    def test_status_shows_active_and_count(self, tmp_path):
        proj = _project(tmp_path)
        (proj / ".agentic" / "pipelines" / "audit-llm.yaml").write_text(
            "name: audit-llm\nstages: []\n", encoding="utf-8"
        )
        (proj / ".agentic" / "config.yaml").write_text(
            "default_pipeline: audit-llm\n", encoding="utf-8"
        )

        result = api.get_status(proj)

        assert result.active_pipeline == "audit-llm"
        assert result.pipeline_count == 2

    def test_run_brief_carries_pipeline_info(self, tmp_path):
        from awf import run_state

        proj = _project(tmp_path)
        run_state.write_run(
            proj,
            active=True,
            queue=["TODO-0001"],
            index=0,
            current="TODO-0001",
        )

        brief = api.run_brief(proj)

        assert brief is not None
        assert brief["active_pipeline"] == "default"
        assert brief["pipeline_count"] == 1
