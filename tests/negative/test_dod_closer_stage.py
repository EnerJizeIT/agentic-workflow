"""IMP1 (R1): ``skip_if`` in the stage schema, loader, write path and both
pipeline snapshots.

T1 of the closer-stage test spec: the key must survive the per-TODO snapshot
and the service-run snapshot; an unknown value must be refused before any
side effect; an empty ``write_allow`` must not be written; the service run
must drop the stale allowlist of the stage whose output it replaces.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf import api
from awf.api import run as api_run
from awf.api._errors import AwfApiError
from awf.pipeline import (
    Stage,
    load_stages,
    pipeline_snapshot_text,
    stage_to_document,
    validate_pipeline_stages,
)


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: CloserStage\n", encoding="utf-8"
    )
    return proj


def _write_pipeline(proj: Path, name: str, stages: list[dict]) -> Path:
    path = proj / ".agentic" / "pipelines" / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump({"name": name, "stages": stages}, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _closer_source() -> list[dict]:
    """Two workers: the first is the closer, the second keeps its fields."""
    return [
        {"name": "plan", "role": "supervisor"},
        {
            "name": "closer",
            "role": "agent-implementer",
            "task": "Закрой пункты",
            "output": "reports/closer.md",
            "write_allow": ["reports/closer-extra.md"],
            "skip_if": "checklist_green",
            "write_check": "off",
        },
        {
            "name": "reviewer",
            "role": "agent-qa-review",
            "task": "Проверь",
            "skip_if": "checklist_green",
            "write_check": "off",
            "output": "reports/review.md",
            "write_allow": ["reports/extra.md"],
        },
        {"name": "verify", "role": "supervisor"},
    ]


def test_skip_if_round_trips_through_both_snapshots(tmp_path):
    proj = _project(tmp_path)
    source = _write_pipeline(proj, "default", _closer_source())

    stages = load_stages(source)
    closer = next(s for s in stages if s.name == "closer")
    assert closer.skip_if == "checklist_green"
    assert next(s for s in stages if s.name == "plan").skip_if == ""

    snapshot_file = proj / ".agentic" / "pipelines" / "PIPELINE-TODO-0001.yaml"
    snapshot_file.write_text(
        pipeline_snapshot_text(stages, "default", "TODO-0001"), encoding="utf-8"
    )
    reloaded = {s.name: s for s in load_stages(snapshot_file)}
    assert reloaded["closer"].skip_if == "checklist_green"
    assert reloaded["reviewer"].skip_if == "checklist_green"
    assert reloaded["closer"].write_check == "off"
    assert reloaded["closer"].write_allow == ("reports/closer-extra.md",)
    assert reloaded["reviewer"].write_allow == ("reports/extra.md",)
    assert reloaded["reviewer"].write_check == "off"

    service_file = api_run._write_service_pipeline(proj, "sla")
    service = {s.name: s for s in load_stages(service_file)}
    assert service["closer"].skip_if == "checklist_green"
    assert service["closer"].output == ".agentic/roles/draft/sla.md"
    assert service["closer"].write_allow == ()
    assert service["closer"].write_check == "off"
    assert service["reviewer"].skip_if == "checklist_green"
    assert service["reviewer"].task == "Проверь"
    assert service["reviewer"].output == "reports/review.md"
    assert service["reviewer"].write_allow == ("reports/extra.md",)


def test_unknown_skip_if_value_refused_before_side_effects(tmp_path):
    proj = _project(tmp_path)
    bad_values = ["checklist-grenn", "", 1, True]

    for value in bad_values:
        stages = [
            {"name": "plan", "role": "supervisor"},
            {"name": "closer", "role": "agent-implementer", "skip_if": value},
            {"name": "verify", "role": "supervisor"},
        ]
        with pytest.raises(AwfApiError) as load_err:
            _write_pipeline(proj, "broken", stages)
            load_stages(proj / ".agentic" / "pipelines" / "broken.yaml")
        assert "closer" in str(load_err.value)
        assert repr(value) in str(load_err.value)

        with pytest.raises(AwfApiError) as write_err:
            api.write_pipeline(proj, "bad-write", stages)
        assert "closer" in str(write_err.value)
        assert repr(value) in str(write_err.value)

        with pytest.raises(AwfApiError):
            validate_pipeline_stages(stages, "inline")

        assert not (proj / ".agentic" / "pipelines" / "bad-write.yaml").exists()

    for absent in ({}, {"skip_if": None}):
        stages = [
            {"name": "plan", "role": "supervisor"},
            {"name": "closer", "role": "agent-implementer", **absent},
            {"name": "verify", "role": "supervisor"},
        ]
        loaded = load_stages(_write_pipeline(proj, "plain", stages))
        assert next(s for s in loaded if s.name == "closer").skip_if == ""


def test_empty_write_allow_is_not_written():
    plain = stage_to_document(Stage(name="s", role="worker"))
    assert "write_allow" not in plain
    assert "write_check" not in plain
    assert "skip_if" not in plain

    allowed = stage_to_document(
        Stage(
            name="s",
            role="worker",
            output="out/result.md",
            write_allow=("out/extra.md",),
            write_check="off",
            skip_if="checklist_green",
        )
    )
    assert allowed["write_allow"] == ["out/extra.md"]
    assert allowed["write_check"] == "off"
    assert allowed["skip_if"] == "checklist_green"
