"""A45 (TODO-0360): one plugin instance per working directory.

Under one ``opencode serve`` two ``agent_workflow_ui`` processes can live
(one per MCP client), each binding its own random HTTP port: duplicate form
servers, competing state writes, an extra tab per worker. The first process
to take the file lock serves HTTP; the second reuses the owner's port, and a
headless worker (``opencode run``) runs tool-only — no server, no tab.

Lock location: ``<cwd>/.agentic/state/plugin.lock`` inside an awf project,
``$XDG_STATE_HOME/awf-plugin/<hash-of-cwd>.lock`` otherwise. The lock is an
``flock`` on an open file description, so the kernel releases it when the
owner dies — a leftover file is not poison, it is rewritten by the next
owner.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # POSIX only; Windows keeps today's behaviour (no lock).
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

LOCK_NAME = "plugin.lock"
XDG_DIR_NAME = "awf-plugin"
#: Explicit opt-out of the dashboard server (tools only).
TOOL_ONLY_ENV = "AWF_TOOL_ONLY"
#: Set by awf for every pipeline worker process.
WORKER_MARKER_ENV = "AWF_BACKGROUND_CHILD"
_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OwnerInfo:
    """Who holds the lock right now."""

    pid: int
    port: int
    project: str


@dataclass
class InstanceLock:
    """An acquired (or observed) single-instance lock."""

    path: Path
    fd: int | None = None
    owner: OwnerInfo | None = None

    @property
    def owned(self) -> bool:
        return self.fd is not None

    def publish_port(self, port: int) -> None:
        """Write ``{pid, port, project}`` into the lock file (owner only)."""
        if self.fd is None:
            return
        blob = json.dumps(
            {
                "pid": os.getpid(),
                "port": int(port),
                "project": str(Path.cwd()),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        os.ftruncate(self.fd, 0)
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, blob)

    def release(self) -> None:
        """Drop the lock and clear the published owner; keep the file.

        Unlinking a ``flock`` file re-opens the hole this module closes: a
        process that opened the path before the release holds an fd on the
        old inode, wins the lock there, and the next start creates a *new*
        inode and wins too — two owners, two dashboards. With the file kept,
        every instance locks one inode; a leftover empty file is harmless
        because the pid is gone and the kernel already dropped the lock.
        """
        if self.fd is None:
            return
        try:
            os.ftruncate(self.fd, 0)
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        finally:
            self.fd = None

    def message(self) -> str:
        """Human line for logs: "использую экземпляр <pid> на порту <n>"."""
        if self.owner is not None and self.owner.port:
            return (
                f"использую экземпляр {self.owner.pid} "
                f"на порту {self.owner.port}"
            )
        pid = self.owner.pid if self.owner is not None else "?"
        return f"экземпляр плагина уже запущен (pid {pid}), порт ещё не опубликован"


@dataclass
class StartupPlan:
    """What the plugin should do on startup."""

    mode: str  # "owner" | "reuse" | "tool-only"
    lock: InstanceLock
    http_port: int | None = None
    message: str = ""

    @property
    def starts_server(self) -> bool:
        return self.mode == "owner"


@dataclass
class BootResult:
    """Outcome of :func:`boot` — the running server, if we own it."""

    mode: str
    port: int | None = None
    server: Any | None = None
    message: str = ""
    lock: InstanceLock | None = None


def lock_dir(project_dir: Path | None) -> Path:
    """Directory that keeps the lock file."""
    if project_dir is not None:
        return project_dir / ".agentic" / "state"
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / XDG_DIR_NAME


def lock_path(project_dir: Path | None) -> Path:
    """Lock file for this working directory (or awf project)."""
    if project_dir is not None:
        return lock_dir(project_dir) / LOCK_NAME
    digest = hashlib.sha1(str(Path.cwd().resolve()).encode("utf-8")).hexdigest()[:12]
    return lock_dir(None) / f"{digest}.lock"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def tool_only_requested() -> bool:
    """True for a headless worker: tools only, no dashboard server."""
    if _truthy(os.environ.get(TOOL_ONLY_ENV)):
        return True
    return _truthy(os.environ.get(WORKER_MARKER_ENV))


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        # OverflowError: pid outside the OS range (a corrupt lock file).
        return False
    return True


def read_owner(path: Path) -> OwnerInfo | None:
    """Owner recorded in ``path``, or ``None`` when it is absent/garbage."""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        pid = int(data.get("pid") or 0)
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or not _pid_alive(pid):
        return None
    return OwnerInfo(pid=pid, port=port, project=str(data.get("project") or ""))


def _covers_project(recorded: str, resolved: Path) -> bool:
    """True when a lock recorded at ``recorded`` can serve ``resolved``.

    The plugin keys its lock by its working directory, which is often an
    ancestor of the project (opencode runs the MCP subprocess in ``$HOME``).
    """
    if not recorded:
        return False
    try:
        recorded_path = Path(recorded).expanduser().resolve()
    except OSError:
        return False
    return resolved == recorded_path or resolved.is_relative_to(recorded_path)


def live_owners(root: Path) -> list[OwnerInfo]:
    """Live plugin instances that can serve ``root`` — one per held lock.

    Single reader of the lock layout for ``awf doctor``: the project lock
    plus XDG locks whose recorded project covers ``root``. A dead pid is not
    an instance — the kernel already dropped its lock.
    """
    owners: list[OwnerInfo] = []
    project_lock = lock_dir(root) / LOCK_NAME
    if project_lock.is_file():
        owner = read_owner(project_lock)
        if owner is not None:
            owners.append(owner)
    xdg_dir = lock_dir(None)
    if xdg_dir.is_dir():
        resolved = root.resolve()
        for path in sorted(xdg_dir.glob("*.lock")):
            owner = read_owner(path)
            if owner is not None and _covers_project(owner.project, resolved):
                owners.append(owner)
    return owners


def _wait_owner(path: Path, timeout: float) -> OwnerInfo | None:
    """Wait until the owner publishes a port (it binds after taking the lock)."""
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        owner = read_owner(path)
        if owner is not None and owner.port:
            return owner
        if time.monotonic() >= deadline:
            return owner
        time.sleep(0.05)


def acquire(project_dir: Path | None, *, timeout: float = 3.0) -> InstanceLock:
    """Take the lock or describe the live owner.

    ``flock`` with ``LOCK_NB``: the second process gets ``EWOULDBLOCK`` and
    reads the owner's pid/port instead of binding a second port.
    """
    path = lock_path(project_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("single-instance lock dir unavailable (%s): %s", path.parent, e)
        return InstanceLock(path=path, fd=None, owner=None)

    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        log.warning("single-instance lock unreadable (%s): %s", path, e)
        return InstanceLock(path=path, fd=None, owner=None)

    if fcntl is None:  # pragma: no cover - Windows
        return InstanceLock(path=path, fd=fd)

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        owner = _wait_owner(path, timeout)
        log.info("single-instance: %s (%s)", owner, path)
        return InstanceLock(path=path, fd=None, owner=owner)
    return InstanceLock(path=path, fd=fd)


def plan_startup(
    project_dir: Path | None,
    *,
    tool_only: bool | None = None,
    timeout: float = 3.0,
) -> StartupPlan:
    """Decide owner / reuse / tool-only before any port is bound."""
    if tool_only is None:
        tool_only = tool_only_requested()
    path = lock_path(project_dir)
    if tool_only:
        owner = read_owner(path)
        suffix = f" (экземпляр {owner.pid} на порту {owner.port})" if owner and owner.port else ""
        return StartupPlan(
            mode="tool-only",
            lock=InstanceLock(path=path, fd=None, owner=owner),
            message=f"tool-only режим: дашборд-порт не поднимаю{suffix}",
        )
    lock = acquire(project_dir, timeout=timeout)
    if lock.owned:
        return StartupPlan(mode="owner", lock=lock, message="owner экземпляра")
    return StartupPlan(
        mode="reuse",
        lock=lock,
        http_port=lock.owner.port if lock.owner else None,
        message=lock.message(),
    )


def boot(
    config: Any,
    project_dir: Path | None,
    registry: Any,
    *,
    tool_only: bool | None = None,
    start_server: Any | None = None,
) -> BootResult:
    """Start (or decline to start) the HTTP server for this process.

    ``start_server`` is the injectable ``start_http_server`` (the entry point
    passes the module-level import, so existing monkeypatches keep working).
    """
    plan = plan_startup(project_dir, tool_only=tool_only)
    if plan.mode != "owner":
        return BootResult(
            mode=plan.mode,
            port=plan.http_port,
            server=None,
            message=plan.message,
            lock=plan.lock,
        )
    if start_server is None:
        from .http_endpoint import start_http_server as start_server

    server, port = start_server(config, registry)
    plan.lock.publish_port(port)
    return BootResult(
        mode="owner",
        port=port,
        server=server,
        message=f"agent_workflow_ui started (HTTP on port {port})",
        lock=plan.lock,
    )


__all__ = [
    "BootResult",
    "InstanceLock",
    "OwnerInfo",
    "StartupPlan",
    "acquire",
    "boot",
    "live_owners",
    "lock_path",
    "plan_startup",
    "read_owner",
    "tool_only_requested",
]
