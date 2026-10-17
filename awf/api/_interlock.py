"""R1.4: одна критическая секция для запуска и разрушения (interlock).

Требование 4 юнита R1.4: «проверка и захват — одна критическая секция».
До этого модуля ``_lease.acquire`` проверял пути и публиковал заявку, а
``reset_runtime`` отдельно спрашивал «есть ли живой владелец». Между
вопросом reset и публикацией заявки запуска существовало окно: reset
видел пустоту и сносил проект, после чего запуск публиковал lease уже
поверх мёртвого каталога.

Теперь обе стороны используют один flock-файл
``.agentic/state/.interlock``:

* запуск берёт его НА ВРЕМЯ guard + публикации заявки (короткое окно, не
  на весь прогон — живой прогон защищён самим lease);
* разрушительная операция (reset/remove_orphans/rollback) берёт его на
  время preflight + удаления.

Обе стороны берут секцию НЕБЛОКИРУЮЩЕ: занятый interlock превращается
вызывающим в внятный отказ, а не в ожидание. Вечное ожидание запрещено
доктриной (03-launch-lease): застрявший владелец не должен блокировать
собственный kill/start, а вызывающий обязан получить текст, а не зависнуть.

Секция реентерабельна в пределах одного потока/процесса: reset внутри
сам вызывает ``remove_orphans``, и повторный flock того же файла через
новый fd заблокировал бы сам себя.
"""
from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path

from .. import paths
from ..infra import managed_fs
from ._errors import AwfApiError

INTERLOCK_NAME = ".interlock"

_LOCAL = threading.local()


def _held() -> set[tuple[int, str]]:
    return set(getattr(_LOCAL, "held", set()))


def interlock_path(project_dir: str | Path) -> Path:
    """``.agentic/state/.interlock`` — файл критической секции."""
    return paths.state_dir(project_dir) / INTERLOCK_NAME


def ensure_interlock_file(project_dir: str | Path) -> None:
    """R1.4: создать пустой файл критической секции, если его нет.

    Файл — часть скелета проекта, а не артефакт запуска: иначе первый
    ``start_pipeline``/``reset`` заводит его как «побочную запись», хотя
    это стабильная точка синхронизации (тест W7 «старт не создаёт
    runtime-записей»). Пустой файл в ``.agentic/state/`` (gitignored).

    Живёт здесь, рядом с ``launch_section``: знать имя, путь и безопасный
    способ открытия файла секции должен только этот модуль, а не каждый
    вызывающий (``init_project`` раньше собирал их у себя — «записано
    рядом», а не на месте).

    F-T1: открытие идёт через безопасный no-follow хелпер, а не bare
    ``touch`` — симлинк ``.agentic``/``state`` наружу увёл бы файл секции
    за пределы проекта; такая попытка отвергается ``AwfApiError``.

    F-P1: на не-POSIX файл секции НЕ создаётся. ``flock`` доступен только
    на POSIX, а ``init`` — безопасная операция: раньше безусловный
    ``open_managed`` (``require_posix``) валил ``awf init`` на Windows.
    Опасные пути (launch/destroy) по-прежнему отказывают на не-POSIX —
    внутри ``launch_section``.
    """
    project = Path(project_dir).resolve()
    if not managed_fs.is_posix():
        return
    lock = interlock_path(project)
    try:
        fd = managed_fs.open_managed(
            lock,
            flags=os.O_RDWR | os.O_CREAT,
            root=project,
            create=True,
        )
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(
            f"cannot create the interlock file — unsafe path: {exc}"
        ) from exc
    else:
        os.close(fd)


@contextlib.contextmanager
def launch_section(project_dir: str | Path):
    """Держать interlock; yield ``True`` — секция наша, ``False`` — занята.

    Секция всегда неблокирующая: занятый interlock означает «другой запуск
    или разрушение уже в полёте», и вызывающий превращает ``False`` в
    внятный отказ (``LeaseHeldError`` / ``AwfApiError``), а не ждёт.
    """
    project = Path(project_dir).resolve()
    key = (os.getpid(), str(project))
    if key in _held():
        # Уже держим (reset → remove_orphans): повторный flock через новый
        # fd заблокировал бы сам себя.
        yield True
        return

    # A-2: a launch on a platform without flock fails closed — but the raw
    # ``require_posix`` text (caught below as a ManagedPathError) reads like
    # an "unsafe path" bug ("interlock refused — unsafe path: ..."). Name the
    # real cause (the platform) in human terms, the same predicate
    # ``destructive_section`` uses for its F-2 pre-check.
    if not managed_fs.is_posix():
        raise AwfApiError(
            "launch refused — starting a pipeline needs POSIX file locking "
            "(flock) and no-follow paths to keep a live run safe; this "
            "platform is unsupported, so awf refuses rather than running a "
            "weaker implementation (fail-closed)."
        )
    try:
        # F-4: ONE predicate for lock availability — ``require_posix``
        # (``managed_fs.is_posix``, i.e. ``os.name``), the SAME one
        # ``ensure_interlock_file`` uses — instead of ``sys.platform`` here
        # and ``os.name`` there. On a platform that is neither POSIX nor
        # Windows the old ``sys.platform == "win32"`` branch fell through to
        # ``import fcntl`` and leaked a RAW ImportError past the
        # ManagedPathError→AwfApiError wrapper (the F-T2 class this module
        # already closes elsewhere).
        managed_fs.require_posix("launch/destroy interlock")
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover — exotic platform
            raise AwfApiError(
                "interlock refused — file locking (flock) is unavailable on "
                "this platform (fail-closed): a launch/destroy cannot run "
                "without the guarantee that protects a live run."
            ) from exc

        fd = managed_fs.open_managed(
            interlock_path(project),
            flags=os.O_RDWR | os.O_CREAT,
            root=project,
            create=True,
        )
    except managed_fs.ManagedPathError as exc:
        # Симлинк ``.agentic``/``state`` наружу: интерлок-файл не должен
        # создаваться за пределами проекта — тот же отказ, что у lease.
        raise AwfApiError(f"interlock refused — unsafe path: {exc}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        _LOCAL.held = _held() | {key}
        try:
            yield True
        finally:
            _LOCAL.held = _held() - {key}
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def destructive_section(project_dir: str | Path, *, label: str):
    """Держать interlock для разрушительной операции или отказать.

    Одна форма «взять секцию → занять → внятный отказ» на все
    разрушительные входы (reset / remove_orphans / rollback): до этого
    каждый вход копировал её рядом и по-своему, и именно такое
    расхождение дало второй, более узкий guard (QA F3). Занятый
    interlock — неблокирующий отказ, называющий ``label``; ``yield``
    наступает только когда секция наша.

    R1.4 (архитектурное ревью): проект без ``.agentic`` секцию не берёт
    вовсе — защищать нечего, а
    захват создал бы ``.agentic/state/.interlock`` — побочную запись на
    не-проекте. Решение «нет ``.agentic`` → свободно» живёт здесь, у
    владельца секции, а не в каждом разрушительном входе отдельной копией
    (иначе третий/четвёртый вход однажды забудет собственную проверку —
    тот же класс, что F3/F-H1). Проверка стоит ДО отказа по платформе,
    чтобы разрушение на пустом каталоге оставалось no-op на любой ОС.
    """
    project = Path(project_dir).resolve()
    if not (project / ".agentic").is_dir():
        yield
        return
    # F-2: a destructive op on a platform without flock fails closed — but
    # the low-level ``require_posix`` text ("no-follow file operations need
    # POSIX") reads like a bug for a "wipe runtime" command. Name the
    # consequence in human terms BEFORE taking the section, using the same
    # ``is_posix`` predicate the section relies on.
    if not managed_fs.is_posix():
        raise AwfApiError(
            f"{label} refused — destructive operations need POSIX file "
            "locking and no-follow paths to keep a live run safe; this "
            "platform is unsupported and awf refuses rather than running a "
            "weaker implementation that could wipe state under a live launch "
            "(fail-closed)."
        )
    with launch_section(project) as free:
        if not free:
            raise AwfApiError(
                f"{label} refused — a launch or destructive operation is in "
                "progress (interlock is held); wait for it to finish, then retry."
            )
        yield


__all__ = [
    "INTERLOCK_NAME",
    "ensure_interlock_file",
    "interlock_path",
    "launch_section",
    "destructive_section",
]
