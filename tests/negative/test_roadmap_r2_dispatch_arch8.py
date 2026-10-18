"""R2.2 (TODO-0221) REVIEW-fix 10 / auditor iter 11 — the final MEDIUM pair.

F-A: ``_path_link.decode_path_link`` decoded the NUL form with
``os.fsdecode`` (the HOST filesystem encoding), while every producer/consumer
of the same path identity uses ``display_path`` (utf-8 + surrogateescape) and
the writer emits utf-8. On a host whose filesystem encoding is not UTF-8 the
decoded exclusion strings would not match ``check_source_tree``'s adopted set:
the adopted file silently drops out of the commit gate. The codec must never
consult the filesystem encoding.

F-B: ``.agentic/roles/draft/`` is awf-written runtime (``api/roles``
add-role --draft, the service-run candidate) but was absent from
``paths.RUNTIME_DIRS`` — not gitignored, not "managed". A live draft counted
as the USER's dirt under ``require_clean`` (dispatch refused) and leaked into
the dirty tables.

Файл КРАСНЫЙ на дереве до фикса стадии agent-implementer (review-fix 10).
Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from awf import api


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
        project_name="R2ARCH8",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "awf init")
    return repo


def test_path_link_decode_never_consults_filesystem_encoding(monkeypatch):
    """F-A: a non-UTF-8 filesystem encoding must not change the decoded link.

    ``os.fsdecode`` is redirected to the ascii+surrogateescape decoder a
    non-UTF-8 host would use. A codec that calls it returns mangled surrogate
    "paths" unequal to the writer's input; a codec that decodes utf-8 itself
    returns the exact bytes back.
    """
    from awf import _path_link

    monkeypatch.setattr(
        os, "fsdecode", lambda b: b.decode("ascii", "surrogateescape"), raising=False
    )

    paths = sorted(["файл.txt", "a\x0bb.txt"])
    body = _path_link.encode_path_link(paths).encode("utf-8", "surrogateescape")
    assert _path_link.decode_path_link(body) == paths


def test_draft_dir_is_managed_runtime(tmp_path):
    """F-B: a force-added (tracked) role draft is awf runtime, not user dirt."""
    from awf.dispatch_policy import check_source_tree, is_managed_runtime_path

    assert is_managed_runtime_path(b".agentic/roles/draft/auditor.md")

    repo = _project(tmp_path)
    draft = repo / ".agentic" / "roles" / "draft"
    draft.mkdir(parents=True, exist_ok=True)
    cand = draft / "auditor.md"
    cand.write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-f", ".agentic/roles/draft/auditor.md")
    _git(repo, "commit", "-qm", "tracked draft")
    cand.write_text("b\n", encoding="utf-8")

    report = check_source_tree(repo)
    assert report.paths == ()
    assert ".agentic/roles/draft/auditor.md" in report.managed
    assert report.clean


def test_untracked_draft_does_not_dirty_the_tree(tmp_path):
    """F-B: an untracked draft must not refuse a require_clean dispatch."""
    from awf.dispatch_policy import check_source_tree

    repo = _project(tmp_path)
    draft = repo / ".agentic" / "roles" / "draft"
    draft.mkdir(parents=True, exist_ok=True)
    (draft / "auditor.md").write_text("# candidate\n", encoding="utf-8")

    assert check_source_tree(repo).clean


def test_draft_dir_is_gitignored_after_init(tmp_path):
    """F-B: the draft area joins the .gitignore block (one source)."""
    repo = _project(tmp_path)
    ignore = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".agentic/roles/draft/" in ignore
