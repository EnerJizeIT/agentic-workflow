"""CTX2: one "forget the unit history" home for every re-run surface.

``CLOSER-<id>.json`` (first verdict, frozen wave, attempts) and
``CHECKLIST-<id>.json`` (gate verdict cache) are keyed only by the TODO id.
A same-id re-run must not inherit them. The service rollback always
cleared them; the main ``awf rollback`` and ``restore_todo`` did not
(TODO-0270 audit, CTX2).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import api
from awf.api.run import _rollback_service_side_effects

TODO_ID = "TODO-0001"


def _project(tmp_git_repo) -> Path:
    api.init_project(tmp_git_repo, project_name="History")
    ctx = tmp_git_repo / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"CLOSER-{TODO_ID}.json").write_text(
        '{"first_verdict": "reject", "attempts": 2}', encoding="utf-8"
    )
    (ctx / f"CHECKLIST-{TODO_ID}.json").write_text(
        '{"green": false}', encoding="utf-8"
    )
    return tmp_git_repo


def _history(project: Path) -> list[Path]:
    ctx = project / ".agentic" / "context"
    names = (f"CLOSER-{TODO_ID}.json", f"CHECKLIST-{TODO_ID}.json")
    return [ctx / name for name in names if (ctx / name).exists()]


def test_forget_unit_history_removes_both_files(tmp_git_repo):
    from awf.unit_history import forget_unit_history

    project = _project(tmp_git_repo)

    removed = forget_unit_history(project, TODO_ID)

    assert sorted(removed) == [f"CHECKLIST-{TODO_ID}.json", f"CLOSER-{TODO_ID}.json"]
    assert _history(project) == []


def test_restore_todo_does_not_inherit_history(tmp_git_repo):
    project = _project(tmp_git_repo)
    done = project / ".agentic" / "done" / TODO_ID
    done.mkdir(parents=True)
    (done / "TODO.md").write_text("# TODO\n", encoding="utf-8")

    api.restore_todo(project, TODO_ID)

    assert (project / ".agentic" / "inbox" / f"{TODO_ID}.md").is_file()
    assert _history(project) == [], "restored rerun must start with no unit history"


def test_main_rollback_does_not_keep_history(tmp_git_repo):
    project = _project(tmp_git_repo)
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=project, text=True
    ).strip()
    (project / ".agentic" / "context" / f"BASELINE-{TODO_ID}.sha").write_text(
        sha + "\n", encoding="utf-8"
    )

    api.rollback(project, TODO_ID, mode="soft")

    assert _history(project) == [], "main rollback must forget the unit history"


def test_service_rollback_does_not_keep_history(tmp_git_repo):
    project = _project(tmp_git_repo)
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO_ID}.md").write_text("# TODO\n", encoding="utf-8")

    _rollback_service_side_effects(project, TODO_ID, "svc-TODO-0001", clear_slot=False)

    assert _history(project) == [], "service rollback must forget the unit history"
