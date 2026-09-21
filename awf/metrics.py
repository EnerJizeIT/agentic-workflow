"""U8: ``awf metrics`` — метрики программы работ (токены, компрессии, строки, стоимость).

Собирает в любой момент и кладёт отчёт на рабочий стол:
  - opencode.db (read-only): воркерские сессии (заголовок
    ``awf-<роль>-TODO-NNNN``) и сессии супервизора (подстроки из конфига
    ``metrics.supervisor_titles``) — токены, компрессии, окна сессий;
  - git-репозиторий: коммиты юнитов (субъект содержит "verify" и
    ``TODO-NNNN``) и ``git show --shortstat`` для строк кода;
  - ``.agentic/context/BASELINE-<todo>.sha`` — mtime старта окна юнита.

Отчёт: таблица «юнит → Δt, сессии, воркер in/out/cache, компрессии,
супервизор in/out/$, +/− строк», блок ИТОГО по ролям, строка конверсии
стоимости («если бы воркеры работали на референс-модели» — цены
models.dev из ``~/.cache/opencode/models.json``).

Ключи конфига (``.agentic/config.yaml``, все опциональны):
  ``metrics.since`` — ISO дата/время или epoch (окно статистики);
  ``metrics.directory`` — фильтр ``session.directory``;
  ``metrics.supervisor_titles`` — список подстрок заголовков супервизора;
  ``metrics.reference_model`` — модель для конверсии
  (дефолт ``anthropic/claude-sonnet-4-6``);
  ``metrics.output_dir`` — каталог отчёта (дефолт ``~/Desktop``, если
  существует, иначе project_dir).
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config as config_mod

DEFAULT_REFERENCE_MODEL = "anthropic/claude-sonnet-4-6"

_WORKER_TITLE_RE = re.compile(r"^awf-.+-TODO-(\d{4})$")
_TODO_RE = re.compile(r"TODO-(\d{4})")
_SHORTSTAT_INS = re.compile(r"(\d+) insertions?")
_SHORTSTAT_DEL = re.compile(r"(\d+) deletions?")

_COST_KEYS = ("input", "output", "cache_read", "cache_write")


@dataclass
class MetricsResult:
    """Итог сбора метрик: данные + путь отчёта + код выхода."""

    project_dir: str
    generated_at: str
    units: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    workers_by_role: dict = field(default_factory=dict)
    supervisor_outside: dict = field(default_factory=dict)
    conversion: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    measured: bool = False
    exit_code: int = 0
    report_path: str | None = None
    report: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_dir": self.project_dir,
            "generated_at": self.generated_at,
            "units": self.units,
            "totals": self.totals,
            "workers_by_role": self.workers_by_role,
            "supervisor_outside": self.supervisor_outside,
            "conversion": self.conversion,
            "notes": self.notes,
            "warnings": self.warnings,
            "measured": self.measured,
            "exit_code": self.exit_code,
            "report_path": self.report_path,
        }


def default_db_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def default_models_path() -> Path:
    return Path.home() / ".cache" / "opencode" / "models.json"


def parse_since(value: Any, warnings: list[str]) -> int | None:
    """ISO date/datetime (локальное время) или epoch → миллисекунды."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n >= 10**12 else n * 1000
    s = str(value).strip()
    try:
        n = int(s)
        return n if n >= 10**12 else n * 1000
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except ValueError:
        warnings.append(f"metrics.since: не распознано значение {value!r} — фильтр отключён")
        return None


def _git(repo: Path, *args: str) -> str:
    # timeout обязателен по контракту subprocess-timeouts: git-прогон по
    # большому репо не должен вешать сбор метрик; TimeoutExpired → "" (degrade).
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _parse_shortstat(output: str) -> tuple[int, int]:
    ins = _SHORTSTAT_INS.search(output)
    dels = _SHORTSTAT_DEL.search(output)
    return (int(ins.group(1)) if ins else 0, int(dels.group(1)) if dels else 0)


def _fmt(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def _open_db(db_path: Path, warnings: list[str]) -> sqlite3.Connection | None:
    if not db_path.exists():
        warnings.append(f"база opencode не найдена: {db_path} — сессии не измеряются")
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        warnings.append(f"база opencode не открывается: {e} — сессии не измеряются")
        return None


def collect_workers(
    con: sqlite3.Connection,
    since_ms: int | None,
    directory: str | None,
    warnings: list[str],
) -> dict[str, dict]:
    """Воркерские сессии (``awf-<роль>-TODO-NNNN``), сгруппированные по TODO."""
    sql = (
        "SELECT id, title, COALESCE(tokens_input,0), COALESCE(tokens_output,0), "
        "COALESCE(tokens_cache_read,0), COALESCE(tokens_cache_write,0), "
        "COALESCE(cost,0), time_created, time_updated FROM session"
    )
    where, params = [], []
    if since_ms is not None:
        where.append("time_created >= ?")
        params.append(since_ms)
    if directory:
        where.append("directory = ?")
        params.append(directory)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY time_created"

    workers: dict[str, dict] = {}
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        warnings.append(f"запрос по сессиям упал: {e} — воркеры не измеряются")
        return workers
    for sid, title, tin, tout, tcr, tcw, cost, tc, tu in rows:
        m = _WORKER_TITLE_RE.match(title or "")
        if not m:
            continue
        todo = f"TODO-{m.group(1)}"
        d = workers.setdefault(
            todo,
            {
                "todo": todo,
                "sessions": 0,
                "in": 0,
                "out": 0,
                "cr": 0,
                "cw": 0,
                "cost": 0.0,
                "first": tc,
                "last": tu,
                "compactions": 0,
                "ids": [],
                "role_sums": {},
            },
        )
        role = (title or "").replace(f"-{todo}", "").removeprefix("awf-")
        rsum = d["role_sums"].setdefault(
            role, {"sessions": 0, "in": 0, "out": 0, "cr": 0, "cost": 0.0}
        )
        d["sessions"] += 1
        rsum["sessions"] += 1
        rsum["in"] += tin
        rsum["out"] += tout
        rsum["cr"] += tcr
        rsum["cost"] += cost
        d["in"] += tin
        d["out"] += tout
        d["cr"] += tcr
        d["cw"] += tcw
        d["cost"] += cost
        d["first"] = min(d["first"] or tc, tc)
        d["last"] = max(d["last"] or tu, tu)
        d["ids"].append(sid)

    id2todo = {sid: todo for todo, d in workers.items() for sid in d["ids"]}
    if id2todo:
        marks = ",".join("?" * len(id2todo))
        try:
            for sid, cnt in con.execute(
                "SELECT session_id, COUNT(*) FROM part "
                "WHERE json_extract(data,'$.type')='compaction' "
                f"AND session_id IN ({marks}) GROUP BY session_id",
                tuple(id2todo),
            ):
                workers[id2todo[sid]]["compactions"] += cnt
        except sqlite3.Error as e:
            warnings.append(f"запрос по компрессиям упал: {e}")
    return workers


def collect_commit_info(repo: Path, warnings: list[str]) -> dict[str, list[tuple[str, int]]]:
    """Коммиты юнитов: субъект содержит "verify" и TODO-NNNN → {todo: [(sha, ct_s)]}."""
    commits: dict[str, list[tuple[str, int]]] = {}
    if not (repo / ".git").exists():
        warnings.append(f"git-репозиторий не найден: {repo} — строки кода не измеряются")
        return commits
    out = _git(repo, "log", "--all", "--format=%H|%ct|%s")
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, ct, subj = parts
        m = _TODO_RE.search(subj)
        if m and "verify" in subj:
            try:
                commits.setdefault(m.group(0), []).append((sha, int(ct)))
            except ValueError:
                continue
    return commits


def build_windows(
    repo: Path,
    todos: list[str],
    first_ts: dict[str, int],
    commits: dict[str, list[tuple[str, int]]],
    now_ms: int,
) -> dict[str, tuple[int, int]]:
    """Окна юнитов: от mtime BASELINE-<todo>.sha до коммита verify (или следующего baseline)."""
    ctx = repo / ".agentic" / "context"
    windows: dict[str, tuple[int, int]] = {}
    for i, todo in enumerate(todos):
        base = ctx / f"BASELINE-{todo}.sha"
        if base.exists():
            try:
                start = int(base.stat().st_mtime * 1000)
            except OSError:
                start = first_ts.get(todo, now_ms)
        else:
            start = first_ts.get(todo, now_ms)
        if todo in commits:
            end = max(ct for _, ct in commits[todo]) * 1000
        else:
            nxt = todos[i + 1] if i + 1 < len(todos) else None
            end = now_ms
            if nxt:
                nbase = ctx / f"BASELINE-{nxt}.sha"
                if nbase.exists():
                    try:
                        end = int(nbase.stat().st_mtime * 1000)
                    except OSError:
                        pass
        if start == 0:
            start = now_ms
        windows[todo] = (start, min(end + 60_000, now_ms))
    return windows


def collect_supervisor(
    con: sqlite3.Connection,
    titles: list[str],
    since_ms: int | None,
    directory: str | None,
    windows: dict[str, tuple[int, int]],
    warnings: list[str],
) -> tuple[dict[str, dict], dict[str, float], int]:
    """Сессии супервизора: атрибутация сообщений по окнам юнитов."""
    per_todo: dict[str, dict] = {
        t: {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0} for t in windows
    }
    outside = {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0}
    session_count = 0
    if not titles:
        return per_todo, outside, session_count

    sql = "SELECT id, title FROM session WHERE 1=1"
    params: list[Any] = []
    if since_ms is not None:
        sql += " AND time_created >= ?"
        params.append(since_ms)
    if directory:
        sql += " AND directory = ?"
        params.append(directory)
    try:
        candidates = con.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        warnings.append(f"запрос по сессиям упал: {e} — супервизор не измеряется")
        return per_todo, outside, session_count

    for sid, title in candidates:
        if not any(t in (title or "") for t in titles):
            continue
        session_count += 1
        try:
            rows = con.execute(
                "SELECT data, time_created FROM message "
                "WHERE session_id=? ORDER BY time_created",
                (sid,),
            ).fetchall()
        except sqlite3.Error as e:
            warnings.append(f"сообщения сессии {sid} не читаются: {e}")
            continue
        wins_desc = sorted(windows.items(), key=lambda kv: kv[1][0], reverse=True)
        for data, ts in rows:
            try:
                d = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                warnings.append("битая строка message.data пропущена (1 шт.)")
                continue
            t = d.get("tokens") or {}
            if not t:
                continue
            cache = t.get("cache") or {}
            vals = {
                "in": t.get("input", 0) or 0,
                "out": t.get("output", 0) or 0,
                "cr": cache.get("read", 0) or 0,
                "cw": cache.get("write", 0) or 0,
                "cost": d.get("cost", 0) or 0,
            }
            placed = False
            for todo, (s, e) in wins_desc:
                if s <= ts <= e:
                    for k in vals:
                        per_todo[todo][k] += vals[k]
                    placed = True
                    break
            if not placed:
                for k in vals:
                    outside[k] += vals[k]
    return per_todo, outside, session_count


def collect_code_lines(
    repo: Path, commits: dict[str, list[tuple[str, int]]]
) -> dict[str, tuple[int, int]]:
    """+/− строки по коммитам юнита (git show --shortstat)."""
    diff: dict[str, tuple[int, int]] = {}
    for todo, shas in commits.items():
        ins = dels = 0
        for sha, _ct in shas:
            i, d = _parse_shortstat(_git(repo, "show", "--shortstat", "--format=", sha))
            ins += i
            dels += d
        diff[todo] = (ins, dels)
    return diff


def load_reference_costs(
    models_path: Path, reference_model: str, warnings: list[str]
) -> dict[str, float] | None:
    """Цены референс-модели из models.json (за 1M токенов) или None."""
    if not models_path.exists():
        warnings.append(f"models.json не найден: {models_path} — цена неизвестна")
        return None
    try:
        data = json.loads(models_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        warnings.append(f"models.json не читается: {e} — цена неизвестна")
        return None
    provider, _, model_id = reference_model.partition("/")
    provider_data = data.get(provider) if isinstance(data, dict) else None
    models = provider_data.get("models") if isinstance(provider_data, dict) else None
    model = models.get(model_id) if isinstance(models, dict) else None
    cost = model.get("cost") if isinstance(model, dict) else None
    if not isinstance(cost, dict):
        warnings.append(
            f"модель {reference_model} не найдена в models.json — цена неизвестна"
        )
        return None
    return {k: float(cost.get(k, 0) or 0) for k in _COST_KEYS}


def convert_cost(
    totals: dict, costs: dict[str, float] | None, reference_model: str
) -> dict[str, Any]:
    """«Если бы воркеры работали на <референс-модели>» — расчёт по ценам models.dev."""
    if costs is None:
        return {
            "reference_model": reference_model,
            "known": False,
            "input_cost": None,
            "output_cost": None,
            "cache_cost": None,
            "total": None,
            "line": (
                f"Если бы воркеры работали на {reference_model}: "
                "стоимость неизвестна (цены не найдены в models.json)"
            ),
        }
    inp = totals["win"] * costs["input"] / 1e6
    outp = totals["wout"] * costs["output"] / 1e6
    cr = totals["wcr"] * costs["cache_read"] / 1e6
    cw = totals["wcw"] * costs["cache_write"] / 1e6
    total = inp + outp + cr + cw
    return {
        "reference_model": reference_model,
        "known": True,
        "input_cost": inp,
        "output_cost": outp,
        "cache_cost": cr + cw,
        "total": total,
        "line": (
            f"Если бы воркеры работали на {reference_model}: "
            f"input ${inp:.2f} + output ${outp:.2f} + cache ${cr + cw:.2f} ≈ "
            f"**${total:.2f}** (при ${costs['input']:g}/M in, ${costs['output']:g}/M out)"
        ),
    }


def render_report(
    project_name: str,
    generated_at: str,
    units: list[dict],
    totals: dict,
    workers_by_role: dict[str, dict],
    supervisor_outside: dict,
    conversion: dict,
    notes: list[str],
    warnings: list[str],
) -> str:
    lines = [f"# Метрики программы — снимок: {project_name}", ""]
    lines.append(f"Дата снимка: {generated_at}. Источник: opencode.db + git.")
    for w in warnings:
        lines.append(f"ПРЕДУПРЕЖДЕНИЕ: {w}")
    lines.append("")
    lines.append(
        "| Юнит | Δt, ч | Сессии | Воркер in | Воркер out | Cache read | Компрессии | "
        "Супервизор in | Супервизор out | Супервизор $ | +строк | -строк |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for u in units:
        lines.append(
            f"| {u['todo']} | {u['hours']:.1f} | {u['sessions']} | {_fmt(u['w_in'])} | "
            f"{_fmt(u['w_out'])} | {_fmt(u['w_cr'])} | {u['compactions']} | "
            f"{_fmt(u['s_in'])} | {_fmt(u['s_out'])} | ${u['s_cost']:.3f} | "
            f"{u['ins']} | {u['dels']} |"
        )
    lines.append(
        f"| **ИТОГО** | {totals['hours']:.1f} | | {_fmt(totals['win'])} | "
        f"{_fmt(totals['wout'])} | {_fmt(totals['wcr'])} | {totals['comp']} | "
        f"{_fmt(totals['sin'])} | {_fmt(totals['sout'])} | ${totals['scost']:.3f} | "
        f"{totals['ins']} | {totals['dels']} |"
    )
    lines.append("")
    lines.append(
        "Супервизор вне окон юнитов (стратегия, разборы, планы): "
        f"in {_fmt(supervisor_outside['in'])}, out {_fmt(supervisor_outside['out'])}, "
        f"cache_read {_fmt(supervisor_outside['cr'])}, "
        f"${supervisor_outside['cost']:.3f}."
    )
    lines.append("")
    lines.append("## ИТОГО — отдельно (группировка по ролям, не по юнитам)")
    lines.append("")
    lines.append(
        f"- Юнитов в таблице: {len(units)}; суммарное окно: {totals['hours']:.1f} ч"
    )
    for role, r in sorted(workers_by_role.items()):
        lines.append(
            f"- Воркер {role}: {r['sessions']} сессий, input {r['in']:,} / "
            f"output {r['out']:,} / cache-read {r['cr']:,} / ${r['cost']:.3f}"
        )
    cost_note = " (локальная модель)" if totals["wcost"] < 0.0005 else ""
    lines.append(
        f"- Воркеры всего: input {totals['win']:,} / output {totals['wout']:,} / "
        f"cache-read {totals['wcr']:,} / компрессий {totals['comp']} / "
        f"стоимость ${totals['wcost']:.3f}{cost_note}"
    )
    lines.append(
        f"- Супервизор, в юнитах: input {totals['sin']:,} / output {totals['sout']:,} / "
        f"${totals['scost']:.3f}"
    )
    lines.append(
        f"- Супервизор, вне юнитов: input {supervisor_outside['in']:,} / "
        f"output {supervisor_outside['out']:,} / ${supervisor_outside['cost']:.3f}"
    )
    lines.append(
        f"- Супервизор, всего: input {totals['sin'] + supervisor_outside['in']:,} / "
        f"output {totals['sout'] + supervisor_outside['out']:,} / "
        f"${totals['scost'] + supervisor_outside['cost']:.3f}"
    )
    lines.append(f"- Строки кода: +{totals['ins']:,} / −{totals['dels']:,}")
    lines.append("")
    lines.append(conversion["line"])
    lines.append("")
    for n in notes:
        lines.append(n)
    return "\n".join(lines) + "\n"


def _resolve_out_path(out: str | None, project_dir: Path, cfg: dict) -> Path:
    if out:
        p = Path(out).expanduser()
        if p.is_dir() or str(out).endswith(("/", "\\")):
            p = p / _report_name()
        return p
    out_dir = config_mod.get(cfg, "metrics.output_dir")
    if not out_dir:
        desktop = Path.home() / "Desktop"
        out_dir = str(desktop) if desktop.is_dir() else str(project_dir)
    return Path(out_dir).expanduser() / _report_name()


def _report_name() -> str:
    return f"awf-metrics-{time.strftime('%Y%m%d-%H%M')}.md"


def collect_metrics(
    project_dir: str | Path,
    *,
    db_path: str | Path | None = None,
    models_path: str | Path | None = None,
    reference_model: str | None = None,
    since: str | int | float | None = None,
    out: str | None = None,
) -> MetricsResult:
    """Собрать метрики программы и записать markdown-отчёт.

    Возвращает :class:`MetricsResult`; ``exit_code`` = 0, если измерено хоть
    что-то (воркеры / коммиты / супервизор), иначе 1. Отчёт пишется всегда
    (с пометками о том, что не измерилось).
    """
    project_dir = Path(project_dir).resolve()
    warnings: list[str] = []
    notes: list[str] = []
    cfg = config_mod.load(project_dir)
    now_ms = int(time.time() * 1000)

    since_ms = parse_since(
        since if since not in (None, "") else config_mod.get(cfg, "metrics.since"),
        warnings,
    )
    directory = config_mod.get(cfg, "metrics.directory") or None
    titles = config_mod.get(cfg, "metrics.supervisor_titles") or []
    if not isinstance(titles, list):
        warnings.append("metrics.supervisor_titles не список — супервизор не измеряется")
        titles = []
    ref_model = (
        reference_model
        or config_mod.get(cfg, "metrics.reference_model")
        or DEFAULT_REFERENCE_MODEL
    )

    db = db_path if db_path is not None else default_db_path()
    models = models_path if models_path is not None else default_models_path()

    con = _open_db(Path(db), warnings)
    workers: dict[str, dict] = {}
    sup_per_todo: dict[str, dict] = {}
    sup_outside: dict[str, float] = {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0}
    sup_sessions = 0
    if con is not None:
        workers = collect_workers(con, since_ms, directory, warnings)
    commits = collect_commit_info(project_dir, warnings)

    todos = sorted(set(workers) | set(commits))
    first_ts = {t: d["first"] for t, d in workers.items() if d.get("first")}
    # fallback start для юнитов без воркерских сессий — время первого коммита
    for t, shas in commits.items():
        first_ts.setdefault(t, min(ct for _, ct in shas) * 1000)
    windows = build_windows(project_dir, todos, first_ts, commits, now_ms)

    if con is not None:
        sup_per_todo, sup_outside, sup_sessions = collect_supervisor(
            con, titles, since_ms, directory, windows, warnings
        )
        con.close()

    diff = collect_code_lines(project_dir, commits)
    costs = load_reference_costs(Path(models), ref_model, warnings)

    units: list[dict] = []
    tot = {
        "hours": 0.0, "win": 0, "wout": 0, "wcr": 0, "wcw": 0, "wcost": 0.0,
        "comp": 0, "sin": 0, "sout": 0, "scost": 0.0, "ins": 0, "dels": 0,
    }
    for todo in todos:
        w = workers.get(todo, {})
        s = sup_per_todo.get(todo, {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0})
        ins, dels = diff.get(todo, (0, 0))
        st, en = windows.get(todo, (now_ms, now_ms))
        hours = max(0.0, (en - st) / 3_600_000)
        units.append(
            {
                "todo": todo,
                "hours": hours,
                "sessions": w.get("sessions", 0),
                "w_in": w.get("in", 0),
                "w_out": w.get("out", 0),
                "w_cr": w.get("cr", 0),
                "compactions": w.get("compactions", 0),
                "s_in": s["in"],
                "s_out": s["out"],
                "s_cost": s["cost"],
                "ins": ins,
                "dels": dels,
            }
        )
        tot["hours"] += hours
        tot["win"] += w.get("in", 0)
        tot["wout"] += w.get("out", 0)
        tot["wcr"] += w.get("cr", 0)
        tot["wcw"] += w.get("cw", 0)
        tot["wcost"] += w.get("cost", 0.0)
        tot["comp"] += w.get("compactions", 0)
        tot["sin"] += s["in"]
        tot["sout"] += s["out"]
        tot["scost"] += s["cost"]
        tot["ins"] += ins
        tot["dels"] += dels
    # Точные суммы по ролям: одна сессия = одна роль, суммы накопились в role_sums.
    by_role = _workers_by_role(workers)

    conversion = convert_cost(tot, costs, ref_model)

    if tot["wcost"] < 0.0005 and workers:
        notes.append("Примечание: у воркеров cost=0 (локальная модель).")
    notes.append(
        "«Компрессии» — число сжатий контекста по частям type=compaction; "
        "строки кода — diff коммитов юнита; Δt — окно юнита (от baseline до коммита)."
    )

    measured = bool(workers or commits or sup_sessions)
    generated_at = time.strftime("%Y-%m-%d %H:%M")
    project_name = (
        config_mod.get(cfg, "project.name") or project_dir.name or str(project_dir)
    )
    report = render_report(
        str(project_name), generated_at, units, tot, by_role,
        sup_outside, conversion, notes, warnings,
    )
    result = MetricsResult(
        project_dir=str(project_dir),
        generated_at=generated_at,
        units=units,
        totals=dict(tot),
        workers_by_role=by_role,
        supervisor_outside=dict(sup_outside),
        conversion=conversion,
        notes=notes,
        warnings=warnings,
        measured=measured,
        exit_code=0 if measured else 1,
        report=report,
    )
    try:
        out_path = _resolve_out_path(out, project_dir, cfg)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        result.report_path = str(out_path)
    except OSError as e:
        warnings.append(f"отчёт не записан: {e}")
        result.warnings = warnings
    return result


def _workers_by_role(workers: dict[str, dict]) -> dict[str, dict]:
    """Суммарные токены по ролям (роль — из заголовка сессии; одна сессия = одна роль)."""
    by_role: dict[str, dict] = {}
    for d in workers.values():
        for role, rsum in d.get("role_sums", {}).items():
            r = by_role.setdefault(
                role, {"sessions": 0, "in": 0, "out": 0, "cr": 0, "cost": 0.0}
            )
            for k in ("sessions", "in", "out", "cr", "cost"):
                r[k] += rsum[k]
    return by_role
