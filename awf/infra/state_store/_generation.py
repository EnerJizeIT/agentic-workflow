"""R1.4 (S3-03): поколение run и активный run в control.json.

``run.yaml`` — проекция: ``reset_runtime`` её сносит как ghost run, и
поколение, взятое из неё, начинается заново (0 → 1). Тогда устаревший
``stop_run``/``run_finish`` со старым поколением CAS-совпадает с НОВЫМ
run и приписывает ему чужую причину — это и есть дефект S3-03.

Лечится тем, что источник поколения — durable-счётчик ``run_generation``
в control.json, который reset НЕ трогает: новый run берёт следующее
значение, монотонность переживает снос проекций. Активный run тоже
пишется сюда, чтобы Store и run.yaml были согласованы по поколению.

Все функции — тонкие обёртки над ``transact`` (CAS по ``revision``);
гонка параллельных стартов разрешается повтором на :class:`StaleState`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from ._errors import StaleState, StateStoreError
from ._snapshot import read_snapshot, require_usable
from ._transact import transact

#: R1.4: one retry budget for every CAS wrapper below. ``transact`` may raise
#: ``StaleState`` when a concurrent writer moved the snapshot between our read
#: and the write; a bounded re-read is the whole concurrency strategy, so the
#: number belongs in one place, not re-spelled per wrapper (5 was written out
#: literally next to a parameter default elsewhere).
_ATTEMPTS = 5


def _resolve_project(project_dir: str | Path) -> Path:
    return Path(project_dir).resolve()


def current_run_generation(project_dir: str | Path) -> int:
    """The durable ``run_generation`` counter — the last allocated value.

    R1.4 (S3-03): this is the SINGLE source of run identity. Every
    allocation (``run_start``, ``run_revise``, and — R1.5/A3 —
    ``run_service_start``) goes through :func:`next_run_generation`, so the
    counter always names the last value handed out and survives
    ``reset_runtime`` (which erases the ``run.yaml`` projection but not this
    counter). R1.5/A3: the counter is a GLOBAL watermark shared by the main
    and service slots — a generation ABOVE it cannot exist; the per-slot
    ``run.yaml``/``service-run.yaml`` CAS (not this counter) decides which
    generation is current for a slot.

    A missing or unreadable snapshot returns 0 — "no durable witness", so
    the caller falls back to the projection CAS (a legacy project that
    never allocated here).
    """
    snap = read_snapshot(_resolve_project(project_dir))
    if snap.status != "valid":
        return 0
    try:
        return int(snap.data.get("run_generation") or 0)
    except (TypeError, ValueError):
        return 0


def active_run_generation(project_dir: str | Path) -> int | None:
    """The generation of the ACTIVE run mirror in control.json, or ``None``.

    R1.4 (S3-03): ``run_start`` / ``run_revise`` publish the active run here
    next to the durable counter, ``_close_run_cas`` mirrors a close, and
    ``reset_runtime`` clears it. This is the snapshot witness a close is
    checked against: a generation that is not the active run's is stale.
    ``None`` (no active run — never started, cleared by reset) means "no
    snapshot": the projection CAS stays the sole authority.
    """
    snap = read_snapshot(_resolve_project(project_dir))
    if snap.status != "valid":
        return None
    active = snap.data.get("active_run")
    if not isinstance(active, dict):
        return None
    if not active.get("active", True):
        # F-P2: ``_close_run_cas`` mirrors a CLOSED run here (active=False).
        # Its generation is the last run's, not the ACTIVE run's — a caller
        # asking for the live witness must see ``None``, not a tombstone.
        return None
    try:
        return int(active.get("generation") or 0)
    except (TypeError, ValueError):
        return None


def next_run_generation(
    project_dir: str | Path,
    *,
    floor: int = 0,
    attempts: int = _ATTEMPTS,
) -> int:
    """Выдать следующее durable-поколение: ``max(счётчик, floor) + 1``.

    ``floor`` — поколение из текущей проекции (миграция старого проекта,
    где ``run.yaml`` знает больше счётчика). Результат записан в
    control.json ДО возврата, поэтому переживает ``reset_runtime``.
    """
    project = _resolve_project(project_dir)
    floor = int(floor or 0)
    op = f"run-gen-{uuid4().hex[:24]}"
    last: StaleState | None = None
    for _ in range(attempts):
        snap = read_snapshot(project)
        base = require_usable(snap, project)
        candidate = max(int(base.get("run_generation") or 0), floor) + 1

        def _mutate(new: dict[str, Any]) -> dict[str, Any]:
            new["run_generation"] = candidate
            return new

        try:
            transact(
                project,
                expected_revision=snap.revision,
                operation_id=op,
                mutate=_mutate,
                args={"floor": floor},
            )
        except StaleState as exc:  # другой писатель успел — перечитать
            last = exc
            continue
        # ``transact`` returns only revision/status/result — never the mutated
        # body — so the value this call wrote is the ``candidate`` planned from
        # the snapshot it read. A dead ``out.get("run_generation", candidate)``
        # here was a false contract signal (AUD02-11 class): it promised a body
        # ``transact`` does not return.
        return candidate
    raise StateStoreError(
        "could not allocate a run generation — control.json kept moving "
        f"under us ({attempts} attempts): {last}"
    )


def seed_run_generation(
    project_dir: str | Path,
    value: int,
    *,
    attempts: int = _ATTEMPTS,
) -> int:
    """Поднять durable-счётчик не ниже ``value`` (монотонный seed).

    R1.4 (F-H1): ``init_project(force=True)`` сносит проекцию ``run.yaml``
    И ``state/control.json`` (весь runtime-каталог ``state``), а с ним —
    durable-память поколений. Тогда следующий run начинал счёт заново
    (0 → 1), и устаревший ``stop_run``/``run_finish`` со старым поколением
    CAS-совпадал с НОВЫМ run — S3-03 через третью дверь. Перед сносом
    вызывающий читает :func:`current_run_generation`, после сноса возвращает
    его сюда: счётчик не откатывается назад, следующий run получает gen+1.

    ``value <= 0`` — «памяти не было»: ничего не пишем (0 — дефолт).
    """
    project = _resolve_project(project_dir)
    value = int(value or 0)
    if value <= 0:
        return current_run_generation(project)
    op = f"run-seed-{uuid4().hex[:24]}"
    last: StaleState | None = None
    for _ in range(attempts):
        snap = read_snapshot(project)
        base = require_usable(snap, project)
        if int(base.get("run_generation") or 0) >= value:
            return int(base.get("run_generation") or 0)

        def _mutate(new: dict[str, Any]) -> dict[str, Any]:
            new["run_generation"] = value
            return new

        try:
            transact(
                project,
                expected_revision=snap.revision,
                operation_id=op,
                mutate=_mutate,
                args={"seed": value},
            )
        except StaleState as exc:  # другой писатель успел — перечитать
            last = exc
            continue
        # ``transact`` returns no snapshot body (see ``next_run_generation``);
        # the seeded value this call wrote is exactly ``value``.
        return value
    raise StateStoreError(
        "could not seed a run generation — control.json kept moving "
        f"under us ({attempts} attempts): {last}"
    )


def publish_active_run(project_dir: str | Path, state: dict[str, Any]) -> None:
    """Записать активный run в control.json (проекция ``run.yaml`` рядом).

    Не трогает ``run_generation`` — его выдаёт :func:`next_run_generation`
    отдельно, чтобы объявление поколения не зависело от готовности тела.
    """
    project = _resolve_project(project_dir)
    # One operation id per CALL, not per attempt: a ``StaleState`` is raised
    # before transact writes any journal line, so the id never collides with a
    # recorded attempt and stays the call's idempotency key. The same
    # convention as ``next_run_generation``/``seed_run_generation`` — the
    # wrappers used to disagree (fresh uuid inside the loop here).
    op = f"run-publish-{uuid4().hex[:24]}"
    for _ in range(_ATTEMPTS):
        snap = read_snapshot(project)
        require_usable(snap, project)

        def _mutate(new: dict[str, Any]) -> dict[str, Any]:
            new["active_run"] = dict(state)
            return new

        try:
            transact(
                project,
                expected_revision=snap.revision,
                operation_id=op,
                mutate=_mutate,
                args={"generation": int(state.get("generation") or 0)},
            )
            return
        except StaleState:
            continue
    raise StateStoreError("could not publish the active run — control.json kept moving")


def clear_active_run(project_dir: str | Path) -> None:
    """Убрать активный run из control.json (reset снёс проекцию).

    ``run_generation`` сохраняется — это и есть durable-память поколений.
    """
    project = _resolve_project(project_dir)
    # One operation id per call — see ``publish_active_run``.
    op = f"run-clear-{uuid4().hex[:24]}"
    for _ in range(_ATTEMPTS):
        snap = read_snapshot(project)
        if snap.status == "missing":
            return
        require_usable(snap, project)

        def _mutate(new: dict[str, Any]) -> dict[str, Any] | None:
            if not new.get("active_run"):
                return None
            new["active_run"] = None
            return new

        try:
            transact(
                project,
                expected_revision=snap.revision,
                operation_id=op,
                mutate=_mutate,
                args={"clear": True},
            )
            return
        except StaleState:
            continue
    raise StateStoreError(
        "could not clear the active run — control.json kept moving under us"
    )


__all__ = [
    "active_run_generation",
    "current_run_generation",
    "next_run_generation",
    "seed_run_generation",
    "publish_active_run",
    "clear_active_run",
]
