"""MCP tool implementations — form lifecycle."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import yaml
from jinja2 import TemplateNotFound

from ..browser import open_path
from ..opencode_config import scan_global_roles
from ..render.engine import render_template
from ..state import FormRecord, get_config, get_http_port, get_jinja_env, get_registry


def _normalize_available_roles(value: str | list[str | dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Normalize available_roles to list[dict] with id/title/description keys."""
    if value is None:
        return None
    if isinstance(value, str):
        return [{"id": value, "title": value, "description": ""}]
    if not isinstance(value, list):
        return value
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            result.append({"id": item, "title": item, "description": ""})
        else:
            result.append(item)
    return result


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

    data = data or {}
    if "available_roles" in data:
        data["available_roles"] = _normalize_available_roles(data["available_roles"])
    form_id = registry.next_form_id()
    submit_url = f"http://127.0.0.1:{port}/submit/{form_id}"

    # Scan global custom roles for supervisor variants + custom agents
    supervisor_variants, custom_agents = scan_global_roles()

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

    submit_file = config.inputs_dir / f"{form_id}.yaml"
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
