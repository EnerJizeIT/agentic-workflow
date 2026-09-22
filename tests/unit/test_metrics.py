"""Unit tests for awf.metrics (U8: awf metrics).

Синтетическая opencode.db (sqlite session/message/part) + временный git-репо
+ models.json → отчёт, ИТОГО-блок, конверсия с точной математикой,
дефолтный вывод на «рабочий стол», деградация без базы/models.json.
"""
from __future__ import annotations

import json
import os
import re
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


# U8b: каталог с 8 моделями дефолтных списков metrics.tiers (простые цены).
TIERS_MODELS = {
    "anthropic": {
        "models": {
            "claude-fable-5": {
                "cost": {"input": 1, "output": 2, "cache_read": 0.1, "cache_write": 0.5}
            },
            "claude-opus-5": {
                "cost": {"input": 2, "output": 4, "cache_read": 0.2, "cache_write": 1}
            },
            "claude-sonnet-5": {
                "cost": {"input": 3, "output": 6, "cache_read": 0.3, "cache_write": 1.5}
            },
            "claude-sonnet-4-6": {
                "cost": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}
            },
        }
    },
    "openai": {
        "models": {
            "gpt-6-astra": {
                "cost": {"input": 0.5, "output": 1, "cache_read": 0.05, "cache_write": 0.25}
            },
            "gpt-5.6-terra": {
                "cost": {"input": 4, "output": 8, "cache_read": 0.4, "cache_write": 2}
            },
        }
    },
    "google": {
        "models": {
            "gemini-3.1-pro-preview": {
                "cost": {"input": 5, "output": 10, "cache_read": 0.5}
            }
        }
    },
    "opencode": {
        "models": {
            "qwen3.8-flash": {
                "cost": {"input": 0.1, "output": 0.2, "cache_read": 0.01, "cache_write": 0.05}
            },
            "deepseek-v4.1-flash": {
                "cost": {"input": 0.2, "output": 0.4, "cache_read": 0.02, "cache_write": 0.1}
            },
        }
    },
}


def _t(price: dict, win=1_600_000, wout=160_000, wcr=3_000_000, wcw=100_000) -> float:
    """Точный итог $: токены фикстуры × цены ÷ 1e6 (как в реализации)."""
    return (
        win * price["input"] / 1e6
        + wout * price["output"] / 1e6
        + wcr * price.get("cache_read", 0) / 1e6
        + wcw * price.get("cache_write", 0) / 1e6
    )


def _cr_share(price: dict, total: float) -> float:
    return (3_000_000 * price.get("cache_read", 0) / 1e6) / total * 100.0


def _set_metrics_cfg(env, extra: str) -> None:
    cfg = env["proj"] / ".agentic" / "config.yaml"
    cfg.write_text(
        "project:\n  name: test-project\n"
        "metrics:\n"
        "  supervisor_titles:\n"
        "    - Audit supervisor session\n"
        "  reference_model: anthropic/claude-sonnet-4-6\n"
        + extra
    )


class TestU8bTiers:
    def test_tier_tables_math_sort_bold(self, env):
        models = make_models(env["tmp"] / "tiers.json", TIERS_MODELS)
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=models,
            out=env["tmp"] / "r.md",
        )
        text = Path(res.report_path).read_text(encoding="utf-8")

        tt = res.tier_tables
        # топ-3: сортировка по итогу, рост
        top = tt["top"]
        assert [r["model"] for r in top] == [
            "openai/gpt-6-astra", "anthropic/claude-fable-5", "anthropic/claude-opus-5"
        ]
        astra_p = TIERS_MODELS["openai"]["models"]["gpt-6-astra"]["cost"]
        fable_p = TIERS_MODELS["anthropic"]["models"]["claude-fable-5"]["cost"]
        opus_p = TIERS_MODELS["anthropic"]["models"]["claude-opus-5"]["cost"]
        astra_t, fable_t, opus_t = _t(astra_p), _t(fable_p), _t(opus_p)
        assert top[0]["total"] == pytest.approx(astra_t)
        assert top[1]["total"] == pytest.approx(fable_t)
        assert top[2]["total"] == pytest.approx(opus_t)
        assert top[0]["cache_read_share"] == pytest.approx(_cr_share(astra_p, astra_t))

        # оптимум-3
        opt = tt["optimal"]
        sonnet_p = TIERS_MODELS["anthropic"]["models"]["claude-sonnet-5"]["cost"]
        terra_p = TIERS_MODELS["openai"]["models"]["gpt-5.6-terra"]["cost"]
        gemini_p = TIERS_MODELS["google"]["models"]["gemini-3.1-pro-preview"]["cost"]
        assert [r["model"] for r in opt] == [
            "anthropic/claude-sonnet-5", "openai/gpt-5.6-terra", "google/gemini-3.1-pro-preview"
        ]
        assert opt[0]["total"] == pytest.approx(_t(sonnet_p))
        assert opt[1]["total"] == pytest.approx(_t(terra_p))
        assert opt[2]["total"] == pytest.approx(_t(gemini_p))

        # разметка: минимум жирным, остальные — нет
        assert f"**${astra_t:.2f}**" in text
        assert f"**${fable_t:.2f}**" not in text
        assert f"**${opus_t:.2f}**" not in text
        sonnet_t = _t(sonnet_p)
        assert f"**${sonnet_t:.2f}**" in text
        assert f"Лучший выбор по цене среди оптимума: anthropic/claude-sonnet-5 — **${sonnet_t:.2f}**." in text

        # дешёвые — одна строка
        qwen_p = TIERS_MODELS["opencode"]["models"]["qwen3.8-flash"]["cost"]
        deep_p = TIERS_MODELS["opencode"]["models"]["deepseek-v4.1-flash"]["cost"]
        cheap_line = next(
            ln for ln in text.splitlines() if ln.startswith("Самые дешёвые:")
        )
        assert f"opencode/qwen3.8-flash ≈ **${_t(qwen_p):.2f}**" in cheap_line
        assert f"opencode/deepseek-v4.1-flash ≈ **${_t(deep_p):.2f}**" in cheap_line

    def test_missing_model_price_unknown(self, env):
        models = make_models(env["tmp"] / "tiers.json", TIERS_MODELS)
        _set_metrics_cfg(env, "  tiers:\n    top:\n      - nope/ghost-1\n")
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=models,
            out=env["tmp"] / "r.md",
        )
        text = Path(res.report_path).read_text(encoding="utf-8")
        top = res.tier_tables["top"]
        assert [r["model"] for r in top] == ["nope/ghost-1"]
        assert top[0]["known"] is False
        assert "| nope/ghost-1 | — | — | — | цена неизвестна | — |" in text
        # дефолтный оптимум при этом рассчитан
        assert any(r["known"] for r in res.tier_tables["optimal"])

    def test_tiers_from_config(self, env):
        models = make_models(env["tmp"] / "tiers.json", TIERS_MODELS)
        _set_metrics_cfg(env, "  tiers:\n    optimal:\n      - opencode/qwen3.8-flash\n")
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=models,
            out=env["tmp"] / "r.md",
        )
        opt = res.tier_tables["optimal"]
        assert [r["model"] for r in opt] == ["opencode/qwen3.8-flash"]
        qwen_p = TIERS_MODELS["opencode"]["models"]["qwen3.8-flash"]["cost"]
        assert opt[0]["total"] == pytest.approx(_t(qwen_p))
        # топ остался дефолтным (3 модели)
        assert len(res.tier_tables["top"]) == 3


class TestU8bSubscriptions:
    # база подписок (отклонённый REVIEW): in 1.6M + out 160k = 1.76M;
    # cache (cr 3M + cw 100k) не входит — переиспользование контекста
    USAGE = 1_600_000 + 160_000
    USAGE_INCL_CACHE = 1_600_000 + 160_000 + 3_000_000 + 100_000

    def test_builtin_message_limit_assumed(self, env):
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r.md",
        )
        s = res.subscriptions
        assert s["usage"] == self.USAGE
        assert s["usage_incl_cache"] == self.USAGE_INCL_CACHE
        pro = next(p for p in s["plans"] if p["name"] == "Claude Pro")
        assert pro["limit_tokens"] == pytest.approx(6480 * 1000)
        assert pro["subs"] == 1  # ceil(1.76M / 6.48M)
        assert pro["cost"] == pytest.approx(20)
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert "## 💳 Подписки (GPT / Claude / GLM)" in text
        vol_line = next(ln for ln in text.splitlines() if ln.startswith("Объём воркеров за окно"))
        assert "**1,760,000 токенов**" in vol_line
        assert "cache-read 3,000,000 и cache-write 100,000 не входят" in vol_line
        claude_line = next(ln for ln in text.splitlines() if ln.startswith("| Claude Pro |"))
        assert "6,480,000 токенов/мес (оценка)" in claude_line
        assert "| 0.3 | **1** |" in claude_line  # 1.76M / 6.48M = 0.27 → 0.3, купить 1
        assert "GLM Coding Plan Lite (Z.ai)" in text
        assert "GLM Coding Plan Pro (Z.ai)" in text
        assert "GLM Coding Plan Max (Z.ai)" in text
        assert "данные от 2026-09-21" in text

    def test_subscription_base_excludes_cache_read(self):
        """Страж: база подписок = in+out, кэш не входит."""
        totals = {"win": 1_600_000, "wout": 160_000, "wcr": 3_000_000, "wcw": 100_000}
        table = {
            "plans": [
                {
                    "name": "GuardPlan", "family": "gpt", "price_usd_month": 10,
                    "limit_tokens_month": 3_000_000,
                }
            ]
        }
        s = M.build_subscriptions(totals, table)
        assert s["usage"] == 1_760_000  # со старым базисом было бы 4_860_000
        assert s["usage_incl_cache"] == 4_860_000
        p = s["plans"][0]
        assert p["subs"] == 1  # ceil(1.76M / 3M); со старым базисом было бы 2
        assert p["subs_incl_cache"] == 2  # пессимистичный базис: ceil(4.86M / 3M)

    def test_token_limit_exact_math(self, env, monkeypatch):
        _set_metrics_cfg(env, "  subscriptions_url: http://fake/subs.json\n")
        mock = {
            "as_of": "2026-09-01",
            "plans": [
                {
                    "name": "TokPlan", "family": "gpt", "price_usd_month": 20,
                    "limit_tokens_month": 2_000_000,
                    "source_url": "http://fake", "as_of": "2026-09-01",
                }
            ],
        }
        monkeypatch.setattr(M, "_http_get", lambda url, timeout=10.0: json.dumps(mock).encode())
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r.md", refresh_subscriptions=True,
        )
        s = res.subscriptions
        assert s["source"] == "url"
        tok = next(p for p in s["plans"] if p["name"] == "TokPlan")
        assert tok["subs"] == 1  # ceil(1.76M / 2M) — база in+out
        assert tok["cost"] == pytest.approx(20)
        text = Path(res.report_path).read_text(encoding="utf-8")
        tok_line = next(ln for ln in text.splitlines() if ln.startswith("| TokPlan |"))
        assert "**1**" in tok_line
        assert "**$20**" in tok_line
        # успешный ответ закэширован
        cache = env["proj"] / ".agentic" / "state" / "metrics_subscriptions.json"
        assert json.loads(cache.read_text())["plans"][0]["name"] == "TokPlan"

    def test_refresh_fallback_on_network_error(self, env, monkeypatch):
        _set_metrics_cfg(env, "  subscriptions_url: http://fake/subs.json\n")

        def dead(url, timeout=10.0):
            raise OSError("network down")

        monkeypatch.setattr(M, "_http_get", dead)
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r.md", refresh_subscriptions=True,
        )
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert "обновление не удалось: OSError: network down" in text
        assert "данные от 2026-09-21" in text  # as_of встроенной таблицы
        # планы всё равно рассчитаны (встроенная таблица)
        assert len(res.subscriptions["plans"]) >= 5
        # неудачный refresh кэш не пишет
        assert not (env["proj"] / ".agentic" / "state" / "metrics_subscriptions.json").exists()
        assert res.subscriptions["refresh_error"] == "OSError: network down"

    def test_refresh_no_url_uses_builtin(self, env):
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r.md", refresh_subscriptions=True,
        )
        assert res.subscriptions["source"] == "builtin"
        assert res.subscriptions["refresh_error"] == "metrics.subscriptions_url не задан"
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert "обновление не удалось: metrics.subscriptions_url не задан" in text


class TestU8bStyle:
    """friday-style-гейт: без фраз-пустышек, с эмодзи-маркерами разделов."""

    FORBIDDEN = ("важно отметить", "следует подчеркнуть", "необходимо учитывать")

    def test_style_gate(self, env):
        models = make_models(env["tmp"] / "tiers.json", TIERS_MODELS)
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=models,
            out=env["tmp"] / "r.md",
        )
        text = Path(res.report_path).read_text(encoding="utf-8")
        for phrase in self.FORBIDDEN:
            assert phrase not in text.lower(), phrase
        for marker in ("🏆", "⚖️", "💡", "💳"):
            assert marker in text, marker
        # существующие блоки сохранены
        assert "Если бы воркеры работали на anthropic/claude-sonnet-4-6" in text
        assert "## ИТОГО — отдельно (группировка по ролям, не по юнитам)" in text


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


class TestU8cMirror:
    """U8c: metrics.mirror_dir — архивная копия отчёта."""

    def _set_mirror_dir(self, env, mirror: Path) -> None:
        cfg = env["proj"] / ".agentic" / "config.yaml"
        cfg.write_text(cfg.read_text() + f"  mirror_dir: {mirror}\n")

    def test_mirror_copies_report(self, env, tmp_path):
        mirror = tmp_path / "archive" / "reports"
        self._set_mirror_dir(env, mirror)
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=str(tmp_path / "out.md"),
        )
        assert res.report_path and Path(res.report_path).exists()
        assert res.mirror_path is not None
        dest = Path(res.mirror_path)
        assert dest.parent == mirror
        assert dest.name == Path(res.report_path).name
        assert dest.read_text(encoding="utf-8") == Path(res.report_path).read_text(encoding="utf-8")

    def test_no_mirror_dir_no_copy(self, env, tmp_path):
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=str(tmp_path / "out.md"),
        )
        assert res.report_path and Path(res.report_path).exists()
        assert res.mirror_path is None

    def test_mirror_false_skips_copy(self, env, tmp_path):
        mirror = tmp_path / "archive"
        self._set_mirror_dir(env, mirror)
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=str(tmp_path / "out.md"), mirror=False,
        )
        assert res.report_path and Path(res.report_path).exists()
        assert res.mirror_path is None
        assert not mirror.exists()

    def test_broken_mirror_dir_warns_report_written(self, env, tmp_path):
        # Каталог зеркала — на самом деле файл: mkdir падает, отчёт всё равно
        # записан, предупреждение в warnings.
        blocker = tmp_path / "blocker"
        blocker.write_text("file, not a dir")
        self._set_mirror_dir(env, blocker / "sub")
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=str(tmp_path / "out.md"),
        )
        assert res.report_path and Path(res.report_path).exists()
        assert res.mirror_path is None
        assert any("зеркало" in w for w in res.warnings)

    def test_cli_no_mirror_flag(self, env, monkeypatch, capsys):
        mirror = env["tmp"] / "archive"
        self._set_mirror_dir(env, mirror)
        home = env["tmp"] / "home"
        (home / ".local" / "share" / "opencode").mkdir(parents=True)
        (home / ".cache" / "opencode").mkdir(parents=True)
        (home / ".local" / "share" / "opencode" / "opencode.db").write_bytes(env["db"].read_bytes())
        (home / ".cache" / "opencode" / "models.json").write_bytes(env["models"].read_bytes())
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        rc = cli.main([
            "metrics", "--project-dir", str(env["proj"]),
            "--out", str(env["tmp"] / "r1.md"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Зеркало: " in out
        assert (mirror / "r1.md").exists()

        rc = cli.main([
            "metrics", "--project-dir", str(env["proj"]),
            "--no-mirror", "--out", str(env["tmp"] / "r2.md"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Зеркало: " not in out
        assert not (mirror / "r2.md").exists()


# U8d: GLM — кредитная модель (GLM-5.3: (in×6.9 + cached_in×1.7 + out×24) ÷ 10 000),
# cache-read входит в кредиты; off-peak (вне Пн–Пт 14:00–18:00 SGT) ×0.5.
GLM_TOTALS = {"win": 52_000_000, "wout": 6_000_000, "wcr": 0, "wcw": 0}
GLM_CREDITS = (52_000_000 * 6.9 + 6_000_000 * 24) / 10_000  # 50 280


def _glm_plans() -> list[dict]:
    mult = {"input": 6.9, "cached_input": 1.7, "output": 24}
    return [
        {
            "name": "GLM Coding Plan Lite (Z.ai)",
            "family": "glm",
            "price_usd_month": 18,
            "limit_model": "credits",
            "credits_5h": 2000,
            "credits_week": 10_000,
            "credit_multipliers": dict(mult),
            "offpeak_multiplier": 0.5,
        },
        {
            "name": "GLM Coding Plan Pro (Z.ai)",
            "family": "glm",
            "price_unverified": True,
            "limit_model": "credits",
            "credits_5h": 12_000,
            "credits_week": 60_000,
            "credit_multipliers": dict(mult),
            "offpeak_multiplier": 0.5,
        },
    ]


def _subs_report(s: dict, totals: dict) -> str:
    """Отчёт (минимальные аргументы) — для assert'ов разметки 💳."""
    full = {
        "hours": 0.0, "win": totals["win"], "wout": totals["wout"],
        "wcr": totals["wcr"], "wcw": totals["wcw"], "wcost": 0.0,
        "comp": 0, "sin": 0, "sout": 0, "scost": 0.0, "ins": 0, "dels": 0,
    }
    sup = {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0}
    return M.render_report(
        "p", "d", [], full, {}, sup, {"line": "conv"}, [], [], subscriptions=s
    )


class TestU8dGlm:
    def test_credits_math_fraction_offpeak(self):
        s = M.build_subscriptions(GLM_TOTALS, {"plans": _glm_plans(), "as_of": "2026-09-21"})
        lite, pro = s["plans"]
        assert lite["credits"] == pytest.approx(GLM_CREDITS)
        assert lite["subs_exact"] == pytest.approx(GLM_CREDITS / 10_000 / M.WEEKS_PER_MONTH)
        assert f"{lite['subs_exact']:.1f}" == "1.2"  # доля с одним знаком
        assert lite["subs"] == 2  # округление вверх
        assert lite["cost"] == pytest.approx(36)  # 2 × $18
        # off-peak: те же токены, кредиты ×0.5
        op = lite["offpeak"]
        assert op["multiplier"] == 0.5
        assert op["credits"] == pytest.approx(GLM_CREDITS * 0.5)
        assert op["subs_exact"] == pytest.approx(GLM_CREDITS * 0.5 / 10_000 / M.WEEKS_PER_MONTH)
        assert op["subs"] == 1
        # cache-read входит в кредиты (кэш-модель GLM)
        s2 = M.build_subscriptions(
            {"win": 0, "wout": 0, "wcr": 5_000_000, "wcw": 0},
            {"plans": [_glm_plans()[0]]},
        )
        assert s2["plans"][0]["credits"] == pytest.approx(5_000_000 * 1.7 / 10_000)
        # Pro: price_unverified — доля считается, цена не выдумана
        assert pro["price"] is None
        assert pro["subs_exact"] == pytest.approx(GLM_CREDITS / 60_000 / M.WEEKS_PER_MONTH)
        assert pro["subs"] == 1
        assert pro["cost"] is None
        assert any("price_unverified" in n for n in s["notes"])

    def test_report_markup(self):
        s = M.build_subscriptions(
            GLM_TOTALS,
            {"plans": _glm_plans(), "as_of": "2026-09-21", "label": "встроенная таблица"},
        )
        text = _subs_report(s, GLM_TOTALS)
        lite_line = next(ln for ln in text.splitlines() if ln.startswith("| GLM Coding Plan Lite"))
        assert "10,000 кредитов/нед (5ч: 2,000)" in lite_line
        assert "| 1.2 | **2** |" in lite_line
        assert "≈ 2 × $18 = **$36**" in lite_line
        pro_line = next(ln for ln in text.splitlines() if ln.startswith("| GLM Coding Plan Pro"))
        assert "| — |" in pro_line  # $/мес не опубликован
        assert "— (цена не опубликована)" in pro_line
        assert "price_unverified" in text  # пометка в отчёте
        offpeak_line = next(ln for ln in text.splitlines() if ln.startswith("Off-peak"))
        assert "GLM Coding Plan Lite (Z.ai) — 0.6 (1)" in offpeak_line
        assert "GLM Coding Plan Pro (Z.ai) — 0.1 (1)" in offpeak_line


class TestU8dFractions:
    def test_messages_fraction_math(self):
        table = {"plans": [
            {"name": "MsgPlan", "family": "gpt", "price_usd_month": 20,
             "limit_tokens_month": 1_000_000}
        ]}
        totals = {"win": 1_100_000, "wout": 100_000, "wcr": 1_000_000, "wcw": 0}
        s = M.build_subscriptions(totals, table)
        p = s["plans"][0]
        assert p["subs_exact"] == pytest.approx(1.2)  # 1.2M / 1M
        assert p["subs"] == 2
        assert p["subs_incl_cache_exact"] == pytest.approx(2.2)  # + кэш
        assert p["subs_incl_cache"] == 3

    def test_report_markup_fraction(self):
        table = {"plans": [
            {"name": "MsgPlan", "family": "gpt", "price_usd_month": 20,
             "limit_tokens_month": 1_000_000}
        ]}
        totals = {"win": 1_100_000, "wout": 100_000, "wcr": 1_000_000, "wcw": 0}
        s = M.build_subscriptions(totals, table)
        text = _subs_report(s, totals)
        header = next(ln for ln in text.splitlines() if ln.startswith("| План |"))
        assert "| Нужно (точно) | Покупать |" in header
        line = next(ln for ln in text.splitlines() if ln.startswith("| MsgPlan |"))
        assert "| 1,000,000 токенов/мес |" in line
        assert "| 1.2 | **2** |" in line
        assert "Пессимистично, если вендор считает и кэш: MsgPlan — 2.2 (3)" in text

    def test_builtin_table_fractional_columns(self, env):
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r.md",
        )
        text = Path(res.report_path).read_text(encoding="utf-8")
        header = next(ln for ln in text.splitlines() if ln.startswith("| План |"))
        assert "| Нужно (точно) | Покупать |" in header
        glm_lite = next(
            p for p in res.subscriptions["plans"]
            if p["name"] == "GLM Coding Plan Lite (Z.ai)"
        )
        # (1.6M×6.9 + 3M×1.7 + 160k×24) / 10 000 = 1 998 кредитов → 0.05 мес
        assert glm_lite["credits"] == pytest.approx(1_998)
        glm_line = next(ln for ln in text.splitlines() if ln.startswith("| GLM Coding Plan Lite"))
        assert "| 0.0 | **1** |" in glm_line
        assert "Off-peak" in text


class TestU8dModelsCache:
    def test_snapshot_written_and_fresh_source(self, env):
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r.md",
        )
        snap = env["proj"] / ".agentic" / "state" / "metrics_models_cache.json"
        assert snap.exists()
        data = json.loads(snap.read_text(encoding="utf-8"))
        rec = data["catalog"]["anthropic"]["models"]["claude-sonnet-4-6"]
        assert rec["cost"]["input"] == 3
        assert data["saved_at"]
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert "Цены моделей: models.dev (свежие)." in text

    def test_fallback_to_snapshot_when_models_json_missing(self, env):
        # первый прогон — снапшот записан (models.json есть)
        M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r1.md",
        )
        # второй — models.json исчез: цены из снапшота
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["tmp"] / "absent.json",
            out=env["tmp"] / "r2.md",
        )
        assert res.conversion["known"] is True
        assert res.conversion["total"] == pytest.approx(
            1_600_000 * 3 / 1e6 + 160_000 * 15 / 1e6
            + 3_000_000 * 0.3 / 1e6 + 100_000 * 3.75 / 1e6
        )
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert re.search(r"Цены моделей: кэш от \d{4}-\d{2}-\d{2}\.", text)
        assert any("models.json недоступен" in w for w in res.warnings)

    def test_no_models_json_no_snapshot_price_unknown(self, env):
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["tmp"] / "absent.json",
            out=env["tmp"] / "r.md",
        )
        assert res.conversion["known"] is False
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert "Цены моделей: цена неизвестна (нет models.json и кэша)." in text

    def test_broken_snapshot_ignored(self, env):
        snap = env["proj"] / ".agentic" / "state" / "metrics_models_cache.json"
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text("{not json")
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["tmp"] / "absent.json",
            out=env["tmp"] / "r.md",
        )
        assert res.conversion["known"] is False
        assert any("кэш цен моделей бит" in w for w in res.warnings)
        assert Path(res.report_path).exists()  # отчёт строится


class TestU8dSubsCache:
    MOCK = {
        "as_of": "2026-09-01",
        "plans": [
            {"name": "TokPlan", "family": "gpt", "price_usd_month": 20,
             "limit_tokens_month": 2_000_000, "source_url": "http://fake",
             "as_of": "2026-09-01"}
        ],
    }

    def _cfg(self, env):
        _set_metrics_cfg(env, "  subscriptions_url: http://fake/subs.json\n")

    def test_fetch_ok_cache_used_without_flag(self, env, monkeypatch):
        self._cfg(env)
        monkeypatch.setattr(M, "_http_get", lambda url, timeout=10.0: json.dumps(self.MOCK).encode())
        M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r1.md", refresh_subscriptions=True,
        )
        # второй прогон без флага: сеть не вызывается, кэш используется
        def dead(url, timeout=10.0):
            raise AssertionError("сеть не должна вызываться")

        monkeypatch.setattr(M, "_http_get", dead)
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r2.md",
        )
        assert res.subscriptions["source"] == "cache"
        assert res.subscriptions["as_of"] == "2026-09-01"
        assert res.subscriptions["refresh_error"] == ""
        assert [p["name"] for p in res.subscriptions["plans"]] == ["TokPlan"]
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert "Данные: кэш подписок, данные от 2026-09-01." in text

    def test_fetch_fail_uses_cache(self, env, monkeypatch):
        self._cfg(env)
        monkeypatch.setattr(M, "_http_get", lambda url, timeout=10.0: json.dumps(self.MOCK).encode())
        M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r1.md", refresh_subscriptions=True,
        )

        def dead(url, timeout=10.0):
            raise OSError("network down")

        monkeypatch.setattr(M, "_http_get", dead)
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r2.md", refresh_subscriptions=True,
        )
        assert res.subscriptions["source"] == "cache"
        assert res.subscriptions["as_of"] == "2026-09-01"
        assert res.subscriptions["refresh_error"] == "OSError: network down"
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert (
            "Данные: кэш подписок, данные от 2026-09-01, "
            "обновление не удалось: OSError: network down." in text
        )

    def test_broken_subs_cache_ignored(self, env):
        cache = env["proj"] / ".agentic" / "state" / "metrics_subscriptions.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{bad json")
        res = M.collect_metrics(
            env["proj"], db_path=env["db"], models_path=env["models"],
            out=env["tmp"] / "r.md",
        )
        assert res.subscriptions["source"] == "builtin"
        assert any("кэш подписок бит" in w for w in res.warnings)
        assert len(res.subscriptions["plans"]) >= 6  # встроенная таблица
