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
STANDALONE_KEY = "opencode.standalone"
KEY = STANDALONE_KEY
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
    """Effective mode: what to add to argv and what the user must be told."""

    mode: str
    add_flag: bool
    warning: str = ""
    refusal: str = ""


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
    except (OSError, subprocess.SubprocessError, ValueError):
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


def standalone_mode(config: dict | None) -> tuple[str, str]:
    """The ``opencode.standalone`` domain AFTER yaml.safe_load (D5).

    Missing — ``auto``. ``True``/``"on"``/``"true"`` — on; ``False``/
    ``"off"``/``"false"`` — off; ``auto`` — auto. Anything else — refusal
    naming the key and the value (AC5).
    """
    section = _opencode_section(config)
    if "standalone" not in section:
        return "auto", ""
    raw = section["standalone"]
    if isinstance(raw, bool):
        return ("on" if raw else "off"), ""
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word == "auto":
            return "auto", ""
        if word in ("on", "true"):
            return "on", ""
        if word in ("off", "false"):
            return "off", ""
    return "", (
        f"{KEY} = {raw!r} — ожидается auto/on/off (bool или строка); "
        "запуск отменён"
    )


def is_managed(config: dict | None) -> bool:
    """Managed opt-in (D4): env ``AWF_OPENCODE_MANAGED`` or the config key."""
    env = os.environ.get(MANAGED_ENV, "").strip().lower()
    if env in _TRUE_WORDS:
        return True
    return _opencode_section(config).get("managed") is True


def resolve_standalone(
    config: dict | None,
    probe: Probe,
    managed: bool | None = None,
) -> Plan:
    """Mode + whether ``--standalone`` belongs in argv (K2)."""
    mode, refusal = standalone_mode(config)
    if refusal:
        return Plan("", add_flag=False, refusal=refusal)
    if managed is None:
        managed = is_managed(config)
    if managed:
        return Plan(mode, add_flag=False, warning=MANAGED_WARNING)
    if (
        probe.status == "ok"
        and probe.version is not None
        and probe.version[0] >= 2
        and mode in ("auto", "on")
    ):
        return Plan(mode, add_flag=True)
    if (
        mode == "on"
        and probe.status == "ok"
        and probe.version is not None
        and probe.version[0] < 2
    ):
        return Plan(
            mode,
            add_flag=False,
            warning=(
                "--standalone не поддерживается версией opencode "
                f"{probe.text} — флаг не передан"
            ),
        )
    return Plan(mode, add_flag=False)


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
    """Compose K1 + K2 + D4 into the preflight answer (K3).

    Returns ``(probe, plan, refusal)``. ``refusal`` is None when the launch
    may proceed; ``plan.warning`` still carries the 1.x-``on`` and managed
    warnings.
    """
    if config is None:
        from . import config as cfg_mod

        config = cfg_mod.load(project_dir)
    if probe is None:
        probe = probe_version()
    managed = is_managed(config)
    plan = resolve_standalone(config, probe, managed)
    if plan.refusal:
        return probe, plan, Refusal(plan.refusal, rc=1)
    if probe.status == "missing":
        return probe, plan, Refusal(
            f"opencode не найден: бинарь {BINARY!r} отсутствует в PATH — "
            "запуск невозможен",
            rc=1,
        )
    if managed:
        # K4: the operator owns the server policy — no version refusal.
        return probe, plan, None
    if probe.status != "ok":
        return probe, plan, Refusal(
            "opencode: версия не определена (--version не ответил или вернул "
            f"мусор) — запуск без изоляции запрещён. Проверьте установку "
            f"opencode и ключ {KEY}",
            rc=0,
        )
    if probe.version is not None and probe.version[0] >= 2 and not plan.add_flag:
        return probe, plan, Refusal(
            f"opencode {probe.text}: {KEY} = off — без изоляции kill оставит "
            "сессию на общем сервере. Включите auto/on или управляемый режим "
            f"({MANAGED_KEY})",
            rc=0,
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
    "KEY",
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
    "probe_version",
    "reset_version_cache",
    "resolve_standalone",
    "standalone_mode",
]
