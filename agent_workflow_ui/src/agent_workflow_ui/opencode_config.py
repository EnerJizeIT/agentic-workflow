"""Read available and recent models from opencode."""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def read_opencode_models() -> list[str]:
    """Get all available model IDs from opencode.

    Primary: `opencode models` CLI. Covers all providers including /connect auth.
    Fallback: parse opencode.json.
    """
    try:
        result = subprocess.run(
            ["opencode", "models"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            models = []
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line and not line.startswith("[") and "/" in line:
                    models.append(line)
            if models:
                return sorted(models)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("opencode models CLI failed: %s", e)

    return _read_models_from_config()


def read_recent_models(limit: int = 8) -> list[str]:
    """Get recently used models from opencode session history (SQLite).

    Reads ~/.local/share/opencode/opencode.db, extracts distinct model IDs
    from session table, ordered by most recent usage.

    Returns list of 'provider/model' strings, deduplicated.
    """
    db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if not db_path.exists():
        return []

    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
        rows = conn.execute(
            """
            SELECT model, MAX(time_created) as last_used
            FROM session
            WHERE model IS NOT NULL
            GROUP BY model
            ORDER BY last_used DESC
            LIMIT ?
            """,
            (limit * 2,),  # fetch extra, dedup after parsing
        ).fetchall()
        conn.close()
    except Exception as e:
        log.warning("Failed to read recent models from DB: %s", e)
        return []

    seen: set[str] = set()
    recent: list[str] = []
    for row in rows:
        model_json = row[0]
        if not model_json:
            continue
        try:
            m = json.loads(model_json)
            provider = m.get("providerID", "")
            model_id = m.get("id", "")
            if provider and model_id:
                full_id = f"{provider}/{model_id}"
                if full_id not in seen:
                    seen.add(full_id)
                    recent.append(full_id)
                    if len(recent) >= limit:
                        break
        except (json.JSONDecodeError, TypeError):
            continue

    return recent


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
