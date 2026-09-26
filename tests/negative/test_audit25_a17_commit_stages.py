"""A-17 (аудит 2026-09-25, слой 7): метрики видят коммиты любой стадии.

До: ``awf/metrics.py::collect_commit_info`` брал коммит только если в
субъекте есть ``verify``. Но commit-гейт
(``awf/commit_gate.py``, ``on_approved: commit_and_next/commit_and_report``)
формирует тему ``awf(<имя стадии>): TODO-<id>`` для ЛЮБОЙ стадии — в том
числе ``execute``. Такой коммит не учитывался: строки не считались
(``collect_code_lines``), время не ставило конец окна (``build_windows``),
а без воркер-сессий юнит исчезал из отчёта вовсе.

Findings covered:
- test_execute_stage_commit_is_counted — subject ``awf(execute): TODO-0001``
  учтён (baseline: не учтён → красный)
- test_verify_commit_still_counted — subject ``awf(verify): TODO-0001``
  учитывается как раньше (регресс)
- test_window_uses_execute_commit_end — execute-коммит задаёт конец окна
  юнита (чистая функция ``build_windows``)
"""
from __future__ import annotations

import subprocess

from awf.metrics import build_windows, collect_commit_info


def _commit(repo, subject: str) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", subject],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_execute_stage_commit_is_counted(tmp_git_repo):
    """A-17: subject ``awf(execute): TODO-0001`` — коммит учтён (любая стадия)."""
    _commit(tmp_git_repo, "awf(execute): TODO-0001")
    commits = collect_commit_info(tmp_git_repo, [])
    assert "TODO-0001" in commits, (
        f"execute-коммит не учтён метриками: {sorted(commits)}"
    )
    assert len(commits["TODO-0001"]) == 1


def test_verify_commit_still_counted(tmp_git_repo):
    """A-17 (регресс): subject ``awf(verify): TODO-0001`` — как раньше учтён."""
    _commit(tmp_git_repo, "awf(verify): TODO-0001")
    commits = collect_commit_info(tmp_git_repo, [])
    assert "TODO-0001" in commits, (
        f"verify-коммит перестал учитываться: {sorted(commits)}"
    )


def test_window_uses_execute_commit_end(tmp_path):
    """A-17: execute-коммит (без verify) задаёт конец окна юнита.

    Чистая функция ``build_windows``: конец окна = время последнего коммита
    юнита (+60s, с обрезкой по now_ms). Юнит без воркер-сессий: старт из
    ``first_ts``. Ключевой факт — конец берётся из коммита, а не из
    фолбэка ``now_ms``.
    """
    now_ms = 2_000_000_000_000
    commits = {"TODO-0001": [("abc1234", 1_600_000_000)]}  # ct — секунды
    windows = build_windows(tmp_path, ["TODO-0001"], {"TODO-0001": 1_000_000_000_000},
                            commits, now_ms)
    start, end = windows["TODO-0001"]
    assert start == 1_000_000_000_000
    assert end == 1_600_000_000 * 1000 + 60_000, (
        f"окно не закрыто временем execute-коммита: end={end}"
    )
