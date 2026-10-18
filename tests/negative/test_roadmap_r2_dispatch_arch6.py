"""R2.2 architect-review, iter 10 (TODO-0221): the ``claim-changes`` CLI
crashed on its DOCUMENTED default — the plan/apply path a supervisor actually
uses.

Файл КРАСНЫЙ на дереве до фикса стадии agent-architect-review (iter 10).

DRIFT: ``awf/cmd_claim_changes.py`` handled the ``--paths`` default with a
dead store — ``if raw_paths is None: paths = []`` set a local that the very
next comprehension overwrote while it still iterated the UNCHANGED ``None``:

    raw_paths = getattr(args, "paths", None)
    if raw_paths is None:
        paths = []            # dead: overwritten below
    ...
    paths = [p for chunk in raw_paths ...]   # raw_paths is still None
                                          # -> TypeError: not iterable

``--paths`` has ``action="append", default=None``, so ``awf claim-changes
<TODO>`` (the default "claim all changes" mode, printed by the tool itself as
``Apply with: awf claim-changes <TODO> --apply <digest>``) died with a bare
``TypeError`` before ever reaching the core. The API-level
``plan_claim_changes(project_dir, source_todo)`` was fine; only the CLI wrapper
was broken — the "written next to instead of correctly" class.

DRIFT (second finding, same iter): ``_rollback_service_side_effects`` (the
"undo EVERY side effect" cleanup of a failed service start) enumerated the
baseline artefacts by hand and omitted ``.worktree`` while adding the new
``.claim`` link — the anti-vacuum question "where else must the same
invariant hold" exposes the incomplete list.

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import api

SRC = "TODO-0001"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _project(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2ARCH6",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "awf init")
    return repo


def _claimable(repo: Path) -> None:
    (repo / "core.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "core")
    api.create_baseline(repo, SRC)
    (repo / "core.py").write_text("predecessor work\n", encoding="utf-8")


def test_claim_cli_default_paths_plans_all_changes(tmp_path, capsys):
    """``awf claim-changes <TODO>`` without ``--paths`` is the documented
    default (all changes): it must print the plan and exit 0, not crash."""
    repo = _project(tmp_path)
    _claimable(repo)

    from awf import cli as cli_mod

    parser, _sub = cli_mod._build_parser()
    args = parser.parse_args(["claim-changes", SRC, "--project-dir", str(repo)])
    rc = cli_mod._run_command(args)
    out = capsys.readouterr().out
    assert rc == 0, (
        f"claim-changes without --paths must default to all changes; out={out!r}"
    )
    assert "core.py" in out, f"the changed file must be in the plan; got {out!r}"


def test_claim_cli_default_apply_consumes_all_changes(tmp_path):
    """The same default must feed the ``--apply`` path: preview the digest
    first (default paths), then apply it — both without ``--paths``."""
    repo = _project(tmp_path)
    _claimable(repo)

    from awf import claim_changes as cc
    from awf import cli as cli_mod

    plan = cc.plan_claim_changes(repo, SRC)
    parser, _sub = cli_mod._build_parser()
    args = parser.parse_args(
        ["claim-changes", SRC, "--apply", plan.digest, "--project-dir", str(repo)]
    )
    rc = cli_mod._run_command(args)
    assert rc == 0, "claim-changes --apply without --paths must consume the plan"
    assert cc.read_claim(repo, SRC) is not None


def test_service_rollback_removes_every_baseline_artefact(tmp_path):
    """DRIFT (second finding, same iter): the "undo EVERY side effect" cleanup
    of a failed service start enumerated the baseline artefacts BY HAND and
    omitted ``.worktree`` (TODO-0204) while adding the new ``.claim`` link
    (TODO-0221) — a stale ``BASELINE-<id>.worktree`` survived the rollback.
    The list must name EVERY baseline artefact a dispatch leaves behind."""
    from awf import paths
    from awf.api import run as run_api

    repo = _project(tmp_path)
    ctx = paths.context_dir(repo)
    ctx.mkdir(parents=True, exist_ok=True)
    artefacts = (
        ".sha", ".status", ".tests.log", ".env.log", ".untracked",
        ".worktree", ".include", ".carry_over", ".claim",
    )
    for suffix in artefacts:
        (ctx / f"BASELINE-{SRC}{suffix}").write_text("x\n", encoding="utf-8")

    run_api._rollback_service_side_effects(repo, SRC, "svc")

    left = sorted(p.name for p in ctx.glob(f"BASELINE-{SRC}.*"))
    assert left == [], f"rollback must remove every baseline artefact; left={left}"
