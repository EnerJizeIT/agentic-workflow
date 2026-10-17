"""R1.3: the reservations matrix and durable reservations.

A long action (model, tests, HTTP, human decision) must NOT hold the project
lock — it takes a durable reservation instead. The matrix keeps read and
heartbeat open even during a live worker (a worker cannot be starved of
observation, and its own stop/recovery is never blocked by a "permanent
general ban"), while a second writer/start/reset is refused.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._errors import ReservationDenied, StateCorrupt, StateStoreError
from ._io import timestamp
from ._snapshot import read_snapshot
from ._transact import transact

_ALWAYS_ACTIONS = frozenset({"read", "heartbeat", "stop", "recovery"})
_BLOCKING_ACTIONS = frozenset({"write", "start", "reset"})


def _validate_action(action: str) -> None:
    """An unknown action is refused by NAME, before any state branch — so a
    bogus action behaves the same on a missing snapshot as on a valid one."""
    if action not in _ALWAYS_ACTIONS and action not in _BLOCKING_ACTIONS:
        raise StateStoreError(
            f"unknown reservation action {action!r} — expected one of "
            f"{sorted(_ALWAYS_ACTIONS | _BLOCKING_ACTIONS)}"
        )


def action_permitted(data: Mapping[str, Any], action: str) -> bool:
    """The reservations matrix: read/heartbeat always pass; the blocking
    actions are refused while ANY reservation is held."""
    _validate_action(action)
    if action in _ALWAYS_ACTIONS:
        return True
    return not data.get("reservations")


def check_action(project_dir: str | Path, action: str) -> None:
    """Raise ReservationDenied if ``action`` is forbidden now. A corrupt
    snapshot denies blocking actions."""
    _validate_action(action)
    snap = read_snapshot(project_dir)
    if snap.status == "missing":
        return
    if not snap.valid:
        if action in _BLOCKING_ACTIONS:
            raise StateCorrupt(f"cannot {action}: {snap.error}")
        return
    if not action_permitted(snap.data, action):
        raise ReservationDenied(
            f"action {action!r} is refused while reservations are held: "
            f"{sorted(snap.data.get('reservations', {}))}"
        )


def reserve(
    project_dir: str | Path,
    kind: str,
    owner: str,
    *,
    operation_id: str,
    expected_revision: int,
) -> int:
    """Take a durable reservation for ``kind`` (e.g. ``"worker"``). The lock
    is RELEASED immediately — the reservation, not the lock, is what a long
    action holds."""
    if not kind or not owner:
        raise StateStoreError("reservation needs a non-empty kind and owner")

    def _mutate(new: dict[str, Any]) -> dict[str, Any]:
        reservations = new.setdefault("reservations", {})
        if reservations:
            holder = reservations.get(kind)
            if holder and holder.get("owner") == owner:
                raise ReservationDenied(f"{kind} is already reserved by {owner}")
            raise ReservationDenied(
                f"cannot reserve {kind!r}: {sorted(reservations)} already held"
            )
        reservations[kind] = {
            "owner": owner,
            "operation_id": operation_id,
            "generation": new.get("run_generation", 0),
            "ts": timestamp(),
        }
        return new

    out = transact(
        project_dir,
        expected_revision=expected_revision,
        operation_id=operation_id,
        mutate=_mutate,
        args={"kind": kind, "owner": owner},
    )
    return out["revision"]


def release_reservation(
    project_dir: str | Path,
    kind: str,
    *,
    owner: str,
    operation_id: str,
    expected_revision: int,
) -> int:
    """Release a reservation only for its recorded owner — a stale reader may
    not free another owner's reservation. Releasing a kind nobody holds is a
    no-op (``no_change``): the revision does not move and no ``applied``
    journal line is written."""

    def _mutate(new: dict[str, Any]) -> dict[str, Any] | None:
        reservations = new.setdefault("reservations", {})
        holder = reservations.get(kind)
        if holder is None:
            return None
        if holder.get("owner") != owner:
            raise ReservationDenied(
                f"cannot release {kind!r}: held by {holder.get('owner')!r}, "
                f"not {owner!r}"
            )
        del reservations[kind]
        return new

    out = transact(
        project_dir,
        expected_revision=expected_revision,
        operation_id=operation_id,
        mutate=_mutate,
        args={"kind": kind, "owner": owner},
    )
    return out["revision"]


def takeover_reservation(
    project_dir: str | Path,
    kind: str,
    *,
    new_owner: str,
    evidence: str,
    operation_id: str,
    expected_revision: int,
) -> int:
    """Replace a reservation's holder — requires identity AND proof the prior
    holder stopped (``evidence``), so a live worker cannot be silently
    supplanted."""
    if not evidence.strip():
        raise StateStoreError("takeover requires evidence the previous owner stopped")

    def _mutate(new: dict[str, Any]) -> dict[str, Any]:
        reservations = new.setdefault("reservations", {})
        prior = reservations.get(kind)
        reservations[kind] = {
            "owner": new_owner,
            "operation_id": operation_id,
            "generation": new.get("run_generation", 0),
            "takeover_from": prior.get("owner") if prior else None,
            "takeover_evidence": evidence,
            "ts": timestamp(),
        }
        return new

    out = transact(
        project_dir,
        expected_revision=expected_revision,
        operation_id=operation_id,
        mutate=_mutate,
        args={"kind": kind, "owner": new_owner, "evidence": evidence},
    )
    return out["revision"]


__all__ = [
    "action_permitted",
    "check_action",
    "reserve",
    "release_reservation",
    "takeover_reservation",
]
