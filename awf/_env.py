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
# tool list (agent-scoped deny rules, see :func:`awf_subprocess_env`)
# so they cannot accidentally approve/kill/rollback/dispatch. This reduces
# accidental errors — it is NOT a security boundary (the worker keeps
# ``shell``). Strict isolation is a separate effort (ORCH plan).
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

    Only the 2.x form is read: ``mcp.servers``. A 1.x ``mcp.<name>`` block
    is not normalized (R5, D3) — such a file is a manual conversion (R11).
    """
    servers: dict = _mcp_servers_of(user_config)
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
        servers.update(_mcp_servers_of(data))
    return servers


def _mcp_servers_of(document: dict) -> dict:
    """``mcp.servers`` of a 2.x opencode config (empty when absent)."""
    mcp = document.get("mcp")
    if not isinstance(mcp, dict):
        return {}
    servers = mcp.get("servers")
    return dict(servers) if isinstance(servers, dict) else {}



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
# ``tools: {allow/deny}`` profile may name. In opencode 2.x the builtin
# actions are ``shell``/``edit``/``webfetch``/``subagent``; the 1.x names
# (``bash``, ``write``, ``task``) stay accepted in user pipelines and are
# mapped onto the 2.x action when the rule is assembled (architecture D2).
# MCP tools are named ``<server>_<tool>``. Single source of truth — the
# loader (awf/pipeline.py) validates against it, so an unknown key is a
# pipeline load error, not a silent no-op.
BUILTIN_TOOLS: tuple[str, ...] = ("shell", "edit", "webfetch", "subagent")

#: 1.x action name → 2.x action name (architecture D2, R5).
TOOL_ACTION_ALIASES: dict[str, str] = {
    "bash": "shell",
    "write": "edit",
    "edit": "edit",
    "task": "subagent",
}

#: Every rule covers all resources unless it says otherwise.
DEFAULT_RESOURCE = "*"

_MCP_KEY_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]*_[A-Za-z0-9][A-Za-z0-9_-]*\Z"
)


def tool_action(key: str) -> str:
    """The 2.x action name for a permission key (``bash`` → ``shell``)."""
    return TOOL_ACTION_ALIASES.get(key, key)


def is_known_permission_key(key: str) -> bool:
    """ORCH M7.2: True when *key* is in the tools-profile registry."""
    if tool_action(key) in BUILTIN_TOOLS:
        return True
    return _MCP_KEY_RE.fullmatch(key) is not None


def permission_rule(action: str, effect: str, resource: str = DEFAULT_RESOURCE) -> dict:
    """One 2.x rule: ``{"action", "resource", "effect"}`` (architecture D2)."""
    return {"action": tool_action(action), "resource": resource, "effect": effect}


def host_permission_rules(user_config: dict) -> list[dict]:
    """The 2.x ``permissions`` list of the host config, kept in its order.

    Only the 2.x form is read: a 1.x ``permission`` mapping is not
    normalized into rules (R5, D3) — the file is the owner's to convert
    (R11), and ``awf doctor`` names it. Malformed entries are dropped
    rather than crashing the spawn.
    """
    raw = user_config.get("permissions")
    if not isinstance(raw, list):
        return []
    rules: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        effect = item.get("effect")
        if not isinstance(action, str) or not action:
            continue
        if effect not in ("allow", "deny", "ask"):
            continue
        rules.append(
            permission_rule(action, str(effect), str(item.get("resource", DEFAULT_RESOURCE)))
        )
    return rules


def merge_tool_profile(
    base_permissions: list[dict],
    tools_profile: dict[str, list[str]] | None,
    locked_deny: tuple[str, ...] = (),
) -> list[dict]:
    """ORCH M7.2: merge a stage tools-profile onto the base rules.

    Rules (M7.2 contract, re-expressed for the 2.x list form — the last
    matching rule wins, so the ORDER is the contract):
    - the base rules (host config + awf overrides) come first;
    - the profile's ``allow`` rules follow, then its ``deny`` rules — a key
      listed in both therefore ends up denied;
    - ``locked_deny`` goes LAST: the base control prohibitions (M2.2
      control tools on execute stages, edit for a readonly role) cannot be
      lifted by a profile or by a host rule.

    Returns a new list; the inputs are not mutated. A profile narrows
    the stage's discretionary tools — it is NOT a security boundary.
    """
    merged = [dict(rule) for rule in base_permissions]
    if tools_profile:
        allow: dict[str, str] = {}
        deny: dict[str, str] = {}
        for key in tools_profile.get("allow") or ():
            allow[tool_action(key)] = "allow"
        for key in tools_profile.get("deny") or ():
            deny[tool_action(key)] = "deny"  # deny beats allow
        for action in allow:
            merged.append(permission_rule(action, "allow"))
        for action in deny:
            merged.append(permission_rule(action, "deny"))
    seen: set[str] = set()
    for key in locked_deny:
        action = tool_action(key)
        if action in seen:
            continue
        seen.add(action)
        merged.append(permission_rule(action, "deny"))
    return merged



# TODO-0323 (A22): opencode 2.x uses Bun's fetch, which rejects the socks
# scheme — a socks proxy inherited by the worker is a dead network, not a
# preference. The worker env therefore never keeps a socks variable, and
# ``worker.network.proxy`` is the one explicit way to say what it should use:
# empty = strip socks only (default), ``none`` = strip every proxy variable,
# ``http://host:port`` = hand that proxy to the worker.
PROXY_VARS: tuple[str, ...] = (
    "http_proxy", "https_proxy", "all_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
)


#: A scheme is the only part of the configured proxy URL that may reach the
#: log: anything else can carry a credential, a host or a port.
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*$")


def is_socks_proxy(value: str) -> bool:
    """True when a proxy URL uses a socks scheme (the value is not returned)."""
    return value.strip().lower().startswith("socks")


def proxy_scheme(value: str) -> str:
    """The scheme of a proxy URL, or ``""`` when the value carries none.

    Only a bare scheme prefix is returned: ``http://user:pass@host:port``
    gives ``"http"``, while ``user:pass@host:port`` (no scheme — a plausible
    typo) gives nothing at all, so a credential can never ride into a log
    line or into ``awf doctor`` output through this door.
    """
    if "://" not in value:
        return ""
    head = value.split("://", 1)[0].lower()
    return head if _SCHEME_RE.match(head) else ""


def strip_socks(env: dict[str, str]) -> list[str]:
    """Remove every socks proxy variable from *env*; return their names."""
    stripped = [
        name for name in PROXY_VARS if is_socks_proxy(env.get(name) or "")
    ]
    for name in stripped:
        env.pop(name, None)
    return stripped


def proxy_setting_from_config(config: dict | None) -> str:
    """``worker.network.proxy`` out of an ALREADY loaded config, stripped.

    One reader for both consumers — the spawn path below and ``awf doctor``
    — so the two cannot drift on what the key says. A wrong value type
    degrades to ``""`` (strip socks only).
    """
    from . import config as cfg_mod

    value = cfg_mod.get(
        config if isinstance(config, dict) else {}, "worker.network.proxy"
    )
    return value.strip() if isinstance(value, str) else ""


def worker_proxy_setting(project_dir: str | Path | None) -> str:
    """``worker.network.proxy`` as a stripped string (``""`` when absent).

    Env assembly for a spawn must never raise: a read failure or a wrong
    value type degrades to the empty default (strip socks only).
    """
    if project_dir is None:
        return ""
    try:
        from . import config as cfg_mod

        config = cfg_mod.load(project_dir)
    except Exception:  # noqa: BLE001 — a spawn must not die on config
        return ""
    return proxy_setting_from_config(config)


def apply_worker_proxy(env: dict[str, str], setting: str) -> str:
    """Rewrite proxy variables in *env* per *setting*; return a fact line.

    The fact carries only the scheme and the variable names — never a
    credential, host or port: it goes to the stage log and to ``awf doctor``.
    ``""`` means nothing was touched (the default with no socks to strip).

    A socks value IN THE KEY is as unusable as an inherited one: Bun's fetch
    rejects the scheme, so the value is dropped instead of being handed to
    the worker and ``awf doctor`` names it as a WARN. Handing it over would
    break the very invariant the key exists for.
    """
    setting = setting.strip()
    if is_socks_proxy(setting):
        stripped = strip_socks(env)
        fact = "strip socks"
        if stripped:
            fact += f" ({', '.join(stripped)})"
        return f"{fact}, worker.network.proxy socks value ignored"

    present = [name for name in PROXY_VARS if (env.get(name) or "").strip()]
    if setting.lower() == "none":
        for name in PROXY_VARS:
            env.pop(name, None)
        return "none (all proxy variables removed)" if present else "none"
    if setting:
        socks = strip_socks(env)
        for name in PROXY_VARS:
            env.pop(name, None)
        env["http_proxy"] = setting
        env["https_proxy"] = setting
        scheme = proxy_scheme(setting)
        fact = (
            f"{scheme} (http_proxy, https_proxy)" if scheme
            # A value without a scheme is handed over as written (the user
            # meant something), but it is never echoed: `host:port` and
            # `user:pass@host:port` both hide a credential behind them.
            else "value without a scheme (http_proxy, https_proxy)"
        )
        if socks:
            fact += f", socks stripped ({', '.join(socks)})"
        return fact
    stripped = strip_socks(env)
    return f"strip socks ({', '.join(stripped)})" if stripped else ""


def awf_subprocess_env(
    *,
    role: str = "",
    project_dir: str | Path | None = None,
    agent_name: str = "",
    restrict_control_tools: bool = False,
    tools_profile: dict[str, list[str]] | None = None,
    isolate_mcp: bool = False,
    proxy_facts: list[str] | None = None,
    stage_anchor: str | None = None,
) -> dict[str, str]:
    """BD-22/KAUD-5: env for opencode subprocess spawned by awf.

    Sets ``OPENCODE_CONFIG_CONTENT`` to a 2.x config document that
    overrides permission rules so the subprocess can run ``edit``/
    ``shell``/``webfetch`` without prompting (R5).

    Shape (architecture K5): top-level ``permissions`` — an ORDERED list
    of ``{action, resource, effect}`` rules — ``agents`` (per agent), and
    ``mcp.servers``. The 1.x keys ``permission``, ``agent`` and
    ``mcp.<name>`` are never written: the last matching rule wins, so the
    stage's rules and ``locked_deny`` go last and beat the host's rules.

    ORCH M2.2 (V-03, 27.09): with ``restrict_control_tools=True`` and a
    non-empty ``agent_name``, the document also carries an agent-scoped
    rule list that DENIES the control MCP tools (see
    :data:`CONTROL_TOOLS`) for that agent only — execute stages pass both
    so the worker's model does not see approve/kill/rollback/dispatch in
    its tool list. Plan/verify stages pass nothing and keep the full set.

    W7 + V-04 (readonly roles): when ``role`` is listed in the project's
    ``automation.readonly_roles`` (``.agentic/config.yaml``, default
    empty), ``edit`` is FORCED to ``deny`` on top of any user permission
    settings — a host config that allows writing cannot re-enable it for a
    listed role. ``shell`` stays ``allow`` (deliberate: QA runs tests);
    ``webfetch`` and everything else stay as before.

    ORCH M7.2 (29.09): ``tools_profile`` (a stage's ``tools:
    {allow/deny}`` mapping, permission keys) merges on top of those base
    rules — deny beats allow; the base control prohibitions (M2.2
    control tools on execute stages, readonly edit) are locked and a
    profile cannot lift them. Without a profile the payload is unchanged.
    The profile narrows the stage's discretionary tools — it is NOT a
    security boundary (``shell`` stays allowed unless the profile denies
    it). 1.x profile keys (``bash``, ``write``) are mapped onto the 2.x
    actions.

    KAUD-5: the host's own 2.x ``permissions`` rules are kept, with our
    overrides after them, and the host's other settings (theme, providers,
    agents, …) are left to opencode itself — env carries permissions only.

    BD-25: strip ``OPENCODE_SERVER_*`` env vars so the subprocess does NOT
    attach to a running ``opencode serve`` instance.

    A22: the worker never inherits a socks proxy (Bun's fetch rejects the
    scheme); ``worker.network.proxy`` is the one explicit way to say what it
    gets. ``proxy_facts`` is an optional collector: the fact describing the
    decision THIS call made is appended to it, so a caller (the stage log)
    reports the env the worker actually receives instead of recomputing it.

    A44 (TODO-0359): ``stage_anchor`` (an opaque per-stage token) is written
    as ``AWF_STAGE_ANCHOR`` so the stage-exit reap can recognize the whole
    tree, including children that ``setsid`` out of the worker's group. No
    anchor → the env is unchanged.
    """
    env = os.environ.copy()

    # KAUD-5: Start from user's config, then merge our overrides
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

    # W7: a readonly role (automation.readonly_roles) does not get the
    # edit allow-override.
    # V-04 (27.09): "not granted" was not a denial — the user's own
    # edit allow in opencode.json survived the merge. Now a listed role
    # gets edit FORCED to deny on top of any host config; shell stays
    # allowed (deliberate: QA runs tests).
    readonly = _is_readonly_role(role, project_dir)
    overrides = [("shell", "allow"), ("webfetch", "allow")]
    overrides.append(("edit", "deny" if readonly else "allow"))
    # ORCH M7.2: base control prohibitions a stage tools-profile cannot
    # lift — M2.2 control tools (execute stages) + readonly edit.
    locked_deny: tuple[str, ...] = ()
    if restrict_control_tools:
        locked_deny = tuple(control_tool_permission_keys())
    if readonly:
        locked_deny = locked_deny + ("edit",)
    base_rules = host_permission_rules(user_config)
    base_rules.extend(
        permission_rule(action, effect) for action, effect in overrides
    )
    final_permissions = merge_tool_profile(
        base_rules, tools_profile, locked_deny
    )

    # ORCH M2.2: execute stages hide the control MCP tools from the
    # stage's agent only — an agent-scoped rule list, merged by opencode
    # with the user's own agent definition.
    agent_block: dict | None = None
    if restrict_control_tools and agent_name:
        agent_block = {
            agent_name: {
                "permissions": [
                    permission_rule(key, "deny")
                    for key in control_tool_permission_keys()
                ]
            }
        }

    # P1 security: only serialize permission overrides to env — never API keys,
    # providers, or other sensitive fields from opencode.json. Workers only
    # need the permission overrides; everything else is loaded by opencode
    # itself from the real config file.
    payload: dict = {"permissions": final_permissions}
    if agent_block is not None:
        payload["agents"] = agent_block

    # TODO-0242 (P0-3): an execute worker gets the configured MCP servers
    # only — the rest are switched off by name in the merged config. The
    # user's own ``mcp.servers`` is read, never rewritten: a server outside
    # the allow-list gets ``disabled: true``; an allow-list name the config
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
            name: {"disabled": True}
            for name in configured_mcp
            if name not in allowed
        }
        if disabled:
            payload["mcp"] = {"servers": disabled}
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

    # TODO-0323 (A22): the worker never inherits a socks proxy — Bun's fetch
    # rejects the scheme, so the stage dies on its first network call. The
    # fact is reported from THIS call (``proxy_facts``): a second pass over
    # os.environ could legitimately disagree with the env the worker gets.
    fact = apply_worker_proxy(env, worker_proxy_setting(project_dir))
    if proxy_facts is not None and fact:
        proxy_facts.append(fact)
    # A44 (TODO-0359): a unique marker the worker's whole tree inherits
    # through exec — including children that leave its process group with
    # ``setsid`` (``opencode serve --stdio`` and the MCP servers). The
    # stage-exit reap matches on it, so those processes stay within reach
    # after the worker itself is gone. The engine never carries the marker
    # (it is set on this child's env only), so nothing outside the tree is
    # ever matched. The name comes from ``worker_tree`` — the module that
    # reads and drops it — so a rename cannot leave the writer behind.
    if stage_anchor:
        from .worker_tree import ANCHOR_ENV

        env[ANCHOR_ENV] = stage_anchor
    return env
