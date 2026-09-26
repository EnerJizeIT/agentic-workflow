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

U8b: три раздела «если бы воркер был…» — 🏆 топ-модели (3), ⚖️ оптимум
(3), 💡 дешёвые (одна строка) — по спискам id из ``metrics.tiers.top|
optimal|cheap`` (цены $/M, итог $ по токенам воркеров ÷1e6, минимум в
таблице жирным, доля cache-read) + раздел 💳 подписки (объём воркеров ≈
N подписок/мес × цена; GPT / Claude / GLM). Подписки: таблица из
``awf/data/subscriptions.json``, кэш
``.agentic/state/metrics_subscriptions.json``, актуализация GET
``metrics.subscriptions_url`` при ``refresh_subscriptions=True``.

U8d: GLM — кредитная модель (лимиты кредитов/нед, cache-read входит в
кредиты, off-peak ×0.5; Pro/Max без опубликованной цены —
``price_unverified``, доля считается, стоимость нет). В 💳 — две
колонки: «Нужно (точно)» (дробная доля, 1 знак) и «Покупать» (ceil).
Цены моделей кэшируются в
``.agentic/state/metrics_models_cache.json`` (снапшот используемых
записей) и используются, когда models.json недоступен; в отчёте —
строка источника цен. Подписки: refresh → кэш → встроенная.

Ключи конфига (``.agentic/config.yaml``, все опциональны):
  ``metrics.since`` — ISO дата/время или epoch (окно статистики);
  ``metrics.directory`` — фильтр ``session.directory``;
  ``metrics.supervisor_titles`` — список подстрок заголовков супервизора;
  ``metrics.reference_model`` — модель для конверсии
  (дефолт ``anthropic/claude-sonnet-4-6``);
  ``metrics.tiers`` — списки id моделей по классам {top, optimal, cheap}
  (дефолты :data:`DEFAULT_TIERS`);
  ``metrics.subscriptions_url`` — JSON-таблица подписок для
  ``--refresh-subscriptions`` (тот же формат, что у встроенной);
  ``metrics.subscriptions_cache`` — путь кэша подписок (дефолт
  ``.agentic/state/metrics_subscriptions.json``, относительно project_dir);
  ``metrics.models_cache`` — путь кэша цен моделей (дефолт
  ``.agentic/state/metrics_models_cache.json``, относительно project_dir);
  ``metrics.output_dir`` — каталог отчёта (дефолт ``~/Desktop``, если
  существует, иначе project_dir);
  (RUN10 #3-fix) по умолчанию собираются только сессии текущего проекта:
  сессия принадлежит проекту, когда её ``part.data`` содержит путь
  проекта (нормализованный ``project_dir``) — ``session.directory`` НЕ
  дискриминатор: воркеров спавнит MCP-сервер из $HOME, у сессий
  супервизора тот же directory. Сопоставление — один проход по ``part``
  (LIKE) для кандидатов по заголовкам, затем пересечение.
  ``all_projects=True`` (CLI ``--all-projects``) — все проекты общей
  opencode.db, данные смешаны; явный ``metrics.directory`` в конфиге —
  legacy-фильтр по ``session.directory``, побеждает оба варианта.
  Исключённые сессии других проектов считаются и называются
  предупреждением в отчёт — без тихого вычета; путь проекта без единого
  совпадения — пустые данные сессий + предупреждение (явное «нет
  данных», а не чужие цифры).
  ``metrics.mirror_dir`` — U8c: архивное зеркало отчёта (путь; пусто =
  выключено). После успешной записи отчёта копия с тем же именем
  складывается туда (каталог создаётся; ошибка копирования —
  предупреждение, сбор не падает). Флаг ``--no-mirror`` / ``mirror=False``
  отключает зеркалирование на один запуск.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config as config_mod

DEFAULT_REFERENCE_MODEL = "anthropic/claude-sonnet-4-6"

# U8b: дефолтные списки моделей по классам (решения владельца 21.09).
# Все id проверены в каталоге models.dev 21.09 (цены присутствуют).
DEFAULT_TIERS: dict[str, list[str]] = {
    "top": [
        "anthropic/claude-fable-5",
        "openai/gpt-6-astra",
        "anthropic/claude-opus-5",
    ],
    "optimal": [
        "anthropic/claude-sonnet-5",
        "openai/gpt-5.6-terra",
        "google/gemini-3.1-pro-preview",
    ],
    "cheap": [
        "opencode/qwen3.8-flash",
        "opencode/deepseek-v4.1-flash",
    ],
}

# U8b: таймаут GET таблицы подписок (сек) — по контракту subprocess-timeouts
# вся внешняя сеть живёт с явным таймаутом.
SUBSCRIPTIONS_TIMEOUT = 10.0

# U8d: недель в месяце (GLM: недельные кредитные лимиты → месячная доля).
WEEKS_PER_MONTH = 4.345

# A-10: общий парсер TODO ID — 4+ цифры с явной правой границей
# (не-словарный символ или конец строки). TODO-10000 не усекается до
# TODO-1000, TODO-10000x не читается как ID вовсе.
_TODO_ID_RE = re.compile(r"TODO-(\d{4,})(?!\w)")
_WORKER_TITLE_RE = re.compile(r"^awf-.+-TODO-(\d{4,})$")  # правая граница — `$`


def _todo_id_from_title(title: str | None) -> str | None:
    """Заголовок воркер-сессии ``awf-<роль>-TODO-NNNN`` → TODO ID или None."""
    m = _WORKER_TITLE_RE.match(title or "")
    return f"TODO-{m.group(1)}" if m else None


def _todo_id_from_subject(subject: str | None) -> str | None:
    """Коммит-субъект → TODO ID или None (явная правая граница)."""
    m = _TODO_ID_RE.search(subject or "")
    return m.group(0) if m else None
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
    tier_tables: dict = field(default_factory=dict)
    subscriptions: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    measured: bool = False
    exit_code: int = 0
    report_path: str | None = None
    mirror_path: str | None = None
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
            "tier_tables": self.tier_tables,
            "subscriptions": self.subscriptions,
            "notes": self.notes,
            "warnings": self.warnings,
            "measured": self.measured,
            "exit_code": self.exit_code,
            "report_path": self.report_path,
            "mirror_path": self.mirror_path,
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


def _norm_dir(p: Any) -> str:
    """Нормализация пути для сравнения ``session.directory`` (RUN10 #3):
    resolve + без хвостового слэша. Пустое → пустая строка."""
    s = str(p or "").strip()
    if not s:
        return ""
    if s == "/":
        return "/"
    try:
        return str(Path(s).resolve())
    except (OSError, RuntimeError):
        return s.rstrip("/")


class _DirGate:
    """Фильтр сессий по каталогу с честным счётом исключённых (RUN10 #3).

    Сравнение — после :func:`_norm_dir` (resolve, без хвостового слэша),
    а не строковое: запись с ``/path/`` и фильтр ``/path`` — один проект.
    Два случая исключений — отдельно: пустой ``directory`` (проект
    неизвестен — старые записи) и другие каталоги (чужие проекты).
    Тихое исключение ложилось бы в отчёт скрытой погрешностью, поэтому
    ``finish()`` дописывает предупреждения.
    """

    def __init__(self, keep_dir: str | None, label: str, warnings: list[str]):
        self.keep = _norm_dir(keep_dir) if keep_dir else None
        self._label = label
        self._warnings = warnings
        self._unknown = 0
        self._others: dict[str, int] = {}

    def allowed(self, directory: str) -> bool:
        if self.keep is None:
            return True
        nd = _norm_dir(directory)
        if nd == self.keep:
            return True
        if not nd:
            self._unknown += 1
        else:
            self._others[nd] = self._others.get(nd, 0) + 1
        return False

    def finish(self) -> None:
        if self._unknown:
            self._warnings.append(
                f"нет признака проекта: сессий {self._label} с пустым "
                f"directory — {self._unknown} (проект неизвестен; исключены "
                "из отчёта, --all-projects включает их)"
            )
        if self._others:
            total = sum(self._others.values())
            dirs_txt = ", ".join(sorted(self._others)[:3])
            if len(self._others) > 3:
                dirs_txt += "…"
            self._warnings.append(
                f"другие проекты: сессий {self._label} из других каталогов — "
                f"{total} ({dirs_txt}) — исключены из отчёта"
            )


def _like_escape(value: str) -> str:
    """Экранирование LIKE-спецсимволов пути для паттерна (``ESCAPE '\\``)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def candidate_session_ids(
    con: sqlite3.Connection,
    titles: list[str],
    warnings: list[str],
) -> list[str]:
    """Кандидаты области отчёта (RUN10 #3-fix): заголовки воркеров и
    подстроки заголовков супервизора.

    Грубый LIKE-предфильтр одним запросом — точная сверка заголовков
    (regex ``awf-<роль>-TODO-NNNN`` / подстроки) остаётся в
    :func:`collect_workers` и :func:`collect_supervisor`.
    """
    clauses = ["title LIKE 'awf-%-TODO-%'"]
    params: list[Any] = []
    for t in titles:
        clauses.append("title LIKE ?")
        params.append(f"%{_like_escape(t)}%")
    try:
        rows = con.execute(
            "SELECT id FROM session WHERE " + " OR ".join(clauses), params
        ).fetchall()
    except sqlite3.Error as e:
        warnings.append(f"запрос по кандидатам упал: {e} — сессии не сопоставляются")
        return []
    return [r[0] for r in rows]


def matched_session_ids(
    con: sqlite3.Connection,
    candidate_ids: list[str],
    project_dir: str,
    warnings: list[str],
) -> frozenset[str]:
    """Один проход по ``part``: чьё содержимое называет путь проекта.

    RUN10 #3-fix: сессия принадлежит проекту, если в её ``part.data``
    встречается путь проекта (нормализованный абсолютный путь).
    ``session.directory`` — не дискриминатор: воркеров спавнит MCP-сервер
    из $HOME, у сессий супервизора тот же directory. Проход ограничен
    кандидатами (``IN``) и чанкуется — лимит параметров SQLite.
    """
    matched: set[str] = set()
    if not candidate_ids:
        return frozenset(matched)
    pattern = f"%{_like_escape(str(project_dir))}%"
    chunk = 500
    for i in range(0, len(candidate_ids), chunk):
        ids = candidate_ids[i : i + chunk]
        marks = ",".join("?" * len(ids))
        try:
            rows = con.execute(
                "SELECT DISTINCT session_id FROM part "
                f"WHERE data LIKE ? ESCAPE '\\' AND session_id IN ({marks})",
                (pattern, *ids),
            ).fetchall()
        except sqlite3.Error as e:
            warnings.append(
                f"запрос по содержимому сессий упал: {e} — "
                "сессии не сопоставляются"
            )
            return frozenset()
        matched.update(r[0] for r in rows)
    return frozenset(matched)


def collect_workers(
    con: sqlite3.Connection,
    since_ms: int | None,
    directory: str | None,
    matched: frozenset[str] | None,
    warnings: list[str],
) -> dict[str, dict]:
    """Воркерские сессии (``awf-<роль>-TODO-NNNN``), сгруппированные по TODO.

    ``directory`` — legacy-фильтр по ``session.directory`` (явный
    ``metrics.directory``); ``matched`` — content-область (RUN10 #3-fix):
    сессия входит, если её id в наборе, чьё содержимое называет путь
    проекта. ``None``/``None`` — все проекты, без фильтра.
    """
    sql = (
        "SELECT id, title, COALESCE(directory,''), COALESCE(tokens_input,0), "
        "COALESCE(tokens_output,0), COALESCE(tokens_cache_read,0), "
        "COALESCE(tokens_cache_write,0), COALESCE(cost,0), time_created, "
        "time_updated FROM session"
    )
    where, params = [], []
    if since_ms is not None:
        where.append("time_created >= ?")
        params.append(since_ms)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY time_created"

    workers: dict[str, dict] = {}
    gate = _DirGate(directory, "воркеров", warnings)
    excluded = 0
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        warnings.append(f"запрос по сессиям упал: {e} — воркеры не измеряются")
        return workers
    for sid, title, sdir, tin, tout, tcr, tcw, cost, tc, tu in rows:
        todo = _todo_id_from_title(title)
        if not todo:
            continue
        if directory is not None:
            if not gate.allowed(sdir):
                continue
        elif matched is not None and sid not in matched:
            excluded += 1
            continue
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

    gate.finish()
    if matched is not None and excluded:
        warnings.append(
            f"исключены сессии других проектов: {excluded} (путь проекта "
            "не найден в содержимом воркерских сессий)"
        )

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
        todo = _todo_id_from_subject(subj)
        if todo and "verify" in subj:
            try:
                commits.setdefault(todo, []).append((sha, int(ct)))
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
    matched: frozenset[str] | None,
    windows: dict[str, tuple[int, int]],
    warnings: list[str],
) -> tuple[dict[str, dict], dict[str, float], int]:
    """Сессии супервизора: атрибутация сообщений по окнам юнитов.

    ``directory``/``matched`` — то же, что в :func:`collect_workers`
    (legacy-фильтр / content-область). У сессий супервизора directory
    тоже $HOME — content-область для них обязательна (RUN10 #3-fix).
    """
    per_todo: dict[str, dict] = {
        t: {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0} for t in windows
    }
    outside = {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0}
    session_count = 0
    if not titles:
        return per_todo, outside, session_count

    sql = "SELECT id, title, COALESCE(directory, '') FROM session WHERE 1=1"
    params: list[Any] = []
    if since_ms is not None:
        sql += " AND time_created >= ?"
        params.append(since_ms)
    gate = _DirGate(directory, "супервизора", warnings)
    excluded = 0
    try:
        candidates = con.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        warnings.append(f"запрос по сессиям упал: {e} — супервизор не измеряется")
        return per_todo, outside, session_count

    for sid, title, sdir in candidates:
        if not any(t in (title or "") for t in titles):
            continue
        if directory is not None:
            if not gate.allowed(sdir):
                continue
        elif matched is not None and sid not in matched:
            excluded += 1
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
    gate.finish()
    if matched is not None and excluded:
        warnings.append(
            f"исключены сессии других проектов: {excluded} (путь проекта "
            "не найден в содержимом сессий супервизора)"
        )
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


def load_models_catalog(models_path: Path, warnings: list[str]) -> dict | None:
    """models.json целиком (провайдер → models → cost) или None."""
    if not models_path.exists():
        warnings.append(f"models.json не найден: {models_path}")
        return None
    try:
        data = json.loads(models_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        warnings.append(f"models.json не читается: {e}")
        return None
    return data if isinstance(data, dict) else None


# U8d: кэш цен моделей — снапшот используемых записей на случай,
# если models.json недоступен.
DEFAULT_MODELS_CACHE = ".agentic/state/metrics_models_cache.json"


def _models_cache_file(project_dir: Path, cache_path: str | None) -> Path:
    f = Path(cache_path or DEFAULT_MODELS_CACHE).expanduser()
    if not f.is_absolute():
        f = project_dir / f
    return f


def load_models_cache(
    project_dir: Path, cache_path: str | None, warnings: list[str]
) -> tuple[dict | None, str | None]:
    """Снапшот цен моделей (каталог + дата) или (None, None).

    Битый кэш игнорируется с предупреждением — сбор не падает (U8d).
    """
    f = _models_cache_file(project_dir, cache_path)
    if not f.exists():
        return None, None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        warnings.append(f"кэш цен моделей бит ({e}) — игнорируется")
        return None, None
    catalog = data.get("catalog") if isinstance(data, dict) else None
    if not isinstance(catalog, dict) or not catalog:
        warnings.append("кэш цен моделей бит (нет каталога) — игнорируется")
        return None, None
    saved_at = data.get("saved_at")
    return catalog, (saved_at if isinstance(saved_at, str) else "")


def save_models_cache(
    catalog: dict,
    model_ids: list[str],
    project_dir: Path,
    cache_path: str | None,
    warnings: list[str],
) -> str | None:
    """После успешного чтения models.json — снапшот используемых записей.

    Записываются только используемые модели (референс + tier-списки):
    id, name, cost.* — в структуре models.json (провайдер → models → …),
    чтобы :func:`model_costs_from_catalog` читал снапшот напрямую.
    Пишется только при успешном чтении; сбой записи — предупреждение.
    """
    used: dict[str, dict] = {}
    for mid in model_ids:
        provider, _, model_id = mid.partition("/")
        provider_data = catalog.get(provider)
        models = provider_data.get("models") if isinstance(provider_data, dict) else None
        model = models.get(model_id) if isinstance(models, dict) else None
        if not isinstance(model, dict):
            continue
        cost = model.get("cost")
        if not isinstance(cost, dict):
            continue
        used[mid] = {
            "name": model.get("name") or model_id,
            "cost": {k: cost.get(k, 0) for k in _COST_KEYS if k in cost},
        }
    if not used:
        return None
    saved_at = time.strftime("%Y-%m-%d")
    snap_catalog: dict[str, dict] = {}
    for mid, rec in used.items():
        provider, _, model_id = mid.partition("/")
        snap_catalog.setdefault(provider, {}).setdefault("models", {})[model_id] = rec
    data = {"version": 1, "saved_at": saved_at, "catalog": snap_catalog}
    f = _models_cache_file(project_dir, cache_path)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        warnings.append(f"кэш цен моделей не записан: {e}")
        return None
    return saved_at


def model_costs_from_catalog(
    catalog: dict | None, reference_model: str, warnings: list[str]
) -> dict[str, float] | None:
    """Цены одной модели из каталога (за 1M токенов) или None."""
    provider, _, model_id = reference_model.partition("/")
    provider_data = catalog.get(provider) if isinstance(catalog, dict) else None
    models = provider_data.get("models") if isinstance(provider_data, dict) else None
    model = models.get(model_id) if isinstance(models, dict) else None
    cost = model.get("cost") if isinstance(model, dict) else None
    if not isinstance(cost, dict):
        warnings.append(
            f"модель {reference_model} не найдена в источнике цен "
            "(models.json / кэш) — цена неизвестна"
        )
        return None
    return {k: float(cost.get(k, 0) or 0) for k in _COST_KEYS}


def load_reference_costs(
    models_path: Path, reference_model: str, warnings: list[str]
) -> dict[str, float] | None:
    """Цены референс-модели из models.json (за 1M токенов) или None."""
    catalog = load_models_catalog(models_path, warnings)
    if catalog is None:
        return None
    return model_costs_from_catalog(catalog, reference_model, warnings)


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


def _model_total(totals: dict, costs: dict[str, float]) -> tuple[float, float]:
    """Итог $ и стоимость cache-read $ (токены × цены ÷ 1e6) по суммам воркеров."""
    inp = totals["win"] * costs["input"] / 1e6
    outp = totals["wout"] * costs["output"] / 1e6
    cr = totals["wcr"] * costs["cache_read"] / 1e6
    cw = totals["wcw"] * costs["cache_write"] / 1e6
    return inp + outp + cr + cw, cr


def build_tier_rows(
    totals: dict,
    catalog: dict | None,
    models: list[str],
    warnings: list[str],
) -> list[dict]:
    """Строки «что если бы» для списка моделей, отсортированные по итогу, рост.

    Строки без цены (модель нет в каталоге) идут последними с честной
    пометкой — раздел отчёта не падает.
    """
    if catalog is None:
        warnings.append(
            "models.json недоступен — цены таблицы «что если бы» неизвестны"
        )
    rows: list[dict] = []
    for model in models:
        costs = (
            None if catalog is None
            else model_costs_from_catalog(catalog, model, warnings)
        )
        if costs is None:
            rows.append(
                {
                    "model": model,
                    "costs": None,
                    "total": None,
                    "cache_read_share": None,
                    "known": False,
                }
            )
            continue
        total, cr_cost = _model_total(totals, costs)
        share = (cr_cost / total * 100.0) if total > 0 else 0.0
        rows.append(
            {
                "model": model,
                "costs": costs,
                "total": total,
                "cache_read_share": share,
                "known": True,
            }
        )
    known = sorted((r for r in rows if r["known"]), key=lambda r: r["total"])
    unknown = [r for r in rows if not r["known"]]
    return list(known) + unknown


def build_tier_tables(
    totals: dict,
    catalog: dict | None,
    tiers: dict[str, list[str]],
    warnings: list[str],
) -> dict[str, list[dict]]:
    """U8b часть A: строки по классам {top, optimal, cheap} (конфиг ``metrics.tiers``)."""
    return {
        name: build_tier_rows(totals, catalog, models, warnings)
        for name, models in tiers.items()
    }


def builtin_subscriptions_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "subscriptions.json"


def load_builtin_subscriptions() -> dict:
    """Встроенная таблица подписок (``awf/data/subscriptions.json``)."""
    try:
        data = json.loads(builtin_subscriptions_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"plans": []}
    except (OSError, json.JSONDecodeError):
        return {"plans": []}


def _num_gt0(v: Any) -> bool:
    """Число (не bool), строго больше нуля."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def validate_subscriptions(data: Any) -> list[dict] | None:
    """Планы валидной таблицы подписок или None (битый формат).

    Формат: список планов либо объект с ключом ``plans``; каждый план —
    dict с ``name`` (str) и ``price_usd_month`` (число). План с
    ``price_unverified: true`` валиден и без цены (U8d: цена не
    опубликована — не выдумывать).
    """
    plans = data.get("plans") if isinstance(data, dict) else data
    if not isinstance(plans, list):
        return None
    out: list[dict] = []
    for p in plans:
        if not isinstance(p, dict):
            return None
        if not isinstance(p.get("name"), str) or not p["name"]:
            return None
        price = p.get("price_usd_month")
        price_ok = isinstance(price, (int, float)) and not isinstance(price, bool)
        if not price_ok and not p.get("price_unverified"):
            return None
        out.append(p)
    return out


def _http_get(url: str, timeout: float = SUBSCRIPTIONS_TIMEOUT) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def refresh_subscription_table(
    url: str, warnings: list[str]
) -> tuple[dict | None, str | None]:
    """U8b: GET таблицы подписок. (data, None) при успехе, (None, причина)."""
    try:
        raw = _http_get(url)
    except Exception as e:  # noqa: BLE001 — сеть: любой исход = фолбэк
        return None, f"{type(e).__name__}: {e}"
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"битый JSON: {e}"
    if validate_subscriptions(data) is None:
        return None, "JSON не в формате таблицы (нужны plans с name и price_usd_month)"
    return data, None


DEFAULT_SUBSCRIPTIONS_CACHE = ".agentic/state/metrics_subscriptions.json"


def resolve_subscriptions_table(
    project_dir: Path,
    *,
    refresh: bool,
    url: str | None,
    cache_path: str | None = None,
    warnings: list[str],
) -> dict:
    """U8b/U8d: таблица подписок — живой fetch, кэш, встроенная.

    Порядок (U8d): refresh (``--refresh-subscriptions``, GET + запись
    кэша) → кэш → встроенная. Неудачный refresh не перескакивает через
    кэш: используется кэш + ``refresh_error`` с причиной (строка «данные
    от <as_of>, обновление не удалось: …» в отчёте строится из ``as_of``
    и ``refresh_error``). Кэш пишется только при успешном fetch; битый —
    игнорируется с предупреждением.

    Возвращает ``{"plans", "as_of", "source", "label", "refresh_error"}``.
    """
    cache_file = Path(
        cache_path or DEFAULT_SUBSCRIPTIONS_CACHE
    ).expanduser()
    if not cache_file.is_absolute():
        cache_file = project_dir / cache_file

    def _table(data: dict, source: str, label: str, refresh_error: str = "") -> dict:
        as_of = data.get("as_of")
        return {
            "plans": validate_subscriptions(data) or [],
            "as_of": as_of if isinstance(as_of, str) else "",
            "source": source,
            "label": label,
            "refresh_error": refresh_error,
        }

    def _from_cache() -> dict | None:
        """Валидный кэш или None (битый — игнорируется с предупреждением)."""
        if not cache_file.exists():
            return None
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            warnings.append(f"кэш подписок бит ({e}) — игнорируется")
            return None
        if validate_subscriptions(cached) is not None:
            return _table(cached, "cache", "кэш подписок")
        warnings.append("кэш подписок бит (формат) — игнорируется")
        return None

    builtin = load_builtin_subscriptions()
    if refresh:
        if not url:
            warnings.append(
                "metrics.subscriptions_url не задан — refresh невозможен, "
                "использована встроенная таблица подписок"
            )
            return _table(
                builtin, "builtin", "встроенная таблица",
                "metrics.subscriptions_url не задан",
            )
        data, err = refresh_subscription_table(url, warnings)
        if data is not None:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as e:
                warnings.append(f"кэш подписок не записан: {e}")
            return _table(data, "url", f"обновлено по {url}")
        warnings.append(f"обновление подписок не удалось: {err}")
        cached_table = _from_cache()
        if cached_table is not None:
            cached_table["refresh_error"] = err or ""
            return cached_table
        return _table(builtin, "builtin", "встроенная таблица", err or "")
    cached_table = _from_cache()
    if cached_table is not None:
        return cached_table
    return _table(builtin, "builtin", "встроенная таблица")


def build_subscriptions(totals: dict, table: dict) -> dict:
    """U8b/U8d: «объём воркеров ≈ N подписок/мес» для каждого плана.

    База: usage = in + out воркеров за окно статистики.
    Cache-read/cache-write не входят — это переиспользование контекста,
    а не объём работы. Пессимистичный базис (все токены, включая кэш)
    считается рядом как ``usage_incl_cache``/``subs_incl_cache_exact`` —
    завышенная оценка «если вендор считает и кэш».

    Лимиты: токенные — напрямую; сообщение-лимиты — через
    ``assumed_tokens_per_message`` (пометка «оценка»). GLM (U8d): лимит —
    ``credits_week``; кредиты окна = ``win×input + wcr×cached_input +
    wout×output`` ÷ 10 000 (множители ``credit_multipliers``). Cache-read
    входит в кредиты — так устроена кредитная модель GLM (в отличие от
    токенной базы). Доля месяца = кредиты ÷ credits_week ÷
    :data:`WEEKS_PER_MONTH`.

    Каждый план несёт две оценки: ``subs_exact`` — точная дробная доля
    (1.2) и ``subs`` — «покупать», ceil (2). План с
    ``price_unverified`` (цена не опубликована) — ``price``/``cost`` None:
    доля считается, стоимость не выдумывается.
    """
    usage = totals["win"] + totals["wout"]
    usage_incl_cache = usage + totals["wcr"] + totals["wcw"]
    cr = totals["wcr"]
    cr_share = (cr / usage_incl_cache * 100.0) if usage_incl_cache > 0 else 0.0
    plans: list[dict] = []
    notes: list[str] = []
    for p in table.get("plans", []):
        name = p.get("name", "?")
        price_unverified = bool(p.get("price_unverified"))
        price = None if price_unverified else float(p["price_usd_month"])
        assumed = bool(p.get("assumed"))
        limit_tokens: float | None = None
        limit_label = ""
        credits_week: float | None = None
        mult: dict[str, float] | None = None
        if p.get("limit_model") == "credits" and _num_gt0(p.get("credits_week")):
            credits_week = float(p["credits_week"])
            m = p.get("credit_multipliers")
            if not isinstance(m, dict):
                m = {}
            mult = {
                "input": float(m.get("input") or 0),
                "cached_input": float(m.get("cached_input") or 0),
                "output": float(m.get("output") or 0),
            }
            limit_label = (
                f"{credits_week:,.0f} кредитов/нед"
                + (f" (5ч: {p['credits_5h']:,.0f})" if _num_gt0(p.get("credits_5h")) else "")
            )
        elif _num_gt0(p.get("limit_tokens_month")):
            limit_tokens = float(p["limit_tokens_month"])
            limit_label = f"{limit_tokens:,.0f} токенов/мес"
        elif _num_gt0(p.get("limit_messages_period")):
            period = str(p.get("period") or "")
            tpm = p.get("assumed_tokens_per_message")
            if period != "month":
                notes.append(
                    f"План {name}: период «{period or 'не задан'}» не поддерживается — не рассчитан"
                )
                continue
            if not isinstance(tpm, (int, float)) or isinstance(tpm, bool) or tpm <= 0:
                notes.append(
                    f"План {name}: нет assumed_tokens_per_message — не рассчитан"
                )
                continue
            limit_tokens = float(p["limit_messages_period"]) * float(tpm)
            limit_label = (
                f"{limit_tokens:,.0f} токенов/мес" + (" (оценка)" if assumed else "")
            )
        else:
            notes.append(
                f"План {name}: нет лимита (limit_tokens_month / "
                "limit_messages_period / credits_week) — не рассчитан"
            )
            continue
        offpeak_raw = p.get("offpeak_multiplier")
        offpeak_mult = (
            float(offpeak_raw)
            if _num_gt0(offpeak_raw) and offpeak_raw < 1
            else None
        )
        if credits_week is not None:
            # GLM: кредиты окна; cache-read входит в кредиты
            credits = (
                totals["win"] * mult["input"]
                + totals["wcr"] * mult["cached_input"]
                + totals["wout"] * mult["output"]
            ) / 10_000
            subs_exact = credits / credits_week / WEEKS_PER_MONTH
            subs_incl_cache_exact = subs_exact  # кэш уже в кредитах
        else:
            credits = None
            subs_exact = usage / limit_tokens if usage > 0 else 0.0
            subs_incl_cache_exact = (
                usage_incl_cache / limit_tokens if usage_incl_cache > 0 else 0.0
            )
        subs = math.ceil(subs_exact) if subs_exact > 0 else 0
        subs_incl_cache = (
            math.ceil(subs_incl_cache_exact) if subs_incl_cache_exact > 0 else 0
        )
        cost = None if price is None else subs * price
        offpeak = None
        if credits is not None and offpeak_mult is not None:
            op_credits = credits * offpeak_mult
            op_exact = op_credits / credits_week / WEEKS_PER_MONTH
            offpeak = {
                "multiplier": offpeak_mult,
                "credits": op_credits,
                "subs_exact": op_exact,
                "subs": math.ceil(op_exact) if op_exact > 0 else 0,
            }
        plans.append(
            {
                "name": name,
                "family": str(p.get("family") or ""),
                "price": price,
                "price_unverified": price_unverified,
                "limit_tokens": limit_tokens,
                "limit_label": limit_label,
                "subs_exact": subs_exact,
                "subs": subs,
                "cost": cost,
                "subs_incl_cache_exact": subs_incl_cache_exact,
                "subs_incl_cache": subs_incl_cache,
                "assumed": assumed,
                "credits": credits,
                "offpeak": offpeak,
            }
        )
    unverified = [pl["name"] for pl in plans if pl["price_unverified"]]
    if unverified:
        notes.append(
            "Цены не опубликованы в документации: " + ", ".join(unverified)
            + " — не выдумываем (price_unverified): доля считается, стоимость нет"
        )
    return {
        "usage": usage,
        "usage_incl_cache": usage_incl_cache,
        "cache_read": cr,
        "cache_read_share": cr_share,
        "as_of": table.get("as_of", ""),
        "source": table.get("source", ""),
        "label": table.get("label", ""),
        "refresh_error": table.get("refresh_error", ""),
        "plans": plans,
        "notes": notes,
    }


def _render_model_table(rows: list[dict]) -> list[str]:
    """Таблица «что если бы»; минимум по итогу — жирным."""
    out = [
        "| Модель | $/M in | $/M out | $/M cache read | Итог $ | Доля cache-read |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    min_total = min((r["total"] for r in rows if r["known"]), default=None)
    for r in rows:
        if not r["known"]:
            out.append(f"| {r['model']} | — | — | — | цена неизвестна | — |")
            continue
        c = r["costs"]
        if min_total is not None and r["total"] == min_total:
            total = f"**${r['total']:.2f}**"
        else:
            total = f"${r['total']:.2f}"
        out.append(
            f"| {r['model']} | {c['input']:g} | {c['output']:g} | {c['cache_read']:g} | "
            f"{total} | {r['cache_read_share']:.1f}% |"
        )
    return out


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
    tier_tables: dict[str, list[dict]] | None = None,
    subscriptions: dict | None = None,
    model_price_source: str = "",
    scope: str = "",
) -> str:
    lines = [f"# Метрики программы — снимок: {project_name}", ""]
    lines.append(f"Дата снимка: {generated_at}. Источник: opencode.db + git.")
    if scope:
        lines.append(f"Область: {scope}.")
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
    if model_price_source:
        lines.append(f"Цены моделей: {model_price_source}.")
    if tier_tables:
        lines.append("")
        lines.append("## 🏆 Топ-модели")
        lines.append("")
        lines.append(
            f"Наши токены (in {totals['win']:,} / out {totals['wout']:,} / "
            f"cache-read {totals['wcr']:,} / cache-write {totals['wcw']:,}) × "
            "цены модели, $/M токенов. Строки — по итогу, рост; минимум — жирным."
        )
        lines.append("")
        lines.extend(_render_model_table(tier_tables.get("top", [])))
        lines.append("")
        lines.append("## ⚖️ Оптимум")
        lines.append("")
        lines.extend(_render_model_table(tier_tables.get("optimal", [])))
        best = next(
            (r for r in tier_tables.get("optimal", []) if r["known"]), None
        )
        if best:
            lines.append("")
            lines.append(
                f"Лучший выбор по цене среди оптимума: {best['model']} — "
                f"**${best['total']:.2f}**."
            )
        lines.append("")
        lines.append("## 💡 Дешёвые")
        lines.append("")
        cheap = [r for r in tier_tables.get("cheap", []) if r["known"]]
        if cheap:
            parts = " / ".join(
                f"{r['model']} ≈ **${r['total']:.2f}**" for r in cheap
            )
            lines.append(f"Самые дешёвые: {parts}.")
        else:
            lines.append("Цены неизвестны (модели не найдены в каталоге).")
    if subscriptions:
        lines.append("")
        lines.append("## 💳 Подписки (GPT / Claude / GLM)")
        lines.append("")
        lines.append(
            f"Объём воркеров за окно: **{subscriptions['usage']:,} токенов** "
            f"(in {totals['win']:,} / out {totals['wout']:,}; cache-read "
            f"{subscriptions['cache_read']:,} и cache-write {totals['wcw']:,} не входят "
            "— это переиспользование контекста, а не объём работы). "
            "«Нужно (точно)» — точная доля подписки (один знак); "
            "«Покупать» — округление вверх в целые (1.2 и 2 — большая разница). "
            "Лимиты, переведённые из сообщений, помечены «оценка»; у GLM лимит — "
            "кредиты/нед, и cache-read входит в кредиты (так устроена "
            "кредитная модель GLM)."
        )
        lines.append("")
        lines.append(
            "| План | Семейство | $/мес | Лимит | Нужно (точно) | Покупать | ≈ Стоимость, $/мес |"
        )
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        ordered = sorted(
            subscriptions["plans"],
            key=lambda p: (p.get("family") or "", p["name"]),
        )
        for p in ordered:
            price_txt = "—" if p["price"] is None else f"{p['price']:g}"
            if p["price"] is None:
                cost_txt = "— (цена не опубликована)"
            else:
                cost_txt = f"≈ {p['subs']} × ${p['price']:g} = **${p['cost']:g}**"
            lines.append(
                f"| {p['name']} | {p.get('family') or '—'} | {price_txt} | "
                f"{p['limit_label']} | {p['subs_exact']:.1f} | **{p['subs']}** | "
                f"{cost_txt} |"
            )
        for n in subscriptions["notes"]:
            lines.append(f"- {n}")
        offpeak = [p for p in ordered if p.get("offpeak")]
        if offpeak:
            parts = ", ".join(
                f"{p['name']} — {p['offpeak']['subs_exact']:.1f} ({p['offpeak']['subs']})"
                for p in offpeak
            )
            lines.append(
                f"Off-peak (кредиты ×{offpeak[0]['offpeak']['multiplier']:g}, "
                f"вне Пн–Пт 14:00–18:00 SGT): {parts}"
            )
        pes = [
            f"{p['name']} — {p['subs_incl_cache_exact']:.1f} ({p['subs_incl_cache']})"
            for p in ordered
            if p.get("subs_incl_cache", 0) > p["subs"]
        ]
        if pes:
            lines.append(
                "Пессимистично, если вендор считает и кэш: "
                + ", ".join(pes)
                + " (завышенная оценка)."
            )
        lines.append("")
        data_line = f"Данные: {subscriptions['label']}"
        if subscriptions["as_of"]:
            data_line += f", данные от {subscriptions['as_of']}"
        if subscriptions.get("refresh_error"):
            data_line += f", обновление не удалось: {subscriptions['refresh_error']}"
        lines.append(data_line + ".")
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


def _mirror_report(report_path: Path, mirror_dir: str, warnings: list[str]) -> str | None:
    """U8c: копия отчёта в ``metrics.mirror_dir`` (то же имя).

    Каталог создаётся; совпадение с исходным путём — тихо пропускается;
    ошибка копирования — предупреждение в ``warnings`` (сбор не падает).
    """
    dest_dir = Path(mirror_dir).expanduser()
    dest = dest_dir / report_path.name
    if dest.resolve() == report_path.resolve():
        return str(dest)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report_path, dest)
    except OSError as e:
        warnings.append(f"зеркало отчёта не записано ({dest_dir}): {e}")
        return None
    return str(dest)


def collect_metrics(
    project_dir: str | Path,
    *,
    db_path: str | Path | None = None,
    models_path: str | Path | None = None,
    reference_model: str | None = None,
    since: str | int | float | None = None,
    out: str | None = None,
    refresh_subscriptions: bool = False,
    mirror: bool = True,
    all_projects: bool = False,
) -> MetricsResult:
    """Собрать метрики программы и записать markdown-отчёт.

    Возвращает :class:`MetricsResult`; ``exit_code`` = 0, если измерено хоть
    что-то (воркеры / коммиты / супервизор), иначе 1. Отчёт пишется всегда
    (с пометками о том, что не измерилось).

    U8c: ``mirror=True`` (дефолт) + заданный ``metrics.mirror_dir`` — после
    успешной записи отчёт копируется в зеркало (``mirror_path`` в результате).
    ``mirror=False`` отключает копирование на один запуск.

    RUN10 #3-fix: ``all_projects=False`` (дефолт) — в отчёт попадают только
    сессии текущего проекта: их ``part.data`` содержит путь проекта
    (``session.directory`` — не дискриминатор, спавн из $HOME);
    ``all_projects=True`` — все проекты общей opencode.db (данные смешаны),
    поведение до RUN10 #3. Явный ``metrics.directory`` в конфиге —
    legacy-фильтр по ``session.directory``, побеждает оба варианта.
    Исключённые сессии других проектов — с предупреждением в отчёте;
    путь проекта без совпадений — пустые данные сессий + предупреждение.
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
    # RUN10 #3-fix: область отчёта. Явный metrics.directory — legacy-
    # фильтр по session.directory, побеждает; иначе дефолт — только
    # текущий проект (сопоставление по содержимому сессий),
    # --all-projects — все.
    directory = config_mod.get(cfg, "metrics.directory") or None
    directory = directory if isinstance(directory, str) and directory else None
    content_scope = not directory and not all_projects
    if directory:
        scope = f"проект: {directory}"
    elif all_projects:
        scope = "все проекты (данные смешаны)"
    else:
        scope = f"проект: {project_dir}"
    titles = config_mod.get(cfg, "metrics.supervisor_titles") or []
    if not isinstance(titles, list):
        warnings.append("metrics.supervisor_titles не список — супервизор не измеряется")
        titles = []
    ref_model = (
        reference_model
        or config_mod.get(cfg, "metrics.reference_model")
        or DEFAULT_REFERENCE_MODEL
    )
    # U8b: списки моделей по классам (конфиг metrics.tiers.top|optimal|cheap).
    tiers: dict[str, list[str]] = {k: list(v) for k, v in DEFAULT_TIERS.items()}
    tiers_cfg = config_mod.get(cfg, "metrics.tiers")
    if isinstance(tiers_cfg, dict):
        for k in ("top", "optimal", "cheap"):
            v = tiers_cfg.get(k)
            if isinstance(v, list) and all(isinstance(m, str) and m for m in v):
                tiers[k] = v
            elif v is not None:
                warnings.append(
                    f"metrics.tiers.{k} не список id моделей — дефолтный список"
                )
    elif tiers_cfg is not None:
        warnings.append(
            "metrics.tiers не мапа {top, optimal, cheap} — дефолтные списки"
        )
    subs_url = config_mod.get(cfg, "metrics.subscriptions_url")
    subs_url = subs_url if isinstance(subs_url, str) and subs_url else None
    subs_cache = config_mod.get(cfg, "metrics.subscriptions_cache")
    subs_cache = subs_cache if isinstance(subs_cache, str) and subs_cache else None
    models_cache = config_mod.get(cfg, "metrics.models_cache")
    models_cache = models_cache if isinstance(models_cache, str) and models_cache else None
    # U8c: архивное зеркало отчёта (пусто = выключено).
    mirror_dir = config_mod.get(cfg, "metrics.mirror_dir")
    mirror_dir = mirror_dir if isinstance(mirror_dir, str) and mirror_dir else None

    db = db_path if db_path is not None else default_db_path()
    models = models_path if models_path is not None else default_models_path()

    con = _open_db(Path(db), warnings)
    workers: dict[str, dict] = {}
    sup_per_todo: dict[str, dict] = {}
    sup_outside: dict[str, float] = {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0}
    sup_sessions = 0
    # RUN10 #3-fix: content-область — один проход по part для кандидатов.
    matched: frozenset[str] | None = None
    if con is not None and content_scope:
        cands = candidate_session_ids(con, titles, warnings)
        matched = matched_session_ids(con, cands, str(project_dir), warnings)
        if not matched:
            warnings.append(
                "не удалось сопоставить сессии: путь проекта не найден ни в "
                "одной сессии — данные сессий не измеряются (коммиты юнитов "
                "измеряются)"
            )
    if con is not None:
        workers = collect_workers(con, since_ms, directory, matched, warnings)
    commits = collect_commit_info(project_dir, warnings)

    todos = sorted(set(workers) | set(commits))
    first_ts = {t: d["first"] for t, d in workers.items() if d.get("first")}
    # fallback start для юнитов без воркерских сессий — время первого коммита
    for t, shas in commits.items():
        first_ts.setdefault(t, min(ct for _, ct in shas) * 1000)
    windows = build_windows(project_dir, todos, first_ts, commits, now_ms)

    if con is not None:
        sup_per_todo, sup_outside, sup_sessions = collect_supervisor(
            con, titles, since_ms, directory, matched, windows, warnings
        )
        con.close()

    diff = collect_code_lines(project_dir, commits)
    catalog = load_models_catalog(Path(models), warnings)
    # U8d: цены моделей — models.json (свежие) + снапшот кэша; при
    # недоступности models.json — кэш, иначе «цена неизвестна».
    model_price_source = "models.dev (свежие)"
    if catalog is not None:
        used_ids = list(dict.fromkeys(
            [ref_model, *[m for ms in tiers.values() for m in ms]]
        ))
        save_models_cache(catalog, used_ids, project_dir, models_cache, warnings)
    else:
        snap_catalog, saved_at = load_models_cache(project_dir, models_cache, warnings)
        if snap_catalog is not None:
            catalog = snap_catalog
            model_price_source = (
                f"кэш от {saved_at}" if saved_at else "кэш (дата неизвестна)"
            )
            warnings.append("models.json недоступен — цены из кэша моделей")
        else:
            model_price_source = "цена неизвестна (нет models.json и кэша)"
    costs = (
        None if catalog is None
        else model_costs_from_catalog(catalog, ref_model, warnings)
    )

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

    # U8b: таблицы «что если бы» по классам (часть A) + подписки (часть B).
    tier_tables = build_tier_tables(tot, catalog, tiers, warnings)
    subs_table = resolve_subscriptions_table(
        project_dir,
        refresh=refresh_subscriptions,
        url=subs_url,
        cache_path=subs_cache,
        warnings=warnings,
    )
    subscriptions = build_subscriptions(tot, subs_table)

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
        tier_tables=tier_tables, subscriptions=subscriptions,
        model_price_source=model_price_source, scope=scope,
    )
    result = MetricsResult(
        project_dir=str(project_dir),
        generated_at=generated_at,
        units=units,
        totals=dict(tot),
        workers_by_role=by_role,
        supervisor_outside=dict(sup_outside),
        conversion=conversion,
        tier_tables=tier_tables,
        subscriptions=dict(subscriptions),
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
        # U8c: зеркало — только после успешной записи отчёта.
        if mirror and mirror_dir:
            result.mirror_path = _mirror_report(out_path, mirror_dir, warnings)
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
