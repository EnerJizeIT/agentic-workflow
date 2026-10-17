"""QA-находка к TODO-0218 (R1.4), роль agent-qa-review (стадия 4 пайплайна).

Тест КРАСНЫЙ на текущей реализации и доказывает поведенческий разрыв в
границах юнита. Прод-код не трогается (доктрина 02 — правка вперёд точечно;
роль QA — только тесты).

Находка (требование 2 юнита: «child стартует с ExecutionId; до подтверждения
reservation работу не выполняет»; класс уже закрытого F-1): reset метку
ExecutionId СОХРАНЯЕТ и ПЕРЕЗАПИСЫВАЕТ свежим id (``lifecycle.py:1087``
пропускает ``LEASE_NAME`` и ``EXECUTION_NAME`` в цикле очистки ``logs/``,
затем ``rotate_execution_id`` на ``:1142``). force-init идёт ДРУГОЙ дверью:
``_clean_runtime`` делает ``shutil.rmtree`` по всему ``logs`` (он в
``INIT_RUNTIME_DIRS``), стирая метку; ``rotate_execution_id`` в его пути не
зовётся. После force-init ``read_execution_id`` даёт ``None``, а
``execution_confirmed`` трактует «нет метки = работай» → crash-orphan ребёнок
СОСТОЯВШЕГОСЯ ранее запуска (его launch умер, заявка мертва, поэтому
destructive-preflight force-init пропускает) проходит handshake и запускает
второй пайплайн поверх заново инициализированного проекта.

F-H1 уже ввёл force-init в разряд разрушительных операций (общий
``destructive_section`` + ``destructive_blockers``) для S3-03 — идентичность
запуска обязана переживать его так же, как reset. Юнит покрывает reset
(``TestResetRotatesExecutionIdentity``), но у force-init парного теста нет.
"""
from __future__ import annotations

from pathlib import Path

from awf import api
from awf.api import _lease


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="QaFindings4")
    return tmp_git_repo


class TestForceInitDropsExecutionIdentity:
    """F-1 через дверь force-init: метка идентичности обязана пережить снос."""

    def test_force_init_preserves_launch_identity_marker(self, tmp_git_repo):
        """force-init обязан СОХРАНИТЬ и СМЕНИТЬ метку ExecutionId.

        Состоявшийся запуск оставил метку ``stale_id``; его процесс умер
        (lease освобождён), поэтому force-init не блокируется preflight'ом.
        После force-init crash-orphan того запуска НЕ должен проходить
        handshake. Сейчас ``logs`` сносится целиком → метки нет →
        ``execution_confirmed(stale_id)`` возвращает True.
        """
        proj = _project(tmp_git_repo)
        lease = _lease.acquire(proj)  # публикует метку идентичности запуска
        stale_id = lease.execution_id
        assert stale_id, "acquire обязан выдать ExecutionId"
        lease.release()  # launch умер; метка живёт дольше заявки
        assert _lease.read_execution_id(proj) == stale_id

        api.init_project(proj, force=True)

        marker = proj / ".agentic" / "logs" / _lease.EXECUTION_NAME
        assert marker.is_file(), (
            "force-init снёс метку ExecutionId: она для handshake — не "
            "runtime-данные, а страховка (та же категория, что lease; F-1 "
            "требует перечислять identity-файлы явно)"
        )
        assert _lease.execution_confirmed(proj, stale_id) is False, (
            "crash-orphan ребёнок запуска ДО force-init прошёл handshake "
            "(«метки нет = работай») и запустит второй пайплайн поверх "
            "заново инициализированного проекта — та же дыра, что F-1 "
            "закрыл для reset"
        )

    def test_force_init_leaves_a_fresh_marker(self, tmp_git_repo):
        """После force-init метка обязана существовать и быть непустой.

        «Файла нет = работай» — то самое правило, которое ротация метки и
        закрывает. force-init, как и reset, обязан оставить свежий id.
        """
        proj = _project(tmp_git_repo)
        marker = proj / ".agentic" / "logs" / _lease.EXECUTION_NAME
        if marker.exists():
            marker.unlink()
        api.init_project(proj, force=True)
        assert _lease.read_execution_id(proj), (
            "после force-init метка обязана существовать (свежий id); "
            "отсутствие метки вновь открывает путь «нет метки = работай»"
        )
