"""M3b (TODO-0233): соседние переносчики hygiene, GC записей, хвост managed_fs.

Один класс S9-01: симлинк managed-поверхности уводит ``move``/``unlink``
за пределы проекта. Барьер у каждого уборщика свой — no-follow preflight
по ПОВЕРХНОСТЯМ ОПЕРАЦИИ (что реально читается/создаётся/переносится,
включая конечный лист), не по союзу всех поверхностей (REVIEW G1), и
срабатывает ДО первого эффекта. GC записей проверяет ``state/records`` и
``records/<kind>`` до ``glob``+``unlink``; ``managed_write_text`` не
оставляет temp-файл при ошибке ``os.replace``. H1 (виток 4): новые
барьеры требуют POSIX — на слабой платформе отказ, а не молча ослабленная
проверка.

Все проекты — disposable в ``tmp_path``; canary лежат ВНЕ проекта.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

T = "TODO-0001"
SURFACES = ("inbox", "outbox", "done", "context")
# REVIEW G1: the barrier is per operation, not a union — each mover is
# parametrized over the surfaces IT touches (leaves excluded, covered by
# the (f)/(g) cases).
CLEAR_SURFACES = ("inbox", "outbox", "context")
RETIRE_SURFACES = ("inbox", "outbox", "done")


def _project(tmp_path: Path, *, started: bool = True) -> Path:
    project = tmp_path / "proj"
    for name in ("inbox", "outbox", "context", "logs", "done", "state"):
        (project / ".agentic" / name).mkdir(parents=True)
    (project / ".agentic" / "inbox" / f"{T}.md").write_text("# task\n", encoding="utf-8")
    (project / ".agentic" / "inbox" / f"{T}.ready").write_text("", encoding="utf-8")
    if started:
        (project / ".agentic" / "outbox" / f"BLOCKED-{T}.ready").write_text(
            "", encoding="utf-8"
        )
        (project / ".agentic" / "outbox" / f"PROGRESS-{T}.md").write_text(
            "step\n", encoding="utf-8"
        )
        (project / ".agentic" / "context" / f"BASELINE-{T}.sha").write_text(
            "sha\n", encoding="utf-8"
        )
    return project


def _replace_with_symlink(path: Path, target: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.symlink_to(target, target_is_directory=target.is_dir())


def _outside(tmp_path: Path) -> tuple[Path, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("keep me", encoding="utf-8")
    return outside, canary


def _link_surface(
    project: Path, surface: str, outside: Path, *, seed: bool = True
) -> None:
    path = project / ".agentic" / surface
    if surface == "outbox" and seed:
        # The mover must have work to do through the symlinked surface.
        (outside / f"BLOCKED-{T}.ready").write_text("", encoding="utf-8")
    if surface == "inbox":
        (outside / f"{T}.md").write_text("# task\n", encoding="utf-8")
    _replace_with_symlink(path, outside)


def _todo_md(project: Path, outside: Path, surface: str) -> Path:
    root = outside if surface == "inbox" else project / ".agentic" / "inbox"
    return root / f"{T}.md"


# ─── (a): три переносчика hygiene — барьер по каждой поверхности ─────────


@pytest.mark.parametrize("surface", CLEAR_SURFACES)
def test_clear_stale_closures_refuses_symlinked_surface(tmp_path, surface):
    from awf.api._errors import AwfApiError
    from awf.api.hygiene import clear_stale_closures

    project = _project(tmp_path)
    outside, canary = _outside(tmp_path)
    _link_surface(project, surface, outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        clear_stale_closures(project, T)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert _todo_md(project, outside, surface).is_file()
    if surface == "outbox":
        assert (outside / f"BLOCKED-{T}.ready").is_file()


@pytest.mark.parametrize("surface", SURFACES)
def test_remove_todo_refuses_symlinked_surface(tmp_path, surface):
    from awf.api import remove_todo
    from awf.api._errors import AwfApiError

    project = _project(tmp_path, started=False)
    outside, canary = _outside(tmp_path)
    _link_surface(project, surface, outside, seed=False)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        remove_todo(project, T)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert _todo_md(project, outside, surface).is_file()
    assert not (outside / T).exists()


@pytest.mark.parametrize("surface", RETIRE_SURFACES)
def test_retire_todo_refuses_symlinked_surface(tmp_path, surface):
    from awf.api import retire_todo
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside, canary = _outside(tmp_path)
    _link_surface(project, surface, outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        retire_todo(project, T, "no longer needed")

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert _todo_md(project, outside, surface).is_file()
    assert not (outside / T).exists()


# ─── (b): оба вызова clear_stale_closures — unblock и dispatch ───────────


def test_unblock_path_refuses_symlinked_outbox(tmp_path):
    """``unblock_todo`` → ``clear_stale_closures``: отказ, canary цел."""
    from awf.api import unblock_todo
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside, canary = _outside(tmp_path)
    _link_surface(project, "outbox", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        unblock_todo(project, T)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / f"BLOCKED-{T}.ready").is_file()
    # The trace dir is `context/unblock-<id>-<ts>`: "no effect" means it
    # never appeared INSIDE the project. `outside/"unblock"` can never exist
    # (the name carries the id and a timestamp) — that assertion was vacuous.
    assert not list((project / ".agentic" / "context").glob("unblock-*"))


def test_dispatch_path_refuses_symlinked_outbox(tmp_path):
    """``dispatch_todo`` → ``clear_stale_closures``: отказ, canary цел."""
    import subprocess

    from awf import api
    from awf.api._errors import AwfApiError

    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"], cwd=project, check=True
    )
    subprocess.run(["git", "config", "user.name", "tester"], cwd=project, check=True)
    api.init_project(
        project,
        project_name="M3b",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "awf init"], cwd=project, check=True)

    outside, canary = _outside(tmp_path)
    _link_surface(project, "outbox", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api.dispatch_todo(project, f"# {T}\nre-dispatch\n", todo_id=T)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / f"BLOCKED-{T}.ready").is_file()
    assert not list((project / ".agentic" / "context").glob("unblock-*"))


# ─── (c): GC записей — барьер на records/ до glob+unlink ─────────────────


def test_gc_records_refuses_symlinked_records_and_keeps_canary(tmp_path):
    from awf.infra import state_store
    from awf.infra.managed_fs import ManagedPathError
    from awf.infra.state_store._records import gc_records

    project = _project(tmp_path)
    state = project / ".agentic" / "state"
    state_store.control_path(project).write_text(
        json.dumps({"schema_version": 2, "revision": 0, "records": []}),
        encoding="utf-8",
    )

    outside, canary = _outside(tmp_path)
    (outside / "kind").mkdir()
    (outside / "kind" / "deadbeef.json").write_text("{}", encoding="utf-8")
    _replace_with_symlink(state / "records", outside)

    with pytest.raises(ManagedPathError):
        gc_records(project, dry_run=False)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / "kind" / "deadbeef.json").is_file()


def test_gc_records_refuses_symlinked_kind_dir_and_keeps_canary(tmp_path):
    """``records/<kind>`` — симлинк: glob уходит внутрь, unlink стирал бы чужое."""
    from awf.infra import state_store
    from awf.infra.managed_fs import ManagedPathError
    from awf.infra.state_store._records import gc_records

    project = _project(tmp_path)
    state = project / ".agentic" / "state"
    state_store.control_path(project).write_text(
        json.dumps({"schema_version": 2, "revision": 0, "records": []}),
        encoding="utf-8",
    )
    (state / "records").mkdir()

    outside, canary = _outside(tmp_path)
    (outside / "deadbeef.json").write_text("{}", encoding="utf-8")
    (state / "records" / "kind").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ManagedPathError):
        gc_records(project, dry_run=False)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / "deadbeef.json").is_file()


def test_gc_records_refusal_aborts_the_whole_run(tmp_path):
    """One unsafe kind dir must not let the GC half-run on the safe ones.

    The implementation collects the garbage first, verifies EVERY parent and
    only then unlinks — a refusal leaves both the safe kind dir and the
    outside canary untouched. A per-file "check, then unlink" loop would
    pass the two tests above yet delete ``good/junk.json`` before meeting
    the symlinked sibling.
    """
    from awf.infra import state_store
    from awf.infra.managed_fs import ManagedPathError
    from awf.infra.state_store._records import gc_records

    project = _project(tmp_path)
    state = project / ".agentic" / "state"
    state_store.control_path(project).write_text(
        json.dumps({"schema_version": 2, "revision": 0, "records": []}),
        encoding="utf-8",
    )
    records = state / "records"
    (records / "good").mkdir(parents=True)
    (records / "good" / "junk.json").write_text("{}", encoding="utf-8")

    outside, canary = _outside(tmp_path)
    (outside / "junk.json").write_text("{}", encoding="utf-8")
    (records / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ManagedPathError):
        gc_records(project, dry_run=False)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (records / "good" / "junk.json").is_file()
    assert (outside / "junk.json").is_file()


# ─── (d): managed_write_text — ошибка rename не оставляет temp ───────────


def test_managed_write_text_removes_temp_when_rename_fails(tmp_path):
    from awf.infra import managed_fs

    project = _project(tmp_path)
    state = project / ".agentic" / "state"
    target = state / "control.json"
    target.mkdir()  # rename файла на каталог → IsADirectoryError

    with pytest.raises(OSError):
        managed_fs.managed_write_text(target, "{}", root=project)

    leftovers = [p.name for p in state.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert target.is_dir()


# ─── (f): лист архива done/<id> — тот же класс S9-01 ────────────────────
#
# QA этого юнита: барьер проверяет КОРЕНЬ ``done``, но не лист
# ``done/<id>``, в который ``shutil.move`` кладёт файл. Симлинк листа
# уводит перемещение за пределы проекта — ровно та же дыра, что
# architect-review закрыл в GC для ``records/<kind>``. ``todos.archive_todo``
# (эталон прошлого юнита) проверяет ``dest = done/<id>`` целиком; общий
# хелпер hygiene — только ``done``.

def test_retire_todo_refuses_symlinked_done_leaf(tmp_path):
    from awf.api import retire_todo
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside, canary = _outside(tmp_path)
    (project / ".agentic" / "done" / T).symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        retire_todo(project, T, "no longer needed")

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (project / ".agentic" / "inbox" / f"{T}.md").is_file()
    assert not [p for p in outside.iterdir() if p.suffix == ".md"]


def test_remove_todo_refuses_symlinked_done_leaf(tmp_path):
    from awf.api import remove_todo
    from awf.api._errors import AwfApiError

    project = _project(tmp_path, started=False)
    outside, canary = _outside(tmp_path)
    (project / ".agentic" / "done" / T).symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        remove_todo(project, T)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (project / ".agentic" / "inbox" / f"{T}.md").is_file()
    assert not [p for p in outside.iterdir() if p.name.startswith("removed-")]


def test_clear_stale_closures_refuses_symlinked_trace_leaf(tmp_path, monkeypatch):
    """Trace-каталог ``context/unblock-<id>-<ts>`` — тоже место записи."""
    from awf.api import hygiene
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside, canary = _outside(tmp_path)
    monkeypatch.setattr(hygiene, "_timestamp", lambda: "20261020T000000000000Z")
    trace = (
        project / ".agentic" / "context" / f"unblock-{T}-20261020T000000000000Z"
    )
    trace.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        hygiene.clear_stale_closures(project, T)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (project / ".agentic" / "outbox" / f"BLOCKED-{T}.ready").is_file()
    assert not (outside / f"BLOCKED-{T}.ready").exists()


# ─── (g): барьер по поверхностям операции (REVIEW G1) ────────────────────

def test_clear_stale_closures_does_not_check_the_untouched_done_surface(tmp_path):
    """``clear_stale_closures`` не трогает ``done`` — симлинк там не отказ."""
    from awf.api.hygiene import clear_stale_closures

    project = _project(tmp_path)
    outside, canary = _outside(tmp_path)
    _replace_with_symlink(project / ".agentic" / "done", outside)

    moved, trace = clear_stale_closures(project, T)

    assert moved == [f"BLOCKED-{T}.ready"]
    assert trace is not None and trace.is_dir()
    assert canary.read_text(encoding="utf-8") == "keep me"
    assert not (outside / f"BLOCKED-{T}.ready").exists()


def test_dispatch_refuses_symlinked_done_before_any_write(tmp_path):
    """Симлинк ``done`` → отказ ДО резервации: ни файла задачи, ни BASELINE."""
    import subprocess

    from awf import api
    from awf.api._errors import AwfApiError

    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"], cwd=project, check=True
    )
    subprocess.run(["git", "config", "user.name", "tester"], cwd=project, check=True)
    api.init_project(
        project,
        project_name="M3b",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "awf init"], cwd=project, check=True)

    agentic = project / ".agentic"
    inbox_before = sorted(p.name for p in (agentic / "inbox").iterdir())
    context_before = sorted(p.name for p in (agentic / "context").iterdir())

    outside, canary = _outside(tmp_path)
    _replace_with_symlink(agentic / "done", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api.dispatch_todo(project, f"# {T}\nre-dispatch\n", todo_id=T)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert sorted(p.name for p in (agentic / "inbox").iterdir()) == inbox_before
    assert sorted(p.name for p in (agentic / "context").iterdir()) == context_before
    assert not list((agentic / "context").glob(f"BASELINE-{T}.*"))
    assert not (agentic / "inbox" / f"{T}.md").exists()


# ─── (h): «не найдено» раньше барьера (REVIEW G2) ─────────────────────────

def test_remove_todo_missing_id_reports_not_found_not_unsafe(tmp_path):
    from awf.api import remove_todo
    from awf.api._errors import AwfApiError

    project = _project(tmp_path, started=False)
    (project / ".agentic" / "inbox" / f"{T}.md").unlink()
    outside, _canary = _outside(tmp_path)
    _replace_with_symlink(project / ".agentic" / "outbox", outside)

    with pytest.raises(AwfApiError) as excinfo:
        remove_todo(project, T)

    message = str(excinfo.value)
    assert "not found in inbox" in message
    assert "unsafe managed path" not in message


def test_retire_todo_missing_id_reports_not_found_not_unsafe(tmp_path):
    from awf.api import retire_todo
    from awf.api._errors import AwfApiError

    project = _project(tmp_path, started=True)
    (project / ".agentic" / "inbox" / f"{T}.md").unlink()
    outside, _canary = _outside(tmp_path)
    _replace_with_symlink(project / ".agentic" / "outbox", outside)

    with pytest.raises(AwfApiError) as excinfo:
        retire_todo(project, T, "no longer needed")

    message = str(excinfo.value)
    assert "not found in inbox" in message
    assert "unsafe managed path" not in message


# ─── (i): последняя запись retire — no-follow (REVIEW G3) ────────────────

def test_retire_todo_refuses_symlinked_retired_note_leaf(tmp_path, monkeypatch):
    """Симлинк на ``done/<id>/RETIRED-<ts>.md`` — файл-лист, не каталог."""
    from awf.api import hygiene, retire_todo
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside, canary = _outside(tmp_path)
    ts = "20261020T000000000000Z"
    monkeypatch.setattr(hygiene, "_timestamp", lambda: ts)
    done_dir = project / ".agentic" / "done" / T
    done_dir.mkdir()
    note_target = outside / "RETIRED-note.md"
    note_target.write_text("outside", encoding="utf-8")
    (done_dir / f"RETIRED-{ts}.md").symlink_to(note_target)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        retire_todo(project, T, "no longer needed")

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert note_target.read_text(encoding="utf-8") == "outside"
    assert (project / ".agentic" / "inbox" / f"{T}.md").is_file()


# ─── (j): dry_run GC не отказывает (REVIEW G4) ───────────────────────────

def test_gc_records_dry_run_lists_garbage_with_symlinked_records(tmp_path):
    """``dry_run=True`` только перечисляет мусор: барьер — у реального прогона."""
    from awf.infra import state_store
    from awf.infra.state_store._records import gc_records

    project = _project(tmp_path)
    state = project / ".agentic" / "state"
    state_store.control_path(project).write_text(
        json.dumps({"schema_version": 2, "revision": 0, "records": []}),
        encoding="utf-8",
    )
    outside, canary = _outside(tmp_path)
    (outside / "kind").mkdir()
    (outside / "kind" / "deadbeef.json").write_text("{}", encoding="utf-8")
    _replace_with_symlink(state / "records", outside)

    listed = gc_records(project, dry_run=True)

    assert any(path.endswith("deadbeef.json") for path in listed)
    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / "kind" / "deadbeef.json").is_file()


# ─── (k): trace-лист remove_todo — та же дыра F4 ───────────────────────────
#
# QA (TODO-0233, attempt 1): барьер `remove_todo` покрывает `done/<id>`, но
# НЕ лист `done/<id>/removed-<ts>.md`, в который `shutil.move` кладёт
# inbox-файл. Симлинк листа на внешний КАТАЛОГ — `shutil.move` видит
# `isdir(dst)` и перекладывает задачу за пределы проекта. Это ровно тот
# лист, который F4 требовал проверять («включая КОНЕЧНЫЙ лист»), и та же
# правка, что уже сделана для `RETIRED-<ts>.md` (G3) — только в retire.
# Путь не уникализирован (`_timestamp`), поэтому пред-созданный симлинк
# не обходится через `_unique_name`.

def test_remove_todo_refuses_symlinked_trace_leaf(tmp_path, monkeypatch):
    from awf.api import hygiene
    from awf.api._errors import AwfApiError

    project = _project(tmp_path, started=False)
    outside, canary = _outside(tmp_path)
    ts = "20261020T000000000000Z"
    monkeypatch.setattr(hygiene, "_timestamp", lambda: ts)
    done_dir = project / ".agentic" / "done" / T
    done_dir.mkdir()
    (done_dir / f"removed-{ts}.md").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        hygiene.remove_todo(project, T)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (project / ".agentic" / "inbox" / f"{T}.md").is_file()
    assert not (outside / f"{T}.md").exists()


# ─── (l): POSIX-требование новых барьеров (REVIEW H1) ────────────────────
#
# Барьер hygiene и GC записей опираются на lstat/O_NOFOLLOW. Платформа без
# них получает явный отказ (та же политика, что в _wipe/_lease), а не
# молча ослабленную проверку.

def test_hygiene_mover_refuses_non_posix(tmp_path, monkeypatch):
    from awf.api import hygiene
    from awf.api._errors import AwfApiError
    from awf.infra import managed_fs

    project = _project(tmp_path)
    monkeypatch.setattr(managed_fs, "is_posix", lambda: False)

    with pytest.raises(AwfApiError, match="POSIX"):
        hygiene.clear_stale_closures(project, T)

    assert (project / ".agentic" / "outbox" / f"BLOCKED-{T}.ready").is_file()


def test_gc_records_refuses_non_posix(tmp_path, monkeypatch):
    from awf.infra import managed_fs, state_store
    from awf.infra.managed_fs import ManagedPathError
    from awf.infra.state_store._records import gc_records

    project = _project(tmp_path)
    state_store.control_path(project).write_text(
        json.dumps({"schema_version": 2, "revision": 0, "records": []}),
        encoding="utf-8",
    )
    (project / ".agentic" / "state" / "records").mkdir()
    monkeypatch.setattr(managed_fs, "is_posix", lambda: False)

    with pytest.raises(ManagedPathError, match="POSIX"):
        gc_records(project, dry_run=False)


# ─── (m): мёртвые отчёты переживают цикл (REVIEW H2) ─────────────────────
#
# ``archive_todo`` нёс только PROGRESS/DONE: BLOCKED- и REVIEW-отчёты
# упавших попыток копились в outbox после закрытия TODO.

def test_archive_todo_moves_blocked_and_review_reports(tmp_path):
    from awf import todos

    project = _project(tmp_path)
    outbox = project / ".agentic" / "outbox"
    (outbox / f"BLOCKED-{T}.md").write_text("blocked reason\n", encoding="utf-8")
    (outbox / f"REVIEW-{T}.md").write_text("rejected\n", encoding="utf-8")

    dest = todos.archive_todo(project, T)

    assert dest is not None
    assert sorted(p.name for p in dest.iterdir()) == [
        "BLOCKED-TODO-0001.md",
        "PROGRESS.md",
        "REVIEW-TODO-0001.md",
        "TODO.md",
    ]
    assert [p.name for p in outbox.iterdir() if T in p.name] == []
    assert not list(outbox.glob("*.ready"))


# ─── (n): лист НАЗНАЧЕНИЯ архива (architect-review) ─────────────────────
#
# Барьер архива проверял ``dest = done/<id>`` как КАТАЛОГ, но не лист, в
# который ``shutil.move`` кладёт файл: симлинк-каталог на листе уводит
# перемещение за пределы проекта (та же дыра, что в (f)). H2 добавил два
# новых листа (BLOCKED/REVIEW) — проверка листьев одна на весь ``moves``,
# включая давние TODO.md/DONE.md.

@pytest.mark.parametrize(
    ("src_name", "leaf"),
    [
        ("BLOCKED-TODO-0001.md", "BLOCKED-TODO-0001.md"),
        ("REVIEW-TODO-0001.md", "REVIEW-TODO-0001.md"),
        ("DONE-TODO-0001.md", "DONE.md"),
    ],
)
def test_archive_todo_refuses_symlinked_destination_leaf(tmp_path, src_name, leaf):
    from awf import todos

    project = _project(tmp_path)
    outside, canary = _outside(tmp_path)
    outbox = project / ".agentic" / "outbox"
    (outbox / src_name).write_text("report\n", encoding="utf-8")
    done_leaf = project / ".agentic" / "done" / T
    done_leaf.mkdir()
    (done_leaf / leaf).symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="refused"):
        todos.archive_todo(project, T)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert not [p for p in outside.iterdir() if p.name != "canary.txt"]
    assert (outbox / src_name).is_file()
    assert (project / ".agentic" / "inbox" / f"{T}.md").is_file()
