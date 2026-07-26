"""MCP tool implementations — form lifecycle.

This file contains STUBS: tool signatures and schemas are final,
but implementations return placeholder data. Full implementation in Epic 7.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..state import FormRecord, get_registry


async def open_form(
    template: str,
    data: Optional[dict[str, Any]] = None,
    ttl_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """Open an HTML form in the user's browser for structured input.

    Args:
        template: Template name (e.g., "role-assignment"). Without extension.
        data: Variables to render in the template.
        ttl_seconds: Auto-cancel after N seconds. Default: no TTL.

    Returns:
        Dict with form_id, browser_opened, submit_url, expires_at.
    """
    registry = get_registry()
    form_id = registry.next_form_id()
    registry.add(
        FormRecord(
            form_id=form_id,
            template=template,
            opened_at=datetime.now(timezone.utc),
        )
    )
    return {
        "form_id": form_id,
        "browser_opened": False,
        "submit_url": "",
        "error": "Not implemented yet (Epic 7). Form registered but not opened.",
    }


async def read_submit(form_id: str) -> dict[str, Any]:
    """Check if user submitted the form, return data if yes.

    Args:
        form_id: Form ID returned by open_form.

    Returns:
        Dict with submitted (bool), status, and data if submitted.
    """
    return {
        "submitted": False,
        "form_id": form_id,
        "status": "pending",
        "error": "Not implemented yet (Epic 7).",
    }


async def cancel_form(form_id: str) -> dict[str, Any]:
    """Cancel a form. Future submits for this form_id are ignored.

    Args:
        form_id: Form ID to cancel.

    Returns:
        Dict with cancelled (bool) and form_id.
    """
    return {
        "cancelled": False,
        "form_id": form_id,
        "error": "Not implemented yet (Epic 7).",
    }


async def list_pending_forms() -> dict[str, Any]:
    """List forms opened but not yet submitted or cancelled.

    Returns:
        Dict with pending (list) and count.
    """
    registry = get_registry()
    pending = registry.list_pending()
    return {
        "pending": [
            {
                "form_id": r.form_id,
                "template": r.template,
                "opened_at": r.opened_at.isoformat(),
            }
            for r in pending
        ],
        "count": len(pending),
    }
