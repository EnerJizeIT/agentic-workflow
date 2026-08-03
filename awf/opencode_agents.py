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


def propose(cfg_path: str, roles: list[str], model: str) -> Proposal:
    """Inspect opencode.json and return what would change.

    Returns :class:`Proposal` with ``kind`` indicating outcome:
    - :attr:`ProposalKind.NOTHING` — all roles already present with matching model.
    - :attr:`ProposalKind.PROPOSE` — there are additions or model updates.
    - :attr:`ProposalKind.ERR` — file unreadable or schema wrong.

    The legacy string-prefix format is preserved via ``Proposal.__str__``
    (renders as ``"NOTHING:..."`` etc.) so old ``startswith`` callers keep
    working — but new callers should check ``proposal.kind`` directly.
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
        elif isinstance(cur, dict) and model and cur.get("model") != model:
            to_update.append(f"{r}: model {cur.get('model')!r} -> {model!r}")

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


def apply(cfg_path: str, roles: list[str], model: str) -> str:
    """Backup + write opencode.json. Return a summary string."""
    cfg = Path(cfg_path)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = Path(f"{cfg_path}.bak-{ts}")
    shutil.copy2(cfg, backup)

    with open(cfg, encoding="utf-8") as f:
        d = json.load(f)

    agents = d.setdefault("agent", {})
    if not isinstance(agents, dict):
        return "  'agent' is not an object — skipping."

    added: list[str] = []
    updated: list[str] = []
    for r in roles:
        cur = agents.get(r)
        agent_def: dict = {"description": f"awf {r} agent"}
        if model:
            agent_def["model"] = model
        if cur is None:
            agents[r] = agent_def
            added.append(r)
        elif isinstance(cur, dict) and model and cur.get("model") != model:
            cur["model"] = model
            updated.append(r)

    if added or updated:
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
