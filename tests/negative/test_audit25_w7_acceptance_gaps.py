"""TODO-0117: добор проверок аудита 2026-09-25 (сверка 26.09).

Сверка каждой формулировки «Проверка после исправления» аудита с
фактическими тестами: четыре проверки заданы, тестов на них нет. Файл
добирает покрытие A-02 и A-06; код не меняется (контракт юнита) — если
кейс вскроет реальный дефект, BLOCKED, фикс отдельным решением.

Пункты → тесты (в этом файле):
1. A-02 (полный набор аудита): два одновременных foreground-запуска и
   смешанная пара (foreground+background), как и background-пара, дают
   ровно один выполняющийся пайплайн без второго worker:
   test_two_concurrent_foreground_starts_run_once (xfail strict — на
   текущем коде красный: lease/liveness только в background-режиме,
   дефект подтверждён, фикс TODO-0118; пометку снимет 0118),
   test_mixed_pair_foreground_running_blocks_background_launch (зелёный).
2. A-06 (публичный путь): неверно типизированный YAML через публичный
   start_pipeline — понятная ошибка, без побочных записей в runtime
   (снимок .agentic/ побайтно; исключение — .agentic/logs/**):
   test_mistyped_yaml_via_public_start_clear_error_no_side_effects,
   test_valid_yaml_via_public_start_reaches_pipeline.

Добор других пунктов (дополнение существующих audit25-файлов):
3. A-10 (сквозная атрибуция) → test_audit25_a10_todo_ids.py::
   test_metrics_e2e_ten_thousand_and_ten_hundred_stay_apart.
4. A-15 (untracked-хвост) → test_audit25_a15_verified_sha.py::
   test_untracked_change_after_approve_blocks_commit.

Popen и run_pipeline замокан(ы), проект — tmp_git_repo.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from awf import api


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="W7Gaps")
    inbox = tmp_git_repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "TODO-0001.md").write_text("# Task\n", encoding="utf-8")
    (inbox / "TODO-0001.ready").touch()
    return tmp_git_repo


def _wait_until(counter: dict, n: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if counter.get("n", 0) >= n:
            return True
        time.sleep(0.02)
    return False


def _launch_async(caller) -> tuple[threading.Thread, dict]:
    """caller() в потоке; возвращает (поток, dict {result | error})."""
    out: dict = {}

    def run():
        try:
            out["result"] = caller()
        except BaseException as e:  # noqa: BLE001 — surfaced in the asserts
            out["error"] = e

    t = threading.Thread(target=run)
    t.start()
    return t, out


def _head(proj: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True, check=True
    ).stdout.strip()


# ── A-02 (полный набор аудита): foreground-пара и смешанная пара ──────────


@pytest.mark.xfail(
    strict=True,
    reason="A-02 foreground-пара: нет lease/liveness на foreground-пути — TODO-0118",
)
def test_two_concurrent_foreground_starts_run_once(tmp_git_repo, monkeypatch):
    """A-02 (полный набор аудита): два одновременных foreground-запуска —
    ровно один выполняющийся пайплайн, второй вызов получает отказ, а не
    второй прогон (как и background-пара).

    XFAIL (strict): на текущем коде красный — lease берётся только в
    background-режиме (`awf/api/pipeline.py`, start_pipeline), и второй
    одновременный foreground-вызов исполняет второй пайплайн. Дефект
    подтверждён (BLOCKED-TODO-0117), фикс — TODO-0118; пометку снимет он.
    """
    import awf.orchestrator as orch_mod

    proj = _project(tmp_git_repo)
    runs: dict = {}
    gate = threading.Event()

    def fake_run_pipeline(args):
        runs["n"] = runs.get("n", 0) + 1
        if runs["n"] == 1:
            gate.wait(timeout=30)
        return 0

    monkeypatch.setattr(orch_mod, "run_pipeline", fake_run_pipeline)
    t, first = _launch_async(lambda: api.start_pipeline(proj, background=False))
    assert _wait_until(runs, 1), "первый запуск должен дойти до пайплайна"

    # Первый foreground-запуск ещё внутри пайплайна — второй вызов обязан
    # получить отказ, а не запустить второй выполняющийся пайплайн.
    second = api.start_pipeline(proj, background=False)

    gate.set()
    t.join(timeout=30)
    assert not t.is_alive(), "первый запуск не завершился"
    assert "error" not in first, f"первый запуск упал: {first.get('error')!r}"

    assert runs.get("n", 0) == 1, (
        f"run_pipeline вызван {runs.get('n')} раз(а) — два одновременных "
        "foreground-запуска дали два выполняющихся пайплайна (A-02: ровно "
        "один пайплайн без второго worker)"
    )
    assert second.run_mode == "noop", (
        f"второй одновременный запуск не получил отказ — "
        f"{second.run_mode!r}: {second.message}"
    )
    assert second.run_id is None


def test_mixed_pair_foreground_running_blocks_background_launch(
    tmp_git_repo, monkeypatch
):
    """A-02 (полный набор аудита): смешанная пара — foreground-пайплайн
    выполняется (state с pipeline_pid, как в реальном оркестраторе),
    одновременный background-запуск отклоняется резолвером живости:
    ровно один выполняющийся пайплайн, второго worker (спавна) нет."""
    import awf.orchestrator as orch_mod
    from awf.api import _liveness
    from awf.pipeline_state import write_state

    proj = _project(tmp_git_repo)
    runs: dict = {}
    spawn: dict = {}
    gate = threading.Event()

    def fake_run_pipeline(args):
        # Как реальный оркестратор: state с pid пайплайна на каждую стадию.
        write_state(
            proj, stage_idx=0, stage_name="plan", stage_kind="plan",
            todo_id="TODO-0001", pipeline_pid=os.getpid(),
        )
        runs["n"] = runs.get("n", 0) + 1
        gate.wait(timeout=30)
        return 0

    def fake_popen(cmd, *args, **kwargs):
        spawn["n"] = spawn.get("n", 0) + 1
        raise AssertionError("второй worker (background-спавн) не должен быть")

    monkeypatch.setattr(orch_mod, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # Документированный шов (см. test_audit25_a02_lease.py): строгому
    # резолверу argv тестового процесса не пройти без подмены.
    monkeypatch.setattr(
        _liveness, "read_cmdline", lambda pid: "python3\x00-m\x00awf\x00start\x00"
    )

    t, first = _launch_async(lambda: api.start_pipeline(proj, background=False))
    assert _wait_until(runs, 1), "foreground-запуск должен дойти до пайплайна"

    # Foreground-пайплайн выполняется (state записан) — смешанный
    # background-запуск обязан получить отказ, а не второй worker.
    second = api.start_pipeline(proj, background=True)

    gate.set()
    t.join(timeout=30)
    assert not t.is_alive(), "foreground-запуск не завершился"
    assert "error" not in first, f"foreground-запуск упал: {first.get('error')!r}"

    assert runs.get("n", 0) == 1, (
        f"выполняющихся пайплайнов {runs.get('n')} — смесью foreground и "
        "background запущено два (A-02: ровно один без второго worker)"
    )
    assert spawn.get("n", 0) == 0, (
        f"background-спавн состоялся ({spawn.get('n')}) — второй worker"
    )
    assert second.run_mode == "noop", (
        f"background-запуск рядом с выполняющимся foreground не отклонён — "
        f"{second.run_mode!r}: {second.message}"
    )
    assert second.run_id is None


# ── A-06 (публичный путь): неверно типизированный YAML через start_pipeline ─


def _snapshot_agentic(proj: Path) -> dict[str, bytes]:
    """Побайтный снимок ``.agentic/`` (относительный путь → содержимое).

    Исключение — ``.agentic/logs/**``: логи, которые может писать сам
    вызов (список исключений зафиксирован в DONE).
    """
    ag = proj / ".agentic"
    snap: dict[str, bytes] = {}
    if ag.is_dir():
        for p in sorted(ag.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(ag))
                if rel.startswith("logs/"):
                    continue
                snap[rel] = p.read_bytes()
    return snap


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("- name: plan\n  role: supervisor\n- name: verify\n  role: supervisor\n",
         "root must be a mapping"),
        ("stages: [42]\n", "stage #0 must be a mapping"),
    ],
    ids=["list_root", "bad_stage_item"],
)
def test_mistyped_yaml_via_public_start_clear_error_no_side_effects(
    tmp_git_repo, content, match
):
    """A-06 (публичный путь): неверно типизированный YAML через публичный
    start_pipeline — понятная ошибка (текст валидатора, не необработанное
    исключение) и без побочных записей в runtime. run_pipeline не замокан:
    реальный оркестратор обязан остановиться на load_stages — до дашборда,
    state и логов."""
    proj = _project(tmp_git_repo)
    pdir = proj / ".agentic" / "pipelines"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "bad.yaml").write_text(content, encoding="utf-8")

    before = _snapshot_agentic(proj)
    result = api.start_pipeline(proj, background=False, pipeline="bad")
    after = _snapshot_agentic(proj)

    assert result.run_mode == "foreground"
    assert result.exit_code == 1, (
        f"битый пайплайн не остановил запуск: {result.run_mode!r}: {result.message}"
    )
    assert match in result.message, (
        f"ошибка без текста валидатора: {result.message[:300]}"
    )
    assert after == before, (
        f"побочные записи в runtime при отказе: "
        f"добавлено {sorted(set(after) - set(before))}, "
        f"изменено {sorted(p for p in set(before) & set(after) if before[p] != after[p])}, "
        f"удалено {sorted(set(before) - set(after))}"
    )


def test_valid_yaml_via_public_start_reaches_pipeline(tmp_git_repo, monkeypatch):
    """A-06 (таблица, строка «валидный»): корректный YAML через публичный
    start_pipeline грузится без ошибки, доходит до исполнения (замоканное),
    runtime-записей не создаёт."""
    import awf.orchestrator as orch_mod

    proj = _project(tmp_git_repo)
    pdir = proj / ".agentic" / "pipelines"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "ok.yaml").write_text(
        "stages:\n"
        "  - name: plan\n    role: supervisor\n"
        "  - name: impl\n    role: worker\n    max_retries: 2\n"
        "  - name: verify\n    role: supervisor\n    on_approved: commit_and_next\n",
        encoding="utf-8",
    )
    runs: dict = {}

    def fake_run_pipeline(args):
        runs["n"] = runs.get("n", 0) + 1
        return 0

    monkeypatch.setattr(orch_mod, "run_pipeline", fake_run_pipeline)

    before = _snapshot_agentic(proj)
    result = api.start_pipeline(proj, background=False, pipeline="ok")
    after = _snapshot_agentic(proj)

    assert result.run_mode == "foreground"
    assert result.exit_code == 0, f"валидный пайплайн упал: {result.message}"
    assert "crashed" not in result.message.lower(), result.message
    assert runs.get("n", 0) == 1, "валидный пайплайн не дошёл до исполнения"
    assert after == before, (
        f"побочные записи в runtime при старте: "
        f"добавлено {sorted(set(after) - set(before))}"
    )
