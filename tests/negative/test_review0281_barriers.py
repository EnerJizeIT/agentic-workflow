"""REVIEW 0281: барьеры rollback, ACK и записи нарушения.

Три поверхности, найденные аудитом 0281, держат тот же инвариант, что
``archive``/``restore``/``approve``: preflight до первого эффекта и
no-follow запись.

- AUD1: ``rollback`` пишет ``context`` (история юнита) и ``inbox`` (ACK)
  после ``git reset`` — симлинк любой уводил запись наружу.
- AUD2: ``_arm_ack_file`` создавал ACK голыми ``mkdir``/``touch``.
- AUD3: ``record_violation`` создавал каталог записи голым ``mkdir``.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from awf import api
from awf.api._errors import AwfApiError
from awf.infra import managed_fs

RB = "TODO-0005"
R = "TODO-0002"
S = "TODO-0003"


def _archived(tmp_path: Path) -> Path:
    project = tmp_path / "review-proj"
    for name in ("inbox", "outbox", "context", "done", "handoff"):
        (project / ".agentic" / name).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: Review\n", encoding="utf-8"
    )
    unit = project / ".agentic" / "done" / R
    unit.mkdir()
    (unit / "TODO.md").write_text("# archived task\n", encoding="utf-8")
    return project


def _outside(tmp_path: Path) -> tuple[Path, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("keep me", encoding="utf-8")
    return outside, canary


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


def _git_rollback_project(repo: Path) -> Path:
    api.init_project(repo, project_name="Rollback")
    inbox = repo / ".agentic" / "inbox"
    (inbox / f"{RB}.md").write_text("# task\n", encoding="utf-8")
    (inbox / f"{RB}.ready").write_text("", encoding="utf-8")
    api.create_baseline(repo, RB, run_tests=False)
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_rollback_hard_refuses_symlinked_context_before_history_unlink(
    tmp_git_repo,
):
    """AUD1: симлинк context — отказ, внешние CLOSER/CHECKLIST целы."""
    repo = _git_rollback_project(tmp_git_repo)
    outside, canary = _outside(tmp_git_repo.parent)
    (outside / f"CLOSER-{RB}.json").write_text("foreign\n", encoding="utf-8")
    (outside / f"CHECKLIST-{RB}.json").write_text("foreign\n", encoding="utf-8")
    ctx = repo / ".agentic" / "context"
    shutil.rmtree(ctx)
    ctx.symlink_to(outside, target_is_directory=True)
    head_before = _head(repo)

    with pytest.raises(AwfApiError, match="symlink"):
        api.rollback(repo, RB, mode="hard")

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / f"CLOSER-{RB}.json").read_text(encoding="utf-8") == "foreign\n"
    assert (outside / f"CHECKLIST-{RB}.json").read_text(encoding="utf-8") == "foreign\n"
    assert ctx.is_symlink(), "a refused rollback must not replace the linked context"
    assert _head(repo) == head_before, "a refused rollback must not touch the repo"


def test_rollback_soft_refuses_symlinked_inbox_before_ack(tmp_git_repo):
    """AUD1: симлинк inbox — отказ, ACK не пишется наружу."""
    repo = _git_rollback_project(tmp_git_repo)
    outside, canary = _outside(tmp_git_repo.parent)
    inbox = repo / ".agentic" / "inbox"
    shutil.rmtree(inbox)
    inbox.symlink_to(outside, target_is_directory=True)
    head_before = _head(repo)

    with pytest.raises(AwfApiError, match="symlink"):
        api.rollback(repo, RB, mode="soft")

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert not (outside / f"ACK-{RB}.ready").exists()
    assert inbox.is_symlink(), "a refused rollback must not replace the linked inbox"
    assert _head(repo) == head_before


def test_arm_ack_file_refuses_symlinked_inbox(tmp_path):
    """AUD2: ACK супервизора не создаётся через симлинк inbox."""
    from awf.api.pipeline import _arm_ack_file

    project = _archived(tmp_path)
    outside, canary = _outside(tmp_path)
    inbox = project / ".agentic" / "inbox"
    inbox.rmdir()
    inbox.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        _arm_ack_file(inbox, R)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert not (outside / f"ACK-{R}.ready").exists()


def test_arm_ack_file_writes_empty_signal_on_clean_inbox(tmp_path):
    """AUD2: обычный путь не сломан — пустой ACK на месте."""
    from awf.api.pipeline import _arm_ack_file

    project = _archived(tmp_path)
    path = _arm_ack_file(project / ".agentic" / "inbox", R)

    assert path.is_file() and not path.is_symlink()
    assert path.read_text(encoding="utf-8") == ""


def test_record_violation_refuses_symlinked_agentic(tmp_path):
    """AUD3: запись нарушения не создаётся через симлинк .agentic."""
    from awf import stage_manifest

    project = tmp_path / "viol-proj"
    project.mkdir()
    outside = tmp_path / "outside-agentic"
    outside.mkdir()
    (project / ".agentic").symlink_to(outside, target_is_directory=True)

    with pytest.raises(managed_fs.ManagedPathError, match="symlink"):
        stage_manifest.record_violation(
            project, R, "worker", paths_outside=["src/x.py"]
        )

    assert not (outside / "context").exists()


def test_arm_ack_file_refuses_symlinked_inbox_with_foreign_ack(tmp_path):
    """AUD2 + QA-0281: симлинк inbox отказывает ДО поиска готового ACK.

    ``_armed_ack_file`` листает ``inbox.glob`` — на симлинке ``glob``
    следует за ссылкой и находил чужой ``ACK-<id>.ready`` снаружи. Прежний
    порядок («сначала reuse, потом барьер») возвращал этот внешний файл как
    ответ супервизора: записи наружу нет, но отказа тоже нет, и движок
    съел бы сигнал из-за пределов проекта. Барьер обязан идти первым.
    """
    from awf.api.pipeline import _arm_ack_file

    project = _archived(tmp_path)
    outside, canary = _outside(tmp_path)
    foreign = outside / f"ACK-{R}.ready"
    foreign.write_text("foreign\n", encoding="utf-8")
    inbox = project / ".agentic" / "inbox"
    inbox.rmdir()
    inbox.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        _arm_ack_file(inbox, R)

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert foreign.read_text(encoding="utf-8") == "foreign\n"
    assert inbox.is_symlink()


def test_service_approve_refuses_symlinked_context_before_evidence(tmp_path):
    """CTX2: симлинк context — отказ до RUN-EVIDENCE (поверхность evidence).

    Ридер T6 называет ``inbox`` и лист сигнала; ``context`` — вторая
    поверхность той же операции (сам RUN-EVIDENCE). Без него preflight
    покрывал бы только половину записи.
    """
    project = _archived(tmp_path)
    _service_active(project)
    outside, canary = _outside(tmp_path)
    ctx = project / ".agentic" / "context"
    ctx.rmdir()
    ctx.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api.run_service_approve(
            project, S, evidence="pytest -q → 3 passed; verdict: approve"
        )

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert not (outside / f"RUN-EVIDENCE-{S}.md").exists()
    assert not (outside / f"APPROVE-{S}.ready").exists()
    assert ctx.is_symlink()


def test_main_rollback_refuses_non_posix_before_effect(tmp_git_repo, monkeypatch):
    """CTX4: главный rollback на не-POSIX отказывает до чтения baseline и reset.

    ``refuse_unsafe_surfaces`` внутри вызывает ``require_posix``; проверка
    стоит до чтения BASELINE и до ``git reset``, поэтому HEAD не двигается.
    """
    repo = _git_rollback_project(tmp_git_repo)
    head_before = _head(repo)
    monkeypatch.setattr(managed_fs, "is_posix", lambda: False)

    with pytest.raises(AwfApiError, match="POSIX"):
        api.rollback(repo, RB, mode="hard")

    assert _head(repo) == head_before
    assert (repo / ".agentic" / "inbox" / f"{RB}.md").is_file()
    assert not (repo / ".agentic" / "inbox" / f"ACK-{RB}.ready").exists()


# ─── PA-0281-1 (аудит 0281, виток 2): генерация пайплайна — managed ───────
#
# ``_write_service_pipeline`` писал файл голыми ``mkdir``/``atomic_write``:
# симлинк ``pipelines`` уводил генерацию наружу, и это ровно та поверхность,
# которую откат сервисного старта теперь отказывается удалять.

DEFAULT_4STAGE = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - name: "agent-implementer"\n'
    '    role: "worker"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
)


def test_service_pipeline_write_refuses_symlinked_pipelines_dir(tmp_git_repo):
    """PA-0281-1: симлинк ``pipelines`` — отказ до записи, файла снаружи нет."""
    from awf.api import run as api_run_module

    repo = tmp_git_repo
    api.init_project(repo, project_name="Pipeline")
    outside, canary = _outside(tmp_git_repo.parent)
    # The source snapshot is read THROUGH the same symlink, so the outside
    # dir mirrors the real one: the writer must refuse before its own file,
    # not because the source went missing.
    (outside / "default.yaml").write_text(DEFAULT_4STAGE, encoding="utf-8")
    pipelines = repo / ".agentic" / "pipelines"
    shutil.rmtree(pipelines)
    pipelines.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api_run_module._write_service_pipeline(repo, "auditor")

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert not (outside / "role-draft-auditor.yaml").exists()
    assert pipelines.is_symlink()


# ─── PA-0281-2 (аудит 0281, виток 2): restore — сигнал через managed ──────

def test_restore_signal_write_never_follows_swapped_leaf(tmp_path, monkeypatch):
    """PA-0281-2: подмена листа ``.ready`` на симлинк не пишет наружу.

    Preflight и эффект разделены: между ними ссылку можно подменить. Второй
    рубеж — managed-запись сигнала (no-follow). Окно эмулируем, отключив
    preflight: голый ``touch`` в этой точке писал бы в файл снаружи.
    """
    from awf.api import lifecycle as api_lifecycle

    project = _archived(tmp_path)
    outside, canary = _outside(tmp_path)
    foreign = outside / "ready.txt"
    foreign.write_text("foreign\n", encoding="utf-8")
    ready = project / ".agentic" / "inbox" / f"{R}.ready"
    ready.symlink_to(foreign)

    monkeypatch.setattr(
        api_lifecycle, "refuse_unsafe_surfaces", lambda *a, **k: None
    )

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        api.restore_todo(project, R)

    assert foreign.read_text(encoding="utf-8") == "foreign\n"
    assert canary.read_text(encoding="utf-8") == "keep me"
    assert ready.is_symlink()


# ─── PA-0281-3 (аудит 0281, виток 2): отказ dispatch снимает пайплайн ────

def test_service_start_dispatch_refusal_leaves_no_generated_pipeline(
    tmp_git_repo, monkeypatch
):
    """PA-0281-3: отказ ``dispatch_todo`` не оставляет сгенерированный файл.

    Докстринг обещает «no pipeline file»: write и dispatch — один
    rollback-контракт. Репро аудита: симлинк ``inbox`` ронял dispatch уже
    после записи, файл оставался.
    """
    from awf.api import pipeline as api_pipeline
    from awf.api import run as api_run_module

    repo = tmp_git_repo
    api.init_project(repo, project_name="ServiceRollback")
    (repo / ".agentic" / "pipelines" / "default.yaml").write_text(
        DEFAULT_4STAGE, encoding="utf-8"
    )
    inbox = repo / ".agentic" / "inbox"
    (inbox / "TODO-0001.md").write_text("# task\n", encoding="utf-8")
    api.run_start(repo, queue=["TODO-0001"])

    def _never_started(project_dir, **kw):
        raise AssertionError("dispatch must refuse before start_pipeline")

    monkeypatch.setattr(api_pipeline, "start_pipeline", _never_started)

    outside, canary = _outside(tmp_git_repo.parent)
    shutil.rmtree(inbox)
    inbox.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AwfApiError):
        api_run_module.run_service_start(
            repo, slug="auditor", description="Аудит безопасности кода."
        )

    assert canary.read_text(encoding="utf-8") == "keep me"
    assert not (repo / ".agentic" / "pipelines" / "role-draft-auditor.yaml").exists()


# ─── PA-0281-4 (аудит 0281, виток 2): remedy без «roll back» ──────────────

def test_violation_refusal_unverifiable_after_advises_delete_only(tmp_path):
    """PA-0281-4: ``after`` не-64-hex — только явное удаление записи.

    Такую запись откат не снимет (docs/unit-contract.md, абзац CTX3), а
    прежний текст советовал «Roll the path back» — владелец тратил круг.
    """
    import json as _json

    from awf import violations

    project = tmp_path / "viol-proj"
    ctx = project / ".agentic" / "context"
    ctx.mkdir(parents=True)
    record = {
        "todo_id": R,
        "stage": "worker",
        "paths": {"src/x.py": {"before": "<missing>", "after": "<missing>"}},
    }
    (ctx / f"WRITE-VIOLATION-{R}.json").write_text(
        _json.dumps(record), encoding="utf-8"
    )

    message = violations.violation_refusal(project)

    assert f"WRITE-VIOLATION-{R}.json" in message
    assert "Roll the path back" not in message
    assert "delete" in message


# ─── AUD-0281-4 (аудит 0281, виток 3): rollback не падает с preflight ─────

def test_surfaces_are_managed_swallows_preflight_oserror(tmp_path, monkeypatch):
    """AUD-0281-4: сбой preflight — пропуск поверхности, не исключение.

    ``surfaces_are_managed`` — best-effort форма барьера для откатов: сам
    отказ уже состоялся, поэтому любая ошибка проверки (в том числе
    ``OSError`` от ``lstat``) обязана стать ``False``, а не вылететь из
    очистки.
    """
    from awf.api import _helpers

    def boom(*args, **kwargs):
        raise OSError("transient stat failure")

    monkeypatch.setattr(_helpers, "refuse_unsafe_surfaces", boom)

    assert _helpers.surfaces_are_managed(
        tmp_path, "service rollback", tmp_path / "inbox"
    ) is False
