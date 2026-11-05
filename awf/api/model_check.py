"""Model configuration validator — checks config.yaml models exist in opencode.json.

Prevents silent fallback to wrong model (dogfood issue: supervisor claimed
'opencode.json empty' when it wasn't — diagnosis error).

TODO-0175: also cross-checks the model lines carried by
``.agentic/roles/*.md`` against the config (the config stays the source
of truth — the engine behavior is unchanged, drift is only reported).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ._helpers import require_agentic
from .roles import parse_role_model_line


def _load_opencode_root(oc_path: Path) -> dict[str, Any]:
    """Parse opencode.json → the whole root dict (v1 AND v2 keys).

    Returns {} if the file is missing or unreadable. Logs errors to stderr.
    ``_net.model_endpoint_url`` needs both ``provider`` (v1) and
    ``providers`` (v2) forms (K7); ``_load_opencode_config`` is the narrow
    view kept for the existing callers.
    """
    if not oc_path.exists():
        return {}
    try:
        import json

        oc_data = json.loads(oc_path.read_text(encoding="utf-8"))
        if not isinstance(oc_data, dict):
            # AUD06-13: a valid-JSON opencode.json with a non-dict root
            # (list, number) must degrade, not AttributeError on .get.
            print(
                f"model_check: opencode.json root is {type(oc_data).__name__}, "
                "expected an object — treating as empty",
                file=sys.stderr,
            )
            return {}
        return oc_data
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(
            f"model_check: cannot read opencode.json: {e}",
            file=sys.stderr,
        )
        return {}


def _load_opencode_config(
    oc_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse opencode.json → (providers, agents); ({}, {}) when unreadable.

    awf 3 reads the 2.x form only: ``providers`` / ``agents``. The 1.x
    roots (``provider`` / ``agent``) are not a fallback — a file still in
    that shape is reported by ``awf doctor`` and ignored here, so the model
    check never validates a role against a provider the launch won't use.
    """
    oc_data = _load_opencode_root(oc_path)
    return (
        oc_data.get("providers", {}) or {},
        oc_data.get("agents", {}) or {},
    )


def _load_cli_models() -> set[str]:
    """All available models via the shared ``awf.opencode_db`` reader (K6).

    Covers internal providers (zai-coding-plan, openai) not in opencode.json.
    Returns an empty set on any error (including CLI not installed); the
    reader prints the reason and never raises (AC25, AC26).
    """
    from .. import opencode_db

    return set(opencode_db.available_models())


def _load_recent_models(db_path: Path, limit: int | None = None) -> set[str]:
    """Model IDs from opencode.db history — same reader as the plugin (D8).

    One SQL copy for both consumers (AC12). The shared reader knows one
    schema, v2 (``session_v2``); a v1 or empty base gives an empty set and
    a "моделей не найдено" line on stderr, never an exception (K3).
    """
    from .. import opencode_db

    return set(opencode_db.recent_models(db_path, limit=limit))


def _pipeline_roles(project_dir: Path, config_data: dict) -> list[str]:
    """A48: role names of the live pipeline, in first-seen order.

    A role may be named by a stage and missing from ``models:`` entirely.
    Doctor must warn about it before launch — ``require_role_model`` would
    refuse it at spawn. A missing or broken pipeline is not this surface's
    finding (the pipeline/config rows own it), so degrade to an empty list.

    Not ``api.roles._read_pipeline_roles``: that one lists WORK POSITIONS
    (skips ``supervisor`` and every ``skip_if`` entry). Here the question is
    "which roles does a launch spawn" — the supervisor and the conditional
    entries spawn too, and the supervisor was exactly the role with no model
    in the 04.11 incident. Reusing that helper would hide the bug again.
    """
    from .. import config as cfg_mod
    from .._errors import AwfApiError
    from ..pipeline import load_stages, resolve_pipeline_file

    name = cfg_mod.get(config_data, "default_pipeline", "default") or "default"
    try:
        stages = load_stages(
            resolve_pipeline_file(project_dir, name, config_data)
        )
    except (FileNotFoundError, AwfApiError, OSError, ValueError):
        return []

    roles: list[str] = []
    for stage in stages:
        role = getattr(stage, "role", "")
        if isinstance(role, str) and role and role not in roles:
            roles.append(role)
    return roles


def check_model_config(project_dir: Path) -> dict[str, Any]:
    """Check that models in config.yaml have valid providers in opencode.json.

    Also cross-checks the model lines of ``.agentic/roles/*.md`` against
    the config (TODO-0175): a drift is a warning, a missing line is not
    an error.

    Returns dict with:
    - models: list of {role, model, provider, valid}
    - warnings: list of human-readable warnings
    - missing_model_roles: roles the engine refuses (no own model and no
      ``models.worker.model``)
    - missing_model_warnings: the subset of ``warnings`` that belongs to
      those roles — doctor drops it from its aggregate row
    - role_files: list of {role, file_model, config_model, match}
    - opencode_path: path to opencode.json used
    - cli_models_available: False when ``opencode models`` gave no answer
      (doctor reads "models unknown", not "no providers")
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    import os

    from .. import config as cfg_mod
    from .. import xdg

    config_data = cfg_mod.load(project_dir)
    models_config = cfg_mod.get(config_data, "models", {}) or {}
    # A48/CTX10: ``models:`` written as a list (``- worker``) is not a
    # mapping. The engine degrades to "no model" and refuses; this reader
    # must not crash on the same config, so it reads that shape as empty.
    if not isinstance(models_config, dict):
        models_config = {}

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
    # A48: roles with no own model and no ``models.worker.model`` — the
    # engine refuses them; doctor reads this list instead of parsing the
    # warning text.
    missing_model_roles: list[str] = []
    # A48: the same warnings as a list of their own — doctor drops these
    # from its aggregate row by membership instead of parsing the text.
    missing_model_warnings: list[str] = []
    # A48: one resolver for the whole product — the engine's rule lives in
    # supervisor.get_role_model (own → models.worker.model), so this check
    # cannot drift from what the launch will do.
    from ..supervisor import get_role_model

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

    # A48: the checked set is ``models:`` keys ∪ pipeline stage roles. A role
    # named only by a stage would otherwise pass silently here and be
    # refused at spawn.
    checked_roles: list[str] = []
    for role in list(models_config.keys()) + _pipeline_roles(
        project_dir, config_data
    ):
        role = str(role)
        if role not in checked_roles:
            checked_roles.append(role)

    for role in checked_roles:
        role_cfg = models_config.get(role)
        # A missing mapping (stage-only role) or a scalar entry is "no
        # model": the engine's resolver refuses both at spawn.
        if not isinstance(role_cfg, dict):
            role_cfg = {}
        model = role_cfg.get("model", "")
        if not isinstance(model, str):
            model = ""
        if not model.strip():
            inherited = get_role_model(config_data, role)
            if inherited:
                results.append({
                    "role": role,
                    "model": inherited,
                    "provider": (
                        inherited.split("/", 1)[0]
                        if "/" in inherited
                        else "(direct)"
                    ),
                    "valid": True,
                    "note": (
                        "No own model — inherits models.worker.model "
                        f"'{inherited}'"
                    ),
                })
            else:
                # A48: this used to say "worker will use opencode default",
                # which is exactly the bug — the default is vllm/llm.
                missing_model_roles.append(role)
                results.append({
                    "role": role,
                    "model": "(none)",
                    "provider": "(none)",
                    "valid": False,
                    "note": (
                        "No model and no models.worker.model — the engine "
                        "refuses to spawn this role"
                    ),
                })
                warnings.append(
                    f"Role '{role}': no model — "
                    + (
                        "add models.worker.model (the project-wide model)"
                        if role == "worker"
                        else f"add models.{role}.model "
                        "(or models.worker.model for the whole project)"
                    )
                )
                missing_model_warnings.append(warnings[-1])
            continue
        model = model.strip()

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
            if provider_name:
                note = (
                    f"Provider '{provider_name}' not found in opencode.json. "
                    f"The launch passes '{model}' explicitly (A48) — check "
                    "that opencode resolves this provider."
                )
                warnings.append(
                    f"Role '{role}': model '{model}' — provider '{provider_name}' "
                    f"not in opencode.json providers ({list(oc_providers.keys())}). "
                    f"The model is passed via --model; an unknown provider may "
                    "fail to resolve."
                )
            else:
                # AUD06-13: bare model name (no 'provider/' prefix) — the
                # provider-existence check does not apply, say so honestly.
                note = (
                    f"Model '{model}' has no provider prefix "
                    "(expected 'provider/model') — check that opencode "
                    "knows it by default."
                )
                warnings.append(
                    f"Role '{role}': model '{model}' has no provider prefix — "
                    "opencode must know it by default."
                )

        results.append({
            "role": role,
            "model": model,
            "provider": provider_name or "(direct)",
            "valid": valid,
            "note": note,
        })

    # TODO-0175: cross-check the model lines carried by .agentic/roles/*.md
    # against the config. The config stays the source of truth — the engine
    # behavior is unchanged, this only reports drift. A missing line (or
    # the unassigned placeholder) is not an error: nothing to compare.
    role_files: list[dict[str, Any]] = []
    roles_dir = project_dir / ".agentic" / "roles"
    if roles_dir.is_dir():
        for f in sorted(roles_dir.glob("*.md")):
            try:
                content = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            file_model = parse_role_model_line(content)
            role_cfg = models_config.get(f.stem)
            config_model = (
                role_cfg.get("model", "") if isinstance(role_cfg, dict) else ""
            )
            if not isinstance(config_model, str):
                config_model = ""
            match = file_model is None or file_model == config_model
            role_files.append({
                "role": f.stem,
                "file_model": file_model,
                "config_model": config_model or None,
                "match": match,
            })
            if not match:
                warnings.append(
                    f"Role '{f.stem}': role file says model={file_model}, "
                    f"config models:{f.stem}.model is "
                    f"{config_model or '(default)'} — the config stays "
                    "the source of truth (update the role file or the config)."
                )

    return {
        "models": results,
        "warnings": warnings,
        "missing_model_roles": missing_model_roles,
        "missing_model_warnings": missing_model_warnings,
        "role_files": role_files,
        "opencode_path": str(oc_path) if oc_path.exists() else None,
        "providers_available": list(oc_providers.keys()),
    }


__all__ = ["check_model_config"]
