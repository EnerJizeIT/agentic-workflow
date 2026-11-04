"""IMP1 (R1/R7/R14): the stage type is data, not a position.

T2 (16 pairs, idempotent init, doctor row), T4 (one carrier — the config
value drives the kind), T8 (spec at position 0) and the K4 mapping.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from awf import api, doctor
from awf._errors import AwfApiError
from awf.pipeline import load_stages
from awf.stage_context_types import (
    FAMILIES,
    VALID_VALUES,
    engine_kind,
    generate_stage_types,
    resolve,
)

REPO = Path(__file__).resolve().parents[2]
PIPELINES = REPO / ".agentic" / "pipelines"

#: R14/AC33: the 16 stage names of the project's pipeline set.
EXPECTED_NAMES = {
    "plan": "plan",
    "verify": "supervisor",
    "spec-context": "spec",
    "spec-requirements": "spec",
    "spec-requirements-review": "spec",
    "spec-design": "spec",
    "spec-tests": "spec",
    "spec-finalize": "spec",
    "spec-readiness": "spec",
    "agent-implementer": "execute",
    "agent-refactoring-specialist": "execute",
    "agent-architect-review": "reviewer",
    "agent-qa-review": "reviewer",
    "agent-project-auditor": "reviewer",
    "checklist-close": "reviewer/checklist",
    "checklist-check": "reviewer/checklist",
}

TWO_STAGES = """\
name: two
stages:
  - name: plan
    role: supervisor
  - name: agent-qa-review
    role: agent-qa-review
"""


def _project(tmp_path: Path, config_text: str = "project:\n  name: T\n") -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    (proj / ".agentic" / "context").mkdir(parents=True)
    shutil.copytree(PIPELINES, proj / ".agentic" / "pipelines", dirs_exist_ok=True)
    (proj / ".agentic" / "config.yaml").write_text(config_text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    return proj


def _pipeline(proj: Path, text: str = TWO_STAGES, name: str = "two.yaml") -> Path:
    path = proj / ".agentic" / "pipelines" / name
    path.write_text(text, encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_families_are_five_rows_plus_checklist() -> None:
    """T1/AC1: five families, six rows; the checklist row belongs to reviewer."""
    assert set(FAMILIES) == {"plan", "execute", "reviewer", "supervisor", "spec"}
    assert "checklist" in FAMILIES["reviewer"]
    assert VALID_VALUES == {
        "plan", "execute", "reviewer", "reviewer/checklist", "supervisor", "spec"
    }


def test_engine_kind_mapping() -> None:
    """K4: plan→plan, supervisor→verify, the working rows→execute."""
    assert engine_kind("plan") == "plan"
    assert engine_kind("supervisor") == "verify"
    for family in ("execute", "reviewer", "spec"):
        assert engine_kind(family) == "execute"


def test_T2_init_writes_16_pairs_and_is_idempotent(tmp_path: Path) -> None:
    """AC33: init fills the 16 R14 pairs; a second init changes no byte."""
    proj = _project(tmp_path)
    api.init_project(proj, project_name="T")
    config_file = proj / ".agentic" / "config.yaml"
    text = config_file.read_text(encoding="utf-8")

    types = yaml.safe_load(text)["stage_types"]
    assert types == EXPECTED_NAMES

    before = _sha(config_file)
    api.init_project(proj, project_name="T")
    assert _sha(config_file) == before
    assert config_file.read_text(encoding="utf-8") == text


def test_T2_doctor_row_is_green_then_names_a_removed_stage(tmp_path: Path) -> None:
    """AC33/RR58: doctor compares, never writes; a removed name is named."""
    proj = _project(tmp_path)
    api.init_project(proj, project_name="T")
    config_file = proj / ".agentic" / "config.yaml"
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))

    out = doctor._Lines()
    doctor._check_stage_types(proj, config, out)
    assert out.warnings == []
    assert any("16 stage names" in line for line in out.lines)

    broken = dict(config)
    broken["stage_types"] = dict(config["stage_types"])
    broken["stage_types"].pop("agent-qa-review")
    before = _sha(config_file)
    out = doctor._Lines()
    doctor._check_stage_types(proj, broken, out)
    assert out.warnings
    assert "agent-qa-review" in out.warnings[0]
    assert _sha(config_file) == before


def test_T4_config_value_changes_the_kind(tmp_path: Path) -> None:
    """AC39: the same pipeline, another value — another stage family/kind.

    The QA stage sits last (position would say ``verify``); the config says
    ``execute``, and the kind follows the config.
    """
    proj = _project(
        tmp_path,
        "project:\n  name: T\n"
        "stage_types:\n"
        "  plan: plan\n"
        "  agent-qa-review: execute\n",
    )
    pipe = _pipeline(proj)
    assert [s.kind for s in load_stages(pipe)] == ["plan", "execute"]

    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: T\n"
        "stage_types:\n"
        "  plan: plan\n"
        "  agent-qa-review: plan\n",
        encoding="utf-8",
    )
    assert [s.kind for s in load_stages(pipe)] == ["plan", "plan"]


def test_T8_spec_at_position_zero(tmp_path: Path) -> None:
    """AC21: spec-context is a spec stage even at index 0 (no plan stage)."""
    proj = _project(
        tmp_path,
        "project:\n  name: T\n"
        "stage_types:\n"
        "  spec-context: spec\n"
        "  spec-requirements: spec\n"
        "  verify: supervisor\n",
    )
    pipe = _pipeline(
        proj,
        "name: spec\n"
        "stages:\n"
        "  - name: spec-context\n    role: agent-project-auditor\n"
        "  - name: spec-requirements\n    role: agent-system-analyst\n"
        "  - name: verify\n    role: supervisor\n",
    )
    stages = load_stages(pipe)
    assert resolve(
        yaml.safe_load(
            (proj / ".agentic" / "config.yaml").read_text(encoding="utf-8")
        ),
        "spec-context",
        "agent-project-auditor",
    ) == ("spec", "")
    assert [s.kind for s in stages] == ["execute", "execute", "verify"]


def test_generation_refuses_unknown_role() -> None:
    """R14: the generator refuses a role outside the rule (no guessed type)."""
    with pytest.raises(AwfApiError, match="not in the stage-type rule"):
        generate_stage_types([("mystery", "plain-worker")])


def test_T2_init_keeps_unrelated_config_bytes(tmp_path: Path) -> None:
    """K2/AC33: migration adds the block, every other config byte stays.

    The project already has a config (no ``stage_types`` yet); ``init`` must
    patch the table in without losing a comment, a section or a value.
    """
    proj = _project(
        tmp_path,
        "project:\n  name: T\n# owner comment\nverification:\n  test_cmd: pytest -q\n",
    )
    before = (proj / ".agentic" / "config.yaml").read_text(encoding="utf-8")

    api.init_project(proj, project_name="T")

    after = (proj / ".agentic" / "config.yaml").read_text(encoding="utf-8")
    assert after.startswith(before.rstrip("\n"))
    assert "# owner comment" in after
    assert yaml.safe_load(after)["verification"] == {"test_cmd": "pytest -q"}
    assert yaml.safe_load(after)["stage_types"] == EXPECTED_NAMES


def test_T2_init_reports_an_unreadable_config_instead_of_crashing(
    tmp_path: Path,
) -> None:
    """AC33/QA: a config ``init`` cannot read is a WARNING, not a traceback.

    The stage-types patch is the first writer in the re-init branch; using
    its warning list before it existed raised ``UnboundLocalError``.
    """
    proj = _project(tmp_path)
    config_file = proj / ".agentic" / "config.yaml"
    config_file.write_bytes(b"project:\n  name: T\nstage_types: \xff\xfe\n")
    before = config_file.read_bytes()

    result = api.init_project(proj, project_name="T")

    assert any("stage_types not refreshed" in w for w in result.warnings)
    assert config_file.read_bytes() == before


def test_K2_write_stage_types_drops_a_stale_name_and_keeps_strict() -> None:
    """K2: the patcher removes a stale stage and never touches ``strict``."""
    from awf.stage_context_types import write_stage_types

    text = (
        "project:\n  name: T\n# keep\n"
        "stage_types:\n"
        "  strict: true\n"
        "  plan: plan\n"
        "  agent-qa-review: execute\n"
        "  ghost-stage: reviewer\n"
    )
    out = write_stage_types(text, {"plan": "plan", "agent-qa-review": "reviewer"})

    parsed = yaml.safe_load(out)["stage_types"]
    assert parsed == {
        "strict": True,
        "plan": "plan",
        "agent-qa-review": "reviewer",
    }
    assert "# keep" in out
    assert write_stage_types(out, {"plan": "plan", "agent-qa-review": "reviewer"}) == out


def test_T3_run_doctor_wires_the_stage_row_into_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC33/T3: the doctor's report and rc carry the stage-types verdict.

    ``_check_stage_types`` alone proves the comparison; this card proves the
    row is wired into ``run_doctor`` — a removed name reaches ``rc != 0``.
    """
    proj = _project(tmp_path)
    api.init_project(proj, project_name="T")
    config_file = proj / ".agentic" / "config.yaml"
    monkeypatch.setattr(
        "awf.api.model_check.check_model_config",
        lambda *_a, **_k: {
            "models": [],
            "warnings": [],
            "providers_available": [],
            "cli_models_available": False,
        },
    )

    report = doctor.run_doctor(proj)
    assert any(
        "stage types" in line and "16 stage names" in line for line in report.lines
    )

    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    config["stage_types"].pop("agent-qa-review")
    config_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    before = _sha(config_file)

    report = doctor.run_doctor(proj)

    assert report.rc == 1
    assert any("agent-qa-review" in w for w in report.warnings)
    assert _sha(config_file) == before
