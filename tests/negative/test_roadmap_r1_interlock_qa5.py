"""QA-находка к TODO-0218 (R1.4), роль agent-qa-review (стадия 4 пайплайна).

Тест КРАСНЫЙ на текущей реализации и доказывает поведенческий разрыв в
границах юнита (требование 2 + строка 1 матрицы приёмки R1). Прод-код не
трогается (доктрина 02; роль QA — только тесты).

**Находка (MEDIUM→HIGH, живость): отказанный параллельный ``start``
переписывает метку ExecutionId уже запущенного ребёнка и гасит его.**

``_lease.acquire`` публикует метку идентичности запуска
(``_publish_execution``) БЕЗУСЛОВНО при успешном захвате lease — ещё ДО
того, как ``start_pipeline`` проверит живость пайплайна. В ``start_pipeline``
порядок такой: ``acquire`` (``pipeline.py:1229``) → проверка живости
(``resolve``, ``:1253``). Фоновый ребёнок держит проект БЕЗ lease: родитель
освобождает lease сразу после спавна (``finally``), а сам ребёнок
(``is_bg_child``) lease не берёт и защищён только меткой ExecutionId плюс
PID-файлом.

Последовательность гонки (окно = загрузка интерпретатора ребёнка):

 1. процесс A: ``acquire`` → метка M_A; ``start_in_background`` пишет
    PID-файл ребёнка; ``finally`` освобождает lease (ребёнок ещё грузится).
 2. процесс B (через несколько мс): ``acquire`` проходит (lease свободен)
    и ПЕРЕПИСЫВАЕТ метку на M_B; ``resolve`` видит живой PID-файл ребёнка A
    → «Pipeline already running» (run_mode="noop").
 3. ребёнок A доходит до handshake: ``execution_confirmed(M_A)`` → текущая
    метка M_B ≠ M_A → False → ребёнок завершается «stale» без работы.

Итог: строка 1 матрицы R1 («один worker; второй refused/pending») даёт НОЛЬ
worker'ов — отказанный запуск молча гасит уже запущенный. Окно короткое
(старт интерпретатора ребёнка), но реальное: в матричном тесте победитель
держит lease 1.5 с сна, поэтому проигравший всегда попадает в «lease занят»;
продовый победитель освобождает lease через миллисекунды, и следующий start
успевает захватить lease и переписать метку раньше, чем ребёнок сверит id.
"""
from __future__ import annotations

import os
from pathlib import Path

import awf.api.pipeline as pl
from awf import api
from awf.api import _lease


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="QaFindings5")
    return tmp_git_repo


class TestFindingRefusedStartRotatesIdentity:
    """Отказанный старт не смеет переписывать идентичность живого запуска."""

    def test_refused_concurrent_start_keeps_running_identity(
        self, tmp_git_repo, monkeypatch
    ):
        proj = _project(tmp_git_repo)
        # Процесс A: acquire публикует метку M_A; родитель освобождает lease
        # сразу после спавна ребёнка (start_in_background + finally) — метка
        # живёт дольше заявки (это и есть её смысл).
        lease_a = _lease.acquire(proj)
        id_a = lease_a.execution_id
        assert id_a, "acquire обязан выдать ExecutionId"
        lease_a.release()
        assert _lease.read_execution_id(proj) == id_a

        # Ребёнок A ещё грузится; приходит ВТОРОЙ start и видит живой
        # PID-файл ребёнка A → отказ «already running» (строка 1 матрицы R1).
        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (True, os.getpid(), "state")
        )
        res = pl.start_pipeline(proj, background=False)
        assert res.run_mode == "noop", res.message
        assert "already running" in res.message.lower(), res.message

        # Отказанный старт обязан НЕ трогать идентичность живого ребёнка.
        # Иначе ребёнок A провалит handshake и завершится — на выходе ноль
        # worker'ов вместо «один worker + один refused».
        assert _lease.read_execution_id(proj) == id_a, (
            "отказанный параллельный start переписал метку ExecutionId: "
            "acquire публикует метку ДО проверки живости пайплайна"
        )
        assert _lease.execution_confirmed(proj, id_a) is True, (
            "уже запущенный ребёнок (его метку переписал ОТКАЗАННЫЙ второй "
            "start) провалит handshake и умрёт — матрица R1 строка 1 даёт "
            "НОЛЬ worker'ов"
        )
