"""A-09 (аудит 2026-09-25, слой 4): отпечаток стадии хеширует содержимое
untracked-файлов и бинарных tracked-файлов.

Дефект: ``work_fingerprint`` (``awf/verify.py``) складывал текст
``git diff <baseline>`` и ИМЕНА untracked-файлов, но не их байты.
``execute_agent_stage`` (``awf/pipeline_engine.py``) сравнивает хеш до и
после стадии: изменение только байтов уже существующего untracked-файла не
видно — стадия, написавшая в файл, выглядит бездействующей и может попасть
в silent retry/salvage.

Инварианты:
1. Изменение байтов существующего untracked-файла меняет отпечаток стадии.
2. Изменение содержимого бинарного tracked-файла меняет отпечаток.
3. Фильтрация «файлы, бывшие до baseline»
   (``BASELINE-{todo}.untracked``) сохраняется; публичная сигнатура
   ``work_fingerprint(project_dir, baseline_sha, todo_id)`` не меняется,
   ``""`` при отсутствующем baseline/не-git репо сохраняется.

До фикса тест 1 красный — это и есть точка. Тест 2 на базлайне
зелёный: ``git diff <sha>`` для бинарника содержит строку ``index`` с blob
SHA, которая уже отличает содержимое; фикс (``--binary``) делает хеш
контент-адресным и перестаёт полагаться на строку index — тест 2 закреплён
как регрессионный (зелёный-до/зелёный-после), расхождение задокументировано
в BLOCKED-отчёте юнита.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import verify

TODO = "TODO-0086"


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_untracked_content_change_changes_fingerprint(tmp_git_repo: Path):
    """Untracked-файл существует до вызова; меняем байты → отпечаток
    отличается. Базлайн: имена одинаковы → отпечатки совпадают → красный.

    Фикстура ``tmp_git_repo`` уже несёт начальный коммит — он и есть
    baseline."""
    baseline_sha = _head(tmp_git_repo)

    notes = tmp_git_repo / "notes.txt"
    notes.write_text("v1\n")

    fp_before = verify.work_fingerprint(tmp_git_repo, baseline_sha, TODO)
    assert fp_before, "fingerprint must be computed for a repo with a baseline"

    notes.write_text("v2 — bytes changed\n")

    fp_after = verify.work_fingerprint(tmp_git_repo, baseline_sha, TODO)
    assert fp_before != fp_after, (
        "changing the bytes of an existing untracked file must change the stage "
        "fingerprint — otherwise a stage that wrote into the file looks like a "
        "no-op (silent retry/salvage)"
    )


def test_binary_tracked_change_changes_fingerprint(tmp_git_repo: Path):
    """Бинарный tracked-файл меняется → отпечаток отличается.

    Регрессия (инвариант 2): на базлайне зелёный — строка ``index`` в
    текстовом diff несла blob SHA, различающие содержимое. После фикса
    diff идёт с ``--binary`` (контент-адресный), инвариант сохраняется.
    """
    (tmp_git_repo / "asset.bin").write_bytes(b"PNGHDR" + bytes(range(256)) * 8)
    _commit_all(tmp_git_repo, "add binary asset")
    baseline_sha = _head(tmp_git_repo)

    fp_before = verify.work_fingerprint(tmp_git_repo, baseline_sha)
    assert fp_before

    (tmp_git_repo / "asset.bin").write_bytes(b"PNGHDR" + bytes(range(255, -1, -1)) * 8)

    fp_after = verify.work_fingerprint(tmp_git_repo, baseline_sha)
    assert fp_before != fp_after, (
        "a binary tracked file's content change must change the fingerprint "
        "(content-addressed diff, not diff-text luck)"
    )

    # идентичная перезапись — отпечаток стабилен (никаких фантомных изменений)
    (tmp_git_repo / "asset.bin").write_bytes(b"PNGHDR" + bytes(range(255, -1, -1)) * 8)
    fp_same = verify.work_fingerprint(tmp_git_repo, baseline_sha)
    assert fp_after == fp_same, "an identical re-write must not move the fingerprint"


def test_preexisting_untracked_still_excluded(tmp_git_repo: Path):
    """Инвариант 3: файл из ``BASELINE-{todo}.untracked`` (был до baseline)
    исключён из отпечатка — его правка отпечаток НЕ меняет. Фильтр
    сохраняется после фикса на содержимое."""
    baseline_sha = _head(tmp_git_repo)

    (tmp_git_repo / ".agentic").mkdir()
    ctx = tmp_git_repo / ".agentic" / "context"
    ctx.mkdir()
    ctx.joinpath(f"BASELINE-{TODO}.untracked").write_text("stale.txt\n")

    stale = tmp_git_repo / "stale.txt"
    stale.write_text("old\n")
    fp_before = verify.work_fingerprint(tmp_git_repo, baseline_sha, TODO)
    assert fp_before

    stale.write_text("new bytes of a pre-existing file\n")

    fp_after = verify.work_fingerprint(tmp_git_repo, baseline_sha, TODO)
    assert fp_before == fp_after, (
        "pre-existing untracked files (BASELINE-{todo}.untracked) must stay "
        "excluded from the fingerprint — only worker-created files count"
    )

    # а новый untracked-файл того же юнита по-прежнему виден
    (tmp_git_repo / "worker-new.txt").write_text("fresh\n")
    fp_new = verify.work_fingerprint(tmp_git_repo, baseline_sha, TODO)
    assert fp_after != fp_new, "a worker-created untracked file must still be visible"
