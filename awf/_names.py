"""Unique-name helpers shared by todos.archive_todo and api.hygiene.

One mechanism, two call conventions (consolidated in wave 6.3 — the pair
was _suffixed_name in awf/todos.py and _unique_name in awf/api/hygiene.py):
``reserve_base`` decides whether the base name itself may win
(``False`` — first free name, base included) or is pre-occupied
(``True`` — the result always carries a ``-N`` suffix, starting at 1).
"""
from __future__ import annotations

from pathlib import Path


def unique_name(dest_dir: Path, name: str, reserve_base: bool = False) -> str:
    """A name that does not exist in ``dest_dir`` yet (never overwrite).

    ``PROGRESS-TODO-0001.md`` → ``PROGRESS-TODO-0001.md``, then
    ``PROGRESS-TODO-0001-1.md``, ``PROGRESS-TODO-0001-2.md``, ...
    With ``reserve_base=True`` the base name is treated as pre-occupied,
    so the first candidate is already the first suffix.
    """
    stem, dot, ext = name.partition(".")

    def _suffixed(n: int) -> str:
        return f"{stem}-{n}.{ext}" if dot else f"{name}-{n}"

    next_suffix = 1
    candidate = _suffixed(next_suffix) if reserve_base else name
    while (dest_dir / candidate).exists():
        candidate = _suffixed(next_suffix)
        next_suffix += 1
    return candidate
