"""AUD04-07: единый резолвер живости пайплайна.

До: живость определяли два независимых кода, и они расходились:

1. ``state.pipeline_pid`` (current.yaml) → ``api/pipeline.py::_is_pipeline_running``
   — читали start/continue-гарды и ``kill_pipeline``;
2. ``.agentic/logs/awf-start.pid`` → ``api/_background.py::check_pipeline_running``
   — читали ``awf status``.

Разная строгость PID-проверки → status говорил «не работает», а start-гард
«уже работает» (и наоборот). Foreground-пайплайн (без PID-файла) status
не видел вовсе.

Теперь один код-путь (:func:`resolve`) смотрит ОБА источника и применяет
одну строгую идентификацию: PID считается нашим только если argv процесса
``... -m awf start|continue ...`` (чтение ``/proc/<pid>/cmdline`` на Linux)
И этот argv принадлежит этому проекту — ``--project-dir`` (если в argv и
наименованный каталог существует) обязан резолвиться в этот проект
(TODO-0153: переиспользованный PID мог держать живой пайплайн ДРУГОГО
проекта). Без /proc (не-Linux) — деградация до проверки живости (как
раньше).

R1.4: рядом с резолвером живёт ОБЩИЙ destructive-preflight
(:func:`destructive_blockers` / :func:`refuse_if_in_use`) — «почему
разрушительную операцию нужно отклонить прямо сейчас». Не случайно в этом
же модуле: проверка «проект занят?» обязана читать те же факты, что
публикует запуск (живой пайплайн, живой launch lease, резервации
Store), иначе второй, более узкий guard со временем разойдётся с первым
(именно так появился класс F3). Отказ формулируется один раз здесь, а не
пересобирается возле каждой точки удаления.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from .. import paths
from ..infra import managed_fs
from ..pipeline_state import read_state
from ._errors import AwfApiError

_OUR_SUBCOMMANDS = ("start", "continue")
PID_FILE_NAME = "awf-start.pid"


def read_cmdline(pid: int) -> str | None:
    """Raw ``/proc/<pid>/cmdline`` (NUL-separated); None if dead/unreadable.

    Seam for tests (PID-reuse scenarios) — patch this, not os internals.
    """
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return None


def _argv_of(cmdline: str) -> list[str]:
    return [a for a in cmdline.replace("\x00", " ").split() if a]


def is_our_argv(argv: Sequence[str]) -> bool:
    """argv looks like ``... -m awf start|continue ...``."""
    if "-m" in argv:
        i = argv.index("-m")
        if i + 2 < len(argv) and argv[i + 1] == "awf" and argv[i + 2] in _OUR_SUBCOMMANDS:
            return True
    return False


def cmdline_is_ours(cmdline: str) -> bool:
    """Identity from a raw cmdline string (NUL-separated or plain text)."""
    return is_our_argv(_argv_of(cmdline))


def project_dir_from_argv(argv: Sequence[str]) -> str | None:
    """Value of ``--project-dir`` in argv, or None when the argv has no
    marker (test seams, hand-typed foreground calls). The launched
    background child always carries it (start_in_background)."""
    for i, a in enumerate(argv):
        if a == "--project-dir" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--project-dir="):
            return a.split("=", 1)[1]
    return None


def cmdline_matches_project(cmdline: str, project_dir: Path) -> bool:
    """The argv's ``--project-dir`` names THIS project (TODO-0153).

    A reused PID can hold ANOTHER project's live ``-m awf start`` child
    (CI: parallel xdist workers each launch their own background
    pipelines) — argv identity alone passes it as ours. The launched
    child always carries ``--project-dir``, so when the marker names an
    EXISTING directory it must resolve to this project. Two degenerate
    argvs keep the old (argv-only) semantics: no ``--project-dir``
    marker at all, and a marker naming a directory that does not exist
    (a live pipeline's project dir exists by construction — such an
    argv cannot be a running foreign pipeline).
    """
    declared = project_dir_from_argv(_argv_of(cmdline))
    if declared is None:
        return True
    declared_path = Path(declared).expanduser()
    if not declared_path.is_dir():
        return True
    return declared_path.resolve() == Path(project_dir).resolve()


def probe_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` liveness probe.

    OverflowError = pid outside the OS range (test fakes like 2**31) —
    such a process cannot be alive.
    """
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        return False
    return True


def pid_is_ours(
    pid: int,
    cmdline: str | None,
    *,
    strict: bool = True,
    project_dir: Path | None = None,
) -> bool:
    """Liveness + identity for one candidate PID.

    strict=True (Linux): cmdline unreadable → NOT ours (never assume,
    QA .14 — a reused PID must not pass as our pipeline).
    strict=False (non-Linux degradation): cmdline unreadable → liveness
    alone, identity cannot be checked without /proc.

    project_dir (TODO-0153): when given, identity is argv + PROJECT —
    the argv's ``--project-dir`` (when it names an existing directory)
    must resolve to this project. A reused PID holding another project's
    live ``-m awf start`` child fails the project check even though its
    argv matches. None = legacy argv-only identity (callers without a
    project scope, e.g. the own-process check).
    """
    if not probe_alive(pid):
        return False
    if cmdline is None:
        return not strict
    if not cmdline_is_ours(cmdline):
        return False
    if project_dir is not None:
        return cmdline_matches_project(cmdline, project_dir)
    return True


def _proc_available() -> bool:
    return Path("/proc/self").is_dir()


def _parse_pid_file(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def resolve(project_dir: Path) -> tuple[bool, int | None, str | None]:
    """(running, pid, source) — the single liveness code path.

    Source 1: ``.agentic/logs/awf-start.pid`` (background launcher).
    Source 2: ``state.pipeline_pid`` (any launcher — the orchestrator
    writes it at every stage, so a FOREGROUND pipeline without a PID
    file is visible to ``awf status``).

    A stale PID file (dead or foreign process) is cleaned up here.
    Identity is argv + project (TODO-0153): a live PID whose argv is
    ``-m awf start`` for ANOTHER project's ``--project-dir`` is not ours
    (CI: a reused PID held a parallel worker's own pipeline).
    """
    pid_file = paths.agentic_dir(project_dir) / "logs" / PID_FILE_NAME
    if pid_file.is_file():
        pid = _parse_pid_file(pid_file)
        if pid is not None and pid_is_ours(
            pid,
            read_cmdline(pid),
            strict=_proc_available(),
            project_dir=project_dir,
        ):
            return True, pid, "pid_file"
        # dead or foreign (another project) → clean up the stale file.
        # TODO-0235 (M3d): the cleanup is best-effort, so an unsafe LOGS
        # surface (symlinked) skips it instead of deleting outside; the
        # liveness answer below is unaffected.
        try:
            managed_fs.require_posix("clean stale pid file")
            managed_fs.assert_no_symlink_escape(
                pid_file, root=managed_fs.agentic_root_for(pid_file)
            )
            pid_file.unlink()
        except (OSError, managed_fs.ManagedPathError):
            pass

    state = read_state(project_dir)
    if state:
        try:
            spid = int(state.get("pipeline_pid"))
        except (TypeError, ValueError):
            spid = None
        if spid and pid_is_ours(
            spid, read_cmdline(spid), strict=True, project_dir=project_dir
        ):
            return True, spid, "state"

    return False, None, None


def destructive_blockers(project_dir: Path, *, action: str = "reset") -> list[str]:
    """Why a destructive op (reset/rollback) must be refused right now.

    R1.4 (S9-02): destructive ops consult the SAME facts the launch path
    publishes — a live pipeline, a live launch lease (an in-flight start),
    and the StateStore reservations matrix (a long action in flight). An
    empty list means the project is provably free.

    A dead owner (stale PID file, dead lease, stale run) yields NO blocker:
    the caller then reconciles (cleans the stale state) without losing the
    original facts. An UNKNOWN owner (a lease file whose pid cannot be
    read) DOES block — "unreadable" is not "dead". ``action`` selects the
    reservations action — ``reset`` for :func:`reset_runtime`, ``write``
    for a destructive rollback.

    A-3: the reservation component below (``state_store.check_action``) has
    no production producer yet — an in-flight start reserves through the run
    projection, not a Store transaction, so the reservations check adds no
    blocker on the production path today. The guarantee becomes live when
    R1.5 wires the Store writers (tracked there, not in this unit).
    """
    blockers: list[str] = []
    running, pid, source = resolve(project_dir)
    if running:
        blockers.append(f"pipeline is running (PID {pid}, {source})")
    # Lazy import: _lease imports probe_alive from this module.
    from . import _lease

    claim, owner = _lease.claim_status(project_dir)
    if claim == _lease.CLAIM_ALIVE:
        blockers.append(f"a launch is in progress (lease held by PID {owner})")
    elif claim == _lease.CLAIM_UNKNOWN:
        # QA F-A: an existing but unreadable claim is NOT provably dead —
        # requirement 3 says "alive OR unknown refuses". Only no file
        # (MISSING) and a readable dead pid (DEAD) leave the project free.
        # F-P3: this branch is NOT cleared by awf_kill — the file must be
        # inspected/fixed by hand, so name it instead of advising a kill.
        blockers.append(
            "a launch claim exists but its owner cannot be read "
            f"(unknown is not free) — inspect .agentic/logs/{_lease.LEASE_NAME}"
        )
    try:
        from ..infra import state_store

        state_store.check_action(str(project_dir), action)
    except state_store.StateStoreError as exc:  # reservation / corrupt / unknown
        blockers.append(f"state store refuses {action}: {exc}")
    return blockers


def refuse_if_in_use(project_dir: Path, *, label: str, action: str = "reset") -> None:
    """Raise the SHARED destructive refusal when any blocker is live.

    One form of the "preflight refused" text for every destructive entry —
    ``reset_runtime``, ``remove_orphans``, ``rollback`` and
    ``init_project(force=True)``. Each used to rebuild the guard block next
    to its own deletion; that divergence is exactly how a second, narrower
    guard appeared (QA F3). Call it INSIDE the ``destructive_section`` hold,
    so the blockers are read under the same critical section as the deletion
    that follows.

    ``label`` names the operation in the refusal (``"reset"``,
    ``"rollback(hard)"`` …); ``action`` selects the reservations action.
    Confirmed facts are named verbatim — ``": ".join`` of the blockers — so
    the caller sees WHY the project is in use, not a generic "busy".
    """
    blockers = destructive_blockers(project_dir, action=action)
    if blockers:
        # F-P3: no blanket "awf_kill" hint here — an UNKNOWN lease claim is
        # not cleared by a kill, and the blocker above already names the exact
        # file to inspect. Advise resolving the named fact, nothing narrower.
        raise AwfApiError(
            f"{label} refused — the project is in use: "
            + "; ".join(blockers)
            + ". Resolve the named blocker, then retry."
        )


__all__ = [
    "read_cmdline",
    "is_our_argv",
    "cmdline_is_ours",
    "project_dir_from_argv",
    "cmdline_matches_project",
    "probe_alive",
    "pid_is_ours",
    "resolve",
    "destructive_blockers",
    "refuse_if_in_use",
]
