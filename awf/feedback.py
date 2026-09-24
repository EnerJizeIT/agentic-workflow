"""RUN4 #2: ``awf feedback`` — фидбек-контур (отчёт супервизора владельцу).

Фрикция с инструментом превращается в структурированный отчёт на рабочем
столе владельца (config ``feedback.dir``, дефолт ``~/Desktop``; каталог
создаётся). Отчёт собирается сам:

- **шапка-факты** — версия awf, проект, фаза, забег (активен/позиция/
  no_checkpoints), текущая задача, git sha awf-репо (best-effort:
  editable-путь → ``git rev-parse --short HEAD``), дата;
- **скелет** «Что пытался / Ожидал / Что получил / Почему мешает /
  Предложение» — ``body`` вставляется в «Что пытался»,
  ``expected``/``got``/``why``/``proposal`` — в остальные секции;
  пустые секции не печатаются (RUN10 #2 — никаких пустых заголовков);
- **хвост последнего лога** проекта (≤20 строк, если файл есть).

Имя файла: ``awf-<bug|feature>-<YYYYMMDD>-<slug>.md``; повторный вызов в
тот же день с тем же слагом — суффикс ``-2``/``-3`` (не перезапись).
``stdout=True`` — печатает текст, файл не пишет.

Секреты: отчёт собирается только из фактов, ``body`` и хвоста лога —
``os.environ`` не читается вовсе.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ._atomic import atomic_write_text
from ._errors import AwfApiError

VALID_TYPES = ("bug", "feature")
VALID_SEVERITIES = ("low", "medium", "high")

_TYPE_TITLES = {
    "bug": "Баг-репорт awf",
    "feature": "Фича-реквест awf",
}

# Скелет тела: (заголовок, имя параметра, который его заполняет).
# RUN10 #2: секция с пустым текстом не печатается вовсе.
_SKELETON_SECTIONS = (
    ("Что пытался", "body"),
    ("Ожидал", "expected"),
    ("Что получил", "got"),
    ("Почему мешает", "why"),
    ("Предложение", "proposal"),
)

_LOG_TAIL_LINES = 20
_SLUG_MAX = 60

# Простая транслитерация кириллицы (латиница — как есть).
_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


@dataclass
class FeedbackResult:
    """Result of :func:`awf.api.feedback` (RUN4 #2 feedback contour)."""

    file: str  # "" when stdout=True
    report_dir: str  # "" when stdout=True
    slug: str
    ftype: str  # "bug" | "feature"
    report: str  # full report text (always filled)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def slugify(title: str) -> str:
    """Слаг из title: ASCII — как есть, кириллица — транслитерация.

    Остальные символы отбрасываются; пустой результат (букв нет вовсе) →
    ``report``. Длина ≤ :data:`_SLUG_MAX` (обрезка по границе слова).
    """
    out: list[str] = []
    for ch in title.lower():
        if ch.isascii():
            out.append(ch if ch.isalnum() else "-")
        elif ch in _CYRILLIC:
            out.append(_CYRILLIC[ch])
        # не-ASCII без транслита — отбрасываем
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    if len(slug) > _SLUG_MAX:
        slug = slug[:_SLUG_MAX].rsplit("-", 1)[0]
    return slug or "report"


def _awf_repo_facts() -> str:
    """Best-effort: editable-путь → ``git rev-parse --short HEAD``.

    Возвращает ``<sha> (<путь>)`` или ``""`` (wheel-установка, нет git,
    git упал — деградация, не ошибка).
    """
    try:
        import awf as _awf

        from . import git_utils

        cur = Path(_awf.__file__).resolve().parent
        for _ in range(8):
            git_dir = cur / ".git"
            if git_dir.exists():
                sha = git_utils.git_stdout(
                    cur, "rev-parse", "--short", "HEAD", check=False
                ).strip()
                return f"{sha} ({cur})" if sha else str(cur)
            if cur.parent == cur:
                break
            cur = cur.parent
    except Exception:  # noqa: BLE001 — best-effort, деградация до ""
        return ""
    return ""


def _run_line(project_dir: Path) -> str:
    """Забег: нет / активен, позиция N/M (+no_checkpoints)."""
    from . import run_state

    try:
        run = run_state.read_run(project_dir)
    except Exception:  # noqa: BLE001 — corrupt state degrades to "нет"
        run = None
    if not run or not run.get("active"):
        return "Забег: нет"
    pos = run_state.position(run)
    extra = " (no_checkpoints)" if run.get("no_checkpoints") else ""
    return f"Забег: активен, позиция {pos}{extra}"


def _current_todo(project_dir: Path) -> str:
    from . import todos

    try:
        return todos.newest_active(project_dir) or "—"
    except Exception:  # noqa: BLE001 — degrades to "—"
        return "—"


def _log_tail(project_dir: Path) -> tuple[str, list[str]] | None:
    """Свежайший ``.agentic/logs/*.out``: (имя, последние ≤20 строк)."""
    from . import paths

    logs = paths.logs_dir(project_dir)
    if not logs.is_dir():
        return None
    candidates = [p for p in logs.glob("*.out") if p.is_file()]
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        lines = newest.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    tail = lines[-_LOG_TAIL_LINES:]
    if not tail:
        return None
    return newest.name, tail


def render_report(
    project_dir: Path,
    *,
    ftype: str,
    title: str,
    body: str = "",
    severity: str = "",
    expected: str = "",
    got: str = "",
    why: str = "",
    proposal: str = "",
) -> str:
    """Собрать текст отчёта (факты + скелет + хвост лога).

    RUN10 #2: ``expected``/``got``/``why``/``proposal`` заполняют секции
    «Ожидал»/«Что получил»/«Почему мешает»/«Предложение»; секция с пустым
    текстом не печатается (ни заголовок, ни пустое тело).
    """
    import awf as _awf

    from . import config as _config
    from . import phase as _phase

    project_dir = Path(project_dir).resolve()
    cfg = _config.load(project_dir)
    name = _config.get(cfg, "project.name") or project_dir.name

    facts = [f"Дата: {datetime.now():%Y-%m-%d}"]
    facts.append(f"Проект: {name} (`{project_dir}`)")
    try:
        facts.append(f"Фаза: {_phase.detect_phase(project_dir)}")
    except Exception:  # noqa: BLE001 — degrades to "—"
        facts.append("Фаза: —")
    facts.append(_run_line(project_dir))
    facts.append(f"Текущая задача: {_current_todo(project_dir)}")
    awf_line = f"awf: {_awf.__version__}"
    repo = _awf_repo_facts()
    if repo:
        awf_line += f" (репо: {repo})"
    facts.append(awf_line)
    if severity:
        facts.insert(1, f"Важность: {severity}")

    lines = [f"# {_TYPE_TITLES[ftype]}: {title}", ""]
    lines.extend(facts)
    lines.append("")

    section_texts = {
        "body": body,
        "expected": expected,
        "got": got,
        "why": why,
        "proposal": proposal,
    }
    for section, field in _SKELETON_SECTIONS:
        text = (section_texts[field] or "").strip()
        if not text:
            continue  # RUN10 #2: пустые секции не печатаются
        lines.append(f"## {section}")
        lines.append("")
        lines.append(text)
        lines.append("")

    tail = _log_tail(project_dir)
    if tail:
        log_name, log_lines = tail
        lines.append(f"## Хвост последнего лога (`{log_name}`)")
        lines.append("")
        lines.append("```")
        lines.extend(log_lines)
        lines.append("```")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _default_feedback_dir() -> str:
    desktop = Path.home() / "Desktop"
    return str(desktop) if desktop.is_dir() else str(Path.home())


def feedback(
    project_dir: str | Path,
    *,
    ftype: str,
    title: str,
    body: str = "",
    severity: str = "",
    expected: str = "",
    got: str = "",
    why: str = "",
    proposal: str = "",
    stdout: bool = False,
) -> FeedbackResult:
    """Написать отчёт фидбек-контура (или вернуть текст, если ``stdout``).

    Args:
        project_dir: корень проекта (источник фактов).
        ftype: ``bug`` или ``feature``.
        title: заголовок в одну строку (из него — слаг имени файла).
        body: текст секции «Что пытался».
        severity: ``low|medium|high`` (пусто — без пометки).
        expected: текст секции «Ожидал» (RUN10 #2).
        got: текст секции «Что получил» (RUN10 #2).
        why: текст секции «Почему мешает» (RUN10 #2).
        proposal: текст секции «Предложение» (RUN10 #2).
        stdout: True — вернуть текст в ``report``, файл не писать.

    Пустые секции (включая «Что пытался» без ``body``) не печатаются —
    в отчёте нет пустых заголовков (RUN10 #2).

    Raises:
        AwfApiError: неверный ``ftype``/``severity``, пустой ``title``.
    """
    from . import config as _config

    project_dir = Path(project_dir).expanduser().resolve()
    ftype = (ftype or "").strip().lower()
    if ftype not in VALID_TYPES:
        raise AwfApiError(
            f"feedback: type must be one of {' / '.join(VALID_TYPES)} (got: {ftype!r})"
        )
    title = (title or "").strip()
    if not title:
        raise AwfApiError(
            "feedback: title is required (non-empty) — it becomes the file slug"
        )
    severity = (severity or "").strip().lower()
    if severity and severity not in VALID_SEVERITIES:
        raise AwfApiError(
            f"feedback: severity must be one of {' / '.join(VALID_SEVERITIES)} "
            f"(got: {severity!r})"
        )

    report = render_report(
        project_dir,
        ftype=ftype,
        title=title,
        body=body,
        severity=severity,
        expected=expected,
        got=got,
        why=why,
        proposal=proposal,
    )
    slug = slugify(title)

    if stdout:
        return FeedbackResult(
            file="", report_dir="", slug=slug, ftype=ftype, report=report
        )

    cfg = _config.load(project_dir)
    configured = _config.get(cfg, "feedback.dir")
    target = Path(configured).expanduser() if configured else Path(_default_feedback_dir())
    target.mkdir(parents=True, exist_ok=True)

    day = datetime.now().strftime("%Y%m%d")
    base = f"awf-{ftype}-{day}-{slug}"
    dest = target / f"{base}.md"
    n = 2
    while dest.exists():
        dest = target / f"{base}-{n}.md"
        n += 1

    atomic_write_text(dest, report)
    return FeedbackResult(
        file=str(dest), report_dir=str(target), slug=slug, ftype=ftype, report=report
    )


__all__ = ["FeedbackResult", "feedback", "slugify", "render_report",
           "VALID_TYPES", "VALID_SEVERITIES"]
