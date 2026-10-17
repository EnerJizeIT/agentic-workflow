"""A-02 / R1.4: один владелец запуска на проект (launch lease).

Окно запуска — «проверка живости → спавн → запись PID-файла» — не было
защитено: два одновременных start/continue видели «пайплайн не работает»
и оба спавнили. Lease закрывает окно: файл в ``.agentic/logs/`` хранит
pid запускающего процесса.

R1.4 (аудит S1-02): захват публикует содержимое АТОМАРНО — сначала
владелец пишется в скрытый временный файл, затем ``os.link`` ставит его
на имя lease. Читатель видит либо отсутствие lease, либо полностью
записанную заявку, но не пустой файл. Раньше файл создавался пустым
(O_EXCL), pid дописывался вторым шагом; конкурент между этими шагами
читал пустоту, считал владельца нечитаемым (= мёртвым) и удалял lease —
два запуска получали один проект.

Правила владения:

- Одновременно запущенный вызов получает отказ (:class:`LeaseHeldError`)
  — внятный и НЕБЛОКИРУЮЩИЙ: второй вызов не ждёт освобождения.
- Мёртвый владелец перехватывается автоматически: staleness — живость
  процесса (:func:`awf.api._liveness.probe_alive`), а не возраст файла.
- Нечитаемая заявка (файл есть, pid не разбирается) НЕ считается
  доказанно мёртвой: перехват по прочитанному ранее pid удаляет только
  ту заявку, которую сам прочитал, — заявку чужого (нового) владельца по
  устаревшему чтению удалить нельзя.
- Захват не следует симлинкам: ``.agentic``/``logs``-симлинк наружу —
  отказ (:class:`~awf.infra.managed_fs.ManagedPathError`), а не утечка
  lease за пределы проекта (добор из приёмки 0215).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .. import paths
from ..infra import managed_fs
from ._errors import AwfApiError
from ._liveness import probe_alive

LEASE_NAME = "awf-launch.lease"
# R1.4 (F-R1, требование 2): отдельная durable-метка ИДЕНТИЧНОСТИ запуска.
# Lease-файл держится только на окно захвата и освобождается сразу после
# спавна; ExecutionId должен пережить освобождение — иначе ребёнок не смог
# бы сверить reserve. Метка переписывается каждым запуском.
EXECUTION_NAME = "awf-launch.execution"
#: R1.4 (F-R1): имя env-переменной, которой запускающий процесс передаёт
#: ExecutionId ребёнку. Пишет ``_background``, читает handshake в
#: ``pipeline`` — одно имя, один владелец (рядом с ``EXECUTION_NAME``), а не
#: два литерала, «записанных рядом» по обе стороны вызова.
EXECUTION_ENV = "AWF_EXECUTION_ID"
# Спини на перехват: 40 * 25ms = до ~1s на конкурентный перехват, затем
# отказ с retry — бесконечный цикл запрещён.
_TAKEOVER_SPINS = 40
_TAKEOVER_PAUSE = 0.025

#: Маркер «файла нет» — отличается от ``None`` (файл есть, но нечитаем).
_MISSING = object()


class LeaseHeldError(AwfApiError):
    """Другой живой запуск владеет проектом — этот вызов отклонён."""


@dataclass
class Lease:
    """Удерживаемый launch lease; :meth:`release` идемпотентен и безопасен."""

    path: Path
    owner_pid: int
    execution_id: str = ""
    project_dir: Path = Path()

    def publish_identity(self) -> str:
        """F-ID: опубликовать метку идентичности ЭТОГО запуска.

        Зовётся только на проходящем пути (после проверки живости), когда
        вызов действительно стартует: отказанный старт не смеет трогать
        метку живого запуска (см. :func:`acquire`, ``publish_identity``).
        """
        _publish_execution(self.project_dir, self.execution_id)
        return self.execution_id

    def release(self) -> None:
        # Удаляем только ту заявку, в которой всё ещё НАШ pid: перехват,
        # успевший впритык, не удаляется из-под нового владельца. Тот же
        # условный unlink использует перехватчик — одна реализация на оба.
        # ExecutionId НЕ удаляется: это метка идентичности запуска, а не
        # часть окна захвата (её перепишет следующий запуск).
        _unlink_if_owner(self.path, self.owner_pid)


def _lease_file(project_dir: Path) -> Path:
    """Путь lease-файла БЕЗ побочных эффектов (каталог не создаётся).

    Read-only preflight (:func:`claim_status`) не должен заводить ``logs`` —
    поэтому локатор вынесен отдельно, а :func:`lease_path` лишь добавляет
    mkdir. Один литерал пути на всех читателей заявки.
    """
    return paths.agentic_dir(project_dir) / "logs" / LEASE_NAME


def lease_path(project_dir: Path) -> Path:
    """Путь lease-файла: ``.agentic/logs/awf-launch.lease`` (создаёт каталог)."""
    logs = paths.agentic_dir(project_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return _lease_file(project_dir)


def _execution_file(project_dir: Path) -> Path:
    """Путь метки ExecutionId БЕЗ побочных эффектов (каталог не создаётся)."""
    return paths.agentic_dir(project_dir) / "logs" / EXECUTION_NAME


def read_execution_id(project_dir: str | Path) -> str | None:
    """ExecutionId текущего запуска, или ``None`` (нет/нечитаемо/симлинк).

    Читается без побочных эффектов — безопасно в любом preflight. Симлинк
    не разыменовывается (метка обязана лежать внутри проекта).
    """
    path = _execution_file(Path(project_dir).resolve())
    if path.is_symlink():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def execution_confirmed(project_dir: str | Path, execution_id: str | None) -> bool:
    """F-R1: has this child the right to work under the current reservation?

    ``True`` when there is NO execution marker: the process was not launched
    by our background launcher (a direct ``awf start`` that merely inherited
    ``AWF_BACKGROUND_CHILD`` from an outer run) — legacy behavior stands.
    When a marker EXISTS, the child must carry its ExecutionId: a
    stale/crash-orphan child (its launch's marker was overwritten by a newer
    launch) is refused, so it never runs a second pipeline over the project.
    """
    current = read_execution_id(project_dir)
    if current is None:
        return True
    return bool(execution_id) and execution_id == current


def _publish_execution(project_dir: Path, execution_id: str) -> None:
    """Записать метку идентичности запуска (атомарно, no-follow all along)."""
    try:
        managed_fs.managed_write_text(
            _execution_file(project_dir),
            f"{execution_id}\n",
            root=project_dir,
        )
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(
            f"launch lease acquire failed — unsafe execution path: {exc}"
        ) from exc


def rotate_execution_id(project_dir: str | Path) -> str:
    """F-1: выдать СВЕЖИЙ ExecutionId после разрушения (reset/force-init).

    Метка идентичности — не runtime-данные, а страховка handshake: ребёнок
    сверяет свой id с ней. Если reset её УДАЛЯЕТ, ``execution_confirmed``
    возвращает ``True`` («метки нет = работай»), и crash-orphan ребёнок
    старого запуска снова проходит проверку. Если reset СОХРАНЯЕТ старое
    значение, проходит ровно тот orphan, чей id совпадает, — то есть
    orphan последнего запуска, ровно тот, кого handshake и должен был
    остановить. Поэтому метка не удаляется и не сохраняется как есть, а
    ПЕРЕЗАПИСЫВАЕТСЯ: её id не совпадёт ни с одним прежним, файл при этом
    остаётся на месте. Возвращает новый id.
    """
    project = Path(project_dir).resolve()
    new_id = uuid4().hex
    _publish_execution(project, new_id)
    return new_id


def _read_owner(path: Path) -> int | None | object:
    """Содержимое заявки: int — pid; ``None`` — файл есть, pid нечитаем;
    :data:`_MISSING` — файла нет (удалён под нами)."""
    if path.is_symlink():
        # Симлинк-заявку не разыменовываем: чужой владелец не доказуемо мёртв.
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return _MISSING
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _guard_paths(project_dir: Path, path: Path) -> None:
    """Отказ, если ``.agentic``/``logs`` — симлинк наружу проекта.

    Проверка идёт ДО создания каталога: ``lease_path`` делает mkdir, а
    mkdir по симлинку-``.agentic`` создал бы ``logs`` вне проекта.
    """
    try:
        managed_fs.require_posix("acquire launch lease")
        managed_fs.assert_managed_dir(paths.agentic_dir(project_dir), root=project_dir)
        managed_fs.assert_no_symlink_escape(path, root=project_dir)
    except managed_fs.ManagedPathError as exc:
        # F-T2: require_posix внутри обёртки — на не-POSIX наружу идёт
        # AwfApiError, единый тип отказа, а не сырой ManagedPathError.
        raise AwfApiError(f"launch lease refused — unsafe path: {exc}") from exc


def _unlink_quietly(path: Path) -> None:
    """Убрать временный файл заявки, не поднимая ошибку уборки (A-4)."""
    try:
        os.unlink(str(path))
    except OSError:
        pass


def _publish_claim(path: Path, pid: int) -> bool:
    """Атомарно поставить заявку ``path`` с содержимым ``pid``.

    Возвращает False, если заявка уже существует. Владелец пишется в
    скрытый временный файл (в том же каталоге) и публикуется через
    ``os.link`` — читатель никогда не видит пустой заявки (S1-02).
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    # R1.4: тот же no-follow walk, что у write-хелперов managed_fs (R1.1), а
    # не только lstat-preflight: симлинк ``.agentic``/``logs`` наружу
    # отказывает на openat, TOCTOU «проверили → открыли» закрыт.
    # F-3: публикация идёт через ``dir_fd`` (см. ниже) — no-follow покрывает
    # и второй шаг, а не только создание temp-файла.
    try:
        fd = managed_fs.open_managed(
            tmp,
            flags=os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            root=managed_fs.agentic_root_for(tmp.parent),
            create=True,
        )
    except (managed_fs.ManagedPathError, OSError) as e:
        raise AwfApiError(f"launch lease acquire failed: {e}") from e
    # A-4: the temp file exists from here on and one ``finally`` drops it on
    # EVERY exit — a write/fsync fault, the dir_fd open fault, the link
    # fault, the FileExistsError refusal or success. The pre-review body
    # kept the cleanup per branch (one call next to the dir_fd open, one in
    # the link ``finally``) and missed the write/fsync branch entirely,
    # leaking a ``.<name>.<pid>.<rand>.tmp`` orphan into ``logs/`` there.
    try:
        try:
            os.write(fd, f"{pid}\n".encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        # F-3: publish through an fd anchored at the (already validated)
        # directory instead of plain string paths. ``os.link(str(tmp), str(path))``
        # re-resolved every component by the normal lookup, so a symlinked
        # ``.agentic``/``logs`` swapped in after the no-follow open would have
        # been followed on THIS step — the comment above claimed a protection
        # the link did not have. ``src_dir_fd``/``dst_dir_fd`` confine both
        # sides to the directory we opened; ``O_NOFOLLOW`` keeps that open on
        # the validated directory itself (a swap to a symlink fails closed
        # instead of redirecting the link outside the project).
        # Honest scope (аудит 0218, F-4): ``O_NOFOLLOW`` on ``tmp.parent``
        # guards only the LEAF (``logs``); the intermediate ``.agentic`` was
        # already walked no-follow by ``open_managed`` when the temp was
        # created. And ``_unlink_quietly(tmp)`` in the ``finally`` re-resolves
        # that temp path by name — best-effort cleanup, never a security
        # boundary (the temp leaf was opened no-follow).
        try:
            dir_fd = os.open(
                tmp.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as e:
            raise AwfApiError(f"launch lease acquire failed: {e}") from e
        try:
            try:
                os.link(
                    tmp.name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd
                )
            except FileExistsError:
                return False
            except OSError as e:
                raise AwfApiError(f"launch lease acquire failed: {e}") from e
        finally:
            os.close(dir_fd)
        return True
    finally:
        _unlink_quietly(tmp)


def _unlink_if_owner(path: Path, owner: int) -> None:
    """Удалить заявку, только если её содержимое всё ещё равно ``owner``.

    Гонка перехвата: пока мы считали владельца мёртвым, другой процесс мог
    поставить свою заявку — её удалять нельзя.
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if raw != str(owner):
        return
    try:
        path.unlink()
    except OSError:
        pass


CLAIM_MISSING = "missing"
CLAIM_UNKNOWN = "unknown"
CLAIM_DEAD = "dead"
CLAIM_ALIVE = "alive"


def claim_status(project_dir: Path) -> tuple[str, int | None]:
    """Состояние заявки: ``(CLAIM_*, pid)``.

    Четыре различимых исхода — их требует preflight разрушения (QA F-A,
    требование 3: «alive ИЛИ unknown запрещают разрушение»):

    - ``CLAIM_MISSING`` — файла нет, проект свободен;
    - ``CLAIM_UNKNOWN`` — файл есть, pid не разбирается (пустой, битый,
      симлинк): владелец НЕ доказуемо мёртв;
    - ``CLAIM_DEAD`` — читаемый pid, процесса нет (stale, можно перехватить);
    - ``CLAIM_ALIVE`` — читаемый pid жив.

    Не создаёт каталогов (как и :func:`_lease_file`) — безопасно звать в
    read-only preflight.
    """
    owner = _read_owner(_lease_file(project_dir))
    if owner is _MISSING:
        return CLAIM_MISSING, None
    if not isinstance(owner, int):
        return CLAIM_UNKNOWN, None
    return (CLAIM_ALIVE if probe_alive(owner) else CLAIM_DEAD), owner


def acquire(project_dir: Path, *, publish_identity: bool = True) -> Lease:
    """Взять launch lease на этот процесс или отказать.

    :class:`LeaseHeldError`, если живой запуск уже владеет проектом.
    Lease с мёртвым pid перехватывается: владелец пропал, его претензия
    stale по живости — возраст файла ни разу не просматривается.
    Нечитаемая заявка доказанно не мертва: она не удаляется, попытка
    повторяется и в пределе отклоняется.

    ``publish_identity`` (F-ID): публиковать ли метку ExecutionId вместе с
    заявкой. Вызывающий, который ещё может ОТКАЗАТЬ (``start_pipeline``
    проверяет живость пайплайна уже ПОСЛЕ захвата), передаёт ``False`` и
    публикует метку сам на проходящем пути (:meth:`Lease.publish_identity`).
    Иначе отказанный параллельный старт переписывает метку живого ребёнка —
    тот проваливает handshake и выходит: ноль worker'ов вместо одного плюс
    отказ.
    """
    project_dir = Path(project_dir).resolve()
    # R1.4 (требование 4): проверка путей и публикация заявки — ОДНА
    # критическая секция с destructive-preflight. Без неё reset мог
    # пройти между «пути чистые» и «заявка опубликована» и снести проект
    # живому запуску (QA F1). Секция короткая: держим только захват, не
    # весь прогон — дальше проект защищает сама заявка.
    from ._interlock import launch_section

    with launch_section(project_dir) as free:
        if not free:
            # Секция занята: чужой запуск ИЛИ разрушение (reset/rollback)
            # уже в полёте. Ждать нельзя — внятный неблокирующий отказ.
            raise LeaseHeldError(
                "another operation is already in progress (interlock is "
                "held by a launch or a destructive reset/rollback): it "
                "proceeds on its own — this call does not wait for it. "
                "Check the pipeline with awf_status and retry later."
            )
        _guard_paths(
            project_dir,
            _lease_file(project_dir),
        )
        path = lease_path(project_dir)
        me = os.getpid()
        execution_id = uuid4().hex
        for _ in range(_TAKEOVER_SPINS):
            if _publish_claim(path, me):
                # F-R1: once we own the project, publish the identity marker
                # the child will read to confirm the reservation. F-ID: only
                # when the caller will actually proceed — a caller that may
                # still refuse passes publish_identity=False and publishes
                # later (see the docstring), so a refused start has no side
                # effects on a live launch's marker.
                if publish_identity:
                    _publish_execution(project_dir, execution_id)
                return Lease(
                    path=path,
                    owner_pid=me,
                    execution_id=execution_id,
                    project_dir=project_dir,
                )
            owner = _read_owner(path)
            if owner is _MISSING:
                continue  # заявка удалена под нами — повторить публикацию
            if owner is None:
                # Файл есть, владелец нечитаем — НЕ доказуемо мёртв. Не
                # удаляем: возможно, владелец дописывает (старый формат) или
                # заявка повреждена — это не повод отдать проект второму.
                time.sleep(_TAKEOVER_PAUSE)
                continue
            if probe_alive(owner):
                raise LeaseHeldError(
                    f"another launch is already in progress (PID {owner}): "
                    "it proceeds on its own — this call does not wait for "
                    "it. Check the pipeline with awf_status and retry later."
                )
            _unlink_if_owner(path, owner)
            time.sleep(_TAKEOVER_PAUSE)
        raise AwfApiError(
            "launch lease is contended — could not acquire it in "
            f"{_TAKEOVER_SPINS} attempts (a live or unreadable owner holds it); "
            "inspect .agentic/logs/awf-launch.lease"
        )


__all__ = [
    "CLAIM_ALIVE",
    "CLAIM_DEAD",
    "CLAIM_MISSING",
    "CLAIM_UNKNOWN",
    "EXECUTION_ENV",
    "EXECUTION_NAME",
    "LEASE_NAME",
    "Lease",
    "LeaseHeldError",
    "acquire",
    "claim_status",
    "rotate_execution_id",
    "execution_confirmed",
    "lease_path",
    "read_execution_id",
]
