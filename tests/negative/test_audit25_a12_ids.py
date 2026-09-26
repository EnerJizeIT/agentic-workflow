"""A-12 (audit 2026-09-25, слой 2): коллизии TODO ID не стирают активный TODO и историю.

До: ``dispatch_todo`` резервировал ID только по отсутствию файла в ``inbox``
— явный ID в ``done/`` не проверялся. ``archive_todo`` переносил файлы через
``shutil.move`` в существующий ``done/<id>/`` — на POSIX существующий файл
назначения заменяется. ``restore_todo`` переносил архивный TODO.md в
``inbox/<id>.md`` без проверки занятости адреса. Повторный ID стирал
текущий TODO и архивную историю.

Findings covered:
- test_dispatch_refuses_id_from_done — явная выдача архивного ID отказана
  (AwfApiError), в inbox и baseline ничего не появилось
- test_restore_does_not_overwrite_active_todo — restore при занятом inbox
  отказан; активный TODO и архивная копия побайтово целы
- test_archive_into_occupied_done_preserves_both — архивация юнита, чей ID
  занят чужим артефактом в done/<id>/, — версионирование (REVIEW F1):
  чужая копия уходит под суффикс, новый отчёт занимает каноническое
  имя, ничего не перезаписывается и не отказывается
- test_archive_identical_content_is_idempotent — повторная архивация того
  же юнита (одинаковые байты) остаётся идемпотентной
- test_restore_rerun_rearchive_cycle_completes — цикл restore → повторный
  прогон → повторная архивация завершается без исключения; оба DONE
  отчёта сохранены (новый — канонический, старый — под суффиксом)
- test_archive_two_different_sources_both_preserved — (QA, RED на
  текущем дереве) два разных outbox-источника на один слот (canonical +
  legacy форма): вытесненный источник обязан уйти под суффикс, а не
  занять каноническое имя и быть перезаписанным победителем
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api, paths
from awf.todos import archive_todo


def _seed_archive(proj: Path, todo_id: str, body: bytes) -> Path:
    """done/<id>/ with one artifact; returns the artifact path."""
    archived = paths.done_dir(proj) / todo_id
    archived.mkdir(parents=True, exist_ok=True)
    (archived / "DONE.md").write_bytes(body)
    return archived


def test_dispatch_refuses_id_from_done(tmp_git_repo):
    """A-12.1: явный dispatch архивного ID — отказ до создания файлов."""
    api.init_project(tmp_git_repo, project_name="A12Ids")
    foreign = b"archived history - do not lose\n"
    _seed_archive(tmp_git_repo, "TODO-0001", foreign)
    inbox = paths.inbox(tmp_git_repo)

    with pytest.raises(api.AwfApiError, match="done/TODO-0001"):
        api.dispatch_todo(
            tmp_git_repo, "# TODO-0001\nre-issued\n", todo_id="TODO-0001"
        )

    # nothing in inbox, no baseline, archive untouched
    assert not list(inbox.glob("TODO-0001*")), "refused dispatch left files in inbox"
    assert not (paths.context_dir(tmp_git_repo) / "BASELINE-TODO-0001.sha").exists()
    assert (paths.done_dir(tmp_git_repo) / "TODO-0001" / "DONE.md").read_bytes() == foreign


def test_restore_does_not_overwrite_active_todo(tmp_git_repo):
    """A-12.2: restore при занятом inbox/<id>.md — отказ, оба файла целы."""
    api.init_project(tmp_git_repo, project_name="A12Restore")
    inbox = paths.inbox(tmp_git_repo)
    active = b"# active task - do not lose\n"
    archive_copy = b"# archived copy\n"
    (inbox / "TODO-0001.md").write_bytes(active)
    (inbox / "TODO-0001.ready").touch()
    archived = paths.done_dir(tmp_git_repo) / "TODO-0001"
    archived.mkdir(parents=True)
    (archived / "TODO.md").write_bytes(archive_copy)

    with pytest.raises(api.AwfApiError, match="already exists"):
        api.restore_todo(tmp_git_repo, "TODO-0001")

    assert (inbox / "TODO-0001.md").read_bytes() == active
    assert (archived / "TODO.md").read_bytes() == archive_copy


def test_archive_into_occupied_done_preserves_both(tmp_git_repo):
    """A-12.3 / F1: чужой DONE.md в done/<id>/ — версионирование, не отказ.

    Архивация завершается: чужая копия сохраняется под уникальным
    суффиксом, новый отчёт занимает каноническое имя — перезаписи нет.
    """
    api.init_project(tmp_git_repo, project_name="A12Archive")
    inbox = paths.inbox(tmp_git_repo)
    outbox = paths.outbox(tmp_git_repo)
    foreign = b"# foreign history - do not lose\n"
    _seed_archive(tmp_git_repo, "TODO-0001", foreign)
    unit = b"# this unit's done report\n"
    (inbox / "TODO-0001.md").write_bytes(b"# task body\n")
    (outbox / "DONE-TODO-0001.md").write_bytes(unit)

    result = archive_todo(tmp_git_repo, "TODO-0001")

    assert result is not None
    done = paths.done_dir(tmp_git_repo) / "TODO-0001"
    # new report — the canonical name; foreign copy — preserved, suffixed
    assert (done / "DONE.md").read_bytes() == unit
    suffixed = sorted(p for p in done.glob("DONE-*.md") if p.name != "DONE.md")
    assert len(suffixed) == 1, f"expected exactly one suffixed copy, got {suffixed}"
    assert suffixed[0].read_bytes() == foreign
    # sources consumed
    assert not (outbox / "DONE-TODO-0001.md").exists()
    assert not (inbox / "TODO-0001.md").exists()


def test_archive_identical_content_is_idempotent(tmp_git_repo):
    """A-12.3 (идемпотентная ветка): одинаковые байты — не конфликт.

    Повторная архивация того же юнита: архивная копия остаётся, источник
    потребляется, ошибка нет.
    """
    api.init_project(tmp_git_repo, project_name="A12Idem")
    inbox = paths.inbox(tmp_git_repo)
    outbox = paths.outbox(tmp_git_repo)
    same = b"# done report - identical\n"
    _seed_archive(tmp_git_repo, "TODO-0001", same)
    (inbox / "TODO-0001.md").write_bytes(b"# task body\n")
    (outbox / "DONE-TODO-0001.md").write_bytes(same)

    result = archive_todo(tmp_git_repo, "TODO-0001")

    assert result is not None
    assert (paths.done_dir(tmp_git_repo) / "TODO-0001" / "DONE.md").read_bytes() == same
    assert not (outbox / "DONE-TODO-0001.md").exists()
    assert not (inbox / "TODO-0001.md").exists()


def test_restore_rerun_rearchive_cycle_completes(tmp_git_repo):
    """A-12.4 / F1: цикл restore → повторный прогон → повторная архивация.

    restore оставляет старый DONE в done/<id>/ (история); повторный прогон
    пишет новый DONE. Повторная архивация обязана пройти без исключения
    (раньше: отказ или тихая перезапись) и сохранить оба отчёта.
    """
    api.init_project(tmp_git_repo, project_name="A12Cycle")
    inbox = paths.inbox(tmp_git_repo)
    outbox = paths.outbox(tmp_git_repo)
    old = b"# first run done report\n"
    archived = paths.done_dir(tmp_git_repo) / "TODO-0001"
    archived.mkdir(parents=True)
    (archived / "TODO.md").write_bytes(b"# task body\n")
    (archived / "DONE.md").write_bytes(old)

    api.restore_todo(tmp_git_repo, "TODO-0001")
    assert (inbox / "TODO-0001.md").is_file()
    assert (inbox / "TODO-0001.ready").is_file()

    # re-run: a fresh DONE report for the same id
    new = b"# second run done report\n"
    (outbox / "DONE-TODO-0001.md").write_bytes(new)

    result = archive_todo(tmp_git_repo, "TODO-0001")

    assert result is not None
    done = paths.done_dir(tmp_git_repo) / "TODO-0001"
    # both reports preserved: new — canonical, old — suffixed
    assert (done / "DONE.md").read_bytes() == new
    suffixed = sorted(p.name for p in done.glob("DONE-*.md") if p.name != "DONE.md")
    assert suffixed == ["DONE-1.md"], suffixed
    assert (done / "DONE-1.md").read_bytes() == old
    # the cycle consumed everything
    assert not (inbox / "TODO-0001.md").exists()
    assert not (outbox / "DONE-TODO-0001.md").exists()


def test_archive_two_different_sources_both_preserved(tmp_git_repo):
    """A-12.5 (QA finding, RED): два разных источника на один слот.

    canonical ``DONE-TODO-NNNN.md`` + legacy ``DONE-NNNN.md`` с разным
    содержимым в outbox'е. Инвариант «archive никогда не
    перезаписывает»: победитель (первый источник) занимает каноническое
    имя, вытесненный источник уходит под уникальный суффикс. На текущем
    дереве (awf/todos.py, цикл verсионирования) вытесненный файл получает
    освободившееся каноническое имя — ``_unique_name`` считает суффикс
    от уже пустого адреса — и победитель тихо перезаписывает его:
    история теряется.
    """
    api.init_project(tmp_git_repo, project_name="A12TwoSrc")
    inbox = paths.inbox(tmp_git_repo)
    outbox = paths.outbox(tmp_git_repo)
    canonical = b"# canonical report\n"
    legacy = b"# legacy report\n"
    (inbox / "TODO-0001.md").write_bytes(b"# task body\n")
    (outbox / "DONE-TODO-0001.md").write_bytes(canonical)
    (outbox / "DONE-0001.md").write_bytes(legacy)

    result = archive_todo(tmp_git_repo, "TODO-0001")

    assert result is not None
    done = paths.done_dir(tmp_git_repo) / "TODO-0001"
    # winner takes the canonical name
    assert (done / "DONE.md").read_bytes() == canonical
    # the displaced legacy source must survive under a unique suffix
    suffixed = sorted(p for p in done.glob("DONE-*.md") if p.name != "DONE.md")
    assert len(suffixed) == 1, f"expected exactly one suffixed copy, got {suffixed}"
    assert suffixed[0].read_bytes() == legacy
    # nothing lost: both reports survive in the archive
    assert {p.read_bytes() for p in done.glob("DONE*.md")} == {canonical, legacy}
    # sources consumed
    assert not (outbox / "DONE-TODO-0001.md").exists()
    assert not (outbox / "DONE-0001.md").exists()
