"""TODO-0241: двигатель оставляет следы — heartbeat, engine-exit, parent-death.

Три тихие смерти пайплайна за мини-волну (0238, 0228, 0240): воркер падал
по внешней ошибке, движок завершался, родитель (MCP-сервер) не забирал
rc — зомби ``Zs``, state говорил «стадия идёт», salvage не писался.

Модуль — единственный владелец двух поверхностей:

- ``.agentic/state/engine.heartbeat`` — JSON (pid, ppid, ts, stage,
  todo_id), атомарная запись через managed-путь. Пишется на старте,
  каждой смене стадии и периодически (watchdog-поток, интервал из
  ``engine.heartbeat_seconds``, по умолчанию 30 с).
- ``.agentic/context/ENGINE-EXIT-<ts>-<pid>[-n].md`` — причина любого
  завершения (rc, сигнал, стадия, todo, хвост трейсбека). ``atexit`` +
  обработчики SIGTERM/SIGINT, плюс явный ``finish(rc)`` из движка.
  Каталог держит последние ``ENGINE_EXIT_RETENTION`` записей (crash-loop
  не вытесняет свежие улики).

Parent-death: выключен по умолчанию (TODO-0242 P0-1 — durable-ран
переживает рестарт хоста). При ``engine.kill_with_parent: true`` — на
Linux ``prctl(PR_SET_PDEATHSIG, SIGTERM)``; fallback (не-Linux или ошибка
prctl) — watchdog сверяет ``os.getppid()`` с исходным и выходит с записью
причины. Фоновый движок (``AWF_BACKGROUND_CHILD``) не вооружается никогда.

Статус (``engine_dead``) читает те же файлы: state говорит «стадия идёт»,
а pid мёртв (kill 0) или heartbeat старше порога — строка с точным
``awf_continue(todo_id=...)``. State не мутируется. Heartbeat принимается
только от того же pid, что записан в state: чужой файл (прошлый запуск)
не даёт вердикта о смерти.
"""
from __future__ import annotations

import atexit
import ctypes
import json
import os
import signal
import sys
import threading
import time
import traceback
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg_mod
from . import paths
from ._log import log as _log
from .infra import managed_fs

HEARTBEAT_NAME = "engine.heartbeat"
DEFAULT_HEARTBEAT_SECONDS = 30.0
DEFAULT_STALE_SECONDS = 120.0
MAX_HEARTBEAT_SECONDS = 30.0
#: TODO-0306: the stage is STALE when ``current.yaml``'s ``updated_at`` is
#: older than this many minutes (or the engine pid is dead). Unlike the
#: opt-in ``run.stall_minutes`` diagnostic this one is ON by default: the
#: incident was a frozen stage that every surface kept painting green.
DEFAULT_STALE_STAGE_MINUTES = 15
#: How many ``ENGINE-EXIT-*.md`` records the context directory keeps; an
#: older crash-loop cannot push the fresh evidence out of the listing.
ENGINE_EXIT_RETENTION = 20
_PR_SET_PDEATHSIG = 1


def heartbeat_path(project_dir: Path) -> Path:
    return paths.state_dir(project_dir) / HEARTBEAT_NAME


def exit_file_path(
    project_dir: Path,
    ts: str | None = None,
    *,
    pid: int | None = None,
    counter: int = 0,
) -> Path:
    """``ENGINE-EXIT-<ts>-<pid>[-<counter>].md`` — the ONE name builder.

    TODO-0242 (A.4): the pid keeps two exits of one second apart (the
    second-resolution stamp alone let a crash-loop erase its own evidence);
    ``counter`` is the last-resort clash suffix for one pid.
    """
    stamp = ts or _stamp()
    owner = os.getpid() if pid is None else pid
    suffix = f"-{counter}" if counter else ""
    return paths.context_dir(project_dir) / f"ENGINE-EXIT-{stamp}-{owner}{suffix}.md"


def _stamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _now_iso(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.isoformat()


def heartbeat_interval_seconds(config: dict | None = None) -> float:
    """Current heartbeat interval, clamped to :data:`MAX_HEARTBEAT_SECONDS`.

    A value above the ceiling is a configuration mistake with a real cost:
    the liveness verdict gets slower than the status surfaces' patience
    (the wait cap is minutes), and the 600s value from a copied config made
    ``engine_dead`` take ten minutes to see. Clamp loudly, never silently.
    """
    value = cfg_mod.get(config or {}, "engine.heartbeat_seconds")
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return DEFAULT_HEARTBEAT_SECONDS
    if interval <= 0:
        return DEFAULT_HEARTBEAT_SECONDS
    if interval > MAX_HEARTBEAT_SECONDS:
        warnings.warn(
            f"engine.heartbeat_seconds={interval:g} exceeds the "
            f"{MAX_HEARTBEAT_SECONDS:g}s ceiling — clamped; a larger "
            "interval delays the engine-dead verdict",
            RuntimeWarning,
            stacklevel=2,
        )
        return MAX_HEARTBEAT_SECONDS
    return interval


def heartbeat_stale_seconds(config: dict | None = None) -> float:
    """Staleness threshold: ``max(3 × interval, 120)`` unless set explicitly."""
    value = cfg_mod.get(config or {}, "engine.heartbeat_stale_seconds")
    try:
        stale = float(value)
    except (TypeError, ValueError):
        interval = heartbeat_interval_seconds(config)
        return max(interval * 3, DEFAULT_STALE_SECONDS)
    return stale if stale > 0 else max(
        heartbeat_interval_seconds(config) * 3, DEFAULT_STALE_SECONDS
    )


def _managed_write(path: Path, text: str, logs_dir: Path | None) -> bool:
    """Managed atomic write; a refusal is logged and degraded, not raised."""
    try:
        root = managed_fs.agentic_root_for(path)
        managed_fs.managed_write_text(path, text, root=root)
        return True
    except managed_fs.ManagedPathError as exc:
        _log_once(logs_dir, f"engine-liveness: managed write refused: {exc}")
    except OSError as exc:
        _log_once(logs_dir, f"engine-liveness: write failed: {exc}")
    # No raw-write fallback: a weaker implementation would step around the
    # managed barrier. Off POSIX this degrades to "no heartbeat / no exit
    # record" with the log line above — explicit, never silent.
    return False


_logged: set[str] = set()


def _log_once(logs_dir: Path | None, message: str) -> None:
    if logs_dir is None or message in _logged:
        return
    _logged.add(message)
    try:
        _log(logs_dir, message)
    except Exception:  # noqa: BLE001 — diagnostics must never kill the engine
        pass


def write_heartbeat(
    project_dir: Path,
    *,
    stage: str = "",
    todo_id: str = "",
    logs_dir: Path | None = None,
    now: datetime | None = None,
) -> dict:
    moment = now or datetime.now(timezone.utc)
    payload = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "ts": _now_iso(moment),
        "ts_epoch": moment.timestamp(),
        "stage": str(stage or ""),
        "todo_id": str(todo_id or ""),
    }
    _managed_write(
        heartbeat_path(project_dir),
        json.dumps(payload, ensure_ascii=False) + "\n",
        logs_dir,
    )
    return payload


def read_heartbeat(project_dir: Path) -> dict | None:
    try:
        raw = heartbeat_path(project_dir).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_engine_exit(
    project_dir: Path,
    *,
    rc: int | None,
    sig: int | str | None = None,
    stage: str = "",
    todo_id: str = "",
    traceback_tail: str = "",
    reason: str = "",
    logs_dir: Path | None = None,
) -> Path | None:
    """Записать ``ENGINE-EXIT-<ts>-<pid>[-n].md``; None, если запись не удалась.

    TODO-0242 (A.4): the second-resolution stamp alone let two exits in one
    second overwrite each other (the crash-loop erased its own evidence).
    The pid plus a counter make every exit its own file. The context
    directory keeps :data:`ENGINE_EXIT_RETENTION` newest records.
    """
    stamp = _stamp()
    target = _unique_exit_path(project_dir, stamp)
    lines = [
        f"# ENGINE-EXIT {stamp}",
        "",
        f"- rc: {rc if rc is not None else '?'}",
        f"- signal: {sig if sig else 'none'}",
        f"- stage: {stage or '?'}",
        f"- todo: {todo_id or '?'}",
        f"- pid: {os.getpid()}",
    ]
    if reason:
        lines.append(f"- reason: {reason}")
    lines += ["", "## traceback tail", ""]
    tail = "\n".join((traceback_tail or "").strip().splitlines()[-40:])
    lines.append(tail if tail else "(no exception)")
    lines.append("")
    if _managed_write(target, "\n".join(lines), logs_dir):
        _prune_engine_exits(project_dir, keep=target)
        return target
    return None


def _unique_exit_path(project_dir: Path, stamp: str) -> Path:
    """The first free ``ENGINE-EXIT-<stamp>-<pid>[-n].md``."""
    counter = 0
    candidate = exit_file_path(project_dir, stamp, counter=counter)
    while candidate.exists():
        counter += 1
        candidate = exit_file_path(project_dir, stamp, counter=counter)
    return candidate


def _prune_engine_exits(project_dir: Path, *, keep: Path) -> None:
    """Keep the newest :data:`ENGINE_EXIT_RETENTION` exit records."""
    try:
        records = sorted(
            paths.context_dir(project_dir).glob("ENGINE-EXIT-*.md"),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
        )
    except OSError:
        return
    for old in records[:-ENGINE_EXIT_RETENTION]:
        if old == keep:
            continue
        # TODO-0242 (arch review): the prune is a DELETE on a managed
        # surface, not a private cleanup — a swapped/symlinked
        # ``.agentic``/``context`` must refuse instead of unlinking
        # outside the project (the same no-follow walk the writers
        # inventory demands of every delete site).
        try:
            managed_fs.require_posix("prune engine-exit records")
            managed_fs.assert_no_symlink_escape(
                old, root=managed_fs.agentic_root_for(old)
            )
            old.unlink()
        except (OSError, managed_fs.ManagedPathError):
            pass


# ── parent-death ────────────────────────────────────────────────────────


def _prctl_pdeathsig(
    value: int, logs_dir: Path | None, *, label: str
) -> bool:
    """One ``prctl(PR_SET_PDEATHSIG, value)`` call, best effort.

    Arming (``SIGTERM``) and disarming (``0``) share the libc handle, the
    errno read and the degrade-to-log rule — two near-identical blocks
    drifted apart on the next edit otherwise. ``label`` is what the log
    line names; ``None`` from the handle (no libc) is the documented no-op.
    """
    try:
        from . import _libc

        libc = _libc.get_libc()
        if libc is None:
            return False
        rc = libc.prctl(_PR_SET_PDEATHSIG, value, 0, 0, 0)
    except Exception as exc:  # noqa: BLE001 — degrade to the ppid watchdog
        _log_once(logs_dir, f"engine-liveness: {label} failed: {exc}")
        return False
    if rc != 0:
        _log_once(
            logs_dir,
            f"engine-liveness: {label} rc={rc} errno={ctypes.get_errno()}",
        )
        return False
    return True


def install_parent_death_signal(
    logs_dir: Path | None = None, *, platform: str | None = None
) -> bool:
    """``PR_SET_PDEATHSIG`` на Linux; иначе явный no-op с одной строкой лога."""
    plat = platform if platform is not None else sys.platform
    if not plat.startswith("linux"):
        _log_once(
            logs_dir,
            f"engine-liveness: parent-death signal is a no-op on {plat} "
            "(watchdog ppid fallback is used)",
        )
        return False
    return _prctl_pdeathsig(
        signal.SIGTERM, logs_dir, label="prctl PR_SET_PDEATHSIG"
    )


def disarm_parent_death_signal(logs_dir: Path | None = None) -> bool:
    """``prctl(PR_SET_PDEATHSIG, 0)`` — чистое завершение не ждёт смерти родителя.

    TODO-0242 (A.1): the opt-in signal describes how the engine DIES
    unexpectedly, not how it finishes. A normal ``finish`` must clear it:
    otherwise the process keeps a parent-death trigger it no longer owns
    (a long-lived launcher that exits later would signal an already
    finished process group). Best effort — a failure is a log line.
    """
    return _prctl_pdeathsig(
        0, logs_dir, label="prctl(PR_SET_PDEATHSIG, 0)"
    )


# ── the engine-side owner ───────────────────────────────────────────────


class EngineLiveness:
    """Жизненный цикл движка: heartbeat, parent-death, engine-exit.

    ``start`` не бросает: любой сбой диагностики деградирует в лог и не
    мешает пайплайну. ``finish`` идемпотентен; сигнал/atexit тоже идут
    через него.
    """

    def __init__(
        self,
        project_dir: Path,
        config: dict | None = None,
        *,
        interval: float | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.logs_dir = paths.agentic_dir(self.project_dir) / "logs"
        self.config = config or {}
        self.interval = interval or heartbeat_interval_seconds(self.config)
        self.stage = ""
        self.todo_id = ""
        self._started = False
        self._finished = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._initial_ppid = os.getppid()
        # A background child is spawned by a SHORT-LIVED launcher CLI that
        # exits right after the launch: PR_SET_PDEATHSIG would SIGTERM the
        # engine at once, and the ppid fallback would see the reparent to
        # init as "parent died". Detached engines are meant to outlive the
        # launcher — parent-death applies to foreground engines only.
        self._detached = os.environ.get("AWF_BACKGROUND_CHILD") == "1"
        # TODO-0242 (P0-1): a durable run must OUTLIVE its launcher. The
        # parent-death signal was the default and an MCP restart/reconnect
        # killed six engines in one incident (2026-10-23). It is now opt-in
        # (``engine.kill_with_parent: true``); the ppid watchdog follows the
        # same switch — otherwise the fallback would do the same damage.
        self._kill_with_parent = bool(
            cfg_mod.get(self.config, "engine.kill_with_parent", False)
        )
        self._pdeath = False
        self._old_handlers: dict[int, object] = {}
        self._exc_tail = ""
        # Reentrant: a signal handler runs in the main thread and calls
        # ``finish`` — it may interrupt ``on_stage`` while it holds the lock.
        self._lock = threading.RLock()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> EngineLiveness:
        if self._started:
            return self
        self._started = True
        self.write()
        if self._kill_with_parent and not self._detached:
            self._pdeath = install_parent_death_signal(self.logs_dir)
            # TODO-0242 (A.1): the parent may have died BETWEEN the process
            # start and the prctl — the signal is not retroactive. Check the
            # ppid once, immediately. The watchdog keeps its own rule: it
            # covers only the failed-prctl path (``_pdeath`` False), not a
            # second opinion on an armed signal.
            if os.getppid() != self._initial_ppid:
                self.finish(1, reason=f"parent died (ppid {self._initial_ppid})")
                os._exit(1)
        self._install_signals()
        try:
            atexit.register(self._atexit)
        except Exception:  # noqa: BLE001
            pass
        self._thread = threading.Thread(
            target=self._watch, name="awf-engine-heartbeat", daemon=True
        )
        self._thread.start()
        return self

    def on_stage(self, stage: str, todo_id: str = "") -> None:
        with self._lock:
            self.stage = str(stage or "")
            if todo_id:
                self.todo_id = str(todo_id)
        self.write()

    def write(self) -> None:
        try:
            write_heartbeat(
                self.project_dir,
                stage=self.stage,
                todo_id=self.todo_id,
                logs_dir=self.logs_dir,
            )
        except Exception as exc:  # noqa: BLE001 — heartbeat must not kill work
            _log_once(self.logs_dir, f"engine-liveness: heartbeat failed: {exc}")

    def note_exception(self, exc_info=None) -> None:
        info = exc_info or sys.exc_info()
        if info and info[0] is not None:
            self._exc_tail = "".join(traceback.format_exception(*info))

    def finish(self, rc: int | None = None, *, sig=None, reason: str = "") -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            if not self._started:
                # Never started (refused launch): there is no engine run to
                # record — and no artifact may be left behind.
                return
        self._stop.set()
        # TODO-0242 REVIEW (F6): rc without a Python exception is NOT a
        # traceback — ``write_engine_exit`` writes "(no exception)" for an
        # empty tail. A fabricated ``format_stack()`` here turned a plain
        # non-zero exit into a misleading "traceback" section.
        tail = self._exc_tail
        try:
            write_engine_exit(
                self.project_dir,
                rc=rc,
                sig=sig,
                stage=self.stage,
                todo_id=self.todo_id,
                traceback_tail=tail,
                reason=reason,
                logs_dir=self.logs_dir,
            )
        except Exception as exc:  # noqa: BLE001 — best effort on every exit
            _log_once(self.logs_dir, f"engine-liveness: exit record failed: {exc}")
        try:
            atexit.unregister(self._atexit)
        except Exception:  # noqa: BLE001
            pass
        if self._pdeath:
            # TODO-0242 (A.1): the armed death trigger belongs to an
            # unexpected death, not to a clean finish — clear it.
            disarm_parent_death_signal(self.logs_dir)
            self._pdeath = False
        self._restore_signals()

    # -- internals ------------------------------------------------------

    def _atexit(self) -> None:
        self.finish(0, reason="process exit")

    def _watch(self) -> None:
        while not self._stop.wait(self.interval):
            if (
                self._kill_with_parent
                and not self._detached
                and not self._pdeath
                and os.getppid() != self._initial_ppid
            ):
                self.finish(1, reason=f"parent died (ppid {self._initial_ppid})")
                os._exit(1)
            self.write()

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                self._old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._on_signal)
            except (ValueError, OSError, RuntimeError):
                self._old_handlers.pop(signum, None)

    def _restore_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum, old in list(self._old_handlers.items()):
            try:
                signal.signal(signum, old)  # type: ignore[arg-type]
            except (ValueError, OSError, RuntimeError):
                pass

    def _on_signal(self, signum: int, frame) -> None:  # noqa: ARG002
        self.finish(128 + signum, sig=signum, reason="signal")
        # Re-raise with the default handler so the shell sees the signal.
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except Exception:  # noqa: BLE001
            os._exit(128 + signum)


# ── status detector ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class EngineDead:
    stage: str
    todo_id: str
    reason: str

    @property
    def line(self) -> str:
        return f"stage={self.stage or '?'}, todo={self.todo_id or '?'}, {self.reason}"


@dataclass(frozen=True)
class StaleStage:
    """The stage on disk is not progressing: the engine pid is dead, or
    ``updated_at`` has not moved for longer than the threshold and no live
    heartbeat proves the engine runs.

    Separate from :class:`EngineDead` on purpose: the death fact feeds the
    resume advice (``engine_dead_hint``), while this one is the explicit
    "not green" line the card, status and dashboard render. A dead engine
    produces both — the surfaces show the STALE line, the advice still
    comes from the single death owner.

    The duration and the threshold live inside :attr:`reason`: the line is
    the only consumer, and a placeholder zero on the engine-death path
    would be a fabricated fact (TODO-0306 architect review).
    """

    stage: str
    todo_id: str
    reason: str

    @property
    def line(self) -> str:
        todo = f" ({self.todo_id})" if self.todo_id else ""
        return f"STALE stage {self.stage or '?'}{todo} — dead: {self.reason}"


def stale_stage_minutes(config: dict | None = None) -> int:
    """``automation.stale_stage_minutes``, default 15.

    A broken or non-positive value degrades to the default: the detector
    must stay ON (the incident's whole point was a silent freeze), and a
    typo must not disable it.
    """
    value = cfg_mod.get(config or {}, "automation.stale_stage_minutes")
    if isinstance(value, bool):
        return DEFAULT_STALE_STAGE_MINUTES
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_STALE_STAGE_MINUTES
    return minutes if minutes > 0 else DEFAULT_STALE_STAGE_MINUTES


def _updated_at_age_minutes(
    state: dict, *, now: float | None = None
) -> float | None:
    raw = state.get("updated_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = time.time() if now is None else now
    return max(current - parsed.timestamp(), 0.0) / 60.0


def _engine_heartbeats(
    project_dir: Path, pid: int, config: dict | None, *, now: float | None = None
) -> bool:
    """True when the heartbeat belongs to ``pid`` and is still fresh.

    Same evidence :func:`detect_engine_dead` calls "alive" (one reader:
    :func:`_heartbeat_for_pid`): a foreign or unreadable heartbeat proves
    nothing (pre-0241 engines wrote none), so it reads as NOT alive and the
    ``updated_at`` age decides.
    """
    present, age = _heartbeat_for_pid(project_dir, pid, now=now)
    if not present or age is None:
        return False
    return age <= heartbeat_stale_seconds(config)


def detect_stale_stage(
    project_dir: Path,
    state: dict | None,
    config: dict | None = None,
    *,
    now: float | None = None,
    live_pid: int | None = None,
) -> StaleStage | None:
    """The active stage is STALE: dead engine pid, or a quiet ``updated_at``.

    A live heartbeat of the SAME pid is proof the engine runs, so the age
    rule is skipped: ``updated_at`` is written on stage start and on
    signals only, so every stage longer than the threshold would otherwise
    be painted dead (TODO-0306 architect review). The opt-in stall line
    stays the diagnostic for a live but silent worker.

    ``live_pid`` is the resolver's answer when the caller already asked it
    (``check_pipeline_running``). A state naming a DIFFERENT pid is a
    leftover from the previous run — the launch window, where the engine is
    starting, not frozen — so it is not reported. The same pid keeps the
    stale rule in force: a live process with a stale heartbeat is exactly
    the frozen engine the incident hit (PA-0306-3).

    Read-only and degradation-safe (a broken state file yields None, never
    a traceback). A marker-less leftover (``phase: done``) is not a running
    stage and is never reported. A pending salvage or checkpoint is a
    DELIBERATE stop with its own badge — a quiet ``updated_at`` there is
    not a freeze. Both guards live here, not in each surface, so the card,
    status and dashboard cannot drift apart (same rule as
    :func:`status_engine_dead`).
    """
    if not isinstance(state, dict) or not state:
        return None
    if state.get("phase") == "done":
        return None
    if state.get("salvage_needed") or state.get("checkpoint_pending"):
        return None
    stage = str(state.get("stage_name") or "").strip()
    todo_id = str(state.get("todo_id") or "").strip()
    if not (stage or todo_id or state.get("stage_kind")):
        return None

    cfg = config if config is not None else cfg_mod.load(project_dir)
    threshold = stale_stage_minutes(cfg)

    try:
        state_pid = int(state.get("pipeline_pid") or 0)
    except (TypeError, ValueError):
        state_pid = 0
    if live_pid is not None and live_pid > 1:
        if state_pid and state_pid != live_pid:
            # The live engine is a DIFFERENT process: this state belongs to
            # the previous run (launch window). The resolver owns the
            # running fact; a leftover state is not a freeze.
            return None
        pid = live_pid
    else:
        pid = state_pid
    if pid > 1:
        from .api._liveness import probe_alive

        if not probe_alive(pid):
            return StaleStage(
                stage=stage,
                todo_id=todo_id,
                reason=f"pid {pid} dead",
            )
        if _engine_heartbeats(project_dir, pid, cfg, now=now):
            return None

    age = _updated_at_age_minutes(state, now=now)
    if age is None or age < threshold:
        return None
    return StaleStage(
        stage=stage,
        todo_id=todo_id,
        reason=f"no state event for {int(age)} min (limit {threshold} min)",
    )


def stale_line_from_engine_dead(dead: EngineDead) -> StaleStage:
    """The STALE view of a confirmed engine death (same fact, display form)."""
    return StaleStage(
        stage=dead.stage,
        todo_id=dead.todo_id,
        reason=dead.reason,
    )


def engine_dead_hint(line: str, todo_id: str = "") -> str:
    """The one wording of the resume hint (status, brief, run status).

    The surfaces used to assemble the same sentence next to their own
    fields and had already drifted apart; the text is part of the unit's
    contract, so it has a single owner here.
    """
    return (
        f"Engine is dead ({line}) — resume with "
        f"awf_continue(todo_id='{todo_id or '?'}')."
    )


def closed_run_hint() -> str:
    """The one wording for ``engine_dead`` over a CLOSED run (F8/Q2).

    A run record exists but is not active: the stage state is history, and
    resuming it would revive a finished run. The same sibling-drift rule
    as :func:`engine_dead_hint`: the surfaces that answer "engine is dead"
    must not assemble this sentence each in their own `brief.py` /
    `wait_event.py` copy.
    """
    return (
        "Run is not active — the stage state is a leftover. Start a new "
        "run with awf_run_start(queue=[...])."
    )


def detect_engine_dead(
    project_dir: Path,
    state: dict | None,
    config: dict | None = None,
    *,
    now: float | None = None,
) -> EngineDead | None:
    """State говорит «стадия идёт», а движка нет → ``EngineDead``.

    PID мёртв (``kill 0``) или heartbeat ЭТОГО pid старше порога. Живой PID,
    свежий или отсутствующий heartbeat, heartbeat чужого pid (старый движок
    до TODO-0241) — не смерть: поведение прежнее.
    """
    if not isinstance(state, dict):
        return None
    stage = str(state.get("stage_name") or "")
    todo_id = str(state.get("todo_id") or "")
    try:
        pid = int(state.get("pipeline_pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not stage or pid <= 1:
        return None

    from .api._liveness import probe_alive

    if not probe_alive(pid):
        return EngineDead(stage=stage, todo_id=todo_id, reason=f"pid {pid} dead")

    present, age = _heartbeat_for_pid(project_dir, pid, now=now)
    if not present:
        # Missing or foreign heartbeat (another launch, a test seam, a
        # pre-0241 engine): its age says nothing about THIS pid. No death
        # verdict from absent evidence.
        return None
    if age is None:
        return EngineDead(
            stage=stage, todo_id=todo_id, reason="heartbeat unreadable"
        )
    stale = heartbeat_stale_seconds(config or cfg_mod.load(project_dir))
    if age > stale:
        return EngineDead(
            stage=stage,
            todo_id=todo_id,
            reason=f"heartbeat stale {int(age)}s > {int(stale)}s",
        )
    return None


def status_engine_dead(
    project_dir: Path,
    state: dict | None,
    config: dict | None = None,
    *,
    pipeline_running: bool,
) -> EngineDead | None:
    """The status rule on top of :func:`detect_engine_dead`.

    A live pipeline means the stage state is merely not rewritten yet (the
    launch window opens between the PID file and the first stage write).
    The shared liveness resolver owns that fact, so the status surfaces ask
    it first and never derive a death verdict next to its answer.

    A pending salvage owns its own surface: the worker-death advice is
    "review the diff / ACK / retry", not "resume the engine". The guard
    lives here (not in each caller) because both status surfaces read the
    same pipeline state — a caller-local copy already drifted once.
    """
    if pipeline_running or not isinstance(state, dict) or not state:
        return None
    if state.get("salvage_needed"):
        return None
    return detect_engine_dead(project_dir, state, config)


def _heartbeat_for_pid(
    project_dir: Path, pid: int, *, now: float | None = None
) -> tuple[bool, float | None]:
    """``(present, age_seconds)`` for a heartbeat owned by ``pid``.

    The ONE evidence reader shared by the death verdict and the stale
    detector: ``present`` is False for a missing or foreign heartbeat
    (another launch, a pre-0241 engine) — neither life nor death may be
    derived from it. ``age`` is None when the heartbeat is present but its
    stamp is unreadable: the caller decides whether that is death
    (:func:`detect_engine_dead`) or merely no proof of life
    (:func:`detect_stale_stage`).
    """
    heartbeat = read_heartbeat(project_dir)
    if not heartbeat or _heartbeat_pid(heartbeat) != pid:
        return False, None
    return True, _heartbeat_age_seconds(heartbeat, now=now)


def _heartbeat_pid(heartbeat: dict) -> int | None:
    try:
        pid = int(heartbeat.get("pid"))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _heartbeat_age_seconds(heartbeat: dict, *, now: float | None = None) -> float | None:
    current = time.time() if now is None else now
    try:
        stamp = float(heartbeat.get("ts_epoch"))
    except (TypeError, ValueError):
        stamp = None
    if stamp is None:
        raw = heartbeat.get("ts")
        if isinstance(raw, str) and raw:
            try:
                stamp = datetime.fromisoformat(raw).timestamp()
            except ValueError:
                return None
    if stamp is None:
        return None
    return max(current - stamp, 0.0)


__all__ = [
    "EngineDead",
    "EngineLiveness",
    "StaleStage",
    "closed_run_hint",
    "detect_engine_dead",
    "detect_stale_stage",
    "disarm_parent_death_signal",
    "engine_dead_hint",
    "exit_file_path",
    "heartbeat_interval_seconds",
    "heartbeat_path",
    "heartbeat_stale_seconds",
    "install_parent_death_signal",
    "read_heartbeat",
    "stale_line_from_engine_dead",
    "stale_stage_minutes",
    "status_engine_dead",
    "write_engine_exit",
    "write_heartbeat",
]
