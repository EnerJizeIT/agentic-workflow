"""BD-22/25: subprocess environment setup for opencode run.

Centralized so signal_watch + supervisor + agent_stage can use it without
circular imports through orchestrator.
"""
from __future__ import annotations

import json
import os
import re
import sys
import warnings
from pathlib import Path

# AUD14-07: load libc in the PARENT at import time. CDLL inside preexec_fn
# does dlopen after fork in a multi-threaded process (orchestrator runs a
# dashboard thread) — unsafe. preexec itself must only call prctl.
# TODO-0242 (A.5): the handle comes from the one libc helper (CDLL(None) —
# works on musl too); the cache is primed here, in the parent.
from . import _libc as _libc_mod

# Kept as the module-level handle AUD14-07 asserts (the helper is the one
# loader; this alias is the loaded PARENT-side handle, not a second CDLL).
_libc = _libc_mod.get_libc() if sys.platform == "linux" else None


def _pdeathsig_preexec() -> None:
    """DF6-8: Set PR_SET_PDEATHSIG so child dies when parent (orchestrator) dies.

    Linux-only. On other platforms, no-op (best effort): calling into a
    libc symbol that does not exist there after ``fork`` is not a fallback.
    Prevents orphan worker subprocesses from continuing after orchestrator crash.
    """
    if sys.platform != "linux":
        return
    libc = _libc_mod.get_libc()
    if libc is None:
        return
    try:
        import signal as _signal

        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, _signal.SIGTERM)
    except Exception:
        pass  # best effort — don't crash if libc/prctl unavailable


def _is_readonly_role(role: str, project_dir: str | Path | None) -> bool:
    """W7: True when ``role`` is in the project's ``automation.readonly_roles``.

    The list lives in ``.agentic/config.yaml`` and is EMPTY by default —
    behavior without it is exactly the previous one. Any read error
    (no .agentic/, malformed YAML, wrong value type) degrades to False:
    env assembly for a spawn must never raise.
    """
    if not role or project_dir is None:
        return False
    try:
        from . import config as cfg_mod

        config = cfg_mod.load(project_dir)
        roles = cfg_mod.get(config, "automation.readonly_roles", [])
    except Exception:
        return False
    if not isinstance(roles, list):
        return False
    return role in {str(r) for r in roles}


# ORCH M2.2 (V-03, audit 2026-09-27): MCP tools that control the run's
# verdicts and state. Execute-stage workers get them hidden from their
# tool list (agent-scoped permission deny, see :func:`awf_subprocess_env`)
# so they cannot accidentally approve/kill/rollback/dispatch. This reduces
# accidental errors — it is NOT a security boundary (the worker keeps
# ``bash``). Strict isolation is a separate effort (ORCH plan).
MCP_CONTROL_SERVER = "agent-workflow-ui"

CONTROL_TOOLS: tuple[str, ...] = (
    # work verdicts
    "awf_approve",
    "awf_reject",
    # pipeline lifecycle
    "awf_start",
    "awf_continue",
    "awf_kill",
    "awf_retry_stage",
    # destructive / repair state
    "awf_reset",
    "awf_rollback",
    "awf_restore",
    "awf_unblock",
    # project / unit lifecycle
    "awf_init",
    "awf_baseline",
    "awf_dispatch_todo",
    "awf_todo_remove",
    "awf_todo_retire",
    "awf_todo_update",
    "awf_review_append",
    # run state
    "awf_run_start",
    "awf_run_next",
    "awf_run_finish",
    "awf_run_note",
    # supervisor phase state
    "awf_set_goal",
    "awf_confirm_normalized",
)


def control_tool_permission_keys() -> list[str]:
    """Full MCP tool names (``<server>_<tool>``) denied for execute stages."""
    return [f"{MCP_CONTROL_SERVER}_{tool}" for tool in CONTROL_TOOLS]


# TODO-0242 (P0-3): an execute worker only needs the control surface that
# the engine itself drives (``agent-workflow-ui``); every other MCP server
# is dead weight in its context (token cost) and an accidental side channel.
# The list is configurable (``worker.mcp_servers``); the default keeps the
# control server, which the supervisor-side tools depend on.
DEFAULT_WORKER_MCP_SERVERS: tuple[str, ...] = (MCP_CONTROL_SERVER,)


def _load_jsonc(path: Path) -> dict | None:
    """Read an opencode config file, tolerating ``//`` and ``/* */`` comments.

    opencode accepts ``opencode.jsonc`` next to ``opencode.json``; a plain
    ``json.loads`` would reject a commented file that opencode itself
    reads. Comment stripping is string-aware so a ``//`` inside a value is
    left alone. ``None`` = unreadable (the caller warns).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    out: list[str] = []
    i, n, in_str, esc = 0, len(text), False, False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    try:
        data = json.loads("".join(out))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def configured_mcp_servers(
    user_config: dict, project_dir: str | Path | None
) -> dict:
    """The MCP servers an opencode run in ``project_dir`` would see.

    TODO-0242 (G3): the worker starts with ``cwd=project_dir``, so a
    server declared in the PROJECT config (``opencode.json``/``.jsonc``,
    ``.opencode/opencode.json(c)``) is live even when the global config
    does not name it. Isolation that reads only the global file silently
    leaves those servers enabled — the promise is "every other configured
    server is switched off".

    Project entries override global ones per server name (opencode's own
    precedence). An unreadable/invalid project config is a warning, never
    a silent skip and never a crash: the spawn must go on.
    """
    servers: dict = {}
    global_mcp = user_config.get("mcp")
    if isinstance(global_mcp, dict):
        servers.update(global_mcp)
    if project_dir is None:
        return servers
    base = Path(project_dir)
    candidates = (
        base / "opencode.json",
        base / "opencode.jsonc",
        base / ".opencode" / "opencode.json",
        base / ".opencode" / "opencode.jsonc",
    )
    for path in candidates:
        if not path.is_file():
            continue
        data = _load_jsonc(path)
        if data is None:
            warnings.warn(
                f"the project opencode config {path} is unreadable or invalid "
                "— MCP isolation cannot see the servers it declares",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        project_mcp = data.get("mcp")
        if isinstance(project_mcp, dict):
            servers.update(project_mcp)
    return servers


def worker_mcp_servers(project_dir: str | Path | None) -> list[str]:
    """``worker.mcp_servers`` from config, or the default control server.

    A read failure or a wrong value type degrades to the default: env
    assembly for a spawn must never raise. An EXPLICIT empty list is a
    value, not an absence: ``worker.mcp_servers: []`` means "no MCP server
    for the worker" (the only way to say so), so it must not fall back to
    the default — the option would otherwise be unable to express "none".
    """
    if project_dir is None:
        return list(DEFAULT_WORKER_MCP_SERVERS)
    try:
        from . import config as cfg_mod

        configured = cfg_mod.get(
            cfg_mod.load(project_dir), "worker.mcp_servers"
        )
    except Exception:  # noqa: BLE001
        return list(DEFAULT_WORKER_MCP_SERVERS)
    if not isinstance(configured, list):
        return list(DEFAULT_WORKER_MCP_SERVERS)
    return [str(n).strip() for n in configured if str(n).strip()]


# ORCH M7.2 (29.09): tools-profile registry — the permission keys a stage
# ``tools: {allow/deny}`` profile may name. The builtin tools opencode
# gates by name, plus MCP tools as ``<server>_<tool>`` (the same
# mechanism M2.2 uses: permission keys glob-match tool names). Single
# source of truth — the loader (awf/pipeline.py) validates against it,
# so an unknown key is a pipeline load error, not a silent no-op.
BUILTIN_TOOLS: tuple[str, ...] = ("bash", "edit", "write", "webfetch")

_MCP_KEY_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]*_[A-Za-z0-9][A-Za-z0-9_-]*\Z"
)


def is_known_permission_key(key: str) -> bool:
    """ORCH M7.2: True when *key* is in the tools-profile registry."""
    if key in BUILTIN_TOOLS:
        return True
    return _MCP_KEY_RE.fullmatch(key) is not None


def merge_tool_profile(
    base_permissions: dict[str, str],
    tools_profile: dict[str, list[str]] | None,
    locked_deny: tuple[str, ...] = (),
) -> dict[str, str]:
    """ORCH M7.2: merge a stage tools-profile onto base permissions.

    Rules (M7.2 contract):
    - the profile adds ``allow``/``deny`` entries on top of the base
      (host config + awf overrides + readonly_roles);
    - deny beats allow — a key listed in both ends up denied;
    - ``locked_deny`` stays denied no matter what the profile says —
      the base control prohibitions (M2.2 control tools on execute
      stages, edit/write for a readonly role) cannot be lifted by a
      profile.

    Returns a new dict; the inputs are not mutated. A profile narrows
    the stage's discretionary tools — it is NOT a security boundary.
    """
    merged = dict(base_permissions)
    if tools_profile:
        profile: dict[str, str] = {}
        for key in tools_profile.get("allow") or ():
            profile[key] = "allow"
        for key in tools_profile.get("deny") or ():
            profile[key] = "deny"  # deny beats allow
        merged.update(profile)
    for key in locked_deny:
        merged[key] = "deny"
    return merged


def awf_subprocess_env(
    *,
    role: str = "",
    project_dir: str | Path | None = None,
    agent_name: str = "",
    restrict_control_tools: bool = False,
    tools_profile: dict[str, list[str]] | None = None,
    isolate_mcp: bool = False,
) -> dict[str, str]:
    """BD-22/KAUD-5: env for opencode subprocess spawned by awf.

    Sets ``OPENCODE_CONFIG_CONTENT`` to override permission rules so
    the subprocess can run ``edit``/``bash``/``write`` without prompting.

    ORCH M2.2 (V-03, 27.09): with ``restrict_control_tools=True`` and a
    non-empty ``agent_name``, the config content also carries an
    agent-scoped permission block that DENIES the control MCP tools
    (see :data:`CONTROL_TOOLS`) for that agent only — execute stages
    pass both so the worker's model does not see approve/kill/rollback/
    dispatch in its tool list. Plan/verify stages pass nothing and keep
    the full set. The deny keys are full MCP tool names
    (``agent-workflow-ui_awf_*``) — opencode matches permission keys as
    glob patterns against tool names, so the same mechanism that gates
    ``bash`` gates MCP tools. Verified live on opencode 1.18.32: the
    deny rule lands after the ``*`` allow in the resolved config and the
    denied tools disappear from the model's tool list.

    W7 + V-04 (readonly roles): when ``role`` is listed in the project's
    ``automation.readonly_roles`` (``.agentic/config.yaml``, default
    empty), ``edit``/``write`` are FORCED to ``deny`` on top of any user
    permission settings — a host config that allows writing cannot
    re-enable them for a listed role. ``bash`` stays ``allow``
    (deliberate: QA runs tests); ``webfetch`` and everything else stay
    as before.

    ORCH M7.2 (29.09): ``tools_profile`` (a stage's ``tools:
    {allow/deny}`` mapping, permission keys) merges on top of those base
    rules — deny beats allow; the base control prohibitions (M2.2
    control tools on execute stages, readonly edit/write) are locked and
    a profile cannot lift them. Without a profile the payload is
    unchanged. The profile narrows the stage's discretionary tools —
    it is NOT a security boundary (``bash`` stays allowed unless the
    profile denies it).

    KAUD-5: MERGES with user's existing opencode.json instead of replacing.
    Reads user's config, adds our permission overrides on top, preserves
    user's other settings (theme, providers, agents, etc.).

    BD-25: strip ``OPENCODE_SERVER_*`` env vars so the subprocess does NOT
    attach to a running ``opencode serve`` instance.
    """
    env = os.environ.copy()

    # KAUD-5: Start from user's existing config, then merge our overrides
    user_config: dict = {}
    # The permission merge tolerates a broken host config (start from
    # empty). MCP isolation cannot: "every other configured server is
    # switched off" is only true when the servers were SEEN. Keep the
    # reason and let the isolation branch warn (TODO-0242 G3 discipline:
    # a silent skip next to a warning branch is the drift this unit fixes).
    user_config_error = ""
    try:
        from .xdg import opencode_config_file
        config_path = opencode_config_file()
        if config_path.is_file():
            user_config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(user_config, dict):
                user_config = {}
                user_config_error = "not a JSON object"
    except (OSError, json.JSONDecodeError) as exc:
        user_config_error = str(exc)  # start from empty; isolation warns below

    # Merge: user config + our permission overrides (ours take priority)
    merged = user_config.copy()
    merged_permissions = merged.get("permission", {})
    if not isinstance(merged_permissions, dict):
        merged_permissions = {}
    # W7: a readonly role (automation.readonly_roles) does not get the
    # edit/write allow-overrides.
    # V-04 (27.09): "not granted" was not a denial — the user's own
    # edit/write=allow in opencode.json survived the merge. Now a listed
    # role gets edit/write FORCED to deny on top of any host config;
    # bash stays allowed (deliberate: QA runs tests).
    readonly = _is_readonly_role(role, project_dir)
    overrides = {
        "bash": "allow",
        "webfetch": "allow",
    }
    if readonly:
        overrides["edit"] = "deny"
        overrides["write"] = "deny"
    else:
        overrides["edit"] = "allow"
        overrides["write"] = "allow"
    # ORCH M7.2: base control prohibitions a stage tools-profile cannot
    # lift — M2.2 control tools (execute stages) + readonly edit/write.
    locked_deny: tuple[str, ...] = ()
    if restrict_control_tools:
        locked_deny = tuple(control_tool_permission_keys())
    if readonly:
        locked_deny = locked_deny + ("edit", "write")
    base_permissions = dict(merged_permissions)
    base_permissions.update(overrides)
    final_permissions = merge_tool_profile(
        base_permissions, tools_profile, locked_deny
    )
    merged["permission"] = final_permissions

    # ORCH M2.2: execute stages hide the control MCP tools from the
    # stage's agent only — an agent-scoped deny block, merged by opencode
    # with the user's own agent definition (probed on 1.18.32).
    agent_block: dict | None = None
    if restrict_control_tools and agent_name:
        agent_block = {
            agent_name: {
                "permission": {key: "deny" for key in control_tool_permission_keys()}
            }
        }

    # P1 security: only serialize permission overrides to env — never API keys,
    # providers, or other sensitive fields from opencode.json. Workers only
    # need the permission overrides; everything else is loaded by opencode
    # itself from the real config file.
    payload: dict = {"permission": final_permissions}
    if agent_block is not None:
        payload["agent"] = agent_block

    # TODO-0242 (P0-3): an execute worker gets the configured MCP servers
    # only — the rest are switched off by name in the merged config. The
    # user's own ``mcp`` block is read, never rewritten: a server outside
    # the allow-list gets ``enabled: false``; an allow-list name the config
    # does not define is a warning (typo) and never a failure.
    if isolate_mcp:
        if user_config_error:
            warnings.warn(
                "the opencode config could not be read "
                f"({user_config_error}) — MCP isolation cannot see the "
                "servers it declares",
                RuntimeWarning,
                stacklevel=2,
            )
        allowed = set(worker_mcp_servers(project_dir))
        configured_mcp = configured_mcp_servers(user_config, project_dir)
        disabled = {
            name: {"enabled": False}
            for name in configured_mcp
            if name not in allowed
        }
        if disabled:
            payload["mcp"] = disabled
        for name in sorted(allowed - set(configured_mcp)):
            warnings.warn(
                f"worker.mcp_servers names {name!r}, which the opencode "
                "config does not define — ignored, not fatal",
                RuntimeWarning,
                stacklevel=2,
            )
    config_json = json.dumps(payload)
    env["OPENCODE_CONFIG_CONTENT"] = config_json

    # BD-25: strip server env vars
    strip_keys = (
        "OPENCODE_SERVER",
        "OPENCODE_SERVER_URL",
        "OPENCODE_SERVER_TOKEN",
        "OPENCODE_HOST",
        "OPENCODE_HOST_TOKEN",
    )
    for key in list(env.keys()):
        if key in strip_keys:
            env.pop(key, None)
    return env
