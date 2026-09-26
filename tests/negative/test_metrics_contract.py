"""Контракт метрик (docs/contracts/metrics.md).

Фиксирует фактическое поведение `awf metrics` (awf/metrics.py):
- битые входы (нет базы, пустая база, мусор в файле базы, путь-каталог,
  project_dir без git) — предупреждение и деградация, не падение сбора;
- область отчёта по умолчанию — только текущий проект: дискриминатор —
  путь проекта в part.data (session.directory — не дискриминатор);
  одинаковый номер TODO в двух проектах не смешивается;
  --all-projects — единственное исключение;
- окна юнитов: сообщение в пересечении двух окон приписывается ровно
  одному; конец окна не улетает в будущее (обрезка по now);
- нулевые токены/компрессии — нули в отчёте, сбор измерен;
- парсер ID юнита (A-10): 4+ цифр с явной правой границей;
- commit-гейт (A-17): `awf(<stage>): ...` — любая стадия.

Все входы — временные: SQLite со схемой кода (session/message/part) и
временные каталоги; реальная opencode.db пользователя не читается.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from awf import metrics as M

_SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, title TEXT NOT NULL,
    directory TEXT NOT NULL DEFAULT '',
    cost REAL DEFAULT 0 NOT NULL,
    tokens_input INTEGER DEFAULT 0, tokens_output INTEGER DEFAULT 0,
    tokens_cache_read INTEGER DEFAULT 0, tokens_cache_write INTEGER DEFAULT 0,
    time_created INTEGER, time_updated INTEGER
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    time_created INTEGER, data TEXT NOT NULL
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
    session_id TEXT NOT NULL, time_created INTEGER, data TEXT NOT NULL
);
"""

# База в прошлом (день назад) — окна юнитов не перескакивают через «сейчас».
T0 = int(time.time()) - 86400


def _conn(path=None) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:" if path is None else str(path))
    con.executescript(_SCHEMA)
    return con


def _sess(con: sqlite3.Connection, sid: str, title: str, tin: int = 0) -> None:
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, title, "", 0.0, tin, 0, 0, 0, T0 * 1000, (T0 + 60) * 1000),
    )


def _part(con: sqlite3.Connection, pid: str, sid: str, text: str) -> None:
    con.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        (pid, "m" + pid, sid, T0 * 1000, json.dumps({"type": "text", "text": text})),
    )


def _msg(con: sqlite3.Connection, mid: str, sid: str, ts_s: int, tin: int) -> None:
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        (
            mid,
            sid,
            ts_s * 1000,
            json.dumps(
                {"tokens": {"input": tin, "output": 0, "cache": {"read": 0, "write": 0}},
                 "cost": 0.0}
            ),
        ),
    )


def make_db(path: Path, sessions: list[tuple]) -> Path:
    """Временная база: sessions — список (sid, title, tokens_input, [тексты part])."""
    con = _conn(path)
    for sid, title, tin, parts in sessions:
        _sess(con, sid, title, tin)
        for i, text in enumerate(parts):
            _part(con, f"{sid}p{i}", sid, text)
    con.commit()
    con.close()
    return path


def _run(tmp_path: Path, proj: Path, sessions: list[tuple] = (), **kw):
    """collect_metrics на временных входах: proj (без git), временная база."""
    db = make_db(tmp_path / (proj.name + ".db"), sessions)
    return M.collect_metrics(
        proj,
        db_path=db,
        models_path=tmp_path / "missing-models.json",
        out=tmp_path / (proj.name + "-report.md"),
        **kw,
    )


class TestBrokenInputs:
    """Битый вход — предупреждение и деградация, не падение сбора."""

    def test_missing_db_warns_and_degrades(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        res = M.collect_metrics(
            proj,
            db_path=tmp_path / "missing.db",
            models_path=tmp_path / "missing.json",
            out=tmp_path / "report.md",
        )
        assert res.exit_code == 1
        assert res.units == []
        assert any("база opencode не найдена" in w for w in res.warnings)
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert "ПРЕДУПРЕЖДЕНИЕ" in text

    def test_empty_db_no_sessions(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        res = _run(tmp_path, proj)
        assert res.units == []
        assert res.exit_code == 1
        assert any("не удалось сопоставить сессии" in w for w in res.warnings)

    def test_broken_db_warns_and_degrades(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        db = tmp_path / "bad.db"
        db.write_bytes(b"this is not a sqlite database, just garbage bytes")
        res = M.collect_metrics(
            proj,
            db_path=db,
            models_path=tmp_path / "missing.json",
            out=tmp_path / "report.md",
        )
        assert res.units == []
        assert res.exit_code == 1
        assert any("упал" in w for w in res.warnings)

    def test_db_path_is_directory_warns(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        dbdir = tmp_path / "dbdir"
        dbdir.mkdir()
        res = M.collect_metrics(
            proj,
            db_path=dbdir,
            models_path=tmp_path / "missing.json",
            out=tmp_path / "report.md",
        )
        assert res.units == []
        assert res.exit_code == 1
        assert any("база opencode не открывается" in w for w in res.warnings)

    def test_project_dir_without_git_warns(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        sessions = [("s1", "awf-developer-TODO-0001", 100, [str(proj) + "/src"])]
        res = _run(tmp_path, proj, sessions)
        assert any("git-репозиторий не найден" in w for w in res.warnings)
        assert res.exit_code == 0
        u = {x["todo"]: x for x in res.units}["TODO-0001"]
        assert u["w_in"] == 100
        assert u["ins"] == 0 and u["dels"] == 0


class TestReportScope:
    """Область отчёта: путь проекта в part.data, не session.directory."""

    def test_project_path_without_sessions_empty_scope(self, tmp_path):
        """Путь проекта ни в одной part.data — пустые данные + явное предупреждение."""
        proj = tmp_path / "proj"
        proj.mkdir()
        sessions = [("s1", "awf-developer-TODO-0001", 100, ["no project path here"])]
        res = _run(tmp_path, proj, sessions)
        assert res.units == []
        assert res.exit_code == 1
        assert any("не удалось сопоставить сессии" in w for w in res.warnings)

    def test_same_todo_id_two_projects_scope_isolated(self, tmp_path):
        """Один номер TODO в двух проектах — по умолчанию только текущий проект."""
        projA = tmp_path / "A"
        projB = tmp_path / "B"
        projA.mkdir()
        projB.mkdir()
        sessions = [
            ("sA", "awf-developer-TODO-0001", 100, [str(projA) + "/.agentic/config.yaml"]),
            ("sB", "awf-developer-TODO-0001", 200, [str(projB) + "/.agentic/config.yaml"]),
            ("sC", "awf-developer-TODO-0002", 300, ["session without a project path"]),
        ]
        db = make_db(tmp_path / "shared.db", sessions)
        resA = M.collect_metrics(
            projA,
            db_path=db,
            models_path=tmp_path / "missing.json",
            out=tmp_path / "a-report.md",
        )
        units = {u["todo"]: u for u in resA.units}
        # sB (чужой проект) и sC (без пути проекта) исключены; sC не образует юнит.
        assert set(units) == {"TODO-0001"}
        assert units["TODO-0001"]["w_in"] == 100
        assert any(
            "исключены сессии других проектов" in w and "2" in w for w in resA.warnings
        )
        resAll = M.collect_metrics(
            projA,
            db_path=db,
            models_path=tmp_path / "missing.json",
            out=tmp_path / "b-report.md",
            all_projects=True,
        )
        units_all = {u["todo"]: u for u in resAll.units}
        assert units_all["TODO-0001"]["w_in"] == 300  # sA + sB смешаны
        assert units_all["TODO-0002"]["w_in"] == 300  # sC включён
        assert not any("исключены" in w for w in resAll.warnings)


class TestWindows:
    """Окна юнитов: без двойного счёта и без улёта в будущее."""

    def test_overlapping_windows_attribute_once(self, tmp_path):
        repo = tmp_path / "repo"
        ctx = repo / ".agentic" / "context"
        ctx.mkdir(parents=True)
        now_ms = 10_000_000_000_000
        baseline = ctx / "BASELINE-TODO-0002.sha"
        baseline.write_text("x")
        os.utime(baseline, (2_000_000, 2_000_000))
        commits = {"TODO-0001": [("s1", 3_000_000)]}
        windows = M.build_windows(
            repo, ["TODO-0001", "TODO-0002"],
            {"TODO-0001": 1_000_000_000}, commits, now_ms,
        )
        w1, w2 = windows["TODO-0001"], windows["TODO-0002"]
        assert w1[1] > w2[0], "окна не пересекаются — тест невалиден"
        con = _conn()
        _sess(con, "sup", "sup")
        _msg(con, "m1", "sup", 2_500_000, 100)  # в пересечении обоих окон
        _msg(con, "m2", "sup", 500_000, 7)      # до всех окон — outside
        con.commit()
        per_todo, outside, _ = M.collect_supervisor(
            con, ["sup"], None, None, None, windows, []
        )
        assert per_todo["TODO-0002"]["in"] == 100  # позже начавшееся окно
        assert per_todo["TODO-0001"]["in"] == 0
        assert outside["in"] == 7
        placed = (
            per_todo["TODO-0001"]["in"] + per_todo["TODO-0002"]["in"] + outside["in"]
        )
        assert placed == 107, "токены посчитаны дважды или потеряны"

    def test_window_end_clamped_to_now(self, tmp_path):
        now_ms = 5_000_000_000_000
        commits = {"TODO-0001": [("s1", 9_999_999_999)]}  # коммит «в будущем»
        windows = M.build_windows(
            tmp_path, ["TODO-0001"],
            {"TODO-0001": 1_000_000_000}, commits, now_ms,
        )
        start, end = windows["TODO-0001"]
        assert start == 1_000_000_000
        assert end == now_ms, "конец окна улетел в будущее"


class TestZeroValues:
    def test_zero_tokens_and_compactions_exit_zero(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        sessions = [("s1", "awf-developer-TODO-0001", 0, [str(proj)])]
        res = _run(tmp_path, proj, sessions)
        assert res.measured and res.exit_code == 0
        u = {x["todo"]: x for x in res.units}["TODO-0001"]
        assert u["w_in"] == 0 and u["w_out"] == 0 and u["w_cr"] == 0
        assert u["compactions"] == 0 and u["sessions"] == 1
        assert res.totals["win"] == 0 and res.totals["comp"] == 0
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert "| TODO-0001 |" in text


class TestIdParserA10:
    """A-10: один парсер ID, 4+ цифр, явная правая граница."""

    def test_five_digit_not_truncated(self):
        assert M._todo_id_from_subject("awf(verify): TODO-10000") == "TODO-10000"
        assert M._todo_id_from_subject("fix TODO-1000 bug") == "TODO-1000"

    def test_word_suffix_is_not_id(self):
        assert M._todo_id_from_subject("awf(verify): TODO-10000x") is None
        assert M._todo_id_from_title("awf-dev-TODO-999") is None
        assert M._todo_id_from_title("awf-dev-TODO-10000") == "TODO-10000"


class TestStageAgnosticCommitA17:
    """A-17: commit-гейт `awf(<stage>): TODO-NNNN` — любая стадия."""

    def test_any_stage_recognized(self):
        for stage in ("verify", "execute", "qa-review", "custom-stage-x"):
            assert M._todo_id_from_stage_commit(f"awf({stage}): TODO-0001") == "TODO-0001"

    def test_without_awf_prefix_not_recognized(self):
        assert M._todo_id_from_stage_commit("fix: TODO-0001") is None
        assert M._todo_id_from_stage_commit("TODO-0001") is None
        assert M._todo_id_from_stage_commit(None) is None
