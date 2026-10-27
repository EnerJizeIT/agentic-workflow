"""REVIEW 0281 residual (TODO-0282): три поверхности без managed-барьера.

Аудит 0281 нашёл ``rollback``/ACK/``record_violation``; ревизия того же
класса нашла ещё три писателя, которые создавали каталог и файл голыми
``mkdir``/``atomic_write_text``:

- ``reject_commit`` — ``outbox`` и ``REVIEW-<id>.md`` без preflight вовсе;
- ``create_baseline`` — ``context`` и до шести ``BASELINE-*`` файлов: preflight
  есть, но между ним и записью остаётся окно подмены;
- ``clear_stale_closures`` — trace-каталог ``context/unblock-*``.

Репро честные: первое — живой симлинк ``outbox``; два других — окно
«preflight → эффект» (preflight отключён, поверхность подменена ссылкой),
как в ``test_restore_signal_write_never_follows_swapped_leaf``.

QA-0282 добавил клетки той же таблицы, которые авторы не закрыли тестом:
лист ``REVIEW-<id>.md`` самого reject и ``done/<id>`` у двух уборщиков.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from awf import api
from awf.api._errors import AwfApiError

R = "TODO-0001"


def _archived(tmp_path: Path) -> Path:
    project = tmp_path / "residual-proj"
    for name in ("inbox", "outbox", "context", "done", "handoff"):
        (project / ".agentic" / name).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: Residual\n", encoding="utf-8"
    )
    return project


def _outside(tmp_path: Path) -> tuple[Path, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("keep me", encoding="utf-8")
    return outside, canary


def _swap_context(project: Path, outside: Path) -> None:
    context = project / ".agentic" / "context"
    if context.exists():
        shutil.rmtree(context)
    context.symlink_to(outside, target_is_directory=True)


def test_reject_refuses_symlinked_outbox(tmp_path):
    """``reject_commit`` не пишет REVIEW через симлинк outbox."""
    project = _archived(tmp_path)
    outside, canary = _outside(tmp_path)
    outbox = project / ".agentic" / "outbox"
    outbox.rmdir()
    outbox.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api.reject_commit(project, R, "fix the failing assertions")

    assert not (outside / f"REVIEW-{R}.md").exists()
    assert canary.read_text(encoding="utf-8") == "keep me"


def test_baseline_never_follows_swapped_context(tmp_git_repo, monkeypatch):
    """``create_baseline`` не пишет BASELINE-* в подменённый context."""
    from awf.api import pipeline as api_pipeline

    repo = tmp_git_repo
    api.init_project(repo, project_name="Baseline")
    outside, canary = _outside(repo.parent)
    _swap_context(repo, outside)
    monkeypatch.setattr(
        api_pipeline, "refuse_unsafe_surfaces", lambda *a, **k: None
    )

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api.create_baseline(repo, R, run_tests=False)

    assert not list(outside.glob("BASELINE-*"))
    assert canary.read_text(encoding="utf-8") == "keep me"


def test_clear_closures_never_follows_swapped_context(tmp_git_repo, monkeypatch):
    """``clear_stale_closures`` не создаёт trace-каталог наружу."""
    from awf.api import hygiene

    repo = tmp_git_repo
    api.init_project(repo, project_name="Trace")
    outbox = repo / ".agentic" / "outbox"
    blocked = outbox / f"BLOCKED-{R}.ready"
    blocked.write_text("", encoding="utf-8")
    outside, canary = _outside(repo.parent)
    _swap_context(repo, outside)
    monkeypatch.setattr(
        hygiene, "refuse_unsafe_surfaces", lambda *a, **k: None
    )

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        hygiene.clear_stale_closures(repo, R)

    assert not list(outside.glob("unblock-*"))
    assert blocked.is_file()
    assert canary.read_text(encoding="utf-8") == "keep me"

# --- QA-0282: лист reject и done-каталог уборщиков -------------------------


def test_reject_refuses_symlinked_review_leaf(tmp_path):
    """Лист ``REVIEW-<id>.md`` — managed-поверхность, не только каталог."""
    project = _archived(tmp_path)
    outside, canary = _outside(tmp_path)
    target = outside / "review-target.md"
    target.write_text("outside", encoding="utf-8")
    (project / ".agentic" / "outbox" / f"REVIEW-{R}.md").symlink_to(target)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api.reject_commit(project, R, "fix the failing assertions")

    assert target.read_text(encoding="utf-8") == "outside"
    assert canary.read_text(encoding="utf-8") == "keep me"


def test_remove_todo_never_creates_done_through_swapped_link(tmp_path, monkeypatch):
    """``done/<id>`` создаёт ``managed_mkdir_api`` — preflight здесь отключён.

    Штатный preflight проверяет ``done/<id>`` и будущий ``removed-*.md``
    раньше, поэтому тест отключает его и доказывает сам барьер записи:
    сырой ``mkdir`` создал бы каталог снаружи и увёл туда задачу.
    """
    from awf.api import hygiene

    project = _archived(tmp_path)
    (project / ".agentic" / "inbox" / f"{R}.md").write_text("# task\n", encoding="utf-8")
    outside, canary = _outside(tmp_path)
    done = project / ".agentic" / "done"
    shutil.rmtree(done)
    done.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(hygiene, "refuse_unsafe_surfaces", lambda *a, **k: None)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        hygiene.remove_todo(project, R)

    assert not (outside / R).exists()
    assert (project / ".agentic" / "inbox" / f"{R}.md").is_file()
    assert canary.read_text(encoding="utf-8") == "keep me"


def test_retire_todo_never_creates_done_through_swapped_link(tmp_path, monkeypatch):
    """Тот же барьер у ``retire_todo``: ``done/<id>`` не создаётся наружу.

    Носитель заметки ``RETIRED-<ts>.md`` пишется своим no-follow писателем
    (REVIEW G3) и отказал бы уже после ``move``; здесь он заменён no-op,
    чтобы красная фаза падала на отсутствующем ``mkdir``, а не на соседе.
    """
    from awf.api import hygiene

    project = _archived(tmp_path)
    (project / ".agentic" / "inbox" / f"{R}.md").write_text("# task\n", encoding="utf-8")
    outside, canary = _outside(tmp_path)
    done = project / ".agentic" / "done"
    shutil.rmtree(done)
    done.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(hygiene, "refuse_unsafe_surfaces", lambda *a, **k: None)
    monkeypatch.setattr(hygiene.managed_fs, "managed_write_text", lambda *a, **k: None)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        hygiene.retire_todo(project, R, "not needed")

    assert not (outside / "TODO.md").exists()
    assert not list(outside.glob("RETIRED-*"))
    assert (project / ".agentic" / "inbox" / f"{R}.md").is_file()
    assert canary.read_text(encoding="utf-8") == "keep me"


def test_reject_refuses_non_posix_before_any_write(tmp_path, monkeypatch):
    """Новая политика reject: без POSIX отказ до записи (архитектор F7).

    До юнита reject работал на любой платформе: у него не было ни одного
    барьера. Теперь он идёт через ``managed_mkdir_api`` и отказывает так
    же, как archive/restore/rollback, а не молча теряет no-follow.
    """
    from awf.infra import managed_fs

    project = _archived(tmp_path)
    monkeypatch.setattr(managed_fs, "is_posix", lambda: False)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api.reject_commit(project, R, "fix the failing assertions")

    assert not (project / ".agentic" / "outbox" / f"REVIEW-{R}.md").exists()
