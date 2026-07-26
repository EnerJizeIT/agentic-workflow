"""MCP tool implementations — form lifecycle."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import yaml
from jinja2 import TemplateNotFound

from ..state import FormRecord, get_registry, get_config, get_jinja_env, get_http_port
from ..render.engine import render_template
from ..browser import open_path


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
    form_id = registry.next_form_id()
    submit_url = f"http://127.0.0.1:{port}/submit/{form_id}"

    try:
        rendered = render_template(env, template, {
            **data,
            "form_id": form_id,
            "submit_url": submit_url,
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


async def wait_for_submit(
    form_id: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: int = 5,
) -> dict[str, Any]:
    """Wait for the user to submit the form. Blocks until submit, cancel, or timeout.

    Use this instead of manually polling read_submit in a loop. The agent
    gets a single response when the form is submitted (or after timeout).

    Args:
        form_id: Form ID returned by open_form.
        timeout_seconds: Max wait time in seconds. Default 300 (5 min).
        poll_interval_seconds: How often to check. Default 5 sec.

    Returns:
        Same shape as read_submit on success/timeout. Returns immediately
        if form is already submitted, cancelled, or unknown.
    """
    import asyncio
    import time

    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        result = await read_submit(form_id)
        status = result.get("status")
        # Return immediately if form reached a terminal state
        if result.get("submitted"):
            return result
        if status in ("cancelled", "expired", "unknown"):
            return result
        await asyncio.sleep(poll_interval_seconds)

    return {
        "submitted": False,
        "form_id": form_id,
        "status": "timeout",
        "error": f"No submit within {timeout_seconds}s. Form may still be pending — call read_submit later.",
    }


async def open_form_and_wait(
    template: str,
    data: dict[str, Any] | None = None,
    timeout_seconds: int = 300,
    poll_interval_seconds: int = 5,
) -> dict[str, Any]:
    """Open a form in the browser AND wait for the user to submit it.

    This is the SIMPLEST way to ask the user a question via form. One call,
    one result. Combines open_form + wait_for_submit internally.

    Use this when you need structured input from the user and don't need
    to do other work while waiting.

    Args:
        template: Template name (e.g., "role-assignment").
        data: Variables to render in the template.
        timeout_seconds: Max wait time. Default 300 (5 min).
        poll_interval_seconds: Poll frequency. Default 5 sec.

    Returns:
        On success: {form_id, browser_opened, submitted: true, data: {...}, ...}
        On browser failure: {form_id, browser_opened: false, error: "..."}
        On timeout: {form_id, submitted: false, status: "timeout", ...}
    """
    open_result = await open_form(template=template, data=data)

    # If browser didn't open, don't wait — return error immediately
    if not open_result.get("browser_opened"):
        return {
            **open_result,
            "submitted": False,
            "status": "browser_failed",
        }

    form_id = open_result["form_id"]
    wait_result = await wait_for_submit(
        form_id=form_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )

    # Merge: keep form_id, submit_url from open; add submitted/data from wait
    return {
        **open_result,
        **wait_result,
    }
