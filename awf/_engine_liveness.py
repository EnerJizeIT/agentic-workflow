"""TODO-0241: двигатель оставляет следы — heartbeat, engine-exit, parent-death.

Три тихие смерти пайплайна за мини-волну (0238, 0228, 0240): воркер падал
по внешней ошибке, движок завершался, родитель (MCP-сервер) не забирал
rc — зомби ``Zs``, state говорил «стадия идёт», salvage не писался.

Модуль — единственный владелец двух поверхностей:

- ``.agentic/state/engine.heartbeat`` — JSON (pid, ppid, ts, stage,
  todo_id), атомарная запись через managed-путь. Пишется на старте,
  каждой смене стадии и периодически (watchdog-поток, интервал из
  ``engine.heartbeat_seconds``, по умолчанию 30 с).
- ``.agentic/context/ENGINE-EXIT-<ts>.md`` — причина любого завершения
  (rc, сигнал, стадия, todo, хвост трейсбека). ``atexit`` + обработчики
  SIGTERM/SIGINT, плюс явный ``finish(rc)`` из движка.

Parent-death: на Linux ``prctl(PR_SET_PDEATHSIG, SIGTERM)`` до спавна
воркера; fallback (не-Linux или ошибка prctl) — watchdog сверяет
``os.getppid()`` с исходным и выходит с записью причины. Не-Linux —
no-op с одной строкой в лог.

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
_PR_SET_PDEATHSIG = 1


def heartbeat_path(project_dir: Path) -> Path:
    return paths.state_dir(project_dir) / HEARTBEAT_NAME


def exit_file_path(project_dir: Path, ts: str | None = None) -> Path:
    stamp = ts or _stamp()
    return paths.context_dir(project_dir) / f"ENGINE-EXIT-{stamp}.md"


def _stamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _now_iso(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.isoformat()


def heartbeat_interval_seconds(config: dict | None = None) -> float:
    value = cfg_mod.get(config or {}, "engine.heartbeat_seconds")
    try:
        interval = float(value)
    except (TypeError, ValueError):
        return DEFAULT_HEARTBEAT_SECONDS
    return interval if interval > 0 else DEFAULT_HEARTBEAT_SECONDS


def heartbeat_stale_seconds(config: dict | None = None) -> float:
    value = cfg_mod.get(config or {}, "engine.heartbeat_stale_seconds")
    try:
        stale = float(value)
    except (TypeError, ValueError):
        interval = heartbeat_interval_seconds(config)
        return max(interval * 3, DEFAULT_STALE_SECONDS)
    return stale if stale > 0 else DEFAULT_STALE_SECONDS


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
    """Записать ``ENGINE-EXIT-<ts>.md``; None, если запись не удалась."""
    stamp = _stamp()
    target = exit_file_path(project_dir, stamp)
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
    lines.append(tail if tail else "(none)")
    lines.append("")
    if _managed_write(target, "\n".join(lines), logs_dir):
        return target
    return None


# ── parent-death ────────────────────────────────────────────────────────


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
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        rc = libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except Exception as exc:  # noqa: BLE001 — degrade to the ppid watchdog
        _log_once(logs_dir, f"engine-liveness: prctl failed: {exc}")
        return False
    if rc != 0:
        _log_once(
            logs_dir,
            f"engine-liveness: prctl PR_SET_PDEATHSIG rc={rc} "
            f"errno={ctypes.get_errno()}",
        )
        return False
    return True


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
        if not self._detached:
            self._pdeath = install_parent_death_signal(self.logs_dir)
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
        tail = self._exc_tail
        if not tail and reason == "" and rc not in (0, None):
            tail = "".join(traceback.format_stack())
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
        self._restore_signals()

    # -- internals ------------------------------------------------------

    def _atexit(self) -> None:
        self.finish(0, reason="process exit")

    def _watch(self) -> None:
        while not self._stop.wait(self.interval):
            if (
                not self._detached
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

    heartbeat = read_heartbeat(project_dir)
    if not heartbeat:
        return None
    if _heartbeat_pid(heartbeat) != pid:
        # The heartbeat belongs to another engine run (a previous launch, a
        # test seam): its age says nothing about THIS pid. No death verdict
        # from foreign evidence — same answer as "no heartbeat" (pre-0241).
        return None
    age = _heartbeat_age_seconds(heartbeat, now=now)
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
    "detect_engine_dead",
    "engine_dead_hint",
    "exit_file_path",
    "heartbeat_interval_seconds",
    "heartbeat_path",
    "heartbeat_stale_seconds",
    "install_parent_death_signal",
    "read_heartbeat",
    "status_engine_dead",
    "write_engine_exit",
    "write_heartbeat",
]
