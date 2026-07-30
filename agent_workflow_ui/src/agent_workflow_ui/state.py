"""In-memory registry of open forms.

Filesystem (.agentic/inputs/) is the source of truth for submits; this registry
is a cache for fast `list_pending_forms` queries and form_id assignment.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml

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
    """Thread-safe registry of forms opened in current plugin session.

    A10: persists to .agentic/state/forms_registry.yaml so MCP subprocess
    crash/restart doesn't lose pending forms.
    """

    PERSIST_FILE = Path.home() / ".config" / "awf" / "state" / "forms_registry.yaml"
    PERSIST_ENABLED = True  # set False in tests via env AWF_DISABLE_FORM_PERSIST=1

    def __init__(self) -> None:
        self._forms: dict[str, FormRecord] = {}
        self._lock = threading.Lock()
        # A10: tests disable persistence via env var to keep isolation
        import os
        if os.environ.get("AWF_DISABLE_FORM_PERSIST", ""):
            FormRegistry.PERSIST_ENABLED = False
        if FormRegistry.PERSIST_ENABLED:
            self._load_persisted()

    def _load_persisted(self) -> None:
        """A10: load registry from disk on startup (if exists)."""
        if not FormRegistry.PERSIST_ENABLED:
            return
        if not self.PERSIST_FILE.is_file():
            return
        try:
            import yaml
            data = yaml.safe_load(self.PERSIST_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            for form_id, rec_dict in data.items():
                if not isinstance(rec_dict, dict):
                    continue
                try:
                    record = FormRecord(
                        form_id=form_id,
                        template=rec_dict.get("template", ""),
                        opened_at=datetime.fromisoformat(rec_dict["opened_at"]) if rec_dict.get("opened_at") else datetime.now(timezone.utc),
                        status=rec_dict.get("status", "pending"),
                        expires_at=datetime.fromisoformat(rec_dict["expires_at"]) if rec_dict.get("expires_at") else None,
                        project_dir=Path(rec_dict["project_dir"]) if rec_dict.get("project_dir") else None,
                    )
                    self._forms[form_id] = record
                except (KeyError, ValueError, TypeError):
                    continue
        except (OSError, yaml.YAMLError):
            pass

    def _persist(self) -> None:
        """A10: write registry to disk."""
        if not FormRegistry.PERSIST_ENABLED:
            return
        try:
            import yaml
            self.PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            for form_id, record in self._forms.items():
                data[form_id] = {
                    "template": record.template,
                    "opened_at": record.opened_at.isoformat() if record.opened_at else None,
                    "status": record.status,
                    "expires_at": record.expires_at.isoformat() if record.expires_at else None,
                    "project_dir": str(record.project_dir) if record.project_dir else None,
                }
            self.PERSIST_FILE.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def add(self, record: FormRecord) -> None:
        with self._lock:
            self._forms[record.form_id] = record
            self._persist()

    def get(self, form_id: str) -> FormRecord | None:
        with self._lock:
            return self._forms.get(form_id)

    def update_status(self, form_id: str, status: str) -> FormRecord | None:
        with self._lock:
            record = self._forms.get(form_id)
            if record is None:
                return None
            record.status = status
            now = datetime.now(timezone.utc)
            if status == "submitted":
                record.submitted_at = now
            elif status == "cancelled":
                record.cancelled_at = now
            self._persist()
            return record

    def list_pending(self) -> list[FormRecord]:
        with self._lock:
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
    with _registry._lock:
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
    """Get the Jinja2 Environment.

    A3: lazily initialize a default Environment if none was set, so
    one-off renders (like _ack_page from http_endpoint) work without
    explicit set_jinja_env() at startup.
    """
    global _jinja_env
    if _jinja_env is None:
        from .render.engine import create_default_env
        _jinja_env = create_default_env()
    return _jinja_env


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text file atomically (temp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def mark_needs_normalize(project_dir: Path | None, team: list[dict]) -> None:
    """Write .agentic/state/needs_normalize.yaml so awf start triggers normalize stage."""
    if project_dir is None or not (project_dir / ".agentic").is_dir():
        return
    state_dir = project_dir / ".agentic" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "needs_normalize.yaml"
    payload = {
        "needed": True,
        "marked_at": datetime.now(timezone.utc).isoformat(),
        "team": [{"role": m.get("agent", ""), "type": m.get("type", "default")}
                  for m in team if m.get("agent")],
    }
    _atomic_write_text(target, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
