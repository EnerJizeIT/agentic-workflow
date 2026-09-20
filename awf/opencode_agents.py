"""Propose and apply changes to ``~/.config/opencode/opencode.json``.

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


class ProposalKind(enum.Enum):
    """What :func:`propose` concluded about the requested change."""

    NOTHING = "nothing"  # all requested roles already present
    PROPOSE = "propose"  # there are changes to apply
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
# the rest with instant feedback instead of a hanging question.
SANCTIONED_EXTERNAL_PATTERNS: dict[str, str] = {
    "/tmp/opencode/**": "allow",
    "/tmp/pytest-*": "allow",
    "/tmp/pytest-*/**": "allow",
}


def _ensure_worker_permission(agent_def: dict) -> bool:
    """Merge sanctioned external_directory patterns into a worker agent def.

    Returns True when the definition changed. Existing entries are preserved:
    a plain "ask"/"deny" becomes the ``*`` default of the pattern map, an
    existing map only gains the missing sanctioned patterns.
    """
    perm = agent_def.get("permission")
    if not isinstance(perm, dict):
        perm = {}
        agent_def["permission"] = perm
    ext = perm.get("external_directory")
    if isinstance(ext, str):
        if ext == "allow":
            return False  # already permissive — nothing to add
        ext = {"*": ext}
        perm["external_directory"] = ext
    elif not isinstance(ext, dict):
        ext = {"*": "deny"}
        perm["external_directory"] = ext

    changed = False
    for pattern, action in SANCTIONED_EXTERNAL_PATTERNS.items():
        if pattern not in ext:
            ext[pattern] = action
            changed = True
    return changed


def propose(cfg_path: str, roles: list[str], model: str) -> Proposal:
    """Inspect opencode.json and return what would change.

    Returns :class:`Proposal` with ``kind`` indicating outcome:
    - :attr:`ProposalKind.NOTHING` — all roles already present with matching model.
    - :attr:`ProposalKind.PROPOSE` — there are additions or model updates.
    - :attr:`ProposalKind.ERR` — file unreadable or schema wrong.

    ``model`` is an API capability for programmatic callers (the CLI init
    flow passes an empty string — per-role models come from the
    project-setup form). Callers branch on ``proposal.kind``;
    ``Proposal.__str__`` renders the legacy ``"KIND:detail"`` form for
    CLI printing.
    """
    try:
        with open(cfg_path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return Proposal(
            kind=ProposalKind.ERR,
            detail=f"cannot read {cfg_path}: {e}",
        )

    agents = d.get("agent")
    if agents is None:
        return Proposal(
            kind=ProposalKind.ERR,
            detail=f"no 'agent' key in {cfg_path}",
        )
    if not isinstance(agents, dict):
        return Proposal(
            kind=ProposalKind.ERR,
            detail="'agent' is not an object",
        )

    to_add: list[str] = []
    to_update: list[str] = []

    for r in roles:
        cur = agents.get(r)
        if cur is None:
            to_add.append(r)
        elif isinstance(cur, dict):
            details: list[str] = []
            if model and cur.get("model") != model:
                details.append(f"model {cur.get('model')!r} -> {model!r}")
            if r == "worker":
                probe = json.loads(json.dumps(cur))  # deep copy, probe only
                if _ensure_worker_permission(probe):
                    detail_parts = [
                        "+ external_directory allow-patterns "
                        "(/tmp/opencode, /tmp/pytest-*)"
                    ]
                    # AUD07-09: when the worker has no external_directory
                    # at all, the merge introduces a ``* -> deny`` default —
                    # the user approves exactly what is summarized here.
                    cur_perm = cur.get("permission")
                    cur_ext = (
                        cur_perm.get("external_directory")
                        if isinstance(cur_perm, dict)
                        else None
                    )
                    if cur_ext is None:
                        detail_parts.append(
                            "* -> deny (all other external directories)"
                        )
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
        lines.append("  model: <blank> — agent entries will have empty model, edit them manually")
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


def apply(cfg_path: str, roles: list[str], model: str) -> str:
    """Backup + write opencode.json. Return a summary string."""
    cfg = Path(cfg_path)

    with open(cfg, encoding="utf-8") as f:
        d = json.load(f)

    agents = d.setdefault("agent", {})
    if not isinstance(agents, dict):
        return "  'agent' is not an object — skipping."

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
            if model and cur.get("model") != model:
                cur["model"] = model
                changed = True
            if r == "worker" and _ensure_worker_permission(cur):
                changed = True
            if changed:
                updated.append(r)

    if added or updated:
        # AUD07-09: backup only when we actually write — a no-op apply
        # used to leave an orphan .bak-<ts> behind.
        shutil.copy2(cfg, _unique_backup(cfg))
        # T2.6 fix: atomic write — crash mid-write no longer corrupts
        # opencode.json.
        atomic_write_text(
            cfg,
            json.dumps(d, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        parts: list[str] = []
        if added:
            parts.append(f"added: {', '.join(added)}")
        if updated:
            parts.append(f"updated model: {', '.join(updated)}")
        return f"  {'; '.join(parts)} (model: {model or '<blank>'})"
    else:
        return "  Nothing to write — all agents already up to date."


def ensure_mcp_block(cfg_path: str) -> str:
    """Ensure the agent-workflow-ui MCP block exists in opencode.json.

    AUD07-06: the CLI wrapper used to do backup/parse/merge/write itself —
    business logic that belongs here, next to :func:`apply`. Guarantees:
    the file is parsed BEFORE any backup is made (broken JSON leaves no
    orphan .bak), the write is atomic, non-dict ``mcp``/root is an ERR
    result instead of a TypeError/AttributeError traceback.

    Returns a human-readable summary line; ``ERR:`` prefix on failure.
    """
    cfg = Path(cfg_path)
    if not cfg.is_file():
        return f"NOTE: {cfg} not found. Skipping."
    try:
        with cfg.open(encoding="utf-8") as f:
            d = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        return f"ERR: cannot parse {cfg}: {e}"
    if not isinstance(d, dict):
        return f"ERR: {cfg} root is not a JSON object"
    mcp = d.get("mcp")
    if mcp is None:
        mcp = {}
        d["mcp"] = mcp
    if not isinstance(mcp, dict):
        return f"ERR: 'mcp' in {cfg} is not a JSON object"
    if "agent-workflow-ui" in mcp:
        return "OK: agent-workflow-ui already in opencode.json."
    # Backup after a successful parse — broken JSON must not leave one.
    backup = _unique_backup(cfg)
    shutil.copy2(cfg, backup)
    mcp["agent-workflow-ui"] = {
        "type": "local",
        "command": ["python3", "-m", "agent_workflow_ui"],
    }
    atomic_write_text(
        cfg,
        json.dumps(d, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return f"OK: added agent-workflow-ui to {cfg} (backup: {backup.name})"
