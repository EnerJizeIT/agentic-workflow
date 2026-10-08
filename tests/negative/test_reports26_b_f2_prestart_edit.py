"""REPORTS26 B-f2 (TODO-0161): нестартовавший юнит — remove и include.

Source: awf-feature-20261007-dispatchnutyy-no-ne-zapuschennyy-todo-nelzya-ni-udalit-ni.md.
После dispatch у юнита сразу есть ``.ready``; ``awf_todo_remove`` отказывал
из-за него, а метаданные (``include_untracked``) было не поправить — только
руками или запуск+reject.

Решение владельца: (а) ``.ready`` — артефакт диспатча, не признак старта —
remove разрешён для юнитов без PROGRESS и без closure/signal-файлов;
(б) ``update_todo`` получает ``include_untracked`` для нестартовавших с
пересчётом BASELINE.

Инварианты (TODO-0161):
(a) dispatch → ``remove_todo`` (без PROGRESS) → ok: ``.md`` и ``.ready`` в
    trace-каталоге, ``BASELINE-<id>.*`` удалены, в ответе ``removed_files``;
(b) remove с PROGRESS / с исходящим сигналом / с ACK в inbox → прежний
    отказ, файлы не тронуты;
(c) ``update_todo(include_untracked=[путь])`` → файл включён: ушёл из
    ``.untracked``-снапшота, есть в ``.include``-трейсе; невалидный путь →
    отказ без side-effect'ов; пустой список очищает include (файл вернулся
    в снапшот); content не тронут, когда передали только include.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import api, include_untracked, paths

TODO = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    """Committed git repo + minimal .agentic (config.yaml — B2 guard);
    .agentic is gitignored so it never lands in the untracked snapshot."""
    for sub in ("inbox", "outbox", "context", "done", "logs"):
        (tmp_git_repo / ".agentic" / sub).mkdir(parents=True)
    (tmp_git_repo / ".agentic" / "config.yaml").write_text("project:\n  name: F2\n")
    (tmp_git_repo / ".gitignore").write_text(".agentic/\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "gitignore"], cwd=tmp_git_repo, check=True)
    return tmp_git_repo


def _untracked_snapshot(proj: Path) -> list[str]:
    f = paths.context_dir(proj) / f"BASELINE-{TODO}.untracked"
    if not f.is_file():
        return []
    return [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]


class TestRemoveDispatchedUnit:
    """(a) the dispatch .ready is an artifact: a never-started unit is
    removable, with the trace and the baseline cleanup."""

    def test_dispatched_ready_is_artifact_removed(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        api.dispatch_todo(proj, "# task\nbody\n", todo_id=TODO)
        inbox = paths.inbox(proj)
        context = paths.context_dir(proj)
        baseline_before = sorted(p.name for p in context.glob(f"BASELINE-{TODO}.*"))
        assert baseline_before, "dispatch must leave BASELINE files"

        result = api.remove_todo(proj, TODO)

        assert not (inbox / f"{TODO}.md").exists()
        assert not (inbox / f"{TODO}.ready").exists()
        # trace: md renamed to removed-<ts>.md, .ready moved beside it
        done_dir = paths.done_dir(proj) / TODO
        traces = list(done_dir.glob("removed-*.md"))
        assert len(traces) == 1
        assert "# task\nbody\n" in traces[0].read_text(encoding="utf-8")
        assert (done_dir / f"{TODO}.ready").is_file()
        assert result.trace_path
        # baseline deleted — the unit no longer exists
        assert not list(context.glob(f"BASELINE-{TODO}.*"))
        # the answer names exactly the deleted baseline files (project-relative)
        assert result.removed_files == sorted(
            f".agentic/context/{name}" for name in baseline_before
        )

    def test_remove_without_ready_still_works(self, tmp_git_repo: Path):
        """Regression: the pre-B-f2 shape (.md only, never armed) is still
        removed the same way."""
        proj = _project(tmp_git_repo)
        (paths.inbox(proj) / f"{TODO}.md").write_text("never armed")

        result = api.remove_todo(proj, TODO)

        assert result.removed_files == []
        assert not list(paths.context_dir(proj).glob(f"BASELINE-{TODO}.*"))


class TestRemoveStillRefusesStarted:
    """(b) a unit that was started is not removable — same refusals as
    before, and nothing is moved."""

    def _dispatched(self, proj: Path) -> None:
        api.dispatch_todo(proj, "# task\nbody\n", todo_id=TODO)

    def test_refused_with_progress(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        self._dispatched(proj)
        (paths.outbox(proj) / f"PROGRESS-{TODO}.md").write_text("half done")

        with pytest.raises(api.AwfApiError, match="signals"):
            api.remove_todo(proj, TODO)

        inbox = paths.inbox(proj)
        assert (inbox / f"{TODO}.md").is_file()
        assert (inbox / f"{TODO}.ready").is_file()
        assert list(paths.context_dir(proj).glob(f"BASELINE-{TODO}.*"))

    def test_refused_with_blocked_signal(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        self._dispatched(proj)
        (paths.outbox(proj) / f"BLOCKED-{TODO}.ready").touch()

        with pytest.raises(api.AwfApiError, match="awf unblock"):
            api.remove_todo(proj, TODO)

    def test_refused_with_done_closure(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        self._dispatched(proj)
        (paths.outbox(proj) / f"DONE-{TODO}.ready").touch()

        with pytest.raises(api.AwfApiError, match="restore"):
            api.remove_todo(proj, TODO)

    def test_refused_with_inbox_ack(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        self._dispatched(proj)
        (paths.inbox(proj) / f"ACK-{TODO}.ready").touch()

        with pytest.raises(api.AwfApiError, match="signals"):
            api.remove_todo(proj, TODO)


class TestUpdateIncludeUntracked:
    """(c) include_untracked on a not-started unit: validation like
    dispatch, BASELINE recomputed, no side effects on refusal."""

    def _dispatched_with_notes(self, proj: Path) -> None:
        (proj / "notes.txt").write_text("keep me\n")
        api.dispatch_todo(proj, "# task\nbody\n", todo_id=TODO)
        assert "notes.txt" in _untracked_snapshot(proj)

    def test_include_reclaims_pre_existing_file(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        self._dispatched_with_notes(proj)
        inbox = paths.inbox(proj)

        result = api.update_todo(proj, TODO, include_untracked=["notes.txt"])

        # the file left the exclusion snapshot and entered the include trace
        assert "notes.txt" not in _untracked_snapshot(proj)
        assert include_untracked.read_include_list(proj, TODO) == ["notes.txt"]
        # content-only side effects absent: no content rewrite, no backup
        assert (inbox / f"{TODO}.md").read_text(encoding="utf-8") == "# task\nbody\n"
        assert not list(paths.context_dir(proj).glob(f"{TODO}.md.bak-*"))
        # .ready and the rest of the baseline stay
        assert (inbox / f"{TODO}.ready").is_file()
        assert (paths.context_dir(proj) / f"BASELINE-{TODO}.sha").is_file()
        assert result.todo_id == TODO

    def test_invalid_path_refused_no_side_effects(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        self._dispatched_with_notes(proj)
        before = _untracked_snapshot(proj)

        with pytest.raises(api.AwfApiError, match="no such file"):
            api.update_todo(proj, TODO, include_untracked=["ghost.txt"])

        assert _untracked_snapshot(proj) == before
        assert include_untracked.read_include_list(proj, TODO) is None
        assert (paths.inbox(proj) / f"{TODO}.md").read_text(encoding="utf-8") == (
            "# task\nbody\n"
        )

    def test_tracked_path_refused(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        self._dispatched_with_notes(proj)

        with pytest.raises(api.AwfApiError, match="already tracked"):
            api.update_todo(proj, TODO, include_untracked=["README.md"])

        assert include_untracked.read_include_list(proj, TODO) is None
        assert "notes.txt" in _untracked_snapshot(proj)

    def test_gitignored_path_refused(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        self._dispatched_with_notes(proj)
        (proj / ".gitignore").write_text(".agentic/\nignored.txt\n")
        (proj / "ignored.txt").write_text("hidden\n")

        with pytest.raises(api.AwfApiError, match="gitignored"):
            api.update_todo(proj, TODO, include_untracked=["ignored.txt"])

        assert include_untracked.read_include_list(proj, TODO) is None
        assert "notes.txt" in _untracked_snapshot(proj)

    def test_empty_list_clears_include(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        (proj / "notes.txt").write_text("keep me\n")
        api.dispatch_todo(
            proj, "# task\nbody\n", todo_id=TODO, include_untracked=["notes.txt"]
        )
        assert "notes.txt" not in _untracked_snapshot(proj)
        assert include_untracked.read_include_list(proj, TODO) == ["notes.txt"]

        api.update_todo(proj, TODO, include_untracked=[])

        # the file is back in the exclusion snapshot, the trace is gone
        assert "notes.txt" in _untracked_snapshot(proj)
        assert not (
            paths.context_dir(proj) / f"BASELINE-{TODO}.include"
        ).exists()

    def test_started_unit_refused_include_refused(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        self._dispatched_with_notes(proj)
        (paths.outbox(proj) / f"PROGRESS-{TODO}.md").write_text("half done")

        with pytest.raises(api.AwfApiError, match="in flight"):
            api.update_todo(proj, TODO, include_untracked=["notes.txt"])

        assert "notes.txt" in _untracked_snapshot(proj)
        assert include_untracked.read_include_list(proj, TODO) is None

    def test_content_and_include_together(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        self._dispatched_with_notes(proj)

        result = api.update_todo(
            proj, TODO, "# task v2\nnew body\n", include_untracked=["notes.txt"]
        )

        assert (paths.inbox(proj) / f"{TODO}.md").read_text(encoding="utf-8") == (
            "# task v2\nnew body\n"
        )
        backup = proj / result.backup
        assert backup.read_text(encoding="utf-8") == "# task\nbody\n"
        assert "notes.txt" not in _untracked_snapshot(proj)
        assert include_untracked.read_include_list(proj, TODO) == ["notes.txt"]

    def test_nothing_to_update_is_refused(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        api.dispatch_todo(proj, "# task\nbody\n", todo_id=TODO)

        with pytest.raises(api.AwfApiError, match="nothing to update"):
            api.update_todo(proj, TODO)
