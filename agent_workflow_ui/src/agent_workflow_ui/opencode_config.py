"""Read available models from opencode."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def read_opencode_models() -> list[str]:
    """Get all available model IDs from opencode.

    Primary source: `opencode models` CLI command. Covers all providers
    including those authenticated via /connect (stored in auth.json).

    Fallback: parse ~/.config/opencode/opencode.json for provider/agent models.
    Used if opencode binary is not on PATH.
    """
    # Try CLI first — most accurate, includes auth.json providers
    try:
        result = subprocess.run(
            ["opencode", "models"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            models = []
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                # Skip log lines like "[page-assist] ..."
                if line and not line.startswith("[") and "/" in line:
                    models.append(line)
            if models:
                return sorted(models)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("opencode models CLI failed: %s, falling back to config", e)

    # Fallback: parse opencode.json
    return _read_models_from_config()


def _read_models_from_config() -> list[str]:
    """Parse ~/.config/opencode/opencode.json for model IDs."""
    cfg_path = Path.home() / ".config" / "opencode" / "opencode.json"
    if not cfg_path.exists():
        return []

    try:
        cfg = json.loads(cfg_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    models: set[str] = set()

    for provider_name, provider_cfg in (cfg.get("provider") or {}).items():
        if isinstance(provider_cfg, dict):
            models_raw = provider_cfg.get("models") or {}
            if isinstance(models_raw, dict):
                for model_id in models_raw:
                    models.add(f"{provider_name}/{model_id}")
            elif isinstance(models_raw, list):
                for model_id in models_raw:
                    models.add(f"{provider_name}/{model_id}")

    for agent_cfg in (cfg.get("agent") or {}).values():
        if isinstance(agent_cfg, dict) and agent_cfg.get("model"):
            models.add(agent_cfg["model"])

    if cfg.get("model"):
        models.add(cfg["model"])

    return sorted(models)
