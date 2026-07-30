"""MCP tool implementations — form lifecycle."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from jinja2 import TemplateNotFound

from ..browser import open_path
from ..opencode_config import _xdg_config_home, scan_global_roles, scan_global_skills
from ..render.engine import render_template
from ..state import FormRecord, get_config, get_http_port, get_jinja_env, get_registry

log = logging.getLogger(__name__)


def _normalize_available_roles(value: Any) -> list[dict[str, Any]]:
    """Normalize available_roles to list of dicts.

    Accepts: list of str | dict, single str, single dict.
    Filters out: None, empty strings, non-str-non-dict items.
    """
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        return []

    result: list[dict[str, Any]] = []
    for item in value:
        if item is None:
            continue
        if isinstance(item, str):
            item = item.strip()
            if not item:
                continue
            result.append({"id": item, "title": item, "description": ""})
        elif isinstance(item, dict):
            if item.get("id"):
                result.append(item)
    return result


def _collect_opencode_models() -> list[str]:
    """BD-32: collect unique model IDs from opencode.json for form dropdown.

    Scans ~/.config/opencode/opencode.json and extracts model strings
    from: agent.<name>.model and provider.<name>.models.<id>.

    Returns a sorted unique list. Falls back to [] on any error
    (form will show '(нет моделей)' placeholder — non-fatal).
    """
    import json

    oc_path = _xdg_config_home() / "opencode" / "opencode.json"
    if not oc_path.is_file():
        return []

    try:
        cfg = json.loads(oc_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("BD-32: failed to parse %s: %s", oc_path, e)
        return []

    if not isinstance(cfg, dict):
        return []

    models: set[str] = set()

    # From agents: agent.<name>.model (e.g. "vllm/llm")
    agents = cfg.get("agent") or {}
    if isinstance(agents, dict):
        for agent_cfg in agents.values():
            if isinstance(agent_cfg, dict):
                m = agent_cfg.get("model")
                if isinstance(m, str) and m.strip():
                    models.add(m.strip())

    # From providers: provider.<name>.models.<id> → "<name>/<id>"
    providers = cfg.get("provider") or {}
    if isinstance(providers, dict):
        for pname, pcfg in providers.items():
            if not isinstance(pcfg, dict):
                continue
            pmodels = pcfg.get("models")
            if isinstance(pmodels, dict):
                for mid in pmodels.keys():
                    if isinstance(mid, str) and mid.strip():
                        models.add(f"{pname}/{mid.strip()}")
            elif isinstance(pmodels, list):
                for mid in pmodels:
                    if isinstance(mid, str) and mid.strip():
                        models.add(f"{pname}/{mid.strip()}")

    return sorted(models)


async def open_form(
    template: str,
    data: dict[str, Any] | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Open an HTML form in the user's browser for structured input.

    Args:
        template: Template name (without extension, e.g. "role-assignment").
        data: Variables to render in the template.
        ttl_seconds: Auto-cancel after N seconds (optional). Default: no TTL.

    Returns:
        Dict with form_id, browser_opened, submit_url, expires_at (optional),
        error (on failure).
    """
    registry = get_registry()
    config = get_config()
    env = get_jinja_env()
    port = get_http_port()

    if port is None:
        return {
            "form_id": "",
            "browser_opened": False,
            "submit_url": "",
            "error": "HTTP endpoint not started.",
        }

    data = dict(data) if data else {}
    # BD-6: extract project_dir from data so submit paths resolve to the project,
    # not cwd (which is $HOME when opencode launches MCP subprocess).
    project_dir_raw = data.pop("project_dir", None)
    project_dir: Path | None = None
    if project_dir_raw:
        p = Path(project_dir_raw).expanduser().resolve()
        if (p / ".agentic").is_dir():
            project_dir = p
        else:
            log.warning("project_dir %s has no .agentic/ — ignoring", p)

    if "available_roles" in data:
        data["available_roles"] = _normalize_available_roles(data["available_roles"])
    form_id = registry.next_form_id()
    submit_url = f"http://127.0.0.1:{port}/submit/{form_id}"

    # Scan global custom roles for supervisor variants + custom agents
    supervisor_variants, custom_agents = scan_global_roles()
    # BD-27: scan global skills (full content) — these are the primary source
    # for team roles. User picks a skill, content goes straight into
    # .agentic/roles/<role>.md — no mapping/guessing needed.
    global_skills = scan_global_skills()
    # BD-32: collect available models from opencode.json so the form can
    # offer per-role model selection. Without this, dropdown was empty and
    # chosen model was silently dropped (project-auditor got vllm/llm
    # regardless of what user picked).
    available_models = _collect_opencode_models()

    # Existing slugs for client-side conflict detection (JS confirm before overwrite)
    existing_supervisor_slugs = [sv["id"] for sv in supervisor_variants]
    existing_agent_slugs = [ca["id"] for ca in custom_agents]

    try:
        rendered = render_template(env, template, {
            **data,
            "form_id": form_id,
            "submit_url": submit_url,
            "custom_supervisor_roles": supervisor_variants,
            "custom_agents": custom_agents,
            "global_skills": global_skills,
            "available_models": available_models,
            "existing_supervisor_slugs": existing_supervisor_slugs,
            "existing_agent_slugs": existing_agent_slugs,
        })
    except TemplateNotFound:
        return {
            "form_id": form_id,
            "browser_opened": False,
            "submit_url": submit_url,
            "error": f"Template '{template}' not found.",
        }

    # A4: cleanup stale temp files before creating a new one
    cleanup_temp_files()

    temp_file = config.temp_dir / f"agent-workflow-ui-{form_id}.html"
    temp_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file.write_text(rendered, encoding="utf-8")

    success, msg = open_path(temp_file, command=config.open_browser_cmd)

    expires_at = None
    if ttl_seconds is not None:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    elif config.default_ttl_seconds > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=config.default_ttl_seconds)).isoformat()

    expires_at_dt: datetime | None = None
    if expires_at is not None:
        expires_at_dt = datetime.fromisoformat(expires_at)

    record = FormRecord(
        form_id=form_id,
        template=template,
        opened_at=datetime.now(timezone.utc),
        status="pending",
        expires_at=expires_at_dt,
        data_keys=list(data.keys()),
        project_dir=project_dir,
    )
    registry.add(record)

    result: dict[str, Any] = {
        "form_id": form_id,
        "browser_opened": success,
        "submit_url": submit_url,
    }
    if expires_at:
        result["expires_at"] = expires_at
    if not success:
        result["error"] = msg
    return result


def cleanup_temp_files(max_age_hours: int = 24) -> int:
    """A4: remove stale agent-workflow-ui-*.html files from temp dir.

    Called lazily from open_form (one cleanup pass per new form opened).
    Removes files older than ``max_age_hours`` (default 24h). Active
    forms in registry are skipped (file might still be open in browser).

    Returns count of files removed.
    """
    config = get_config()
    if not config.temp_dir.is_dir():
        return 0

    registry = get_registry()
    active_form_ids = {r.form_id for r in registry.list_pending()}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    removed = 0

    for f in config.temp_dir.glob("agent-workflow-ui-*.html"):
        try:
            # Skip if form is still active in registry
            form_id = f.stem.replace("agent-workflow-ui-", "")
            if form_id in active_form_ids:
                continue
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        log.info("A4: cleaned up %d stale temp HTML files", removed)
    return removed


async def read_submit(form_id: str) -> dict[str, Any]:
    """Check if user submitted the form, return data if yes.

    Args:
        form_id: Form ID returned by open_form.

    Returns:
        Dict with submitted (bool), form_id, status. If submitted: data, submitted_at, template.
    """
    registry = get_registry()
    config = get_config()

    record = registry.get(form_id)
    if record is None:
        return {
            "submitted": False,
            "form_id": form_id,
            "status": "unknown",
            "error": f"Form '{form_id}' not found in registry.",
        }

    if record.status == "pending" and record.expires_at:
        if datetime.now(timezone.utc) > record.expires_at:
            registry.update_status(form_id, "expired")
            record = registry.get(form_id)

    inputs_dir = (record.project_dir / ".agentic" / "inputs") if record.project_dir else config.inputs_dir
    submit_file = inputs_dir / f"{form_id}.yaml"
    if not submit_file.exists():
        return {
            "submitted": False,
            "form_id": form_id,
            "status": record.status,
            "opened_at": record.opened_at.isoformat(),
        }

    try:
        with submit_file.open(encoding="utf-8") as f:
            payload = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return {
            "submitted": False,
            "form_id": form_id,
            "status": "error",
            "error": f"Failed to parse submit file: {e}",
        }

    if not isinstance(payload, dict):
        return {
            "submitted": False,
            "form_id": form_id,
            "status": "error",
            "error": "Submit file is not a dict.",
        }

    # A4: clean up the temp HTML file (form is consumed)
    try:
        temp_html = config.temp_dir / f"agent-workflow-ui-{form_id}.html"
        if temp_html.exists():
            temp_html.unlink()
    except OSError:
        pass

    return {
        "submitted": True,
        "form_id": form_id,
        "status": "submitted",
        "submitted_at": payload.get("submitted_at"),
        "template": payload.get("template", record.template),
        "data": payload.get("data", {}),
    }


async def cancel_form(form_id: str) -> dict[str, Any]:
    """Cancel a form. Future submits for this form_id are ignored.

    Args:
        form_id: Form ID to cancel.

    Returns:
        Dict with cancelled (bool) and form_id.
    """
    registry = get_registry()
    record = registry.get(form_id)

    if record is None:
        return {
            "cancelled": False,
            "form_id": form_id,
            "reason": "not_found",
        }

    if record.status == "submitted":
        return {
            "cancelled": False,
            "form_id": form_id,
            "reason": "already_submitted",
        }

    registry.update_status(form_id, "cancelled")
    return {
        "cancelled": True,
        "form_id": form_id,
    }


async def list_pending_forms() -> dict[str, Any]:
    """List forms opened but not yet submitted or cancelled.

    Returns:
        Dict with pending (list of {form_id, template, opened_at, age_seconds}) and count.
    """
    registry = get_registry()
    now = datetime.now(timezone.utc)
    pending = registry.list_pending()

    result = []
    for record in pending:
        if record.expires_at:
            if now > record.expires_at:
                registry.update_status(record.form_id, "expired")
                continue
        age = int((now - record.opened_at).total_seconds())
        result.append({
            "form_id": record.form_id,
            "template": record.template,
            "opened_at": record.opened_at.isoformat(),
            "age_seconds": age,
        })

    return {
        "pending": result,
        "count": len(result),
    }
