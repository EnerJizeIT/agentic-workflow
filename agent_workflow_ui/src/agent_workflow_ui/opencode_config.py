"""Read model configuration from opencode.json."""
from __future__ import annotations

import json
from pathlib import Path


def read_opencode_models() -> list[str]:
    """Read available model IDs from ~/.config/opencode/opencode.json.

    Scans three locations:
    - provider.<name>.models.<model_id> -> "<name>/<model_id>"
    - agent.<name>.model -> the model string as-is
    - top-level "model" -> the model string as-is

    Returns sorted list of unique model IDs. Returns [] if config not found
    or unreadable.
    """
    cfg_path = Path.home() / ".config" / "opencode" / "opencode.json"
    if not cfg_path.exists():
        return []

    try:
        cfg = json.loads(cfg_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    models: set[str] = set()

    # From provider configs
    for provider_name, provider_cfg in (cfg.get("provider") or {}).items():
        if isinstance(provider_cfg, dict):
            models_raw = provider_cfg.get("models") or {}
            if isinstance(models_raw, dict):
                for model_id in models_raw:
                    models.add(f"{provider_name}/{model_id}")
            elif isinstance(models_raw, list):
                for model_id in models_raw:
                    models.add(f"{provider_name}/{model_id}")

    # From agent configs
    for agent_name, agent_cfg in (cfg.get("agent") or {}).items():
        if isinstance(agent_cfg, dict) and agent_cfg.get("model"):
            models.add(agent_cfg["model"])

    # From top-level model
    if cfg.get("model"):
        models.add(cfg["model"])

    return sorted(models)
