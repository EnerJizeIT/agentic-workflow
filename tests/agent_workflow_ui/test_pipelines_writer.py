"""Tests for pipelines_writer.py: BD-9 form selections become pipeline stages."""
from __future__ import annotations

from pathlib import Path

import yaml
from agent_workflow_ui.pipelines_writer import build_pipeline_stages, write_pipeline

# ── build_pipeline_stages ─────────────────────────────────────────────────────


def test_single_member_three_stages():
    """One team member → plan + worker + verify = 3 stages."""
    stages = build_pipeline_stages([{"agent": "worker"}])
    assert len(stages) == 3
    assert stages[0]["name"] == "plan"
    assert stages[0]["role"] == "supervisor"
    assert stages[0]["action"] == "create_todo"
    assert stages[1]["name"] == "worker"
    assert stages[1]["role"] == "worker"
    assert stages[1]["action"] == "execute_todo"
    assert stages[2]["name"] == "verify"
    assert stages[2]["role"] == "supervisor"
    assert stages[2]["action"] == "verify_result"


def test_three_members_five_stages():
    """Three team members → plan + 3 stages + verify = 5 stages in order."""
    team = [
        {"agent": "worker"},
        {"agent": "tester"},
        {"agent": "auditor"},
    ]
    stages = build_pipeline_stages(team)
    assert len(stages) == 5
    assert [s["name"] for s in stages] == ["plan", "worker", "tester", "auditor", "verify"]
    assert [s["action"] for s in stages[1:-1]] == ["execute_todo"] * 3


def test_empty_team_two_stages():
    """Empty team → plan + verify only."""
    stages = build_pipeline_stages([])
    assert len(stages) == 2
    assert stages[0]["name"] == "plan"
    assert stages[1]["name"] == "verify"


def test_stage_has_required_keys():
    """Each stage has name, role, action at minimum."""
    stages = build_pipeline_stages([{"agent": "worker"}, {"agent": "tester"}])
    for stage in stages:
        assert "name" in stage
        assert "role" in stage
        assert "action" in stage


def test_team_stage_has_extra_keys():
    """Team stages have on_blocked and max_retries."""
    stages = build_pipeline_stages([{"agent": "worker"}])
    team_stage = stages[1]
    assert team_stage["on_blocked"] == "escalate"
    assert team_stage["max_retries"] == 3


def test_verify_stage_has_extra_keys():
    """Verify stage has on_approved and on_rejected."""
    stages = build_pipeline_stages([])
    verify = stages[1]
    assert verify["on_approved"] == "commit_and_next"
    assert verify["on_rejected"] == "replan"


def test_empty_agent_skipped():
    """Member with empty/whitespace agent is skipped."""
    stages = build_pipeline_stages([{"agent": ""}, {"agent": "  "}, {"agent": "worker"}])
    assert len(stages) == 3
    assert stages[1]["name"] == "worker"


def test_duplicate_roles_deduped():
    """Same role twice → deduped, first occurrence kept."""
    stages = build_pipeline_stages([{"agent": "worker"}, {"agent": "worker"}, {"agent": "tester"}])
    assert len(stages) == 4
    assert [s["name"] for s in stages] == ["plan", "worker", "tester", "verify"]


def test_empty_agent_skipped_dedup():
    """Member with empty agent string is skipped during dedup."""
    stages = build_pipeline_stages([{"agent": ""}, {"agent": "worker"}])
    assert len(stages) == 3
    assert [s["name"] for s in stages] == ["plan", "worker", "verify"]


def test_custom_agent_type():
    """Custom type agents are treated the same as default."""
    team = [
        {"type": "default", "agent": "worker"},
        {"type": "custom", "agent": "my-agent"},
    ]
    stages = build_pipeline_stages(team)
    assert len(stages) == 4
    assert stages[1]["name"] == "worker"
    assert stages[2]["name"] == "my-agent"


# ── write_pipeline ────────────────────────────────────────────────────────────


def _make_project(tmp_path: Path) -> Path:
    """Create a project dir with .agentic/ structure."""
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / ".agentic").mkdir()
    return proj


def test_write_pipeline_creates_file(tmp_path):
    """write_pipeline writes default.yaml in .agentic/pipelines/."""
    proj = _make_project(tmp_path)
    team = [{"agent": "worker"}]
    result = write_pipeline(team, proj)

    assert result is not None
    assert result == proj / ".agentic" / "pipelines" / "default.yaml"
    assert result.exists()


def test_write_pipeline_yaml_parseable(tmp_path):
    """Written file is valid YAML with expected structure."""
    proj = _make_project(tmp_path)
    team = [{"agent": "worker"}, {"agent": "tester"}]
    result = write_pipeline(team, proj)

    data = yaml.safe_load(result.read_text())
    assert data["name"] == "default"
    assert len(data["stages"]) == 4
    assert data["stages"][0]["name"] == "plan"
    assert data["stages"][-1]["name"] == "verify"


def test_write_pipeline_no_agentic_returns_none(tmp_path):
    """project_dir without .agentic/ → returns None, no error."""
    proj = tmp_path / "project"
    proj.mkdir()
    result = write_pipeline([{"agent": "worker"}], proj)
    assert result is None


def test_write_pipeline_existing_backed_up(tmp_path):
    """Existing default.yaml is backed up to .bak."""
    proj = _make_project(tmp_path)
    pipelines = proj / ".agentic" / "pipelines"
    pipelines.mkdir()
    (pipelines / "default.yaml").write_text("name: old\n")

    write_pipeline([{"agent": "worker"}], proj)

    backup = pipelines / "default.yaml.bak"
    assert backup.exists()
    assert backup.read_text() == "name: old\n"


def test_write_pipeline_empty_team(tmp_path):
    """Empty team still writes valid pipeline with plan+verify."""
    proj = _make_project(tmp_path)
    result = write_pipeline([], proj)

    assert result is not None
    data = yaml.safe_load(result.read_text())
    assert len(data["stages"]) == 2
    assert data["stages"][0]["name"] == "plan"
    assert data["stages"][1]["name"] == "verify"


def test_write_pipeline_preserves_order(tmp_path):
    """Pipeline stages reflect team order from form."""
    proj = _make_project(tmp_path)
    team = [
        {"agent": "architect"},
        {"agent": "worker"},
        {"agent": "tester"},
        {"agent": "auditor"},
    ]
    result = write_pipeline(team, proj)

    data = yaml.safe_load(result.read_text())
    names = [s["name"] for s in data["stages"]]
    assert names == ["plan", "architect", "worker", "tester", "auditor", "verify"]


def test_write_pipeline_creates_pipelines_dir(tmp_path):
    """If .agentic/pipelines/ doesn't exist, it's created."""
    proj = _make_project(tmp_path)
    assert not (proj / ".agentic" / "pipelines").exists()

    write_pipeline([{"agent": "worker"}], proj)
    assert (proj / ".agentic" / "pipelines").is_dir()
