"""R5 (ops reliability): ``awf doctor`` — diagnose before the launch.

The operator's question is "will this project run with the code and config I
think it will?" — asked *before* a stage burns an hour on a stale plugin or a
model provider that silently disappeared. The doctor answers it from surfaces
that already exist:

- versions (``awf.versions``): the running module, the pinned checkout
  (``automation.runner_dir``), the plugin module and the install metadata;
- models: ``check_model_config`` (providers, role files) — its exception
  degrades to one ``unknown`` line, never a traceback;
- config format: the opencode config is read in the 2.x form only; a file
  with 1.x keys is named, never converted (R6, R11);
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
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import command_parse, config_keys, paths, suite_command, versions, xdg
from . import config as cfg_mod
from .supervisor import argv_agent_flag

OK = "ok"
WARN = "WARN"
NA = "n/a"

# A11: opencode 2.x uses Bun's fetch, which rejects the socks scheme. The
# proxy inventory and the scheme test live in ``awf._env`` (the module that
# rewrites the worker env); doctor only reports what that module does. A
# second copy here would be a second answer to "is this a socks proxy".


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


def _different_code(runner_dir: Path, commit: str, process: str) -> bool:
    """Do the two recorded commits name DIFFERENT code? (A40)

    ``--short`` picks its length per repository, so the same commit prints as
    ``b2fa1840`` in one checkout and ``b2fa184`` in another. The rows still
    show the recorded short forms; only the verdict compares one form per
    side, and it is :func:`versions.commits_differ`'s — the same one the
    launch handshake applies, so the two advisory surfaces cannot disagree.
    """
    return versions.commits_differ(
        runner_dir, commit, versions.process_repo(), process
    )


def _check_versions(
    project_dir: Path,
    config: dict[str, Any],
    config_problem: str,
    out: _Lines,
) -> None:
    """Version rows: awf module, pinned runner, plugin module, metadata."""
    process = versions.process_version()
    process_commit = versions.process_commit()
    if process_commit:
        process_fact = f"{process}, commit {process_commit}"
    else:
        # AUD-0296 CTX1: say the same thing the launch handshake says when
        # the running module has no commit to identify its code.
        process_fact = f"{process} — process has no commit evidence"
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
        elif process_commit and _different_code(runner_dir, commit, process_commit):
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


def _load_opencode_root() -> tuple[dict[str, Any] | None, str]:
    """The opencode config root and one problem line (``""`` when it reads).

    Read-only, one read: the format row and the models row share the same
    parse, so a broken file is reported once with the same wording. A
    missing file is ``(None, "")`` — nothing to convert, not a problem.
    """
    path = xdg.opencode_config_file()
    if not path.is_file():
        return None, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, f"opencode.json unreadable ({type(e).__name__})"
    if not isinstance(data, dict):
        return None, f"opencode.json root is {type(data).__name__}, expected an object"
    return data, ""


def _opencode_json_problem() -> str:
    """Why ``opencode.json`` cannot be trusted, or ``""`` when it reads fine."""
    return _load_opencode_root()[1]


def _check_dashboard(
    project_dir: Path, config: dict[str, Any], config_problem: str, out: _Lines
) -> None:
    """A14: the tab opens on its own — say where it points and whether it will.

    One line: the live port (or "not running") and the auto-open switch.
    Information, never a WARN: a closed dashboard is not a defect.
    """
    if config_problem:
        return
    from . import config as cfg_mod
    from .api import dashboard_open

    auto = bool(cfg_mod.get(config, "dashboard.auto_open", True))
    url = dashboard_open._live_url(project_dir)
    where = url or "not running"
    state = "on" if auto else "off"
    out.ok("dashboard", f"{where} · auto-open {state}")


def _check_config_format(out: _Lines) -> None:
    """K8: the opencode config is in the 2.x form, or says how to get there.

    V1 keys are named, never fixed: reading does not write (INV7), the
    conversion is the owner's manual step (R11). A string ``model`` is
    valid 2.x and gives no WARN. A mixed file counts as V1 — one 1.x key
    means the file is not the form awf understands.
    """
    path = xdg.opencode_config_file()
    root, problem = _load_opencode_root()
    if problem:
        out.warn("config", f"{problem} — V2-форму не проверить")
        return
    if root is None:
        out.ok("config", f"no {path.name} at {path} — nothing to convert")
        return
    found = config_keys.v1_keys(root)
    if found:
        out.warn(
            "config",
            f"сконвертируй в V2: {', '.join(found)} — R11, вручную с "
            "бэкапом (V2-ключи: providers, agents, mcp.servers, permissions)",
        )
        return
    out.ok("config", "V2: providers, agents, mcp.servers, permissions")


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


def _check_agent_names(config: dict[str, Any], config_problem: str, out: _Lines) -> None:
    """A13: roles without ``agent_name`` run on the default agent — one ok line."""
    if config_problem:
        return
    models = config.get("models")
    if models is None:
        # CTX3: no ``models`` block at all — every stage runs unnamed, and the
        # missing line read as "nothing to check".
        out.ok("agents", "no roles named in models — every stage runs on the default agent")
        return
    # Same predicate as argv: `models: []` (wrong shape) is silence — the
    # config-shape check owns that report; a role whose value cannot become a
    # flag (`agent_name: 123`) is named here.
    roles = models if isinstance(models, dict) else {}
    # str(): a non-string YAML key must not make the doctor traceback.
    unnamed = sorted(str(role) for role in roles if not argv_agent_flag(config, role))
    if unnamed:
        out.ok("agents", "default agent (no usable agent_name): " + ", ".join(unnamed))


def _check_opencode(config: dict[str, Any], config_problem: str, out: _Lines) -> None:
    """K3/K4: the effective opencode mode and the managed warning.

    Read-only: one ``opencode --version`` probe (5s cap), no launch. The
    doctor never refuses (INV7) — a fail-closed condition is a WARN line.
    """
    if config_problem:
        return
    from . import opencode_version

    probe = opencode_version.probe_version()
    managed = opencode_version.is_managed(config)
    plan = opencode_version.resolve_plan(config, probe, managed)
    if probe.status == "missing":
        out.warn("opencode", "не найден в PATH — запуск невозможен")
    elif probe.status != "ok" or probe.version is None:
        out.warn("opencode", "версия не определена — запуск запрещён (fail-closed)")
    elif probe.version[0] < 2:
        out.warn(
            "opencode",
            f"{probe.text} — awf 3 требует opencode 2.x, запуск будет отклонён",
        )
    else:
        flag = " · --standalone" if plan.add_flag else ""
        out.ok("opencode", f"{probe.text}{flag}")
    if managed:
        out.warn("managed", opencode_version.MANAGED_WARNING)


def _check_proxy(config: dict[str, Any], config_problem: str, out: _Lines) -> None:
    """A22: say what the worker will get, not that socks might break it.

    The worker env strips socks variables by itself (``awf._env``), so the
    row reports the fact: the scheme and the variable names, never a
    credential, host or port. A config that names a socks proxy is still a
    WARN: the worker ignores that value, and the user should know.
    """
    from ._env import (
        apply_worker_proxy,
        is_socks_proxy,
        proxy_setting_from_config,
    )

    setting = "" if config_problem else proxy_setting_from_config(config)
    if is_socks_proxy(setting):
        out.warn(
            "proxy",
            "worker.network.proxy uses a socks scheme — opencode 2.x fetch "
            "does not support it; the worker ignores the value. Set "
            "http://host:port or none",
        )
        return
    fact = apply_worker_proxy(os.environ.copy(), setting)
    if fact:
        out.ok("proxy", fact)


def _check_verification(config: dict[str, Any], out: _Lines) -> None:
    """AUD10: a pytest option repeated with another value picks the last one.

    The duplicate `--timeout` in ``verification.test_cmd`` already cost six
    red runs and was invisible; the doctor names it before the launch.
    """
    test_cmd = cfg_mod.get(config, "verification.test_cmd")
    if not isinstance(test_cmd, str) or not test_cmd.strip():
        return
    conflicts = command_parse.duplicate_option_values(test_cmd)
    if not conflicts:
        out.ok("verification", "no pytest option is repeated with two values")
        return
    facts = "; ".join(
        f"{option} appears with {', '.join(values)}"
        for option, values in conflicts.items()
    )
    out.warn(
        "verification",
        f"{facts} — pytest keeps only one value; keep a single {', '.join(conflicts)}",
    )


def _check_contract_gate(root: Path, out: _Lines) -> None:
    """A28: name the interpreter the contract gate's ``Gate:`` commands use.

    ``scripts/check-contracts.sh`` resolves the project interpreter from
    ``verification.test_cmd`` (``AWF_PYTHON`` first). The operator asked
    "will the gate run the same python I test with?" gets one line, not a
    guess — a wrong interpreter used to paint the gate red on a green tree.
    """
    if not (root / "scripts" / "check-contracts.sh").is_file():
        # No gate in this project: a line about "Gate: commands" would promise
        # behaviour of a script that never runs (`awf init` does not ship one).
        return
    interp, source = _contract_gate_interpreter(root)
    if "/" in interp:
        # Same test the shell resolver applies: `[ -x "$AWF_PY" ]`, and for a
        # relative path that means "relative to where the gate runs" — the
        # project root, not the doctor's cwd (the interpreter is resolved
        # against ``root`` above for exactly that reason).
        # `X_OK` alone is true for a directory: a config naming the venv's
        # `bin` folder passed here while every gate died with "Permission
        # denied". A named interpreter is a file you can run.
        found = os.path.isfile(interp) and os.access(interp, os.X_OK)
    else:
        found = shutil.which(interp) is not None
    fact = f"Gate: commands run through {interp} ({source})"
    if found:
        out.ok("contracts gate", fact)
    else:
        out.warn("contracts gate", f"{fact} — not found, the gate refuses (rc=2)")


def _contract_gate_interpreter(root: Path) -> tuple[str, str]:
    """The interpreter plus where the answer came from, as the shell reads it.

    A relative path is resolved against ``root`` — the gate runs ``Gate:``
    commands from the project root, so ``./bin/py`` means ``<root>/bin/py``
    even when the doctor is invoked from somewhere else.
    """
    env = os.environ.get("AWF_PYTHON")
    if env:
        return _against_root(root, env), "AWF_PYTHON"
    resolved = suite_command.resolve(root)
    parts = resolved.command.split() if resolved is not None else []
    if len(parts) >= 2 and parts[1] == "-m":
        return _against_root(root, parts[0]), "verification.test_cmd"
    return "python3", "PATH"


def _against_root(root: Path, interp: str) -> str:
    """A relative interpreter in the config means "relative to the project"."""
    if "/" in interp and not os.path.isabs(interp):
        return str(root / interp)
    return interp


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
    _check_opencode(config, config_problem, out)
    _check_proxy(config, config_problem, out)
    _check_config_format(out)
    _check_dashboard(root, config, config_problem, out)
    _check_models(root, config_problem, out)
    _check_agent_names(config, config_problem, out)
    if config_problem:
        out.warn("config keys", f"{config_problem} — nothing to check")
    else:
        _check_verification(config, out)
        _check_config_keys(config, out)
    _check_contract_gate(root, out)
    return DoctorReport(
        rc=1 if out.warnings else 0,
        lines=out.lines,
        warnings=out.warnings,
    )


__all__ = ["DoctorReport", "run_doctor"]
