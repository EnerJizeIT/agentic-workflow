"""A38 (TODO-0351): ожидание exec ребёнка перед чтением его argv.

Живёт отдельным модулем, а не в теле тест-файла: самих пользователей
помощника немного, а `test_run_next_failure_transitions.py` уже стоит у
порога храповика «файлов длиннее 400 строк».
"""
from __future__ import annotations

import time


def wait_looks_like_awf_start(
    pid: int, timeout: float = 5.0, step: float = 0.05
) -> None:
    """Дождаться exec ребёнка, пока его argv не станет ``... -m awf start``.

    После ``Popen`` ядро копирует argv родителя: в окно fork→exec
    ``/proc/<pid>/cmdline`` отвечает argv'ом pytest, и одиночное чтение
    ложно объявляет процесс чужим (флейк только в CI). Поэтому поллим, и
    проверка остаётся строгой: не дождались — AssertionError с последним
    прочитанным argv, а не молчаливый пропуск.

    Имя говорит ровно то, что проверяется — ФОРМА argv
    (``cmdline_is_ours``), без привязки к проекту: в тестах так выглядит
    намеренно ЧУЖОЙ процесс, и ``--project-dir`` разбирает уже
    ``_liveness.resolve``.
    """
    from awf.api import _liveness

    deadline = time.monotonic() + timeout
    seen: str | None = None
    while True:
        seen = _liveness.read_cmdline(pid)
        if seen is not None and _liveness.cmdline_is_ours(seen):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(step)
    raise AssertionError(
        f"за {timeout:g}s argv процесса {pid} так и не стал '-m awf start': "
        f"{seen!r}"
    )
