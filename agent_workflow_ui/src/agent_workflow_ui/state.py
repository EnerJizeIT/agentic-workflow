"""In-memory registry of open forms.

Filesystem (.agentic/inputs/) is the source of truth for submits; this registry
is a cache for fast `list_pending_forms` queries and form_id assignment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Avoid circular import: config.py imports nothing from state.py at module level,
    # but the type hint is enough to trigger circular import if imported eagerly.
    from .config import Config


@dataclass
class FormRecord:
    """Metadata for an opened form."""
    form_id: str
    template: str
    opened_at: datetime
    status: str = "pending"
    submitted_at: datetime | None = None
    cancelled_at: datetime | None = None
    expires_at: datetime | None = None
    data_keys: list = field(default_factory=list)
    project_dir: Path | None = None  # absolute path to awf project, set by agent


class FormRegistry:
    """Thread-safe registry of forms opened in current plugin session."""

    def __init__(self) -> None:
        self._forms: dict[str, FormRecord] = {}

    def add(self, record: FormRecord) -> None:
        self._forms[record.form_id] = record

    def get(self, form_id: str) -> FormRecord | None:
        return self._forms.get(form_id)

    def update_status(self, form_id: str, status: str) -> FormRecord | None:
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
        """Generate a globally unique form_id.

        Format: FORM-YYYYMMDDHHMMSS-XXXX
        Where XXXX = 4 random alphanumeric chars.

        This is globally unique — no collision with stale files from previous
        sessions in .agentic/inputs/.
        """
        import random
        import string
        import time

        timestamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        return f"FORM-{timestamp}-{suffix}"


_registry = FormRegistry()


def get_registry() -> FormRegistry:
    return _registry


def reset_registry() -> None:
    """Reset the global registry. Used by tests for isolation."""
    _registry._forms.clear()


_http_port: int | None = None


def set_http_port(port: int) -> None:
    """Set the HTTP endpoint port (called once at startup)."""
    global _http_port
    _http_port = port


def get_http_port() -> int | None:
    """Get the HTTP endpoint port. None if not started yet."""
    return _http_port


_config: Config | None = None
_jinja_env: Any | None = None  # Environment instance
_project_dir: Path | None = None


def set_config(config: Config) -> None:
    """Set the plugin Config (called once at startup)."""
    global _config
    _config = config


def get_config() -> Config:
    """Get the plugin Config. Raises RuntimeError if not set."""
    if _config is None:
        raise RuntimeError("Config not initialized. Call set_config() at startup.")
    return _config


def set_project_dir(path: Path | None) -> None:
    """Set the active project directory (called once at startup)."""
    global _project_dir
    _project_dir = path


def get_project_dir() -> Path | None:
    """Get the active project directory. None if not inside an awf project."""
    return _project_dir


def set_jinja_env(env: Any) -> None:
    """Set the Jinja2 Environment (called once at startup)."""
    global _jinja_env
    _jinja_env = env


def get_jinja_env() -> Any:
    """Get the Jinja2 Environment. Raises RuntimeError if not set."""
    if _jinja_env is None:
        raise RuntimeError("Jinja env not initialized. Call set_jinja_env() at startup.")
    return _jinja_env
