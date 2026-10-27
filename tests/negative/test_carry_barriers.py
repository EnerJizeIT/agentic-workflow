"""CARRY IMP3: POSIX-барьер архивации (carry-cd R2, AC4–AC5).

Закрытый класс (C2): ``archive_todo`` проверял no-follow ДО перемещения, но
на платформе без lstat/O_NOFOLLOW этой гарантии не существует — архив либо
падал невнятно, либо (при будущей слабой реализации) молча работал.
Инвариант: не-POSIX отказывает ПЕРВЫМ действием, тем же ``OSError``, и ни
один файл не перемещён и не удалён.

Тесты не-архивные поверхности (inbox → симлинк) держат тот же барьер: отказ
до эффекта, внешняя канарейка цела.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from awf import api, todos
from awf.api._errors import AwfApiError
from awf.infra import managed_fs

T = "TODO-0001"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for name in ("inbox", "outbox", "context", "done", "handoff"):
        (project / ".agentic" / name).mkdir(parents=True)
    inbox = project / ".agentic" / "inbox"
    (inbox / f"{T}.md").write_text("# task\n", encoding="utf-8")
    (inbox / f"{T}.ready").write_text("", encoding="utf-8")
    outbox = project / ".agentic" / "outbox"
    (outbox / f"DONE-{T}.md").write_text("done\n", encoding="utf-8")
    (outbox / f"DONE-{T}.json").write_text("{}\n", encoding="utf-8")
    (outbox / f"DONE-{T}.ready").write_text("", encoding="utf-8")
    return project


def _snapshot(project: Path) -> dict[str, bytes]:
    root = project / ".agentic"
    return {
        p.relative_to(project).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _outside(tmp_path: Path) -> tuple[Path, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("keep me", encoding="utf-8")
    return outside, canary


def test_archive_refuses_non_posix_before_any_move(tmp_path, monkeypatch):
    """AC4: не-POSIX — отказ до перемещения; дерево и канарейка те же."""
    project = _project(tmp_path)
    before = _snapshot(project)
    monkeypatch.setattr(managed_fs, "is_posix", lambda: False)

    with pytest.raises(OSError, match="POSIX") as exc:
        todos.archive_todo(project, T)

    assert f"archive {T}" in str(exc.value)
    assert _snapshot(project) == before, "a refused archive must not move or delete anything"
    assert not (project / ".agentic" / "done" / T).exists()


def test_archive_transfers_on_posix(tmp_path):
    """AC5: на POSIX переносятся те же файлы, сигналы потреблены."""
    project = _project(tmp_path)

    dest = todos.archive_todo(project, T)

    assert dest is not None
    assert dest == project / ".agentic" / "done" / T
    assert (dest / "TODO.md").read_text(encoding="utf-8") == "# task\n"
    assert (dest / "DONE.md").read_text(encoding="utf-8") == "done\n"
    assert (dest / "DONE.json").read_text(encoding="utf-8") == "{}\n"
    inbox = project / ".agentic" / "inbox"
    outbox = project / ".agentic" / "outbox"
    assert not (inbox / f"{T}.md").exists()
    assert not (inbox / f"{T}.ready").exists()
    assert not (outbox / f"DONE-{T}.ready").exists()


def test_archive_refuses_symlinked_inbox_before_effect(tmp_path):
    """AC5: симлинк поверхности по-прежнему отказывает до эффекта."""
    project = _project(tmp_path)
    outside, canary = _outside(tmp_path)
    (outside / f"{T}.md").write_text("# foreign\n", encoding="utf-8")
    inbox = project / ".agentic" / "inbox"
    (inbox / f"{T}.md").unlink()
    (inbox / f"{T}.ready").unlink()
    inbox.rmdir()
    inbox.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="symlink") as exc:
        todos.archive_todo(project, T)

    assert f"archive {T}" in str(exc.value)
    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / f"{T}.md").read_text(encoding="utf-8") == "# foreign\n"
    assert not (project / ".agentic" / "done" / T).exists()


# ─── CTX1 (TODO-0281): restore_todo — тот же POSIX/no-follow барьер ─────
#
# Восстановление ПЕРЕМЕЩАЕТ файл из архива в inbox и создаёт сигнал. До
# фикса симлинк ``inbox`` или ``done`` уводил и перемещение, и ``touch``
# за пределы проекта; не-POSIX платформа не отказывала вовсе. Инвариант:
# отказ ДО первого ``mkdir``/перемещения, внешняя канарейка цела.

R = "TODO-0002"


def _archived(tmp_path: Path) -> Path:
    project = tmp_path / "restore-proj"
    for name in ("inbox", "outbox", "context", "done", "handoff"):
        (project / ".agentic" / name).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: Restore\n", encoding="utf-8"
    )
    unit = project / ".agentic" / "done" / R
    unit.mkdir()
    (unit / "TODO.md").write_text("# restored task\n", encoding="utf-8")
    return project


def test_restore_refuses_non_posix_before_any_move(tmp_path, monkeypatch):
    """CTX1: не-POSIX — отказ до mkdir/перемещения; архив цел."""
    project = _archived(tmp_path)
    monkeypatch.setattr(managed_fs, "is_posix", lambda: False)

    with pytest.raises(AwfApiError, match="POSIX"):
        api.restore_todo(project, R)

    assert (project / ".agentic" / "done" / R / "TODO.md").is_file()
    assert not (project / ".agentic" / "inbox" / f"{R}.md").exists()
    assert not (project / ".agentic" / "inbox" / f"{R}.ready").exists()


def test_restore_transfers_on_posix(tmp_path):
    """CTX1: обычный путь не сломан — TODO.md в inbox, сигнал создан."""
    project = _archived(tmp_path)

    api.restore_todo(project, R)

    inbox = project / ".agentic" / "inbox"
    assert (inbox / f"{R}.md").read_text(encoding="utf-8") == "# restored task\n"
    assert (inbox / f"{R}.ready").is_file()
    assert not (project / ".agentic" / "done" / R / "TODO.md").exists()


def test_restore_refuses_symlinked_inbox_before_effect(tmp_path):
    """CTX1: симлинк inbox — отказ до перемещения; внешний файл цел."""
    project = _archived(tmp_path)
    outside, canary = _outside(tmp_path)
    inbox = project / ".agentic" / "inbox"
    inbox.rmdir()
    inbox.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AwfApiError, match="symlink"):
        api.restore_todo(project, R)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert not (outside / f"{R}.md").exists()
    assert (project / ".agentic" / "done" / R / "TODO.md").is_file()


def test_restore_refuses_symlinked_done_surface(tmp_path):
    """CTX1: симлинк ``done`` — тоже поверхность restore (источник move).

    Барьер перечисляет ``done_dir`` явно: без него архив читался бы через
    ссылку, а ``shutil.move`` забрал бы TODO.md из чужого каталога. Отказ
    обязан прийти до перемещения.
    """
    project = _archived(tmp_path)
    outside, canary = _outside(tmp_path)
    done = project / ".agentic" / "done"
    shutil.move(str(done), str(outside / "done"))
    done.symlink_to(outside / "done", target_is_directory=True)

    with pytest.raises(AwfApiError, match="symlink"):
        api.restore_todo(project, R)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / "done" / R / "TODO.md").is_file()
    assert not (project / ".agentic" / "inbox" / f"{R}.md").exists()


def test_restore_refuses_symlinked_context_before_history_unlink(tmp_path):
    """CTX1 + architect-review: ``context`` — тоже поверхность restore.

    ``forget_unit_history`` ниже удаляет ``CLOSER-<id>.json`` и
    ``CHECKLIST-<id>.json`` из ``context``. Симлинк ``context`` уводил эти
    удаления наружу; барьер обязан отказать до первого ``unlink`` и до
    перемещения архива.
    """
    project = _archived(tmp_path)
    outside, canary = _outside(tmp_path)
    (outside / f"CLOSER-{R}.json").write_text("foreign\n", encoding="utf-8")
    (outside / f"CHECKLIST-{R}.json").write_text("foreign\n", encoding="utf-8")
    ctx = project / ".agentic" / "context"
    ctx.rmdir()
    ctx.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AwfApiError, match="symlink"):
        api.restore_todo(project, R)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / f"CLOSER-{R}.json").read_text(encoding="utf-8") == "foreign\n"
    assert (outside / f"CHECKLIST-{R}.json").read_text(encoding="utf-8") == "foreign\n"
    assert (project / ".agentic" / "done" / R / "TODO.md").is_file()
    assert not (project / ".agentic" / "inbox" / f"{R}.md").exists()


# ─── CTX4 (TODO-0281): rollback сервисного старта не идёт по ссылкам ────
#
# Откат — best-effort, но и он не должен УДАЛЯТЬ через симлинк. Барьер
# один (``surfaces_are_managed``), отказ = пропуск поверхности.

def _rollback_service(project: Path) -> None:
    from awf.api.run import _rollback_service_side_effects

    _rollback_service_side_effects(project, R, "role-draft-x", clear_slot=False)


def _linked(project: Path, name: str, outside: Path) -> Path:
    surface = project / ".agentic" / name
    if surface.is_dir():
        surface.rmdir()
    surface.symlink_to(outside, target_is_directory=True)
    return surface


def test_service_rollback_never_deletes_through_symlinked_inbox(tmp_path):
    project = _archived(tmp_path)
    outside, canary = _outside(tmp_path)
    (outside / f"{R}.md").write_text("foreign\n", encoding="utf-8")
    inbox = _linked(project, "inbox", outside)

    _rollback_service(project)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / f"{R}.md").read_text(encoding="utf-8") == "foreign\n"
    assert inbox.is_symlink()


def test_service_rollback_never_deletes_through_symlinked_baseline(tmp_path):
    project = _archived(tmp_path)
    outside, canary = _outside(tmp_path)
    (outside / f"BASELINE-{R}.sha").write_text("foreign\n", encoding="utf-8")
    (outside / f"CLOSER-{R}.json").write_text("foreign\n", encoding="utf-8")
    ctx = _linked(project, "context", outside)

    _rollback_service(project)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / f"BASELINE-{R}.sha").read_text(encoding="utf-8") == "foreign\n"
    assert (outside / f"CLOSER-{R}.json").read_text(encoding="utf-8") == "foreign\n"
    assert ctx.is_symlink()


def test_service_rollback_never_deletes_through_symlinked_pipelines(tmp_path):
    project = _archived(tmp_path)
    outside, canary = _outside(tmp_path)
    pipelines = _linked(project, "pipelines", outside)
    (outside / "role-draft-x.yaml").write_text("foreign\n", encoding="utf-8")

    _rollback_service(project)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / "role-draft-x.yaml").read_text(encoding="utf-8") == "foreign\n"
    assert pipelines.is_symlink()


def test_service_rollback_still_removes_own_artifacts_on_clean_surfaces(tmp_path):
    """CTX4: на обычных поверхностях откат чистит ВСЁ, что оставил старт.

    Гейт ``surfaces_are_managed`` не должен стать «тихим пропуском» на
    нормальном проекте: TODO, бейзлайн-артефакты, история юнита и
    служебный pipeline обязаны исчезнуть. Ловит регресс, при котором
    барьер ошибочно возвращает ``False`` и откат молча ничего не делает.
    """
    from awf.api.run import _rollback_service_side_effects

    project = _archived(tmp_path)
    inbox = project / ".agentic" / "inbox"
    ctx = project / ".agentic" / "context"
    (inbox / f"{R}.md").write_text("# task\n", encoding="utf-8")
    (inbox / f"{R}.ready").write_text("", encoding="utf-8")
    for suffix in (
        "sha", "status", "tests.log", "env.log", "untracked",
        "worktree", "include", "carry_over", "claim",
    ):
        (ctx / f"BASELINE-{R}.{suffix}").write_text("x\n", encoding="utf-8")
    (ctx / f"CLOSER-{R}.json").write_text("{}\n", encoding="utf-8")
    (ctx / f"CHECKLIST-{R}.json").write_text("{}\n", encoding="utf-8")
    pipelines = project / ".agentic" / "pipelines"
    pipelines.mkdir()
    target = pipelines / "role-draft-x.yaml"
    target.write_text("name: role-draft-x\n", encoding="utf-8")

    _rollback_service_side_effects(project, R, "role-draft-x", clear_slot=False)

    assert not (inbox / f"{R}.md").exists()
    assert not (inbox / f"{R}.ready").exists()
    assert not list(ctx.glob(f"BASELINE-{R}.*"))
    assert not (ctx / f"CLOSER-{R}.json").exists()
    assert not (ctx / f"CHECKLIST-{R}.json").exists()
    assert not target.exists()


# ─── CTX2 (TODO-0281): service approve — тот же managed-барьер ──────────
#
# Служебный approve публикует пару для движка (RUN-EVIDENCE + пустой
# маркер), но писал её голыми ``mkdir``/``atomic_write_text``/``touch``:
# симлинк ``inbox``/``context`` или подмена листа уводили запись наружу.
# Инвариант R9-класса: preflight ДО первого файла, маркер — no-follow.

S = "TODO-0003"


def _service_active(project: Path, todo_id: str = S) -> None:
    from awf import run_state

    run_state.write_run(
        project,
        slot="service",
        active=True,
        queue=[{"todo_id": todo_id, "pipeline": "role-draft-x"}],
        index=1,
        current=todo_id,
        outcomes={},
        rejects={},
    )


def _context_names(project: Path) -> set[str]:
    return {p.name for p in (project / ".agentic" / "context").iterdir()}


def test_service_approve_refuses_symlinked_inbox_before_writes(tmp_path):
    """CTX2: симлинк inbox — отказ без RUN-EVIDENCE, сигнала и файлов."""
    project = _archived(tmp_path)
    _service_active(project)
    outside, canary = _outside(tmp_path)
    inbox = project / ".agentic" / "inbox"
    inbox.rmdir()
    inbox.symlink_to(outside, target_is_directory=True)
    before = _context_names(project)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api.run_service_approve(
            project, S, evidence="pytest -q → 3 passed; verdict: approve"
        )

    assert _context_names(project) == before
    assert f"RUN-EVIDENCE-{S}.md" not in _context_names(project)
    assert not (outside / f"APPROVE-{S}.ready").exists()
    assert canary.read_text(encoding="utf-8") == "keep me"


def test_service_approve_refuses_symlinked_signal_leaf(tmp_path):
    """CTX2: симлинк-подмена APPROVE-<id>.ready — отказ до записей."""
    project = _archived(tmp_path)
    _service_active(project)
    outside, canary = _outside(tmp_path)
    foreign = outside / "foreign.txt"
    foreign.write_text("keep me", encoding="utf-8")
    signal = project / ".agentic" / "inbox" / f"APPROVE-{S}.ready"
    signal.symlink_to(foreign)
    before = _context_names(project)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api.run_service_approve(
            project, S, evidence="pytest -q → 3 passed; verdict: approve"
        )

    assert _context_names(project) == before
    assert f"RUN-EVIDENCE-{S}.md" not in _context_names(project)
    assert signal.is_symlink(), "a refused approve must not replace the linked leaf"
    assert foreign.read_text(encoding="utf-8") == "keep me"


def test_service_approve_writes_evidence_and_empty_marker(tmp_path):
    """CTX2: обычный путь — evidence на месте, маркер пустой, вердикт записан."""
    from awf import run_state

    project = _archived(tmp_path)
    _service_active(project)

    result = api.run_service_approve(
        project, S, evidence="pytest -q → 3 passed; verdict: approve"
    )

    evidence = project / ".agentic" / "context" / f"RUN-EVIDENCE-{S}.md"
    signal = project / ".agentic" / "inbox" / f"APPROVE-{S}.ready"
    assert evidence.is_file()
    assert "verdict: approve" in evidence.read_text(encoding="utf-8")
    assert signal.is_file() and not signal.is_symlink()
    assert signal.read_text(encoding="utf-8") == "", "service marker stays empty"
    assert result.signal_file == str(signal)
    svc = run_state.read_run(project, slot="service")
    assert svc["outcomes"][S]["verdict"] == "approved"



# ─── PA-0281-1, лист: подмена самого файла пайплайна на симлинк ──────────
#
# Тест выше держит каталог ``pipelines``. Вторая поверхность той же записи —
# лист ``role-draft-<slug>.yaml``: preflight обязан перечислить и его, иначе
# проверка покрывала бы только половину пути.

_SERVICE_SOURCE = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - name: "agent-implementer"\n'
    '    role: "worker"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
)


def test_service_pipeline_write_refuses_symlinked_leaf(tmp_git_repo):
    """PA-0281-1: лист ``role-draft-<slug>.yaml`` — тоже поверхность записи.

    Каталог настоящий, но лист подменён ссылкой на файл снаружи. Барьер
    обязан отказать до ``managed_write_api_text``: внешний файл остаётся
    нетронутым, а ссылка — на месте (её не заменили записью).
    """
    from awf.api import run as api_run_module

    repo = tmp_git_repo
    api.init_project(repo, project_name="PipelineLeaf")
    (repo / ".agentic" / "pipelines" / "default.yaml").write_text(
        _SERVICE_SOURCE, encoding="utf-8"
    )
    outside, canary = _outside(tmp_git_repo.parent)
    foreign = outside / "role-draft-auditor.yaml"
    foreign.write_text("foreign\n", encoding="utf-8")
    leaf = repo / ".agentic" / "pipelines" / "role-draft-auditor.yaml"
    leaf.symlink_to(foreign)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api_run_module._write_service_pipeline(repo, "auditor")

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert foreign.read_text(encoding="utf-8") == "foreign\n"
    assert leaf.is_symlink(), "a refused write must not replace the linked leaf"
