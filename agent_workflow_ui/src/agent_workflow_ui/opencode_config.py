"""Read available models, recent models, and custom roles from opencode/awf."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

GLOBAL_ROLES_DIR = Path.home() / ".config" / "awf" / "roles"


def read_opencode_models() -> list[str]:
    """Get all available model IDs from opencode.

    Primary: `opencode models` CLI. Covers all providers including /connect auth.
    Fallback: parse opencode.json.
    """
    try:
        result = subprocess.run(
            ["opencode", "models"],
            capture_output=True, text=True, timeout=10, check=False,
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
            if isinstance(models_raw, dict) or isinstance(models_raw, list):
                for model_id in models_raw:
                    models.add(f"{provider_name}/{model_id}")

    for agent_cfg in (cfg.get("agent") or {}).values():
        if isinstance(agent_cfg, dict) and agent_cfg.get("model"):
            models.add(agent_cfg["model"])

    if cfg.get("model"):
        models.add(cfg["model"])

    return sorted(models)


def scan_global_roles() -> tuple[list[dict], list[dict]]:
    """Scan ~/.config/awf/roles/ for custom roles.

    Returns:
        Tuple (supervisor_variants, custom_agents).
        supervisor_variants: files starting with 'supervisor-'.
        custom_agents: all other .md files.
    """
    if not GLOBAL_ROLES_DIR.exists():
        return [], []

    supervisor_variants: list[dict] = []
    custom_agents: list[dict] = []

    for md_file in sorted(GLOBAL_ROLES_DIR.glob("*.md")):
        name = md_file.stem
        try:
            content = md_file.read_text(encoding="utf-8")
            first_line = content.strip().split("\n")[0]
            title = first_line.lstrip("# ").strip() or name
        except Exception:
            title = name

        entry = {"id": name, "title": title, "filename": md_file.name}

        if name.startswith("supervisor-"):
            supervisor_variants.append(entry)
        else:
            custom_agents.append(entry)

    return supervisor_variants, custom_agents


def save_custom_role(name: str, content: str, role_type: str = "agent") -> Path:
    """Save a custom role .md to ~/.config/awf/roles/.

    Args:
        name: Role name (will be slugified).
        content: .md file content.
        role_type: 'agent' or 'supervisor'. Supervisor gets 'supervisor-' prefix.

    Returns:
        Path to saved file.
    """
    GLOBAL_ROLES_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(name)
    if role_type == "supervisor" and not slug.startswith("supervisor-"):
        slug = f"supervisor-{slug}"
    path = (GLOBAL_ROLES_DIR / f"{slug}.md").resolve()
    # Defense-in-depth: ensure resolved path stays inside GLOBAL_ROLES_DIR.
    if not path.is_relative_to(GLOBAL_ROLES_DIR.resolve()):
        raise ValueError(f"Slug {slug!r} escapes roles dir")
    path.write_text(content, encoding="utf-8")
    log.info("Saved custom role: %s", path)
    return path


def delete_custom_role(name: str) -> bool:
    """Delete a custom role .md from ~/.config/awf/roles/.

    Args:
        name: Role name (slug).

    Returns:
        True if deleted, False if not found.
    """
    # Reject anything that could escape GLOBAL_ROLES_DIR via path traversal.
    if "/" in name or "\\" in name or name in (".", ".."):
        log.warning("Rejected delete_custom_role with suspicious name: %r", name)
        return False
    path = (GLOBAL_ROLES_DIR / f"{name}.md").resolve()
    if not path.is_relative_to(GLOBAL_ROLES_DIR.resolve()):
        log.warning("Rejected delete_custom_role: path escapes roles dir: %s", path)
        return False
    if path.exists():
        path.unlink()
        log.info("Deleted custom role: %s", path)
        return True
    return False


def _slugify(name: str) -> str:
    """Convert role name to filesystem-safe slug.

    Must match JS slugify() in project-setup.html.j2 for client-side conflict
    detection to work correctly. Test cross-check: tests/awf_ui_plugin/test_slugify.py.
    """
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", name.lower()).strip("-")
    return slug or "unnamed"
