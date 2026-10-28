"""R5 (ops reliability): the one home for version facts.

``awf doctor`` and the launch handshake (R6) must agree on what "the version"
means, so every source is read here and nowhere else:

- ``process_version`` — ``awf.__version__`` of the imported module (the code
  that is actually running);
- ``pinned_version`` — ``__version__`` of the pinned checkout named by
  ``automation.runner_dir``. The file is parsed as text: importing a foreign
  checkout would execute its code inside the MCP process (D6). ``""`` means
  "the key is absent", ``"unknown"`` means "the file cannot be read";
- ``plugin_version`` — ``agent_workflow_ui.__version__``; an import error is
  ``"unknown"`` (AC27: ``unknown`` never forms a drift pair);
- ``install_metadata`` — the version recorded by the distribution that owns
  the *imported* module (its nearest ``*.dist-info`` / ``*.egg-info``), not
  ``importlib.metadata.version(name)``: that answer follows ``sys.path``, so
  ``awf`` run from ``$HOME`` reported the venv's 1.7.0 while importing the
  tree's 2.1.0 (AUD-0296-2). Its lag behind the module means "the package was
  not reinstalled" (A9).

A version alone does not prove the code is identical (two checkouts can both
say 2.1.0 — AUD-0293-2). The identity of the code is the commit: ``repo_commit``
reads the pinned checkout's HEAD, ``process_commit`` reads the running tree's,
and ``doctor`` compares them. The launch handshake compares versions only; its
own commit check is IMP6's work (TODO-0297). ``version_warning`` is the
handshake's compact drift line (K5/K7); ``doctor`` prints the same rules as
separate per-check lines (AC23).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import git_utils

UNKNOWN = "unknown"
RUNNER_DIR_KEY = "automation.runner_dir"

#: Outcomes of ``plugin_drift_kind`` (the plugin is compared with the process).
DRIFT_VERSION = "version"
DRIFT_FLOOR = "floor"
#: The one advice string per outcome: ``doctor`` and the handshake line print
#: the same words, so a reworded advice cannot survive in only one home.
ADVICE_RESTART = "restart the opencode session"
ADVICE_UPGRADE = "upgrade awf"

_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)


def process_version() -> str:
    """``awf.__version__`` of the imported module."""
    from . import __version__

    return str(__version__)


def runner_path(project_dir: str | Path, config: dict[str, Any]) -> Path | None:
    """The pinned checkout directory, or ``None`` when ``runner_dir`` is unset.

    The same resolution as the launch path (``api/_background.py``): relative
    values are relative to the project root, ``~`` is expanded.
    """
    raw = cfg_mod.get(config, RUNNER_DIR_KEY)
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(project_dir) / path
    return path


def pinned_version(project_dir: str | Path, config: dict[str, Any]) -> str:
    """``__version__`` of ``<runner_dir>/awf/__init__.py`` (text, not import).

    Returns ``""`` when ``automation.runner_dir`` is absent, ``"unknown"``
    when the file is missing or unreadable.
    """
    path = runner_path(project_dir, config)
    if path is None:
        return ""
    init = path / "awf" / "__init__.py"
    try:
        text = init.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return UNKNOWN
    match = _VERSION_RE.search(text)
    return match.group(1) if match else UNKNOWN


def runner_setting_problem(config: dict[str, Any]) -> str:
    """A fact when ``automation.runner_dir`` is set but unusable, else ``""``.

    The launch (``api/_background.py``) refuses a non-string or empty value
    before spawning. ``runner_path`` cannot tell "absent" from "invalid"
    (both give ``None``), so the doctor must ask this question itself: calling
    such a project "not pinned" would report a healthy launch for one that
    cannot start.
    """
    raw = cfg_mod.get(config, RUNNER_DIR_KEY)
    if raw is None:
        return ""
    if not isinstance(raw, str) or not raw:
        # AC17/AUD-0296-5: name the key and its type, never the value — a
        # mapping here can carry an api_key, and the report travels to the
        # model's context. ``42`` and ``{api_key: sk-live-…}`` read alike.
        kind = "an empty string" if raw == "" else f"type {type(raw).__name__}"
        return f"{RUNNER_DIR_KEY} has {kind}, not a path — the launch will refuse"
    return ""


def runner_version_problem(project_dir: str | Path, config: dict[str, Any]) -> str:
    """Why the pinned checkout's version cannot be read, or ``""``.

    ``pinned_version`` answers one ``"unknown"`` for three different
    breakages; the doctor must name which one it is (AUD-0296d-3), the way
    ``runner_setting_problem`` names a bad setting. The checks are ordered
    from the coarse one to the fine one: a missing directory is not also
    reported as a missing ``awf/__init__.py``.
    """
    path = runner_path(project_dir, config)
    if path is None:
        return ""
    if not path.is_dir():
        return f"missing dir {path}"
    init = path / "awf" / "__init__.py"
    if not init.is_file():
        return f"no awf/__init__.py at {path}"
    try:
        text = init.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return f"unreadable awf/__init__.py ({type(e).__name__})"
    if not _VERSION_RE.search(text):
        return "no __version__ in awf/__init__.py"
    return ""


def runner_version(project_dir: str | Path, config: dict[str, Any]) -> str:
    """The version of the code that will execute stages: pinned, else process."""
    return pinned_version(project_dir, config) or process_version()


def plugin_version() -> str:
    """``agent_workflow_ui.__version__``; ``"unknown"`` when the import fails."""
    try:
        from agent_workflow_ui import __version__
    except Exception:  # noqa: BLE001 — AC27: any import failure is "unknown"
        return UNKNOWN
    return str(__version__ or UNKNOWN)


def _dist_metadata_file(dist_dir: Path) -> Path:
    """The metadata file inside a ``*.dist-info`` / ``*.egg-info`` directory."""
    if dist_dir.name.endswith(".dist-info"):
        return dist_dir / "METADATA"
    return dist_dir / "PKG-INFO"


def _dist_project(dist_dir: Path) -> str:
    """Project name of a distribution directory (``awf-2.1.0.egg-info`` → ``awf``).

    The version is cut from the right, not the name at its first dash: a
    distribution name may itself contain dashes (``agent-workflow-ui``), and a
    version-less ``agent-workflow-ui.egg-info`` has no dash to cut at all.
    Splitting at the first dash read that name as ``agent`` and the owning
    distribution was never found (AUD-0296-2).
    """
    stem = dist_dir.name
    for suffix in (".dist-info", ".egg-info"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = re.sub(r"-\d[^-]*$", "", stem)
    return re.sub(r"[-_.]+", "-", stem).lower()


def _dist_version(dist_dir: Path) -> str:
    """``Version:`` of a distribution directory; ``"unknown"`` when unreadable."""
    try:
        text = _dist_metadata_file(dist_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return UNKNOWN
    match = re.search(r"^Version:\s*(\S+)\s*$", text, re.MULTILINE)
    return match.group(1) if match else UNKNOWN


def _owning_dist(module_file: Path, dist_name: str) -> Path | None:
    """Nearest distribution directory above *module_file* for *dist_name*."""
    wanted = dist_name.replace("_", "-").lower()
    parent = module_file.resolve().parent
    for base in (parent, *parent.parents):
        for pattern in ("*.dist-info", "*.egg-info"):
            for candidate in sorted(base.glob(pattern)):
                if _dist_project(candidate) == wanted:
                    return candidate
    return None


def install_metadata(name: str) -> str:
    """Version of the distribution that owns the imported module for *name*.

    The anchor is the module file, so the answer does not depend on the
    process cwd (AUD-0296-2): the tree's ``awf.egg-info`` describes the tree's
    ``awf``, wherever the command was started. ``"unknown"`` when the module
    cannot be imported or no owning distribution is found.
    """
    module_name = name.replace("-", "_")
    try:
        import importlib

        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 — an unimportable module has no metadata
        return UNKNOWN
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return UNKNOWN
    dist = _owning_dist(Path(module_file), name)
    return _dist_version(dist) if dist is not None else UNKNOWN


def plugin_requirement() -> str:
    """The ``awf>=`` floor the installed plugin declares, or ``""``.

    ``agent-workflow-ui`` depends on ``awf>=2.1.0``; that floor — not the
    pinned runner version — is what the plugin needs (AUD-0296-3).
    """
    try:
        import importlib

        module = importlib.import_module("agent_workflow_ui")
    except Exception:  # noqa: BLE001 — no plugin module, no requirement
        return ""
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return ""
    dist = _owning_dist(Path(module_file), "agent-workflow-ui")
    if dist is None:
        return ""
    try:
        text = _dist_metadata_file(dist).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    match = re.search(
        r"^Requires-Dist:\s*awf\s*(?:\(\s*)?>=\s*([0-9][0-9A-Za-z.\-+]*)",
        text,
        re.MULTILINE,
    )
    return match.group(1) if match else ""


def repo_commit(path: str | Path) -> str:
    """Short HEAD commit of ``path``'s repo; ``""`` when it is not a repo.

    Best-effort by design (AUD-0293-2): a wheel install, a missing git binary
    or a repo without commits is "no commit evidence", not an error. The walk
    is ``git_utils``'s, so the git surface stays one home.
    """
    try:
        return git_utils.git_stdout(
            path, "rev-parse", "--short", "HEAD", check=False
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def process_commit() -> str:
    """Short commit of the tree the imported ``awf`` package lives in."""
    return repo_commit(Path(__file__).resolve().parent.parent)


def repo_dirty_count(path: str | Path) -> int:
    """Changed or untracked entries in ``path``'s repo; 0 when clean/not a repo.

    A commit alone does not identify the code either: the engine imports the
    checkout's *working tree*, so uncommitted edits run as they are
    (AUD-0296d-1). ``git status --porcelain`` is the same listing the
    mutation dirty-gate trusts; a non-repo or an unreadable status is 0 —
    "no evidence of dirt", and the caller has no commit to compare anyway.
    """
    try:
        listing = git_utils.status_porcelain(path)
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in listing.splitlines() if line.strip())


def runner_drift(process: str, runner: str) -> bool:
    """True when the runner version differs from the running process version."""
    return runner != process


def _version_tuple(version: str) -> tuple[int, ...]:
    """Numeric parts of a version (``2.1.0rc1`` → ``(2, 1, 0)``); ``()`` if none."""
    match = re.search(r"(\d+(?:\.\d+)*)", version)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def awf_satisfies(version: str, required: str) -> bool:
    """True when *version* meets the ``awf>=`` floor in *required*.

    An empty or unparsable floor is satisfied by anything: a missing
    requirement proves no conflict. Numeric comparison only — a pre-release
    suffix is ignored, which is enough for the ``X.Y.Z`` floor.
    """
    floor = _version_tuple(required)
    if not floor:
        return True
    actual = _version_tuple(version)
    if not actual:
        return True
    width = max(len(floor), len(actual))
    return actual + (0,) * (width - len(actual)) >= floor + (0,) * (width - len(floor))


def plugin_drift_kind(process: str, plugin: str, required: str = "") -> str:
    """How the plugin disagrees with the running awf: ``""``, ``version`` or ``floor``.

    The plugin is loaded by the *process*, not by the pinned checkout: a newer
    ``runner_dir`` (2.5.0) does not make a plugin that matches the running
    2.1.0 stale, and "restart the session" would not change it (AUD-0296-3).
    The pinned runner's own drift is a separate line.

    ``required`` is the plugin's declared ``awf>=`` floor. ``unknown`` on
    either side never forms a pair (AC27): an unimportable plugin or an
    unreadable process proves nothing. An unmet floor outranks a version
    difference: the running awf is too old for the plugin whatever version
    string the plugin itself carries, and "restart the session" cannot install
    a newer awf (AUD-0296 CTX4). With the floor met and the versions unequal
    the answer is the plain version pair — the doctor and the handshake line
    both branch on this one answer, so their advice cannot diverge.
    """
    if plugin == UNKNOWN or process == UNKNOWN:
        return ""
    if required and not awf_satisfies(process, required):
        return DRIFT_FLOOR
    if plugin != process:
        return DRIFT_VERSION
    return ""


def version_warning(
    *,
    process: str,
    runner: str,
    plugin: str,
    pinned_dir: str = "",
    plugin_required: str = "",
) -> str:
    """The launch handshake's drift line (K5/K7), ``""`` when versions agree.

    ``runner`` is the *effective* runner version — the one ``runner_version``
    returns (pinned, else process), never ``""``; an empty value means "the
    key is absent" and is not a drift, so it cannot produce a malformed
    ``runner , process`` pair (the trap IMP6 would otherwise hit).

    Only drifted participants are listed next to the process version; the
    plugin is omitted when it is ``unknown`` (AC27). The plugin is compared
    with the process that loads it, not with the pinned runner: a newer
    checkout does not make a matching plugin stale (AUD-0296-3). Metadata
    staleness is not part of this line — ``doctor`` reports it as its own
    check (K6).
    """
    runner_differs = bool(runner) and runner_drift(process, runner)
    kind = plugin_drift_kind(process, plugin, plugin_required)
    if not (runner_differs or kind):
        return ""
    # Equal plugin and process with a drift pair means the plugin's declared
    # ``awf>=`` floor is unmet: "plugin != process" there would be false, and
    # a restart would not help — the advice is "upgrade awf" (AUD-0296-3).
    floor_only = kind == DRIFT_FLOOR
    parts: list[str] = []
    if runner_differs:
        fact = f"runner {runner}"
        if pinned_dir:
            fact += f" (pinned: {pinned_dir})"
        parts.append(fact)
    if not floor_only:
        parts.append(f"process {process}")
    if kind == DRIFT_FLOOR:
        parts.append(
            f"plugin {plugin} requires awf>={plugin_required} (process {process})"
        )
    elif kind == DRIFT_VERSION:
        parts.append(f"plugin {plugin}")
    advice = ADVICE_UPGRADE if floor_only else ADVICE_RESTART
    return "version drift: " + ", ".join(parts) + f" — run awf doctor; {advice}"


__all__ = [
    "UNKNOWN",
    "RUNNER_DIR_KEY",
    "ADVICE_RESTART",
    "ADVICE_UPGRADE",
    "DRIFT_FLOOR",
    "DRIFT_VERSION",
    "awf_satisfies",
    "install_metadata",
    "pinned_version",
    "plugin_drift_kind",
    "plugin_requirement",
    "plugin_version",
    "process_commit",
    "process_version",
    "repo_commit",
    "repo_dirty_count",
    "runner_drift",
    "runner_path",
    "runner_setting_problem",
    "runner_version",
    "runner_version_problem",
    "version_warning",
]
