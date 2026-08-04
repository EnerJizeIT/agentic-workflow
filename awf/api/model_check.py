"""Model configuration validator — checks config.yaml models exist in opencode.json.

Prevents silent fallback to wrong model (dogfood issue: supervisor claimed
'opencode.json empty' when it wasn't — diagnosis error).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ._helpers import require_agentic


def check_model_config(project_dir: Path) -> dict[str, Any]:
    """Check that models in config.yaml have valid providers in opencode.json.

    Returns dict with:
    - models: list of {role, model, provider, valid}
    - warnings: list of human-readable warnings
    - opencode_path: path to opencode.json used
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    import json

    from .. import config as cfg_mod
    from .. import xdg

    config_data = cfg_mod.load(project_dir)
    models_config = cfg_mod.get(config_data, "models", {}) or {}

    oc_path = xdg.opencode_config_file()
    oc_providers: dict[str, Any] = {}
    oc_agents: dict[str, Any] = {}

    if oc_path.exists():
        try:
            oc_data = json.loads(oc_path.read_text(encoding="utf-8"))
            oc_providers = oc_data.get("provider", {}) or {}
            oc_agents = oc_data.get("agent", {}) or {}
        except (json.JSONDecodeError, OSError):
            pass

    results: list[dict[str, Any]] = []
    warnings: list[str] = []

    def _models_keys(val: Any) -> list[str]:
        """Normalize provider.models to list of model IDs.

        opencode.json allows both forms:
        - dict: ``{"claude-3.5": {...}, "claude-3.7": {...}}`` → keys
        - list: ``["claude-3.5", "claude-3.7"]`` → elements
        """
        if isinstance(val, dict):
            return [str(k) for k in val.keys()]
        if isinstance(val, list):
            return [str(m) for m in val if isinstance(m, str)]
        return []

    for role, role_cfg in models_config.items():
        if not isinstance(role_cfg, dict):
            continue
        model = role_cfg.get("model", "")
        if not model:
            results.append({
                "role": role,
                "model": "(none)",
                "provider": "(none)",
                "valid": True,
                "note": "No model specified — worker will use opencode default",
            })
            continue

        if "/" in model:
            provider_name, model_id = model.split("/", 1)
        else:
            provider_name = ""
            model_id = model

        # Check if provider exists in opencode.json
        provider_exists = provider_name in oc_providers if provider_name else False
        provider_has_model = False
        available_in_provider: list[str] = []
        if provider_exists:
            provider_cfg = oc_providers[provider_name]
            available_in_provider = _models_keys(provider_cfg.get("models", {}))
            provider_has_model = model_id in available_in_provider

        # Also check if model is referenced in agent configs
        agent_has_model = any(
            isinstance(a, dict) and a.get("model") == model
            for a in oc_agents.values()
        )

        if provider_exists and provider_has_model:
            valid = True
            note = f"Provider '{provider_name}' has model '{model_id}'"
        elif agent_has_model:
            valid = True
            note = f"Model '{model}' used by opencode agent config"
        elif provider_exists and not provider_has_model:
            valid = False
            note = f"Provider '{provider_name}' exists but model '{model_id}' not in its models list"
            warnings.append(
                f"Role '{role}': provider '{provider_name}' exists but model "
                f"'{model_id}' not found in its models list. "
                f"Available: {available_in_provider[:5]}"
            )
        else:
            valid = False
            note = (
                f"Provider '{provider_name}' not found in opencode.json. "
                f"Worker will silently fallback to opencode default model."
            )
            warnings.append(
                f"Role '{role}': model '{model}' — provider '{provider_name}' "
                f"not in opencode.json providers ({list(oc_providers.keys())}). "
                f"Worker will use fallback model (may be slow/cloud)."
            )

        results.append({
            "role": role,
            "model": model,
            "provider": provider_name or "(direct)",
            "valid": valid,
            "note": note,
        })

    return {
        "models": results,
        "warnings": warnings,
        "opencode_path": str(oc_path) if oc_path.exists() else None,
        "providers_available": list(oc_providers.keys()),
    }


__all__ = ["check_model_config"]
