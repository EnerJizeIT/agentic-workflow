"""R5 (ops reliability): ``awf doctor`` — diagnose before the launch.

The operator's question is "will this project run with the code and config I
think it will?" — asked *before* a stage burns an hour on a stale plugin or a
model provider that silently disappeared. The doctor answers it from surfaces
that already exist:

- versions (``awf.versions``): the running module, the pinned checkout
  (``automation.runner_dir``), the plugin module and the install metadata;
- models: ``check_model_config`` (providers, role files) — its exception
  degrades to one ``unknown`` line, never a traceback;
- config keys: ``config_keys.dead_keys`` names the keys no code reads.

Every line is ``<ok|WARN>  <check>: <fact>``; the rc-2 line (no
``.agentic/``) is marked ``n/a`` — nothing was checked, so neither ``ok`` nor
``WARN`` would be honest. Values of config keys and tokens are never printed —
only key names (AC17).

Exit codes: 0 — no WARN; 1 — WARNs; 2 — the project is not assessed (no
``.agentic/``), one line, no traceback. The doctor never refuses a launch
(INV7): ``warning`` is data for the operator, not a gate.

Deviation from R5 (AUD-0293-2, recorded in the unit's DONE and spec-change
2.2): a version string does not identify code — two checkouts can both say
2.1.0. When the pinned checkout is a git repo, the runner line carries its
commit and the process line carries the tree commit; equal versions with
different commits are a WARN.

Install metadata is read from the distribution that owns the imported module
(its nearest ``*.dist-info`` / ``*.egg-info``): the answer describes the code
that is running, not whichever install ``sys.path`` names first (AUD-0296-2).
The plugin is compared with the process that loads it, never with the pinned
runner (AUD-0296-3). A malformed ``config.yaml`` is a WARN, not an empty
healthy config (AUD-0296-4).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import config_keys, paths, versions, xdg

OK = "ok"
WARN = "WARN"
NA = "n/a"


@dataclass(frozen=True)
class DoctorReport:
    """One doctor run: the exit code, the printable lines, the WARN lines."""

    rc: int
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _Lines:
    """Collects lines and remembers which of them are WARNs."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.warnings: list[str] = []

    def ok(self, check: str, fact: str) -> None:
        self.lines.append(f"{OK}  {check}: {fact}")

    def warn(self, check: str, fact: str) -> None:
        line = f"{WARN}  {check}: {fact}"
        self.lines.append(line)
        self.warnings.append(line)


def _metadata_line(out: _Lines, check: str, dist: str, module: str) -> None:
    """One metadata row: stale metadata says "reinstall", an unknown module not.

    Both metadata checks (``awf`` and the plugin) share one rule; a module that
    is ``unknown`` has nothing to compare against, so its metadata stays ok.
    """
    meta = versions.install_metadata(dist)
    if module == versions.UNKNOWN:
        out.ok(check, f"{meta} (module not importable)")
    elif meta not in (versions.UNKNOWN, module):
        out.warn(check, f"{meta} != module {module} — stale, reinstall the package")
    else:
        out.ok(check, meta)


def _check_versions(
    project_dir: Path,
    config: dict[str, Any],
    config_problem: str,
    out: _Lines,
) -> None:
    """Version rows: awf module, pinned runner, plugin module, metadata."""
    process = versions.process_version()
    process_commit = versions.process_commit()
    process_fact = f"{process}, commit {process_commit}" if process_commit else process
    out.ok("awf", process_fact)

    _metadata_line(out, "awf metadata", "awf", process)

    if config_problem:
        out.warn("runner", f"{versions.UNKNOWN} ({config_problem})")
        _check_plugin(process, out)
        return

    pinned = versions.pinned_version(project_dir, config)
    if pinned == "":
        problem = versions.runner_setting_problem(config)
        if problem:
            out.warn("runner", f"{problem}, process {process}")
        else:
            out.ok("runner", f"not pinned, process {process}")
    else:
        runner_dir = versions.runner_path(project_dir, config)
        commit = versions.repo_commit(runner_dir) if runner_dir is not None else ""
        fact = f"pinned {pinned} at {runner_dir}"
        if commit:
            fact += f", commit {commit}"
        if pinned == versions.UNKNOWN:
            problem = (
                versions.runner_version_problem(project_dir, config)
                or "unreadable checkout"
            )
            out.warn("runner", f"{versions.UNKNOWN} at {runner_dir} ({problem})")
        elif versions.runner_drift(process, pinned):
            out.warn("runner", f"{fact}, process {process}")
        elif not commit:
            # AUD-0296d-2: a checkout outside git has no commit to compare —
            # say so, the way an unimportable module's metadata does. A repo
            # without commits is not that case: its status is readable, and
            # the engine imports whatever sits in the working tree (d-1).
            dirty = versions.repo_dirty_count(runner_dir)
            if dirty:
                out.warn(
                    "runner",
                    f"{fact} (no commit evidence), tree dirty ({dirty} "
                    "changed file(s)) — the engine imports this tree",
                )
            else:
                out.ok("runner", f"{fact} (no commit evidence)")
        elif process_commit and commit != process_commit:
            out.warn(
                "runner",
                f"{fact}, process commit {process_commit} — same version, "
                "different code",
            )
        else:
            # AUD-0296d-1: the same HEAD can still carry uncommitted edits,
            # and the engine imports that working tree.
            dirty = versions.repo_dirty_count(runner_dir)
            if dirty:
                out.warn(
                    "runner",
                    f"{fact}, tree dirty ({dirty} changed file(s)) — "
                    "the engine imports this tree",
                )
            else:
                out.ok("runner", fact)

    _check_plugin(process, out)


def _check_plugin(process: str, out: _Lines) -> None:
    """Plugin row: the module against the *process* that loads it.

    Comparing with the pinned runner was a false WARN with unfulfillable
    advice (AUD-0296-3): a restart changes neither version. The runner's own
    drift is the ``runner`` row above. Equal versions with a drift pair mean
    the plugin's ``awf>=`` floor is unmet: the row names the floor and says
    "upgrade awf", never the false "module X != process X".
    """
    plugin = versions.plugin_version()
    required = versions.plugin_requirement()
    kind = versions.plugin_drift_kind(process, plugin, required)
    if plugin == versions.UNKNOWN:
        out.ok("plugin", f"{versions.UNKNOWN} (module not importable)")
    elif kind == versions.DRIFT_FLOOR:
        out.warn(
            "plugin",
            f"module {plugin} requires awf>={required}, process {process} "
            f"— {versions.ADVICE_UPGRADE}",
        )
    elif kind == versions.DRIFT_VERSION:
        out.warn(
            "plugin",
            f"module {plugin} != process {process} — {versions.ADVICE_RESTART}",
        )
    else:
        out.ok("plugin", plugin)

    _metadata_line(out, "agent-workflow-ui metadata", "agent-workflow-ui", plugin)


def _opencode_json_problem() -> str:
    """Why ``opencode.json`` cannot be trusted, or ``""`` when it reads fine."""
    path = xdg.opencode_config_file()
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return f"opencode.json unreadable ({type(e).__name__})"
    if not isinstance(data, dict):
        return f"opencode.json root is {type(data).__name__}, expected an object"
    return ""


def _check_models(project_dir: Path, config_problem: str, out: _Lines) -> None:
    """Models row: ``check_model_config`` plus the opencode.json readability."""
    if config_problem:
        out.warn(
            "models",
            f"{versions.UNKNOWN} ({config_problem}) — nothing to check",
        )
        return
    problem = _opencode_json_problem()
    try:
        from .api.model_check import check_model_config

        result = check_model_config(project_dir)
        if not isinstance(result, dict):
            raise TypeError(
                f"check_model_config returned {type(result).__name__}"
            )
        roles = len(result.get("models") or [])
        config_warnings = result.get("warnings") or []
        providers = result.get("providers_available") or []
    except Exception as e:  # noqa: BLE001 — R5: a broken surface is "unknown"
        out.warn("models", f"{versions.UNKNOWN} ({type(e).__name__})")
        return
    if problem or config_warnings:
        facts = []
        if problem:
            facts.append(problem)
        if config_warnings:
            facts.append(
                f"{len(config_warnings)} warning(s) — run awf_check_model_config"
            )
        out.warn("models", f"{roles} role(s) checked; " + "; ".join(facts))
        return
    out.ok("models", f"{roles} role(s) checked, {len(providers)} provider(s)")


def _check_config_keys(config: dict[str, Any], out: _Lines) -> None:
    """Config keys row: every key of the live config has a reader."""
    dead = config_keys.dead_keys(config)
    if dead:
        out.warn(
            "config keys",
            f"{', '.join(dead)} — read by no code (the setting does nothing; "
            "drop the line or restore its reader)",
        )
    else:
        out.ok("config keys", "every key has a reader")


def _load_config(root: Path) -> tuple[dict[str, Any], str]:
    """The live config plus one problem line (``""`` when it reads fine).

    ``config.load`` degrades a malformed file to ``{}`` and writes the error
    to stderr; the doctor reuses neither effect (AUD-0296-4): a silent ``{}``
    made a broken project look healthy, and every later reader printed the
    same ``ERROR`` line again. One read, one explicit reason.
    """
    path = paths.config_file(root)
    if not path.exists():
        return {}, ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return {}, f"config.yaml unreadable ({type(e).__name__})"
    if data is None:
        return {}, ""
    if not isinstance(data, dict):
        return {}, f"config.yaml root is {type(data).__name__}, expected a mapping"
    return data, ""


def run_doctor(project_dir: str | Path = ".") -> DoctorReport:
    """Assemble the report; read-only.

    The only process spawns are the ones the reused surfaces already make:
    two ``git`` reads bounded by ``GIT_TIMEOUT`` (30s each, not 5s — every
    git surface goes through ``git_utils``), and ``opencode models`` inside
    ``check_model_config`` (10s, degrading to an empty set). The worst case
    (both git reads timing out) can exceed the ~55s MCP transport window; the
    tool runs it in a worker thread, but a timeout there is a real residual
    risk, not a claim of speed. Nothing is written and no stage is started
    (INV7).
    """
    root = Path(project_dir)
    if not paths.agentic_dir(root).is_dir():
        line = f"{NA}  project: no .agentic/ at {root} — nothing to assess"
        return DoctorReport(rc=2, lines=[line], warnings=[])
    config, config_problem = _load_config(root)
    out = _Lines()
    _check_versions(root, config, config_problem, out)
    _check_models(root, config_problem, out)
    if config_problem:
        out.warn("config keys", f"{config_problem} — nothing to check")
    else:
        _check_config_keys(config, out)
    return DoctorReport(
        rc=1 if out.warnings else 0,
        lines=out.lines,
        warnings=out.warnings,
    )


__all__ = ["DoctorReport", "run_doctor"]
