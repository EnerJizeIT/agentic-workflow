"""Propose and apply changes to ``~/.config/opencode/opencode.json``.

The file is read and written in the opencode 2.x form ONLY (R5,
architecture K6): ``agents`` for the agent definitions, ``mcp.servers``
for the MCP block, and a role's rules as an ordered ``permissions`` list.
The 1.x form (``agent``, ``permission``, ``mcp.<name>``) is neither read
nor normalized: a file carrying it is left byte-for-byte alone and the
owner is told to convert it (R11).

State matrix — the same one for both writers (:func:`apply` for the role
agents, :func:`ensure_mcp_block` for the plugin's MCP block):

| File state | Action | Backup | Output |
|---|---|---|---|
| missing | create a 2.x document | no — nothing to lose | ok |
| 2.x | add what is missing | before the write | ok |
| has a V1 key | do not touch the bytes | no | WARN |

T2.8 fix: ``propose()`` was returning stringly-typed prefixes
(``"ERR:"/"NOTHING:"/"PROPOSE:"``) parsed by caller via ``startswith``.
Replaced with :class:`Proposal` dataclass + :class:`ProposalKind` enum.
"""
from __future__ import annotations

import enum
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ._atomic import atomic_write_text
from .config_keys import V1_KEYS, v1_keys


class ProposalKind(enum.Enum):
    """What :func:`propose` concluded about the requested change."""

    NOTHING = "nothing"  # all requested roles already present
    PROPOSE = "propose"  # there are changes to apply
    SKIP_V1 = "v1"  # the file is in the 1.x form — awf does not touch it
    ERR = "err"  # precondition failed (file unreadable, schema wrong)


@dataclass
class Proposal:
    """Typed result of :func:`propose` — replaces stringly-typed prefixes.

    Caller checks ``kind`` instead of ``startswith("PROPOSE:")`` — type-safe,
    exhaustiveness-checked by linter on Enum match.
    """

    kind: ProposalKind
    detail: str = ""  # human-readable summary (PROPOSE: change list, ERR: reason)
    to_add: list[str] = field(default_factory=list)
    to_update: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        """Back-compat: render as PREFIX:detail string for legacy callers."""
        prefix = self.kind.value.upper()
        if self.detail:
            return f"{prefix}:{self.detail}"
        return f"{prefix}:"


# SPEC-2 (N1): a headless worker that hits an "ask" prompt has no one to
# answer — the run dead-ends. Allow the sanctioned temp locations and deny
# the rest with instant feedback instead of a hanging question. In 2.x the
# rules are an ordered list, so the ``*`` deny goes first and the
# sanctioned allows after it: the last match wins.
SANCTIONED_EXTERNAL_PATTERNS: tuple[str, ...] = (
    "/tmp/opencode/**",
    "/tmp/pytest-*",
    "/tmp/pytest-*/**",
)

V1_HINT = (
    "сконвертируй в V2 по R11 (providers, agents, mcp.servers, "
    "permissions списком); awf файл не правит"
)


def _v1_detail(cfg_path: str, found: list[str]) -> str:
    """The WARN text for a file awf must not touch (AC20)."""
    return f"{cfg_path} в форме V1 (ключи: {', '.join(found)}) — {V1_HINT}"


def _external_directory_rules() -> list[dict]:
    """The worker's external-directory rules: ``*`` deny, then the allows.

    The allows are last and therefore win for the sanctioned temp
    locations (K6). The ``external_directory`` action is a rule key in the
    2.x schema; if 2.2.3 does not know it, the rules stay inert in the
    payload and the host policy applies — they cannot break the allows
    that do work.
    """
    rules = [
        {"action": "external_directory", "resource": "*", "effect": "deny"}
    ]
    rules.extend(
        {
            "action": "external_directory",
            "resource": pattern,
            "effect": "allow",
        }
        for pattern in SANCTIONED_EXTERNAL_PATTERNS
    )
    return rules


EXTERNAL_DIRECTORY_ACTION = "external_directory"


def _ensure_worker_permission(agent_def: dict) -> bool:
    """Merge the worker's external-directory rules into an agent def.

    Writes ``agents.<role>.permissions`` (the 2.x ordered list). The group
    is self-consistent — ``*`` deny first, the sanctioned allows last — so
    it is appended as a block, after any foreign rules. An agent that
    already allows ``external_directory`` for ``*`` is left alone (the
    owner's intent is broader than ours). Returns True when the definition
    changed.
    """
    perm = agent_def.get("permissions")
    if not isinstance(perm, list):
        perm = []
        agent_def["permissions"] = perm

    def is_external(rule: object) -> bool:
        return (
            isinstance(rule, dict)
            and rule.get("action") == EXTERNAL_DIRECTORY_ACTION
        )

    for rule in perm:
        if (
            is_external(rule)
            and rule.get("resource") == "*"
            and rule.get("effect") == "allow"
        ):
            return False  # already permissive — nothing to add
    wanted = [rule for rule in perm if not is_external(rule)]
    wanted.extend(_external_directory_rules())
    if wanted == perm:
        return False
    perm[:] = wanted
    return True



def _model_text(value: object) -> str:
    """Model of an agent as text: a string as-is, an object as provider/model."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        provider = str(value.get("providerID") or "")
        model = str(value.get("model") or "")
        return f"{provider}/{model}" if provider else model
    return ""


def _load(cfg_path: str) -> tuple[dict | None, str]:
    """Read the config root; ``(None, reason)`` when it cannot be used."""
    try:
        with open(cfg_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001 — a broken file is a reported state
        return None, f"cannot read {cfg_path}: {e}"
    if not isinstance(data, dict):
        return None, f"{cfg_path} root is not a JSON object"
    return data, ""


def propose(cfg_path: str, roles: list[str], model: str) -> Proposal:
    """Inspect opencode.json and return what would change.

    Returns :class:`Proposal` with ``kind`` indicating outcome:
    - :attr:`ProposalKind.NOTHING` — all roles already present with matching model.
    - :attr:`ProposalKind.PROPOSE` — there are additions or model updates
      (a missing FILE is a propose: "create the 2.x document", K6/D6).
    - :attr:`ProposalKind.SKIP_V1` — the file is in the 1.x form; awf leaves
      it alone and says how to convert it (R11).
    - :attr:`ProposalKind.ERR` — file unreadable or schema wrong.

    ``model`` is an API capability for programmatic callers (the CLI init
    flow passes an empty string — per-role models come from the
    project-setup form). Callers branch on ``proposal.kind``;
    ``Proposal.__str__`` renders the legacy ``"KIND:detail"`` form for
    CLI printing.
    """
    path = Path(cfg_path)
    if not path.is_file():
        lines = [
            "",
            f"  + create {cfg_path} in V2 form (agents, mcp.servers)",
            f"  + add agents: {', '.join(roles)}",
            "  (no backup: the file does not exist yet)",
        ]
        return Proposal(
            kind=ProposalKind.PROPOSE,
            detail="\n".join(lines),
            to_add=list(roles),
        )

    d, problem = _load(cfg_path)
    if d is None:
        return Proposal(kind=ProposalKind.ERR, detail=problem)

    found = v1_keys(d)
    if found:
        return Proposal(kind=ProposalKind.SKIP_V1, detail=_v1_detail(cfg_path, found))

    agents = d.get("agents")
    if agents is None:
        agents = {}  # D6: a missing section is "create", not an error
    if not isinstance(agents, dict):
        return Proposal(kind=ProposalKind.ERR, detail="'agents' is not an object")

    to_add: list[str] = []
    to_update: list[str] = []

    for r in roles:
        cur = agents.get(r)
        if cur is None:
            to_add.append(r)
        elif isinstance(cur, dict):
            details: list[str] = []
            if model and _model_text(cur.get("model")) != model:
                details.append(f"model {cur.get('model')!r} -> {model!r}")
            if r == "worker":
                probe = json.loads(json.dumps(cur))  # deep copy, probe only
                if _ensure_worker_permission(probe):
                    detail_parts = [
                        "+ external_directory allow-patterns "
                        "(/tmp/opencode, /tmp/pytest-*)"
                    ]
                    # AUD07-09: the merge also introduces the ``* -> deny``
                    # default — the user approves exactly what is summarized.
                    if not cur.get("permissions"):
                        detail_parts.append("* -> deny (all other external directories)")
                    details.append("; ".join(detail_parts))
            if details:
                to_update.append(f"{r}: " + "; ".join(details))

    if not to_add and not to_update:
        return Proposal(
            kind=ProposalKind.NOTHING,
            detail=f"all of {', '.join(roles)} already present.",
        )

    lines: list[str] = []
    if to_add:
        lines.append(f"  + add agents: {', '.join(to_add)}")
    if to_update:
        lines.append("  ~ update:")
        for u in to_update:
            lines.append(f"      {u}")
    if model:
        lines.append(f"  model: {model}")
    else:
        lines.append(
            "  model: <blank> — agent entries will have empty model, "
            "edit them manually"
        )
    return Proposal(
        kind=ProposalKind.PROPOSE,
        detail="\n" + "\n".join(lines),
        to_add=to_add,
        to_update=to_update,
    )


def _unique_backup(cfg: Path) -> Path:
    """``<cfg>.bak-<ts>`` that does not collide with an existing backup.

    AUD07-09: second-resolution timestamps made two applies in the same
    second overwrite each other's backup. Microseconds plus a counter
    make the name unique; the backup is the user's only restore point.
    """
    base = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    backup = Path(f"{cfg}.bak-{base}")
    n = 1
    while backup.exists():
        n += 1
        backup = Path(f"{cfg}.bak-{base}-{n}")
    return backup


def _write(cfg: Path, data: dict, *, existed: bool) -> str:
    """Back up (only an existing file) and atomically write *data*.

    Returns the backup file name, or ``""`` when nothing was backed up —
    an absent file has nothing to restore (K6, D5).
    """
    backup_name = ""
    if existed:
        backup = _unique_backup(cfg)
        shutil.copy2(cfg, backup)
        backup_name = backup.name
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if not text.endswith("\n"):
        text += "\n"
    atomic_write_text(cfg, text, encoding="utf-8")
    return backup_name


def _new_document() -> dict:
    """The 2.x skeleton written when no config file exists (K6)."""
    return {"agents": {}, "mcp": {"servers": {}}}


def apply(cfg_path: str, roles: list[str], model: str) -> str:
    """Backup + write opencode.json in the 2.x form. Return a summary string.

    A missing file is created in the 2.x form without a backup; a file with
    any 1.x key is left byte-for-byte alone and answers WARN (R5, AC19,
    AC20).
    """
    cfg = Path(cfg_path)
    existed = cfg.is_file()
    if existed:
        d, problem = _load(cfg_path)
        if d is None:
            return f"  ERR: {problem} — skipping."
        found = v1_keys(d)
        if found:
            return f"  WARN: {_v1_detail(cfg_path, found)}"
    else:
        d = _new_document()

    agents = d.get("agents")
    if agents is None:
        agents = {}
        d["agents"] = agents
    if not isinstance(agents, dict):
        return "  'agents' is not an object — skipping."

    added: list[str] = []
    updated: list[str] = []
    for r in roles:
        cur = agents.get(r)
        if cur is None:
            agent_def: dict = {"description": f"awf {r} agent"}
            if model:
                agent_def["model"] = model
            if r == "worker":
                _ensure_worker_permission(agent_def)
            agents[r] = agent_def
            added.append(r)
        elif isinstance(cur, dict):
            changed = False
            if model and _model_text(cur.get("model")) != model:
                cur["model"] = model
                changed = True
            if r == "worker" and _ensure_worker_permission(cur):
                changed = True
            if changed:
                updated.append(r)

    if added or updated or not existed:
        # AUD07-09: backup only when we actually write — a no-op apply
        # used to leave an orphan .bak-<ts> behind.
        backup_name = _write(cfg, d, existed=existed)
        parts: list[str] = []
        if added:
            parts.append(f"added: {', '.join(added)}")
        if updated:
            parts.append(f"updated model: {', '.join(updated)}")
        if not parts:
            parts.append("created in V2 form")
        note = f"(backup: {backup_name})" if backup_name else "(new file, no backup)"
        return f"  {'; '.join(parts)} {note}"
    return "  Nothing to write — all agents already up to date."


def ensure_mcp_block(cfg_path: str) -> str:
    """Ensure the agent-workflow-ui MCP block exists in opencode.json.

    The second writer of the same file, under the same matrix as
    :func:`apply`: a missing file is created in the 2.x form without a
    backup, a 1.x file is not touched, and an existing 2.x file is backed
    up before the write. The block lives at ``mcp.servers.<name>`` (2.x).

    AUD07-06: the CLI wrapper used to do backup/parse/merge/write itself —
    business logic that belongs here, next to :func:`apply`. Guarantees:
    the file is parsed BEFORE any backup is made (broken JSON leaves no
    orphan .bak), the write is atomic, non-dict ``mcp``/root is an ERR
    result instead of a TypeError/AttributeError traceback.

    Returns a human-readable summary line; ``WARN:``/``ERR:`` prefix on
    failure or skip.
    """
    cfg = Path(cfg_path)
    existed = cfg.is_file()
    if existed:
        d, problem = _load(cfg_path)
        if d is None:
            return f"ERR: {problem}"
        found = v1_keys(d)
        if found:
            return f"WARN: {_v1_detail(cfg_path, found)}"
    else:
        d = _new_document()
    if not isinstance(d, dict):
        return f"ERR: {cfg} root is not a JSON object"
    mcp = d.get("mcp")
    if mcp is None:
        mcp = {"servers": {}}
        d["mcp"] = mcp
    if not isinstance(mcp, dict):
        return f"ERR: 'mcp' in {cfg} is not a JSON object"
    servers = mcp.get("servers")
    if servers is None:
        servers = {}
        mcp["servers"] = servers
    if not isinstance(servers, dict):
        return f"ERR: 'mcp.servers' in {cfg} is not a JSON object"
    if "agent-workflow-ui" in servers:
        return "OK: agent-workflow-ui already in opencode.json."
    servers["agent-workflow-ui"] = {
        "type": "local",
        "command": ["python3", "-m", "agent_workflow_ui"],
    }
    backup_name = _write(cfg, d, existed=existed)
    note = f"(backup: {backup_name})" if backup_name else "(new file, no backup)"
    return f"OK: added agent-workflow-ui to {cfg} {note}"


__all__ = [
    "V1_KEYS",
    "Proposal",
    "ProposalKind",
    "apply",
    "ensure_mcp_block",
    "propose",
]
