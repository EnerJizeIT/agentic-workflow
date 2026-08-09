"""Model configuration validator — checks config.yaml models exist in opencode.json.

Prevents silent fallback to wrong model (dogfood issue: supervisor claimed
'opencode.json empty' when it wasn't — diagnosis error).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ._helpers import require_agentic


def _load_opencode_config(
    oc_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse opencode.json → (providers, agents).

    Returns ({}, {}) if file missing or unreadable. Logs errors to stderr.
    """
    if not oc_path.exists():
        return {}, {}
    try:
        import json

        oc_data = json.loads(oc_path.read_text(encoding="utf-8"))
        return (
            oc_data.get("provider", {}) or {},
            oc_data.get("agent", {}) or {},
        )
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"model_check: cannot read opencode.json: {e}",
            file=sys.stderr,
        )
        return {}, {}


def _load_cli_models() -> set[str]:
    """Get ALL available models from 'opencode models' CLI.

    Covers internal providers (zai-coding-plan, openai) not in opencode.json.
    Returns empty set on any error (including CLI not installed).
    """
    try:
        import subprocess

        result = subprocess.run(
            ["opencode", "models"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return set()
        models: set[str] = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and "/" in line and not line.startswith("["):
                models.add(line)
        return models
    except (FileNotFoundError, OSError) as e:
        print(f"model_check: 'opencode models' CLI unavailable: {e}", file=sys.stderr)
        return set()
    except Exception as e:
        print(f"model_check: 'opencode models' failed: {e}", file=sys.stderr)
        return set()


def _load_recent_models(db_path: Path) -> set[str]:
    """Read model IDs from opencode.db session history.

    Returns empty set if db missing, unreadable, or no sessions.
    """
    if not db_path.is_file():
        return set()
    try:
        import json
        import sqlite3

        conn = sqlite3.connect(str(db_path), timeout=3)
        try:
            rows = conn.execute(
                "SELECT DISTINCT model FROM session WHERE model IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()

        models: set[str] = set()
        for r in rows:
            raw = r[0]
            if not raw:
                continue
            # opencode.db stores model as JSON: {"id":"x","providerID":"y"}
            try:
                m = json.loads(raw)
                pid = m.get("providerID", "")
                mid = m.get("id", "")
                if pid and mid:
                    models.add(f"{pid}/{mid}")
            except (json.JSONDecodeError, TypeError):
                if isinstance(raw, str):
                    models.add(raw)
        return models
    except Exception as e:
        print(f"model_check: cannot read opencode.db: {e}", file=sys.stderr)
        return set()


def check_model_config(project_dir: Path) -> dict[str, Any]:
    """Check that models in config.yaml have valid providers in opencode.json.

    Returns dict with:
    - models: list of {role, model, provider, valid}
    - warnings: list of human-readable warnings
    - opencode_path: path to opencode.json used
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    import os

    from .. import config as cfg_mod
    from .. import xdg

    config_data = cfg_mod.load(project_dir)
    models_config = cfg_mod.get(config_data, "models", {}) or {}

    oc_path = xdg.opencode_config_file()
    oc_providers, oc_agents = _load_opencode_config(oc_path)
    cli_models = _load_cli_models()

    # Recent models from opencode.db
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if data_home:
        db_path = Path(data_home) / "opencode" / "opencode.db"
    else:
        db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    recent_models = _load_recent_models(db_path)

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
        elif model in cli_models:
            valid = True
            note = (
                f"Model '{model}' available via 'opencode models' CLI "
                f"(internal provider). opencode resolves it automatically."
            )
        elif model in recent_models:
            valid = True
            note = (
                f"Model '{model}' found in recent opencode sessions. "
                f"Provider may be internal (zai-coding-plan, openai, etc.) — "
                f"opencode resolves it without explicit config."
            )
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
