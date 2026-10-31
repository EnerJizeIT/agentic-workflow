"""opencode 2.x adaptation (K1–K4): version probe, standalone mode, preflight.

opencode 2.x runs ``opencode run`` as a client to a shared background server;
only ``--standalone`` puts that server into the worker's own lifetime. Without
it a kill leaves the session on the server (CTX5), so awf must know the
binary version BEFORE the first side effect of any launch.

Version is probed per process and cached: a stage fork counts for itself
(D3, AC6). The module does not import ``awf.api`` — ``awf.api`` imports it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

BINARY = "opencode"
MANAGED_KEY = "opencode.managed"
MANAGED_ENV = "AWF_OPENCODE_MANAGED"
MANAGED_WARNING = (
    "managed: on — worker env может игнорироваться, standalone выключен"
)
VERSION_TIMEOUT = 5.0

_TRUE_WORDS = frozenset({"1", "true", "yes"})
# ``opencode 2.x`` prints ``opencode v2.2.3``: a leading letter before
# the number is not a word boundary, so the lookarounds replace \b.
_VERSION_RE = re.compile(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)(?![\d.])")


@dataclass(frozen=True)
class Probe:
    """Result of ``opencode --version``: ``ok`` / ``missing`` / ``unknown``."""

    status: str
    version: tuple[int, int, int] | None = None

    @property
    def text(self) -> str:
        if self.status == "ok" and self.version is not None:
            return ".".join(str(part) for part in self.version)
        return "неизвестна"


@dataclass(frozen=True)
class Plan:
    """What to add to argv and what the user must be told (R2)."""

    add_flag: bool
    warning: str = ""


@dataclass(frozen=True)
class Refusal:
    """A typed launch refusal (never an exception).

    ``rc`` — 0 for a state condition (2.x without isolation, unknown
    version: the caller reports, nothing is broken), 1 for a broken setup
    (bad config value, binary missing).
    """

    message: str
    rc: int = 0
    warning: str = ""


# (binary, resolved path) → Probe. One ``--version`` per process (D3/AC6).
_CACHE: dict[tuple[str, str], Probe] = {}


def reset_version_cache() -> None:
    """Test seam: forget every probe so the next call runs the binary."""
    _CACHE.clear()


def prime_probe(probe: Probe, binary: str = BINARY) -> None:
    """Test seam: record the answer for this process without spawning.

    The suite patches ``subprocess.Popen``/``run`` globally; a real probe
    would collide with those fakes in every unrelated test. Priming keeps
    the production path identical — a test that exercises the probe calls
    :func:`reset_version_cache` first.
    """
    _resolved, key = _resolve(binary)
    _CACHE[(binary, key)] = probe


def _resolve(binary: str) -> tuple[str | None, str]:
    """(resolved path, cache key). A path-like binary is not searched."""
    if os.sep in binary or (os.altsep and os.altsep in binary) or binary.startswith("~"):
        path = Path(binary).expanduser()
        return (str(path) if path.is_file() else None), str(path)
    found = shutil.which(binary)
    return found, found or binary


def _probe(resolved: str | None, timeout: float) -> Probe:
    if resolved is None:
        return Probe("missing")
    try:
        result = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return Probe("missing")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        # TypeError: a patched Popen in tests; never let the probe crash.
        return Probe("unknown")
    if result.returncode != 0:
        return Probe("unknown")
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = _VERSION_RE.search(line)
        if match is None:
            return Probe("unknown")
        return Probe("ok", (int(match.group(1)), int(match.group(2)), int(match.group(3))))
    return Probe("unknown")


def probe_version(binary: str = BINARY, timeout: float = VERSION_TIMEOUT) -> Probe:
    """``opencode --version`` → Probe. No exception escapes (K1)."""
    resolved, key = _resolve(binary)
    cache_key = (binary, key)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached
    probe = _probe(resolved, timeout)
    _CACHE[cache_key] = probe
    return probe


def _opencode_section(config: dict | None) -> dict:
    section = (config or {}).get("opencode") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def is_managed(config: dict | None) -> bool:
    """Managed opt-in (D4): env ``AWF_OPENCODE_MANAGED`` or the config key."""
    env = os.environ.get(MANAGED_ENV, "").strip().lower()
    if env in _TRUE_WORDS:
        return True
    return _opencode_section(config).get("managed") is True


def resolve_plan(
    config: dict | None,
    probe: Probe,
    managed: bool | None = None,
) -> Plan:
    """Whether ``--standalone`` belongs in argv (R2, awf 3).

    There is no mode to choose: 2.x always runs with the flag, and the only
    exception is the managed server (the operator owns the policy there).
    """
    if managed is None:
        managed = is_managed(config)
    if managed:
        return Plan(add_flag=False, warning=MANAGED_WARNING)
    return Plan(add_flag=True)


def apply_standalone(argv: list[str], plan: Plan) -> list[str]:
    """Insert ``--standalone`` right after the subcommand (K2/INV1)."""
    if not plan.add_flag or len(argv) < 2:
        return list(argv)
    return [argv[0], argv[1], "--standalone", *argv[2:]]


def launch_plan(
    project_dir: str | Path,
    config: dict | None = None,
    probe: Probe | None = None,
) -> tuple[Probe, Plan, Refusal | None]:
    """Compose R1 + R2 + managed into the preflight answer (R1).

    awf 3 supports opencode 2.x only: an older binary and an undetermined
    version are refusals (fail-closed), each with its own text and rc, and
    both fire before the first side effect of any entry point.
    """
    if config is None:
        from . import config as cfg_mod

        config = cfg_mod.load(project_dir)
    if probe is None:
        probe = probe_version()
    managed = is_managed(config)
    plan = resolve_plan(config, probe, managed)
    if probe.status == "missing":
        return probe, plan, Refusal(
            f"opencode не найден: бинарь {BINARY!r} отсутствует в PATH — "
            "запуск невозможен",
            rc=1,
        )
    if managed:
        # The operator owns the server policy — no version refusal.
        return probe, plan, None
    if probe.status != "ok" or probe.version is None:
        return probe, plan, Refusal(
            "opencode: версия не определена (--version не ответил или вернул "
            "мусор) — awf 3 требует opencode 2.x, запуск запрещён. "
            "Проверьте установку opencode",
            rc=0,
        )
    if probe.version[0] < 2:
        return probe, plan, Refusal(
            f"awf 3 требует opencode 2.x — найден opencode {probe.text}. "
            "Обновите opencode до 2.x (awf 2.x остаётся на прежнем теге)",
            rc=1,
        )
    return probe, plan, None


def preflight(project_dir: str | Path, config: dict | None = None) -> Refusal | None:
    """Typed refusal for a launch without isolation, or None (K3)."""
    _probe, _plan, refusal = launch_plan(project_dir, config)
    return refusal


def gate(project_dir: str | Path, config: dict | None = None) -> tuple[str, int, str]:
    """Entry-point helper: ``(message, rc, warning)``.

    An empty message means the launch may proceed; a non-empty one is the
    refusal text the caller turns into its own result type. The warning
    (1.x ``on`` / managed) is printed by the caller — the launch continues.
    """
    _probe, plan, refusal = launch_plan(project_dir, config)
    warning = plan.warning
    if refusal is None:
        return "", 0, warning
    return refusal.message, refusal.rc, warning


__all__ = [
    "BINARY",
    "MANAGED_ENV",
    "MANAGED_KEY",
    "MANAGED_WARNING",
    "Plan",
    "Probe",
    "Refusal",
    "apply_standalone",
    "gate",
    "is_managed",
    "launch_plan",
    "preflight",
    "prime_probe",
    "probe_version",
    "reset_version_cache",
    "resolve_plan",
]
