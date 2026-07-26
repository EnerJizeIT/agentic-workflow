"""In-memory registry of open forms.

Filesystem (.agentic/inputs/) is the source of truth for submits; this registry
is a cache for fast `list_pending_forms` queries and form_id assignment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class FormRecord:
    """Metadata for an opened form."""
    form_id: str
    template: str
    opened_at: datetime
    status: str = "pending"
    submitted_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    data_keys: list = field(default_factory=list)


class FormRegistry:
    """Thread-safe registry of forms opened in current plugin session."""

    def __init__(self) -> None:
        self._forms: dict[str, FormRecord] = {}
        self._counter = 0
        self._counter: int = 0

    def add(self, record: FormRecord) -> None:
        self._forms[record.form_id] = record

    def get(self, form_id: str) -> Optional[FormRecord]:
        return self._forms.get(form_id)

    def update_status(self, form_id: str, status: str) -> Optional[FormRecord]:
        record = self._forms.get(form_id)
        if record is None:
            return None
        record.status = status
        now = datetime.now(timezone.utc)
        if status == "submitted":
            record.submitted_at = now
        elif status == "cancelled":
            record.cancelled_at = now
        return record

    def list_pending(self) -> list[FormRecord]:
        return [r for r in self._forms.values() if r.status == "pending"]

    def next_form_id(self) -> str:
        """Generate next form_id in sequence (FORM-001, FORM-002, ...).

        Uses an internal counter that starts at max(existing NNN) + 1, then
        increments on each call. For session-only uniqueness;
        file system scanning for cross-session continuity — Epic 7.
        """
        existing_numbers = []
        for form_id in self._forms:
            try:
                n = int(form_id.split("-")[1])
                existing_numbers.append(n)
            except (IndexError, ValueError):
                continue
        max_existing = max(existing_numbers) if existing_numbers else 0
        self._counter = max(self._counter, max_existing)
        self._counter += 1
        return f"FORM-{self._counter:03d}"


_registry = FormRegistry()


def get_registry() -> FormRegistry:
    return _registry


def reset_registry() -> None:
    """Reset the global registry. Used by tests for isolation."""
    _registry._forms.clear()
    _registry._counter = 0


_http_port: int | None = None


def set_http_port(port: int) -> None:
    """Set the HTTP endpoint port (called once at startup)."""
    global _http_port
    _http_port = port


def get_http_port() -> int | None:
    """Get the HTTP endpoint port. None if not started yet."""
    return _http_port
