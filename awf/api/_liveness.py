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
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from .. import paths
from ..pipeline_state import read_state

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
        # dead or foreign (another project) → clean up the stale file
        try:
            pid_file.unlink()
        except OSError:
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


__all__ = [
    "read_cmdline",
    "is_our_argv",
    "cmdline_is_ours",
    "project_dir_from_argv",
    "cmdline_matches_project",
    "probe_alive",
    "pid_is_ours",
    "resolve",
]
