"""IMP1 negative cards: no/empty/broken key, an old snapshot, ``strict``.

T5 (AC40/AC41), T6 (AC11/AC32), T7 (AC32).  T5 calls ``resolve`` — the
owner of the missing/broken-key refusal; T6/T7 go through ``load_stages``,
where ``strict`` refuses before a Stage exists.  ``load_stages`` still falls
back to the role rule when the whole table is missing: AC40's launch-path
refusal is OPEN (QA TODO-0378).  The old snapshot file is only read (its
sha256 stays the same).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from awf.pipeline import load_stages
from awf.stage_context_types import (
    StageTypeMissing,
    StageTypeUndeclared,
    resolve,
)

SNAPSHOT = """\
name: snap
stages:
  - name: plan
    role: supervisor
  - name: implementer-close
    role: agent-implementer
  - name: verify
    role: supervisor
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config(types: str) -> dict:
    import yaml

    return yaml.safe_load(types)


def _snapshot(proj: Path) -> Path:
    path = proj / ".agentic" / "context" / "PIPELINE-TODO-0001.yaml"
    path.write_text(SNAPSHOT, encoding="utf-8")
    return path


def _project(tmp_path: Path, config_text: str) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic" / "context").mkdir(parents=True)
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(config_text, encoding="utf-8")
    return proj


@pytest.mark.parametrize(
    "config_text",
    [
        "project:\n  name: T\n",
        "project:\n  name: T\nstage_types: {}\n",
    ],
)
def test_T5_missing_or_empty_key_refuses_with_path_and_init_hint(
    tmp_path: Path, config_text: str
) -> None:
    """AC40: no/empty key → CTX-TYPE-MISSING, config path, `awf init`."""
    proj = _project(tmp_path, config_text)
    config = _config(config_text)
    with pytest.raises(StageTypeMissing) as exc:
        resolve(
            config,
            "plan",
            "supervisor",
            config_path=proj / ".agentic" / "config.yaml",
        )
    message = str(exc.value)
    assert "CTX-TYPE-MISSING" in message
    assert str(proj / ".agentic" / "config.yaml") in message
    assert "awf init" in message


def test_T5_bad_value_names_the_pair_and_the_config(tmp_path: Path) -> None:
    """AC41: a value outside the closed list is a refusal, not a guess."""
    config_path = tmp_path / ".agentic" / "config.yaml"
    config = _config(
        "project:\n  name: T\n"
        "stage_types:\n  agent-qa-review: audit\n"
    )
    with pytest.raises(StageTypeMissing) as exc:
        resolve(config, "agent-qa-review", "agent-qa-review", config_path=config_path)
    message = str(exc.value)
    assert "agent-qa-review" in message
    assert str(config_path) in message

    unknown_row = _config("stage_types:\n  agent-qa-review: reviewer/unknown\n")
    with pytest.raises(StageTypeMissing):
        resolve(
            unknown_row, "agent-qa-review", "agent-qa-review", config_path=config_path
        )


def test_T5_checklist_row_is_valid() -> None:
    """R1: ``reviewer/checklist`` is the sixth row, not an unknown value."""
    config = _config("stage_types:\n  checklist-close: reviewer/checklist\n")
    assert resolve(config, "checklist-close", "agent-implementer") == (
        "reviewer",
        "checklist",
    )


def test_T6_old_snapshot_defaults_by_role_and_stays_byte_identical(
    tmp_path: Path,
) -> None:
    """AC11/AC32: an old snapshot starts by name; the file is not rewritten."""
    proj = _project(
        tmp_path,
        "project:\n  name: T\n"
        "stage_types:\n  plan: plan\n  verify: supervisor\n",
    )
    snap = _snapshot(proj)
    before = _sha(snap)

    stages = load_stages(snap)

    assert [s.name for s in stages] == ["plan", "implementer-close", "verify"]
    assert stages[1].kind == "execute"  # role agent-implementer, not position
    assert _sha(snap) == before


def test_T7_strict_refuses_an_undeclared_name(tmp_path: Path) -> None:
    """AC32: strict turns CTX-TYPE-DEFAULT into a refusal, no Stage exists."""
    proj = _project(
        tmp_path,
        "project:\n  name: T\n"
        "stage_types:\n  strict: true\n  plan: plan\n  verify: supervisor\n",
    )
    with pytest.raises(StageTypeUndeclared, match="CTX-TYPE-STRICT"):
        load_stages(_snapshot(proj))
