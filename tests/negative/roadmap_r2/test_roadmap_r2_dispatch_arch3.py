"""R2.2 auditor, iter 7 (TODO-0221): the remaining guard / robustness gaps.

Файл КРАСНЫЙ на текущем дереве (находки стадии agent-project-auditor, iter 7).

* M3 — ``awf/api/pipeline.py``: ``create_baseline`` reads the PRINTED git
  listings (``git status --short`` and ``git ls-files --others``) through
  ``git_utils.git_stdout(text=True)`` with NO guard (its twin
  ``reject_files._untracked_view`` got the triple guard in iter 5). A
  non-UTF-8 name under ``core.quotepath=false`` (a legitimate user config)
  makes git emit the raw bytes, the strict decode raises ``UnicodeDecodeError``
  and it escapes the baseline write — a raw crash on ``awf dispatch`` /
  ``awf run_next`` re-baseline (repro'd).
* LOW-1 — ``awf/claim_changes.py::claim_drift`` calls ``tree_fingerprint``
  outside its try: a ``GitModelError`` (a ``RuntimeError``) escapes instead of
  the ``AwfApiError`` the docstring promises.
* LOW-2 — ``awf/cmd_claim_changes.py`` splits ``--paths`` on a comma, so a
  path that CONTAINS a comma can never be named.

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from awf import api, git_utils
from awf.api import pipeline
from awf.api._errors import AwfApiError

SRC = "TODO-0001"


def _git(repo: Path, *args: str, check: bool = True) -> None:
    subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True)


def _project(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2A3",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "awf init")
    return repo


def _raw_untracked_file(repo: Path, name: bytes) -> None:
    fd = os.open(os.fsencode(repo) + b"/" + name, os.O_CREAT | os.O_WRONLY, 0o644)
    os.write(fd, b"x\n")
    os.close(fd)


# ─── M3: the baseline's printed git reads must not crash the dispatch ─────


def test_untracked_snapshot_guards_transient_git_failure(tmp_path, monkeypatch):
    """A hung/missing git on the printed read must degrade, not raise."""
    repo = _project(tmp_path)

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="git ls-files", timeout=30)

    monkeypatch.setattr(git_utils, "git_stdout", boom)
    result = pipeline._untracked_snapshot(repo, set())
    assert isinstance(result, str), (
        "_untracked_snapshot must swallow a transient git failure and return a "
        f"string; got {result!r}"
    )


def test_untracked_snapshot_survives_non_utf8_under_quotepath_false(tmp_path):
    """``core.quotepath=false`` + a non-UTF-8 name: the printed read raises a
    ``UnicodeDecodeError`` (a ``ValueError``) — it must not escape."""
    repo = _project(tmp_path)
    _git(repo, "config", "core.quotepath", "false")
    _raw_untracked_file(repo, b"\xff\xfe.txt")

    result = pipeline._untracked_snapshot(repo, set())
    assert isinstance(result, str)


def test_dispatch_survives_non_utf8_untracked_under_quotepath_false(tmp_path):
    """End-to-end: the audit's raw crash was ``awf dispatch`` (the same
    ``create_baseline`` also serves the ``awf run_next`` re-baseline)."""
    repo = _project(tmp_path)
    _git(repo, "config", "core.quotepath", "false")
    _raw_untracked_file(repo, b"\xff\xfe.txt")

    result = api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)
    assert result.todo_id == SRC
    assert (repo / ".agentic" / "context" / f"BASELINE-{SRC}.sha").is_file()


# ─── LOW-1: claim_drift wraps a tree-read failure ────────────────────────


def test_claim_drift_wraps_tree_read_failure(tmp_path, monkeypatch):
    repo = _project(tmp_path)
    (repo / "core.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "core")
    api.create_baseline(repo, SRC)
    (repo / "core.py").write_text("predecessor work\n", encoding="utf-8")

    from awf import claim_changes
    from awf.infra import git_model

    plan = claim_changes.plan_claim_changes(repo, SRC)

    def boom(_root):
        raise git_model.GitModelError("git could not run (timeout or git unavailable)")

    monkeypatch.setattr(git_model, "tree_fingerprint", boom)
    with pytest.raises(AwfApiError):
        claim_changes.claim_drift(repo, plan)


# ─── LOW-2: a comma in a claimed path ────────────────────────────────────


def test_claim_cli_paths_accepts_a_comma_in_a_name(tmp_path, capsys):
    repo = _project(tmp_path)
    (repo / "a,b.txt").write_text("x\n", encoding="utf-8")
    api.create_baseline(repo, SRC)

    from awf import cli as cli_mod

    parser, _sub = cli_mod._build_parser()
    args = parser.parse_args(
        ["claim-changes", SRC, "--paths", "a,b.txt", "--project-dir", str(repo)]
    )
    rc = cli_mod._run_command(args)
    out = capsys.readouterr().out
    assert rc == 0, f"a comma-containing path must be nameable; output: {out!r}"
    assert "a,b.txt" in out


def test_claim_cli_paths_repeatable_selects_multiple(tmp_path, capsys):
    repo = _project(tmp_path)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    api.create_baseline(repo, SRC)

    from awf import cli as cli_mod

    parser, _sub = cli_mod._build_parser()
    args = parser.parse_args(
        [
            "claim-changes", SRC,
            "--paths", "a.txt",
            "--paths", "b.txt",
            "--project-dir", str(repo),
        ]
    )
    rc = cli_mod._run_command(args)
    out = capsys.readouterr().out
    assert rc == 0, f"multiple --paths must select both; output: {out!r}"
    assert "a.txt" in out and "b.txt" in out
