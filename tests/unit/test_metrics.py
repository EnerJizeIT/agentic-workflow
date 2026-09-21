"""Unit tests for awf.metrics (U8: awf metrics).

Синтетическая opencode.db (sqlite session/message/part) + временный git-репо
+ models.json → отчёт, ИТОГО-блок, конверсия с точной математикой,
дефолтный вывод на «рабочий стол», деградация без базы/models.json.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from conftest import _git_init

from awf import cli
from awf import metrics as M

MODELS = {
    "anthropic": {
        "models": {
            "claude-sonnet-4-6": {
                "cost": {
                    "input": 3,
                    "output": 15,
                    "cache_read": 0.3,
                    "cache_write": 3.75,
                }
            }
        }
    }
}

# База в прошлом (2 дня назад) — окна юнитов не перескакивают через «сейчас».
T0 = int(time.time()) - 2 * 86400


def _ms(sec: int) -> int:
    return sec * 1000


def make_db(path: Path, *, supervisor_title: str = "Audit supervisor session") -> Path:
    """Синтетическая база: 2 юнита, 3 воркерские сессии, 1 супервизор."""
    con = sqlite3.connect(path)
    con.executescript(
        """
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
    )
    proj_dir = "/proj/test"

    def sess(sid, title, tin, tout, tcr, tcw, cost, tc):
        con.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, title, proj_dir, cost, tin, tout, tcr, tcw, tc, tc + 60_000),
        )

    # TODO-0001: две сессии (implementer + qa-review)
    sess("s1", "awf-agent-implementer-TODO-0001", 1_000_000, 100_000, 2_000_000, 100_000, 0.0, _ms(T0))
    sess("s2", "awf-agent-qa-review-TODO-0001", 500_000, 50_000, 1_000_000, 0, 0.25, _ms(T0 + 1800))
    # TODO-0002: одна сессия
    sess("s3", "awf-agent-implementer-TODO-0002", 100_000, 10_000, 0, 0, 0.0, _ms(T0 + 4000))
    # супервизор + сессия, не похожая ни на воркера, ни на супервизора —
    # не должна попасть ни в какую статистику
    sess("sup", supervisor_title, 0, 0, 0, 0, 0.0, _ms(T0))
    sess("other", "awf-supervisor-plan", 9_999_999, 9_999, 0, 0, 9.9, _ms(T0 + 9000))

    def msg(mid, sid, ts, data):
        con.execute("INSERT INTO message VALUES (?,?,?,?)", (mid, sid, ts, data))

    msg("m1", "sup", _ms(T0 + 1000), json.dumps(
        {"tokens": {"input": 1000, "output": 100, "cache": {"read": 2000, "write": 50}}, "cost": 0.01}
    ))
    msg("m2", "sup", _ms(T0 + 9000), json.dumps(
        {"tokens": {"input": 3000, "output": 300, "cache": {"read": 0, "write": 0}}, "cost": 0.03}
    ))
    msg("m3", "sup", _ms(T0 + 1500), "not-json{{{")

    con.execute("INSERT INTO part VALUES (?,?,?,?,?)", ("p1", "m1", "s1", _ms(T0), json.dumps({"type": "compaction"})))
    con.execute("INSERT INTO part VALUES (?,?,?,?,?)", ("p2", "m1", "s1", _ms(T0), json.dumps({"type": "compaction"})))
    con.execute("INSERT INTO part VALUES (?,?,?,?,?)", ("p3", "m1", "s2", _ms(T0), json.dumps({"type": "compaction"})))
    con.execute("INSERT INTO part VALUES (?,?,?,?,?)", ("p4", "m1", "s2", _ms(T0), json.dumps({"type": "text"})))
    con.commit()
    con.close()
    return path


def make_repo(proj: Path) -> None:
    """Git-репо: baseline-файлы + verify-коммиты с известными датами и diff'ами."""
    _git_init(proj)
    ctx = proj / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: test-project\n"
        "metrics:\n"
        "  supervisor_titles:\n"
        "    - Audit supervisor session\n"
        "  reference_model: anthropic/claude-sonnet-4-6\n"
    )
    b1 = ctx / "BASELINE-TODO-0001.sha"
    b2 = ctx / "BASELINE-TODO-0002.sha"
    b1.write_text("deadbeef")
    b2.write_text("deadbeef")
    os.utime(b1, (T0 - 60, T0 - 60))
    os.utime(b2, (T0 + 3900, T0 + 3900))

    def commit(subject, path, content, when):
        f = proj / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        env = dict(os.environ)
        env["GIT_COMMITTER_DATE"] = f"@{when} +0000"
        env["GIT_AUTHOR_DATE"] = f"@{when} +0000"
        subprocess.run(["git", "add", str(f)], cwd=proj, check=True)
        subprocess.run(["git", "commit", "-qm", subject], cwd=proj, check=True, env=env)

    commit("awf(verify): TODO-0001", "a.py", "a = 1\n" * 10, T0 + 3600)
    commit("awf(verify): TODO-0002", "b.py", "b = 2\n" * 4, T0 + 5000)
    # не-verify коммит с TODO в субъекте — не должен попасть в статистику
    commit("docs: note about TODO-0001", "c.txt", "c\n" * 7, T0 + 6000)


def make_models(path: Path, data=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data if data is not None else MODELS))
    return path


@pytest.fixture
def env(tmp_path: Path):
    """proj (git+конфиг) + db + models; пути передаёт в тесты."""
    proj = tmp_path / "proj"
    proj.mkdir()
    make_repo(proj)
    db = make_db(tmp_path / "opencode.db")
    models = make_models(tmp_path / "models.json")
    return {"proj": proj, "db": db, "models": models, "tmp": tmp_path}


class TestCollect:
    def test_report_units_totals_conversion(self, env):
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "report.md",
        )
        assert res.exit_code == 0
        assert res.measured
        assert res.report_path == str(env["tmp"] / "report.md")
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert "| TODO-0001 |" in text
        assert "| TODO-0002 |" in text
        assert "## ИТОГО — отдельно (группировка по ролям, не по юнитам)" in text
        assert "Если бы воркеры работали на anthropic/claude-sonnet-4-6" in text

        u1 = next(u for u in res.units if u["todo"] == "TODO-0001")
        assert u1["sessions"] == 2
        assert u1["w_in"] == 1_500_000
        assert u1["w_out"] == 150_000
        assert u1["w_cr"] == 3_000_000
        assert u1["compactions"] == 3
        assert u1["ins"] == 10
        # супервизор: m1 в окне 0001 (1000/100), m2 вне всех окон
        assert u1["s_in"] == 1000
        assert u1["s_out"] == 100
        assert u1["s_cost"] == pytest.approx(0.01)
        u2 = next(u for u in res.units if u["todo"] == "TODO-0002")
        assert u2["w_in"] == 100_000
        assert u2["ins"] == 4
        assert u2["s_in"] == 0

        t = res.totals
        assert t["win"] == 1_600_000
        assert t["wout"] == 160_000
        assert t["wcr"] == 3_000_000
        assert t["wcw"] == 100_000
        assert t["comp"] == 3
        assert t["ins"] == 14
        assert res.supervisor_outside["in"] == 3000
        assert res.supervisor_outside["cost"] == pytest.approx(0.03)

        # точная математика конверсии: за 1M токенов
        conv = res.conversion
        assert conv["known"] is True
        assert conv["input_cost"] == pytest.approx(1_600_000 * 3 / 1e6)
        assert conv["output_cost"] == pytest.approx(160_000 * 15 / 1e6)
        assert conv["cache_cost"] == pytest.approx(
            3_000_000 * 0.3 / 1e6 + 100_000 * 3.75 / 1e6
        )
        assert conv["total"] == pytest.approx(
            1_600_000 * 3 / 1e6 + 160_000 * 15 / 1e6
            + 3_000_000 * 0.3 / 1e6 + 100_000 * 3.75 / 1e6,
            abs=1e-9,
        )
        # битая строка message.data пропущена с предупреждением; сессия
        # «awf-supervisor-plan» (9.9M токенов) не попала ни в воркеры,
        # ни в супервизора — её токенов нет ни в t, ни в sup
        assert any("битая строка" in w for w in res.warnings)
        assert t["wout"] == 160_000

    def test_workers_by_role(self, env):
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r.md",
        )
        impl = res.workers_by_role["agent-implementer"]
        assert impl["sessions"] == 2  # s1 + s3
        assert impl["in"] == 1_100_000
        qa = res.workers_by_role["agent-qa-review"]
        assert qa["sessions"] == 1
        assert qa["in"] == 500_000

    def test_reference_model_override(self, env):
        # другой моделью: те же токены, другие цены
        make_models(env["tmp"] / "m2.json", {
            "openai": {"models": {"gpt-x": {"cost": {
                "input": 1, "output": 2, "cache_read": 0, "cache_write": 0}}}}
        })
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["tmp"] / "m2.json",
            reference_model="openai/gpt-x", out=env["tmp"] / "r.md",
        )
        assert res.conversion["total"] == pytest.approx(1_600_000 / 1e6 + 160_000 * 2 / 1e6)
        assert "openai/gpt-x" in res.conversion["line"]


class TestOutput:
    def test_default_output_home_desktop(self, env, monkeypatch):
        home = env["tmp"] / "home"
        (home / "Desktop").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        res = M.collect_metrics(env["proj"], db_path=env["db"], models_path=env["models"])
        assert res.report_path is not None
        p = Path(res.report_path)
        assert p.parent == home / "Desktop"
        assert p.name.startswith("awf-metrics-") and p.name.endswith(".md")
        assert p.exists()

    def test_out_dir_and_out_file(self, env):
        d = env["tmp"] / "outdir"
        d.mkdir()
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"], out=str(d)
        )
        assert Path(res.report_path).parent == d

        f = env["tmp"] / "explicit.md"
        res2 = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"], out=str(f)
        )
        assert res2.report_path == str(f)
        assert f.exists()

    def test_output_dir_falls_back_to_project(self, env, monkeypatch):
        home = env["tmp"] / "home"  # без Desktop
        home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"]
        )
        assert Path(res.report_path).parent == env["proj"]


class TestDegradation:
    def test_no_models_json_price_unknown(self, env):
        res = M.collect_metrics(
            env["proj"], db_path=env["db"],
            models_path=env["tmp"] / "absent.json",
            out=env["tmp"] / "r.md",
        )
        assert res.exit_code == 0  # воркеры всё ещё измерены
        assert res.conversion["known"] is False
        assert "стоимость неизвестна" in res.conversion["line"]
        assert Path(res.report_path).exists()

    def test_no_db_no_commits_nothing_measurable(self, env):
        proj = env["tmp"] / "empty"
        proj.mkdir()
        _git_init(proj)  # только init-коммит, без verify-коммитов
        (proj / ".agentic" / "context").mkdir(parents=True)
        res = M.collect_metrics(
            proj, db_path=env["tmp"] / "absent.db",
            models_path=env["models"], out=env["tmp"] / "r.md",
        )
        assert res.exit_code == 1
        assert res.measured is False
        assert any("база opencode не найдена" in w for w in res.warnings)
        assert Path(res.report_path).exists()  # отчёт пишется с пометками

    def test_no_db_but_commits_measured(self, env):
        res = M.collect_metrics(
            env["proj"], db_path=env["tmp"] / "absent.db",
            models_path=env["models"], out=env["tmp"] / "r.md",
        )
        assert res.exit_code == 0
        assert res.totals["ins"] == 14  # строки кода без базы

    def test_broken_models_json(self, env):
        bad = env["tmp"] / "bad.json"
        bad.write_text("{not json")
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=bad, out=env["tmp"] / "r.md"
        )
        assert res.conversion["known"] is False
        assert res.exit_code == 0

    def test_reference_model_missing(self, env):
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            reference_model="nope/missing-1", out=env["tmp"] / "r.md",
        )
        assert res.conversion["known"] is False
        assert any("не найдена" in w for w in res.warnings)


class TestFilters:
    def test_since_filter(self, env):
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            since=str(T0 + 3900), out=env["tmp"] / "r.md",
        )
        worker_todos = {
            u["todo"] for u in res.units if u["sessions"] > 0
        }
        assert worker_todos == {"TODO-0002"}  # s1/s2 до since, s3 после

    def test_directory_filter_from_config(self, env):
        cfg = env["proj"] / ".agentic" / "config.yaml"
        cfg.write_text(cfg.read_text() + "  directory: /other/proj\n")
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r.md",
        )
        assert all(u["sessions"] == 0 for u in res.units)
        assert res.totals["win"] == 0
        # коммиты при этом измерены
        assert res.totals["ins"] == 14

    def test_parse_since(self):
        w = []
        assert M.parse_since(None, w) is None
        assert M.parse_since("", w) is None
        ms = M.parse_since(T0, w)  # epoch секунд
        assert ms == T0 * 1000
        assert M.parse_since(T0 * 1000, w) == T0 * 1000  # epoch ms
        d = M.parse_since("2020-01-01", w)
        assert d == int(
            __import__("datetime").datetime(2020, 1, 1).timestamp() * 1000
        )
        assert M.parse_since("garbage", w) is None
        assert any("не распознано" in x for x in w)


class TestCli:
    def _fake_home(self, tmp_path: Path, db: Path, models: Path) -> Path:
        # Path.home() вызывается несколько раз за прогон — метод идемпотентен.
        home = tmp_path / "home"
        (home / ".local" / "share" / "opencode").mkdir(parents=True, exist_ok=True)
        (home / ".cache" / "opencode").mkdir(parents=True, exist_ok=True)
        (home / ".local" / "share" / "opencode" / "opencode.db").write_bytes(db.read_bytes())
        (home / ".cache" / "opencode" / "models.json").write_bytes(models.read_bytes())
        (home / "Desktop").mkdir(exist_ok=True)
        return home

    def test_cli_json(self, env, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: self._fake_home(env["tmp"], env["db"], env["models"])))
        rc = cli.main(["metrics", "--project-dir", str(env["proj"]), "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["exit_code"] == 0
        assert data["totals"]["win"] == 1_600_000
        assert data["conversion"]["known"] is True
        assert data["report_path"].startswith(str(env["tmp"] / "home" / "Desktop"))

    def test_cli_default_report_to_desktop(self, env, monkeypatch, capsys):
        home = self._fake_home(env["tmp"], env["db"], env["models"])
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        rc = cli.main(["metrics", "--project-dir", str(env["proj"])])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Отчёт: " in out
        assert "Если бы воркеры работали на" in out
        reports = list((home / "Desktop").glob("awf-metrics-*.md"))
        assert len(reports) == 1

    def test_cli_rc1_when_nothing_measurable(self, env, monkeypatch):
        # Пустая база (только схемы) + репо без verify-коммитов → rc 1.
        empty_db = env["tmp"] / "empty.db"
        con = sqlite3.connect(empty_db)
        con.executescript(
            "CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT,"
            " cost REAL, tokens_input INTEGER, tokens_output INTEGER,"
            " tokens_cache_read INTEGER, tokens_cache_write INTEGER,"
            " time_created INTEGER, time_updated INTEGER);"
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,"
            " time_created INTEGER, data TEXT);"
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT,"
            " session_id TEXT, time_created INTEGER, data TEXT);"
        )
        con.commit()
        con.close()
        proj = env["tmp"] / "empty"
        proj.mkdir()
        _git_init(proj)
        (proj / ".agentic" / "context").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: self._fake_home(env["tmp"], empty_db, env["models"])))
        rc = cli.main(["metrics", "--project-dir", str(proj), "--out", str(env["tmp"] / "r.md")])
        assert rc == 1
