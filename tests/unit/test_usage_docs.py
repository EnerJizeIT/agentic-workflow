"""AUD13-04: USAGE.md pipeline examples must use real schema keys.

The old examples taught ``on_done:`` and ``kind:`` — keys the loader
silently ignores — so a user copying them got a pipeline that never
commits and a stage running as plan-kind. This test loads every
``stages:`` YAML block from USAGE.md through the real ``load_stages``
and rejects any key outside the schema, so doc/code drift fails CI.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from awf.pipeline import Stage, load_stages

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
USAGE_MD = REPO_ROOT / "USAGE.md"

# Keys load_stages accepts on a stage entry (awf/pipeline.py).
ALLOWED_STAGE_KEYS = {
    "name", "role", "description",
    "on_blocked", "on_approved", "on_rejected", "on_failed",
    "max_retries", "max_rollbacks",
}


def _stages_blocks() -> list[str]:
    """All ```yaml blocks in USAGE.md that declare a pipeline (stages:)."""
    text = USAGE_MD.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.DOTALL)
    return [b for b in blocks if re.search(r"^\s*stages:", b, re.MULTILINE)]


def _load(block: str, tmp_path: Path) -> list[Stage]:
    path = tmp_path / "pipeline.yaml"
    path.write_text(block, encoding="utf-8")
    return load_stages(path)


def test_usage_has_pipeline_examples():
    blocks = _stages_blocks()
    assert len(blocks) >= 2, (
        f"expected >=2 stages: examples in USAGE.md, found {len(blocks)}"
    )


def test_usage_examples_use_only_schema_keys():
    for block in _stages_blocks():
        data = yaml.safe_load(block)
        for stage in data["stages"]:
            unknown = set(stage) - ALLOWED_STAGE_KEYS
            assert not unknown, (
                f"USAGE.md example uses keys the loader ignores: {sorted(unknown)}"
            )


def test_single_worker_example_shape(tmp_path):
    """First example: plan → 1 worker → verify, commit on approve."""
    stages = _load(_stages_blocks()[0], tmp_path)
    assert [s.kind for s in stages] == ["plan", "execute", "verify"]
    assert [s.role for s in stages] == ["supervisor", "agent-implementer", "supervisor"]
    assert stages[-1].on_approved == "commit_and_next", (
        "the verify stage must commit the result (on_approved: commit_and_next)"
    )


def test_full_chain_example_shape(tmp_path):
    """Second example: plan → 4 workers → verify."""
    stages = _load(_stages_blocks()[1], tmp_path)
    assert [s.kind for s in stages] == ["plan", "execute", "execute", "execute", "execute", "verify"]
    assert stages[0].role == "supervisor"
    assert stages[-1].role == "supervisor"
    assert stages[-1].on_approved == "commit_and_next"
