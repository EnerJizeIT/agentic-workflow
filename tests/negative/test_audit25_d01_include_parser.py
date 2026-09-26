"""D-01/R-07 (аудит 2026-09-25, слои 10/11): один парсер ``.include``.

Дефект: ``run_next`` (awf/api/run.py) сам разбирал ``BASELINE-<id>.include``,
пока ``read_include_list`` (awf/include_untracked.py) реализовала тот же
разбор и оставалась не вызванной — два парсера одного формата.

Инварианты:
1. Формат ``.include`` читается одним местом: ``run_next`` использует
   ``read_include_list``; ручной разбор удалён.
2. Поведение include/exclude не меняется: единственное расхождение
   (пустой файл → ``[]`` у helper против ``None`` у legacy-разбора) —
   no-op в ``create_baseline`` (``set(include or ())``); паритет закреплён.
3. Публичное имя ``read_include_list`` сохраняется.

Findings covered:
- test_helper_and_legacy_parse_agree — паритет: пустые строки, строки-
  «комментарии» (разбор не выделяет комментарии), несуществующие пути,
  пустой файл, отсутствующий файл — поведение legacy-разбора и helper
   эквивалентно (множество путей, None при отсутствии файла)
- test_helper_missing_file_returns_none — файла нет → None
- test_helper_does_not_validate_paths — читатель не валидатор: пути из
  файла возвращаются как есть, существование не проверяется
- test_run_next_reads_include_via_helper — monkeypatch-счётчик: ``run_next``
  вызывает ``read_include_list`` для запускаемого TODO
- test_include_survives_run_next_rebaseline — поведение: включённый файл
  остаётся исключённым из untracked-снапшота после re-baseline в run_next
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import awf.api.pipeline as api_pipeline
from awf import api, include_untracked


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="D01Test")
    return tmp_git_repo


def _include_file(proj: Path, todo_id: str = "TODO-0001") -> Path:
    f = include_untracked.include_file_path(proj, todo_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    return f


def _legacy_parse(path: Path) -> set[str] | None:
    """The pre-D-01 manual parse from run_next (now removed): a set of
    stripped non-empty lines, or None when the file is missing/empty."""
    if not path.is_file():
        return None
    lines = {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()}
    return lines or None


def _fake_start(monkeypatch, proj: Path) -> None:
    def fake_bg(project_dir, **kw):
        return 424242, proj / ".agentic" / "logs" / "awf-start.out", None

    monkeypatch.setattr(api_pipeline, "start_in_background", fake_bg)
    monkeypatch.setattr(api_pipeline, "_verify_child_alive", lambda *a, **kw: True)


class TestHelperParseParity:
    """read_include_list is the single .include parser — pin what it does
    and that it is behavior-equivalent to the removed manual parse."""

    def test_helper_and_legacy_parse_agree(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        f = _include_file(proj)
        # empty lines, whitespace-only lines, a comment-looking line, and
        # two real paths (deliberately unsorted)
        f.write_text(
            "\n   \nx.md\n# note is not a comment\n\ny.md\n", encoding="utf-8"
        )
        got = include_untracked.read_include_list(proj, "TODO-0001")
        want = _legacy_parse(f)
        assert want is not None
        assert got is not None and set(got) == want, (
            "D-01: helper and the removed manual parse disagree on the "
            f"same file content: {got!r} vs {want!r}"
        )
        assert got == sorted(got), "helper returns the sorted list"

    def test_helper_missing_file_returns_none(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        assert not _include_file(proj).exists()
        assert include_untracked.read_include_list(proj, "TODO-0001") is None

    def test_helper_empty_file_is_empty_not_none(self, tmp_git_repo: Path) -> None:
        """The one visible difference from the legacy parse: an existing
        whitespace-only file yields [] (the legacy parse gave None).
        create_baseline treats both identically — set(include or ()) — so
        the behavior is unchanged; the pin keeps that contract honest."""
        proj = _project(tmp_git_repo)
        _include_file(proj).write_text("\n   \n", encoding="utf-8")
        assert include_untracked.read_include_list(proj, "TODO-0001") == []

    def test_helper_does_not_validate_paths(self, tmp_git_repo: Path) -> None:
        """The reader is not the validator (resolve_include_untracked is):
        paths are returned verbatim, no existence check."""
        proj = _project(tmp_git_repo)
        _include_file(proj).write_text("ghost.md\nsrc/ghost2.py\n", encoding="utf-8")
        assert include_untracked.read_include_list(proj, "TODO-0001") == [
            "ghost.md",
            "src/ghost2.py",
        ]


class TestRunNextUsesHelper:
    """Wiring + behavior: run_next reads the .include link through
    read_include_list, and the inclusion still survives the re-baseline."""

    def _setup_include(self, proj: Path) -> None:
        (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
        subprocess.run(["git", "commit", "-qm", "gitignore"], cwd=proj, check=True)
        # two pre-existing untracked files: x.md is re-claimed, y.md is not
        (proj / "x.md").write_text("include me\n", encoding="utf-8")
        (proj / "y.md").write_text("leave me out\n", encoding="utf-8")
        api.dispatch_todo(
            proj,
            "# TODO-0001\ninclude\n",
            todo_id="TODO-0001",
            include_untracked=["x.md"],
        )

    def test_run_next_reads_include_via_helper(
        self, tmp_git_repo: Path, monkeypatch
    ) -> None:
        proj = _project(tmp_git_repo)
        self._setup_include(proj)

        calls: list[str] = []
        real = include_untracked.read_include_list

        def counting(project_dir, todo_id):
            calls.append(todo_id)
            return real(project_dir, todo_id)

        monkeypatch.setattr(include_untracked, "read_include_list", counting)
        api.run_start(proj, queue=["TODO-0001"])
        _fake_start(monkeypatch, proj)
        result = api.run_next(proj)

        assert result.action == "started"
        assert "TODO-0001" in calls, (
            "D-01: run_next no longer reads the .include link through "
            "read_include_list — the duplicate parse is back"
        )

    def test_include_survives_run_next_rebaseline(
        self, tmp_git_repo: Path, monkeypatch
    ) -> None:
        proj = _project(tmp_git_repo)
        self._setup_include(proj)
        untracked_file = proj / ".agentic" / "context" / "BASELINE-TODO-0001.untracked"
        # dispatch already applied the inclusion
        assert "x.md" not in untracked_file.read_text(encoding="utf-8").splitlines()

        api.run_start(proj, queue=["TODO-0001"])
        _fake_start(monkeypatch, proj)
        result = api.run_next(proj)
        assert result.action == "started"

        lines = untracked_file.read_text(encoding="utf-8").splitlines()
        assert "x.md" not in lines, (
            "D-01: run_next re-baseline dropped the inclusion — the "
            "re-claimed file would fall out of the unit commit"
        )
        assert "y.md" in lines, "the non-included file stays listed"
