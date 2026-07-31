"""Propose and apply changes to ~/.config/opencode/opencode.json."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def propose(cfg_path: str, roles: list[str], model: str) -> str:
    """Return a human-readable description of what would change.

    Returns "NOTHING: ..." when no change is needed, "PROPOSE: ..." when there
    are pending changes, or "ERR: ..." on error.
    """
    try:
        with open(cfg_path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return f"ERR:cannot read {cfg_path}: {e}"

    agents = d.get("agent")
    if agents is None:
        return f"ERR:no 'agent' key in {cfg_path}"
    if not isinstance(agents, dict):
        return "ERR:'agent' is not an object"

    to_add: list[str] = []
    to_update: list[str] = []

    for r in roles:
        cur = agents.get(r)
        if cur is None:
            to_add.append(r)
        elif isinstance(cur, dict) and model and cur.get("model") != model:
            to_update.append(f"{r}: model {cur.get('model')!r} -> {model!r}")

    if not to_add and not to_update:
        return f"NOTHING: all of {', '.join(roles)} already present."

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
    return "PROPOSE:\n" + "\n".join(lines)


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
        # M5 fix: only include "model" key if model is non-empty.
        # Empty string in opencode.json could be interpreted as 'use model
        # with empty name' rather than 'no model specified'.
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
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        parts: list[str] = []
        if added:
            parts.append(f"added: {', '.join(added)}")
        if updated:
            parts.append(f"updated model: {', '.join(updated)}")
        return f"  {'; '.join(parts)} (model: {model or '<blank>'})"
    else:
        return "  Nothing to write — all agents already up to date."
