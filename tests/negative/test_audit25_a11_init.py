"""A-11 (audit 2026-09-25, слой 2): повторный init не трогает runtime и архив.

До: ``init_project(force=False)`` при существующей ``.agentic/`` вызывал
``_clean_runtime`` и удалял inbox/outbox/handoff/done/state/logs — один
непреднамеренный ``awf_init`` на живом проекте сбрасывал активный TODO и
весь архив. ``force=True`` удалял, не проверяя живой ли пайплайн.

Findings covered:
- test_reinit_without_force_preserves_runtime — повторный init без force
  побайтов не меняет ни один существующий файл
- test_force_with_live_pipeline_is_refused — force=True отказывает
  (AwfApiError), пока жив пайплайн, до любых удалений
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api


def _snapshot(agentic: Path) -> dict[str, bytes]:
    """relpath -> bytes for every file under .agentic/."""
    return {
        str(p.relative_to(agentic)): p.read_bytes()
        for p in sorted(agentic.rglob("*"))
        if p.is_file()
    }


def _seed_runtime(proj: Path) -> None:
    """Active TODO in inbox/ + an artifact in done/TODO-0001/ + stage state."""
    agentic = proj / ".agentic"
    inbox = agentic / "inbox"
    archived = agentic / "done" / "TODO-0001"
    state = agentic / "state"
    inbox.mkdir(parents=True, exist_ok=True)
    archived.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    (inbox / "TODO-0001.md").write_bytes(b"# active task\n")
    (inbox / "TODO-0001.ready").write_bytes(b"")
    (archived / "TODO.md").write_bytes(b"archived work\n")
    (state / "current.yaml").write_bytes(b"phase: run\n")


def test_reinit_without_force_preserves_runtime(tmp_git_repo):
    """A-11.1: re-init без force — каждый файл побайтов как был.

    Сравнение СОДЕРЖИМОГО (read_bytes), не существования: файл мог быть
    перезаписан пустым и «пережить» проверку is_file().
    """
    api.init_project(tmp_git_repo, project_name="A11Init")
    _seed_runtime(tmp_git_repo)
    agentic = tmp_git_repo / ".agentic"

    before = _snapshot(agentic)
    assert before, "seed files are missing — the test is void"

    result = api.init_project(tmp_git_repo, project_name="A11Init")

    after = _snapshot(agentic)
    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    modified = sorted(
        r for r in set(before) & set(after) if before[r] != after[r]
    )
    assert not removed, f"re-init deleted files: {removed}"
    assert not added, f"re-init wrote new files: {added}"
    assert not modified, f"re-init modified files: {modified}"

    # The preserved path reads the existing config, not a fresh one.
    assert result.project_name == "A11Init"
    assert result.stack == "(preserved)"


def test_force_with_live_pipeline_is_refused(tmp_git_repo, monkeypatch):
    """A-11.2: force=True при живом пайплайне — отказ до любых удалений.

    Подменяем именно общий резолвер ``awf.api._liveness.resolve`` — тот же,
    что использует ``start_pipeline`` (awf/api/pipeline.py).
    """
    api.init_project(tmp_git_repo, project_name="A11Init")
    _seed_runtime(tmp_git_repo)
    agentic = tmp_git_repo / ".agentic"

    before = _snapshot(agentic)
    assert before, "seed files are missing — the test is void"

    monkeypatch.setattr(
        "awf.api._liveness.resolve",
        lambda project_dir: (True, 4242, "test"),
    )

    with pytest.raises(api.AwfApiError, match="running"):
        api.init_project(tmp_git_repo, project_name="A11Init", force=True)

    after = _snapshot(agentic)
    removed = sorted(set(before) - set(after))
    modified = sorted(
        r for r in set(before) & set(after) if before[r] != after[r]
    )
    assert not removed, f"refused force-init still deleted files: {removed}"
    assert not modified, f"refused force-init still modified files: {modified}"


def test_force_dry_run_is_a_pure_read(tmp_git_repo):
    """A-11 regression pin (QA): force+dry_run must not delete.

    The docstring contract is "``dry_run=True`` is a pure read and changes
    nothing" (awf/api/lifecycle.py), and the CLI path
    ``awf init --force --dry-run --non-interactive`` passes both flags
    together (awf/cmd_init.py:43). Baseline behaviour: force=True skipped
    the re-init block entirely and the fresh-init ``if dry_run: return``
    made the call a pure read. After the A-11 restructure the ``if force:``
    branch runs BEFORE the dry_run check, so ``_clean_runtime`` deletes
    inbox/outbox/handoff/done/state/logs/context/dashboards/inputs and the
    result still claims "[dry-run] no files written" — silent data loss
    with a false confirmation.
    """
    api.init_project(tmp_git_repo, project_name="A11Init")
    _seed_runtime(tmp_git_repo)
    agentic = tmp_git_repo / ".agentic"

    before = _snapshot(agentic)
    assert before, "seed files are missing — the test is void"

    result = api.init_project(
        tmp_git_repo, project_name="A11Init", force=True, dry_run=True
    )

    after = _snapshot(agentic)
    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    modified = sorted(
        r for r in set(before) & set(after) if before[r] != after[r]
    )
    assert not removed, f"force+dry-run deleted files: {removed}"
    assert not added, f"force+dry-run wrote new files: {added}"
    assert not modified, f"force+dry-run modified files: {modified}"
    assert "dry-run" in result.next_action, (
        f"result no longer reports dry-run: {result.next_action!r}"
    )
