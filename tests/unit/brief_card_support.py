"""Shared fixtures for the IMP5 card tests (TODO-0348).

The card tests are split by meaning (entry / verify / budget) so no file
crosses the ratchet's 400-line threshold; the project, status and report
builders live here once.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import yaml

from awf.api._results import StatusResult
from awf.run_plan_read import RunRecord

TODO = "TODO-0326"
FIRST_FAILURE = "- `pytest tests/unit/test_contracts.py`: FAIL (rc=1) — 2 failed"

FAILED_PACK = f"""# GATES {TODO}

## Verdict: **failed** (exit 1) — 2 section(s) measured

## 1. contracts: FAIL

{FIRST_FAILURE}
- `pytest tests/unit/test_other.py`: FAIL (rc=1) — later

## 2. tests: PASS

- `pytest`: pass
"""

OK_PACK = """# GATES

## Verdict: **ok** (exit 0) — 1 section(s) measured

## 1. tests: PASS

- `pytest`: pass
"""

EMERGENCY_PACK = (
    f"# GATES-{TODO} — verify-pack report\n\n"
    "## Verdict: **error** (verify-pack did not run)\n\n"
    "verify-pack failed before measuring anything:\n\n"
    "```\nRuntimeError: boom\n```\n"
)


def project(tmp_path: Path, name: str = "proj") -> Path:
    """A configured project with work history (a continued project)."""
    root = tmp_path / name
    (root / ".agentic" / "state").mkdir(parents=True)
    (root / ".agentic" / "context").mkdir(parents=True)
    pipes = root / ".agentic" / "pipelines"
    pipes.mkdir(parents=True)
    (pipes / "default.yaml").write_text(
        "name: default\nstages:\n"
        "  - name: plan\n    role: supervisor\n"
        "  - name: worker\n    role: worker\n"
        "  - name: verify\n    role: supervisor\n",
        encoding="utf-8",
    )
    done = root / ".agentic" / "done" / "TODO-0001"
    done.mkdir(parents=True)
    (done / "TODO.md").write_text("# TODO-0001\n", encoding="utf-8")
    return root


def status(**overrides) -> StatusResult:
    base = dict(
        project_name="proj",
        active_todos=[],
        done_count=1,
        blocked_count=0,
        blocked_ids=[],
        conflict_warning=None,
        suggestion=None,
    )
    base.update(overrides)
    return StatusResult(**base)


def record(**overrides) -> RunRecord:
    base = dict(active=False, goal="", last_decision=None, position="0/0")
    base.update(overrides)
    return RunRecord(**base)


def situations() -> list[tuple[str, StatusResult | None, RunRecord | None]]:
    """One fixture per REACHABLE situation code of the card (S0–S13, S1a).

    ``S11`` is the reject family's data id (tool_map.yaml rows, order), not
    a state the card can render: a reject with a live record is S11a, with a
    closed one S11b. A fixture for it duplicated S11a and proved nothing
    (AUD9).
    """
    reject = {"kind": "reject", "todo_id": "TODO-0001", "reason": "tests failed"}
    approve = {"kind": "approve", "todo_id": "TODO-0001", "reason": ""}
    return [
        ("S0", None, None),
        ("S1", status(salvage_needed=True, salvage_stage="verify"), None),
        ("S1a", status(foreign_commit="abc123 pushed outside the gate"), None),
        ("S2", status(blocked_ids=["TODO-0001"]), None),
        ("S3", status(budget_exhausted_todo="TODO-0001"), None),
        ("S4", status(engine_dead="stage=verify, todo=TODO-1, pid gone"), None),
        ("S5", status(stale_stage="STALE stage verify — dead"), None),
        ("S6", status(zombie_state="verify for TODO-1, no signal"), None),
        ("S7", status(pipeline_running=True, expected_action="plan stage — wait"), None),
        (
            "S8",
            status(
                pipeline_running=True,
                current_stage_kind="verify",
                expected_action="VERIFY STAGE — pipeline waiting for YOUR decision.",
            ),
            None,
        ),
        ("S9", status(active_todos=[{"todo_id": "TODO-0001"}]), None),
        ("S10", status(suggestion="plan the next one"), None),
        ("S11a", status(), record(active=True, last_decision=reject)),
        ("S11b", status(), record(active=False, last_decision=reject)),
        ("S12", status(), record(active=True, last_decision=approve)),
        ("S13", status(checkpoint_pending=True), None),
    ]


def mask_date(text: str) -> str:
    return re.sub(r"date: \d{4}-\d{2}-\d{2}", "date: DATE-MASKED", text)


def write_state(
    root: Path,
    todo: str = TODO,
    updated: str = "2026-01-01T00:00:00+00:00",
) -> None:
    (root / ".agentic" / "state" / "current.yaml").write_text(
        yaml.safe_dump(
            {
                "stage_name": "verify",
                "todo_id": todo,
                "stage_kind": "verify",
                "updated_at": updated,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def write_gates(root: Path, body: str, *, old: bool = False) -> Path:
    path = root / ".agentic" / "context" / f"GATES-{TODO}.md"
    path.write_text(body, encoding="utf-8")
    if old:
        past = time.time() - 86400
        os.utime(path, (past, past))
    return path


def verify_status(**overrides) -> StatusResult:
    base = dict(
        pipeline_running=True,
        current_stage_name="verify",
        current_stage_kind="verify",
        expected_action="VERIFY STAGE — pipeline waiting for YOUR decision.",
        current_todo=TODO,
        state_updated_at="2026-01-01T00:00:00+00:00",
        next_action="VERIFY STAGE — pipeline waiting for YOUR decision.",
    )
    base.update(overrides)
    return status(**base)


def heavy_project(tmp_path: Path) -> Path:
    root = project(tmp_path)
    write_state(root)
    write_gates(root, FAILED_PACK)
    doctrine = root / ".agentic" / "doctrine"
    doctrine.mkdir()
    (doctrine / "01-long.md").write_text(
        "# Долгая доктрина\n\n" + ("слово " * 400) + "\n", encoding="utf-8"
    )
    return root


def heavy_status() -> StatusResult:
    todos = [{"todo_id": f"TODO-{index:04d}"} for index in range(20)]
    return verify_status(
        active_todos=todos,
        blocked_ids=["TODO-0002"],
        salvage_needed=True,
        salvage_stage="verify",
        foreign_commit="abc123",
        budget_exhausted_todo="TODO-0003",
        engine_dead="stage=verify, pid gone",
        stale_stage="STALE stage verify — dead",
        zombie_state="verify for TODO-1, no signal",
    )
