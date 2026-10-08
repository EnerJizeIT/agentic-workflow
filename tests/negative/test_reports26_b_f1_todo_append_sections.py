"""REPORTS26 B-f1 (TODO-0162): ``awf_todo_update`` — append и section_updates.

Source: awf-feature-20261007-awf-todo-update-tolko-polnaya-zamena-kontenta-net-append.md.
Супервизор дополняет нестартовавшие TODO находками аудиторов по несколько
раз за забег; до — только полная замена 40-80-строчного контента.

Решение владельца: частичные правки, идемпотентные, сохраняющие
номер/``.ready``/baseline — ``append`` (блок в конец) и ``section_updates``
(замена тела ``## <заголовок>`` секции, отсутствующий заголовок — добавление).

Инварианты (TODO-0162):
(a) append → блок в конце через пустую строку, прежний текст цел,
    номер/``.ready``/baseline не тронуты, бэкап прежнего контента;
(b) section_updates по существующему заголовку → тело заменено, соседние
    секции целы; по отсутствующему → секция добавлена в конец;
(c) два контент-режима сразу / пустой append / пустая карта → отказ без
    side-effect'ов (файл цел, бэкапов нет);
(d) повторный идентичный ``section_updates`` → файл байт-в-байт тот же,
    бэкапы сохраняются (по одному на успешный вызов).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import api, paths

TODO = "TODO-0001"

DOC = (
    "# TODO — задача\n"
    "\n"
    "## Решение\n"
    "старое тело A\n"
    "\n"
    "## Границы\n"
    "старое тело B\n"
)


def _project(tmp_git_repo: Path) -> Path:
    """Committed git repo + minimal .agentic (config.yaml — B2 guard);
    .agentic is gitignored so it never lands in the untracked snapshot."""
    for sub in ("inbox", "outbox", "context", "done", "logs"):
        (tmp_git_repo / ".agentic" / sub).mkdir(parents=True)
    (tmp_git_repo / ".agentic" / "config.yaml").write_text("project:\n  name: F1\n")
    (tmp_git_repo / ".gitignore").write_text(".agentic/\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "gitignore"], cwd=tmp_git_repo, check=True)
    return tmp_git_repo


def _dispatched(proj: Path) -> Path:
    api.dispatch_todo(proj, DOC, todo_id=TODO)
    inbox = paths.inbox(proj)
    assert (inbox / f"{TODO}.md").is_file()
    assert (inbox / f"{TODO}.ready").is_file()
    return inbox


def _backups(proj: Path) -> list[Path]:
    return sorted(paths.context_dir(proj).glob(f"{TODO}.md.bak-*"))


class TestAppend:
    """(a) append — the block lands at the end, the previous text and the
    dispatch shape (number/.ready/baseline) are untouched."""

    def test_append_adds_block_at_end(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)
        sha = paths.context_dir(proj) / f"BASELINE-{TODO}.sha"
        sha_before = sha.read_text(encoding="utf-8")

        result = api.update_todo(
            proj, TODO, append="## Доп. находки\n\n- аудит: уточнить границы"
        )

        text = (inbox / f"{TODO}.md").read_text(encoding="utf-8")
        # the old text is intact, the block follows after one blank line
        assert text.startswith(DOC.rstrip("\n") + "\n\n")
        assert text == DOC.rstrip("\n") + "\n\n## Доп. находки\n\n- аудит: уточнить границы\n"
        # number/.ready/baseline kept
        assert (inbox / f"{TODO}.ready").is_file()
        assert sha.read_text(encoding="utf-8") == sha_before
        # backup of the previous content
        assert result.backup
        backup = proj / result.backup
        assert backup.read_text(encoding="utf-8") == DOC

    def test_second_append_accumulates(self, tmp_git_repo: Path):
        """Append is NOT idempotent by design — each call adds a block
        (only section_updates is the idempotent mode)."""
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        api.update_todo(proj, TODO, append="finding one")
        api.update_todo(proj, TODO, append="finding two")

        text = (inbox / f"{TODO}.md").read_text(encoding="utf-8")
        assert "finding one" in text
        assert "finding two" in text
        assert text.index("finding one") < text.index("finding two")
        assert len(_backups(proj)) == 2


class TestSectionUpdates:
    """(b) section_updates — replace the body of an existing ``##``
    section, append the section when the heading is absent."""

    def test_replaces_body_keeps_neighbors(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        result = api.update_todo(proj, TODO, section_updates={"Решение": "новое тело A"})

        text = (inbox / f"{TODO}.md").read_text(encoding="utf-8")
        assert text == (
            "# TODO — задача\n"
            "\n"
            "## Решение\n"
            "новое тело A\n"
            "\n"
            "## Границы\n"
            "старое тело B\n"
        )
        assert result.backup
        assert (proj / result.backup).read_text(encoding="utf-8") == DOC

    def test_absent_heading_appended_at_end(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        api.update_todo(proj, TODO, section_updates={"Приёмка": "критерии"})

        text = (inbox / f"{TODO}.md").read_text(encoding="utf-8")
        # the old text stays a prefix, the section lands at the end
        assert text.startswith(DOC.rstrip("\n") + "\n\n")
        assert text.endswith("## Приёмка\nкритерии\n")
        assert "старое тело B" in text

    def test_multi_section_single_call(self, tmp_git_repo: Path):
        """One call may touch several sections — replaced and added in
        the same map."""
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        api.update_todo(
            proj,
            TODO,
            section_updates={"Границы": "новые границы", "План": "шаги"},
        )

        text = (inbox / f"{TODO}.md").read_text(encoding="utf-8")
        assert "## Границы\nновые границы\n" in text
        assert "## План\nшаги\n" in text
        assert "старое тело A" in text


class TestRefusals:
    """(c) exactly one content mode per call, empty inputs refused —
    nothing is written on refusal (file intact, no backups)."""

    def _intact(self, proj: Path, inbox: Path) -> None:
        assert (inbox / f"{TODO}.md").read_text(encoding="utf-8") == DOC
        assert not _backups(proj)

    def test_content_and_append_refused(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        with pytest.raises(api.AwfApiError, match="exactly one of"):
            api.update_todo(proj, TODO, "new content", append="extra")

        self._intact(proj, inbox)

    def test_content_and_sections_refused(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        with pytest.raises(api.AwfApiError, match="exactly one of"):
            api.update_todo(proj, TODO, "new content", section_updates={"Решение": "x"})

        self._intact(proj, inbox)

    def test_append_and_sections_refused(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        with pytest.raises(api.AwfApiError, match="exactly one of"):
            api.update_todo(proj, TODO, append="extra", section_updates={"Решение": "x"})

        self._intact(proj, inbox)

    def test_empty_append_refused(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        with pytest.raises(api.AwfApiError, match="append is empty"):
            api.update_todo(proj, TODO, append="   \n")

        self._intact(proj, inbox)

    def test_empty_section_map_refused(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        with pytest.raises(api.AwfApiError, match="empty map"):
            api.update_todo(proj, TODO, section_updates={})

        self._intact(proj, inbox)

    def test_empty_heading_key_refused(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        with pytest.raises(api.AwfApiError, match="empty heading"):
            api.update_todo(proj, TODO, section_updates={"  ": "x"})

        self._intact(proj, inbox)

    def test_nothing_to_update_still_refused(self, tmp_git_repo: Path):
        """Regression: the pre-B-f1 refusal (no content mode, no include)
        keeps its message."""
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        with pytest.raises(api.AwfApiError, match="nothing to update"):
            api.update_todo(proj, TODO)

        self._intact(proj, inbox)


class TestIdempotency:
    """(d) a repeated identical section_updates is a no-op for the file
    (byte-identical), while every successful call still writes its
    backup."""

    def test_identical_call_is_byte_identical(self, tmp_git_repo: Path):
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        first = api.update_todo(proj, TODO, section_updates={"Решение": "финал"})
        after_first = (inbox / f"{TODO}.md").read_bytes()
        second = api.update_todo(proj, TODO, section_updates={"Решение": "финал"})

        assert (inbox / f"{TODO}.md").read_bytes() == after_first
        # every successful call writes its own backup; the second one
        # holds the content as it was before the second call
        backups = _backups(proj)
        assert len(backups) == 2
        assert backups[0].read_text(encoding="utf-8") == DOC
        assert backups[1].read_text(encoding="utf-8") == after_first.decode("utf-8")
        assert first.backup != second.backup

    def test_identical_absent_heading_is_byte_identical(self, tmp_git_repo: Path):
        """Replace-or-append: the first call appends the section, the
        second finds it and rewrites the same body — same bytes."""
        proj = _project(tmp_git_repo)
        inbox = _dispatched(proj)

        api.update_todo(proj, TODO, section_updates={"Приёмка": "критерии"})
        after_first = (inbox / f"{TODO}.md").read_bytes()
        api.update_todo(proj, TODO, section_updates={"Приёмка": "критерии"})

        assert (inbox / f"{TODO}.md").read_bytes() == after_first
