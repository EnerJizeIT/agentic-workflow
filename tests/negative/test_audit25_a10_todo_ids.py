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

import sqlite3
import subprocess

from awf.metrics import collect_commit_info, collect_workers

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
