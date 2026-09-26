"""A-02: один владелец запуска на проект (launch lease).

Окно запуска — «проверка живости → спавн → запись PID-файла» — не было
защитено: два одновременных start/continue видели «пайплайн не работает»
и оба спавнили. Lease закрывает окно: O_EXCL-файл в ``.agentic/logs/``
хранит pid запускающего процесса.

- Одновременно запущенный вызов получает отказ (:class:`LeaseHeldError`)
  — внятный и НЕБЛОКИРУЮЩИЙ: второй вызов не ждёт освобождения.
- Мёртвый владелец перехватывается автоматически: staleness — живость
  процесса (:func:`awf.api._liveness.probe_alive`), а не возраст файла.
- Освобождение защищено: удаляется только lease, в котором всё ещё наш
  pid — перехват, успевший впритык, не удаляется из-под нового владельца.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from .. import paths
from ._errors import AwfApiError
from ._liveness import probe_alive

LEASE_NAME = "awf-launch.lease"
# Спини на перехват: 40 * 25ms = до ~1s на конкурентный перехват, затем
# отказ с retry — бесконечный цикл запрещён.
_TAKEOVER_SPINS = 40
_TAKEOVER_PAUSE = 0.025


class LeaseHeldError(AwfApiError):
    """Другой живой запуск владеет проектом — этот вызов отклонён."""


@dataclass
class Lease:
    """Удерживаемый launch lease; :meth:`release` идемпотентен и безопасен."""

    path: Path
    owner_pid: int

    def release(self) -> None:
        try:
            content = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return  # уже удалён
        if content != str(self.owner_pid):
            return  # впритык успел перехват — lease больше не наш
        try:
            self.path.unlink()
        except OSError:
            pass


def lease_path(project_dir: Path) -> Path:
    """Путь lease-файла: ``.agentic/logs/awf-launch.lease``."""
    logs = paths.agentic_dir(project_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / LEASE_NAME


def _read_owner(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def acquire(project_dir: Path) -> Lease:
    """Взять launch lease на этот процесс или отказать.

    :class:`LeaseHeldError`, если живой запуск уже владеет проектом.
    Lease с мёртвым (или нечитаемым) pid перехватывается: владелец
    пропал, его претензия на проект stale по живости — возраст файла
    ни разу не просматривается.
    """
    path = lease_path(project_dir)
    me = os.getpid()
    for _ in range(_TAKEOVER_SPINS):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            owner = _read_owner(path)
            if owner is not None and probe_alive(owner):
                raise LeaseHeldError(
                    f"another launch is already in progress (PID {owner}): "
                    "it proceeds on its own — this call does not wait for "
                    "it. Check the pipeline with awf_status and retry later."
                )
            try:
                path.unlink()
            except OSError:
                pass
            time.sleep(_TAKEOVER_PAUSE)
            continue
        except OSError as e:
            raise AwfApiError(f"launch lease acquire failed: {e}") from e
        try:
            os.write(fd, f"{me}\n".encode())
        finally:
            os.close(fd)
        return Lease(path=path, owner_pid=me)
    raise AwfApiError(
        "launch lease is contended — could not acquire it in "
        f"{_TAKEOVER_SPINS} attempts; retry"
    )


__all__ = ["LEASE_NAME", "Lease", "LeaseHeldError", "acquire", "lease_path"]
