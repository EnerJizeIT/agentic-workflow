"""R2.2 (TODO-0221) QA regression guards — the adoption channels under require_clean.

Independent QA review of the R2.2 unit (dirty_policy + claim_changes). Two
behavioural findings on the NEW ``require_clean`` default were raised on the
first QA pass (see the TODO-0221 REVIEW-fix block) and fixed by the
implementer; these tests are the regression guards that keep them fixed:

F1. ``include_untracked`` must be usable under ``require_clean``.  The caller
    explicitly adopts a pre-existing untracked file (RUN10 #4), so that very
    file is the unit's own work — it must NOT make the source tree refuse.
    (Fixed: the adoption channels resolve BEFORE the policy gate; adopted
    paths are excluded from the refusal.)

F2. ``carry_over_from`` (the R5 leak-gate finding channel) must keep working
    under ``require_clean``: a rejected unit's untracked finding file is
    adopted, not refused.  (Fixed the same way.)

Coverage. ``claim_from`` (the CODE channel) must own a claimed UNTRACKED
    change too, not only a tracked one — the untracked case is exactly the
    dirt ``require_clean`` would otherwise refuse, and the unit's own retry
    test covers only the tracked case.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from awf import api, reject_files

SRC = "TODO-0001"
RETRY = "TODO-0002"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _require_clean_project(tmp_path: Path) -> Path:
    """A git project with awf init, committed, and dirty_policy=require_clean."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2QA",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    cfg = repo / ".agentic" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["discipline"] = {"dirty_policy": "require_clean"}
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "awf init")
    return repo


def test_include_untracked_allowed_under_require_clean(tmp_path):
    """F1: a tree whose ONLY dirt is a path the caller explicitly adopts via
    ``include_untracked`` must not be refused by ``require_clean``."""
    repo = _require_clean_project(tmp_path)
    (repo / "finding.txt").write_text("finding\n", encoding="utf-8")

    result = api.dispatch_todo(
        repo,
        f"# {SRC}\nTask\n",
        todo_id=SRC,
        include_untracked=["finding.txt"],
    )
    assert result.todo_id == SRC
    # the adopted path must NOT be reported as un-owned dirt
    assert "finding.txt" not in result.dirty_files


def test_carry_over_allowed_under_require_clean(tmp_path):
    """F2: the R5 leak-gate (``carry_over_from``) must keep working under the
    new ``require_clean`` default — a rejected unit's untracked finding file
    is adopted, not refused."""
    repo = _require_clean_project(tmp_path)
    # the origin unit exists (a rejected TODO stays in the inbox)
    api.dispatch_todo(repo, f"# {SRC}\norigin\n", todo_id=SRC)
    (repo / "notes.txt").write_text("finding\n", encoding="utf-8")
    reject_files.reject_files_path(repo, SRC).write_text("notes.txt\n", encoding="utf-8")

    result = api.dispatch_todo(
        repo,
        f"# {RETRY}\nretry\n",
        todo_id=RETRY,
        carry_over_from=SRC,
    )
    assert result.todo_id == RETRY
    assert (repo / ".agentic" / "context" / f"BASELINE-{RETRY}.sha").is_file()


def test_claim_from_adopts_untracked_change_under_require_clean(tmp_path):
    """Coverage: a claimed UNTRACKED predecessor change is owned by the retry
    under ``require_clean`` — the adoption holds for the untracked case too
    (the unit's own retry test covers the tracked case)."""
    repo = _require_clean_project(tmp_path)
    api.create_baseline(repo, SRC)
    (repo / "newmod.py").write_text("brand new\n", encoding="utf-8")  # untracked

    plan = api.plan_claim_changes(repo, SRC)
    api.apply_claim_changes(repo, plan, confirmed_digest=plan.digest)
    result = api.dispatch_todo(
        repo, f"# {RETRY}\nretry\n", todo_id=RETRY, claim_from=SRC
    )
    assert result.claimed_files == ["newmod.py"]
    assert result.dirty_files == []
