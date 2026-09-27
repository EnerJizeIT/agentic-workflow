"""A-10 (аудит 2026-09-25, слой 7): TODO ID после 9999 не теряются и не путаются в метриках.

До: ``awf/metrics.py`` — регулярка заголовка воркер-сессии ровно на 4
цифры (``awf-<роль>-TODO-10000`` не попадает в worker totals), а в
commit subject 4 цифры без правой границы (``TODO-10000`` матчится как
``TODO-1000`` — коммит и строки кода приписываются чужому юниту).

Findings covered:
- test_worker_title_matches_five_digit_todo — заголовок с TODO-10000
  даёт юнит TODO-10000 (baseline: сессия исключена, юнит пуст)
- test_commit_subject_does_not_truncate_10000_to_1000 — subject
  ``awf(verify): TODO-10000`` даёт TODO-10000, а не TODO-1000
- test_worker_title_four_digit_ids_unchanged — 4-значные ID как раньше
- test_worker_title_ten_thousand_and_ten_hundred_stay_apart —
  TODO-1000 и TODO-10000 — независимые юниты
- test_commit_subject_four_digit_ids_unchanged — 4-значные subject как раньше
- test_commit_subject_word_suffix_is_not_a_todo — ``TODO-10000x`` не
  читается ни как TODO-10000, ни как TODO-1000
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from awf.metrics import collect_commit_info, collect_metrics, collect_workers

_SESSION_SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    title TEXT,
    directory TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    tokens_cache_read INTEGER,
    tokens_cache_write INTEGER,
    cost REAL,
    time_created INTEGER,
    time_updated INTEGER
)
"""


def _worker_db(titles: list[tuple[str, str, int]]) -> sqlite3.Connection:
    """In-memory opencode.db: session-строки (id, title, tokens_input) + пустая part."""
    con = sqlite3.connect(":memory:")
    con.execute(_SESSION_SCHEMA)
    con.execute("CREATE TABLE part (session_id TEXT, data TEXT)")
    con.executemany(
        "INSERT INTO session (id, title, directory, tokens_input, tokens_output,"
        " tokens_cache_read, tokens_cache_write, cost, time_created, time_updated)"
        " VALUES (?, ?, '', ?, 0, 0, 0, 0.0, 1000, 2000)",
        [(sid, title, tin) for sid, title, tin in titles],
    )
    con.commit()
    return con


def _commit(repo, subject: str) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", subject],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_worker_title_matches_five_digit_todo():
    """A-10.1: заголовок ``awf-developer-TODO-10000`` — это юнит TODO-10000."""
    con = _worker_db([("s-1", "awf-developer-TODO-10000", 10)])
    workers = collect_workers(con, None, None, None, [])
    assert "TODO-10000" in workers, (
        f"TODO-10000 потерялся из worker totals: {sorted(workers)}"
    )
    assert workers["TODO-10000"]["in"] == 10


def test_commit_subject_does_not_truncate_10000_to_1000(tmp_git_repo):
    """A-10.1: subject TODO-10000 не усекается до TODO-1000."""
    _commit(tmp_git_repo, "awf(verify): TODO-10000")
    commits = collect_commit_info(tmp_git_repo, [])
    assert "TODO-10000" in commits, (
        f"коммит приписан неверному юниту: {sorted(commits)}"
    )
    assert "TODO-1000" not in commits


def test_worker_title_four_digit_ids_unchanged():
    """A-10.3: 4-значные ID в заголовках — поведение без изменений."""
    con = _worker_db(
        [
            ("s-1", "awf-developer-TODO-0001", 7),
            ("s-2", "awf-qa-TODO-9999", 4),
        ]
    )
    workers = collect_workers(con, None, None, None, [])
    assert set(workers) == {"TODO-0001", "TODO-9999"}
    assert workers["TODO-0001"]["in"] == 7
    assert workers["TODO-9999"]["in"] == 4


def test_worker_title_ten_thousand_and_ten_hundred_stay_apart():
    """A-10.2: TODO-1000 и TODO-10000 — независимые worker-юниты."""
    con = _worker_db(
        [
            ("s-1", "awf-developer-TODO-1000", 1),
            ("s-2", "awf-developer-TODO-10000", 2),
        ]
    )
    workers = collect_workers(con, None, None, None, [])
    assert set(workers) == {"TODO-1000", "TODO-10000"}, sorted(workers)
    assert workers["TODO-1000"]["in"] == 1
    assert workers["TODO-10000"]["in"] == 2


def test_commit_subject_four_digit_ids_unchanged(tmp_git_repo):
    """A-10.3: 4-значные subject — поведение без изменений."""
    _commit(tmp_git_repo, "awf(verify): TODO-0001")
    _commit(tmp_git_repo, "awf(verify): TODO-9999")
    commits = collect_commit_info(tmp_git_repo, [])
    assert set(commits) == {"TODO-0001", "TODO-9999"}


def test_commit_subject_word_suffix_is_not_a_todo(tmp_git_repo):
    """A-10.1 (граница): ``TODO-10000x`` — не TODO ID вовсе."""
    _commit(tmp_git_repo, "awf(verify): TODO-10000x")
    commits = collect_commit_info(tmp_git_repo, [])
    assert commits == {}


# ── Сквозная атрибуция (добор 26.09, TODO-0117): collect_metrics целиком ────


def _worker_db_file(db_path: Path, repo: Path, titles: list[tuple[str, str, int]]) -> None:
    """Временная SQLite из схемы кода: session-строки + part, чьё
    содержимое называет путь проекта (content-область отчёта, RUN10 #3)."""
    con = sqlite3.connect(db_path)
    con.execute(_SESSION_SCHEMA)
    con.execute("CREATE TABLE part (session_id TEXT, data TEXT)")
    repo_s = str(Path(repo).resolve())
    for sid, title, tin in titles:
        con.execute(
            "INSERT INTO session (id, title, directory, tokens_input,"
            " tokens_output, tokens_cache_read, tokens_cache_write, cost,"
            " time_created, time_updated) VALUES (?, ?, '', ?, 0, 0, 0, 0.0,"
            " 1000, 2000)",
            (sid, title, tin),
        )
        con.execute(
            "INSERT INTO part (session_id, data) VALUES (?, ?)",
            (sid, json.dumps({"type": "text", "text": f"workdir: {repo_s}"})),
        )
    con.commit()
    con.close()


def _commit_file(repo: Path, name: str, lines: int, subject: str) -> None:
    (repo / name).write_text(
        "".join(f"line {i}\n" for i in range(1, lines + 1)), encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", subject], cwd=repo, check=True)


def test_metrics_e2e_ten_thousand_and_ten_hundred_stay_apart(tmp_git_repo, tmp_path):
    """A-10 (сквозная атрибуция): collect_metrics на синтетических данных
    (временная SQLite + два коммита) — worker-сессии, коммиты и строки
    кода TODO-1000 и TODO-10000 независимы: не слипаются, не усекаются."""
    repo = tmp_git_repo
    _commit_file(repo, "f1000.txt", 3, "awf(verify): TODO-1000")
    _commit_file(repo, "f10000.txt", 5, "awf(verify): TODO-10000")

    db = tmp_path / "opencode.db"
    _worker_db_file(
        db,
        repo,
        [
            ("s-1000", "awf-developer-TODO-1000", 100),
            ("s-10000", "awf-developer-TODO-10000", 200),
        ],
    )

    result = collect_metrics(
        repo,
        db_path=db,
        models_path=tmp_path / "models.json",  # отсутствует — цена неизвестна
        out=tmp_path / "report.md",
        mirror=False,
    )

    units = {u["todo"]: u for u in result.units}
    assert set(units) == {"TODO-1000", "TODO-10000"}, (
        f"юниты слиплись или потерялись: {sorted(units)}"
    )
    # worker-сессии: токены каждого юнита — только свои
    assert units["TODO-1000"]["sessions"] == 1
    assert units["TODO-1000"]["w_in"] == 100, (
        f"токены TODO-10000 слиплись с TODO-1000: {units['TODO-1000']['w_in']}"
    )
    assert units["TODO-10000"]["sessions"] == 1
    assert units["TODO-10000"]["w_in"] == 200
    # строки кода: diff каждого коммита — только своему юниту
    assert units["TODO-1000"]["ins"] == 3 and units["TODO-1000"]["dels"] == 0, (
        f"строки TODO-10000 приписаны TODO-1000: {units['TODO-1000']}"
    )
    assert units["TODO-10000"]["ins"] == 5 and units["TODO-10000"]["dels"] == 0, (
        f"строки TODO-10000 усекаются или теряются: {units['TODO-10000']}"
    )
