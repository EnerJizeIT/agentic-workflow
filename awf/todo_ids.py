"""R-06 (TODO-0112): единый парсер TODO ID.

Семантика (A-10, аудит 2026-09-25, слой 10; контракт ``metrics.md``):
- TODO ID — ``TODO-`` + 4 и больше цифр;
- правая граница явная (не-словарный символ или конец строки):
  ``TODO-10000`` не усекается до ``TODO-1000``, ``TODO-10000x`` не
  читается как ID вовсе.

Модуль — один источник для метрик (извлечение из заголовков сессий и
commit-субъектов, ``awf/metrics.py``) и публичной валидации ID
(``awf/api/run.py``, ``awf/api/dispatch.py``): ``TODO-10000`` принимается
везде одинаково.
"""
from __future__ import annotations

import re

# 4+ цифры с явной правой границей (A-10): group(1) — цифры, group(0) — ID.
TODO_ID_RE = re.compile(r"TODO-(\d{4,})(?!\w)")

# Публичная валидация: вся строка — ID. Якоря ``^...$`` + ``match`` —
# дословная семантика прежних локальных регулярных выражений
# (``run.py``/``dispatch.py``), публичный контракт не меняется.
_VALID_TODO_ID_RE = re.compile(r"^TODO-\d{4,}$")


def extract_todo_id(text: str | None) -> str | None:
    """Первый TODO ID в свободном тексте (заголовок сессии, commit-субъект).

    Возвращает полный ID (``TODO-NNNN``) или None. Правая граница явная:
    ``TODO-10000`` не усекается до ``TODO-1000``, ``TODO-10000x`` не
    читается как ID вовсе.
    """
    m = TODO_ID_RE.search(text or "")
    return m.group(0) if m else None


def is_valid_todo_id(todo_id: str | None) -> bool:
    """Вся строка — TODO ID (публичный вход: run/dispatch)."""
    return _VALID_TODO_ID_RE.match(todo_id or "") is not None
