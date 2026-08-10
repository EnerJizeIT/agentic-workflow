"""In-memory registry of open forms.

Filesystem (.agentic/inputs/) is the source of truth for submits; this registry
is a cache for fast `list_pending_forms` queries and form_id assignment.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.xdg import xdg_config_home  # AUD-12: consolidated (was local copy)

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
    claimed_at: datetime | None = None
    data_keys: list = field(default_factory=list)
    project_dir: Path | None = None  # absolute path to awf project, set by agent


class FormRegistry:
    """Thread-safe registry of forms opened in current plugin session.

    A10: persists to .agentic/state/forms_registry.yaml so MCP subprocess
    crash/restart doesn't lose pending forms.
    """

    # AUD-12: uses consolidated xdg_config_home from awf.xdg
    PERSIST_FILE = xdg_config_home() / "awf" / "state" / "forms_registry.yaml"
    # QA-A: class-level default; instances read via property. Tests that
    # need to disable persistence set ``reg.persist_enabled = False`` on
    # the specific instance, not the class attribute (avoids test pollution
    # where one disabled instance would silently affect all future ones).
    PERSIST_ENABLED = True

    def __init__(self) -> None:
        self._forms: dict[str, FormRecord] = {}
        self._lock = threading.Lock()
        # QA-A fix: instance attribute shadows class default. Tests that
        # set reg.PERSIST_ENABLED = False now hit this instance attr.
        # Class attribute remains True for fresh instances.
        if os.environ.get("AWF_DISABLE_FORM_PERSIST", ""):
            self._persist_enabled = False
        else:
            self._persist_enabled = FormRegistry.PERSIST_ENABLED
        if self._persist_enabled:
            self._load_persisted()

    @property
    def persist_enabled(self) -> bool:
        """QA-A: instance-level persistence flag (replaces class mutation)."""
        return self._persist_enabled

    @persist_enabled.setter
    def persist_enabled(self, value: bool) -> None:
        self._persist_enabled = bool(value)

    def _load_persisted(self) -> None:
        """A10: load registry from disk on startup (if exists)."""
        if not self._persist_enabled:
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
        """A10: write registry to disk atomically."""
        if not self._persist_enabled:
            return
        try:
            import yaml
            data = {}
            for form_id, record in self._forms.items():
                data[form_id] = {
                    "template": record.template,
                    "opened_at": record.opened_at.isoformat() if record.opened_at else None,
                    "status": record.status,
                    "expires_at": record.expires_at.isoformat() if record.expires_at else None,
                    "project_dir": str(record.project_dir) if record.project_dir else None,
                }
            _atomic_write_text(self.PERSIST_FILE, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
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

    def claim_for_submit(self, form_id: str) -> bool:
        """H4 fix: atomic check-and-set for TOCTOU race protection.

        Returns True if the form was pending and this call successfully
        claimed it (caller may proceed to write submit file). Returns
        False if form was already submitted/cancelled/missing — caller
        must abort.

        Without this, two parallel POSTs in ThreadingHTTPServer could
        both pass the `status == 'pending'` check and both write to
        the same submit file (last wins, first lost silently).
        """
        with self._lock:
            record = self._forms.get(form_id)
            if record is None or record.status != "pending":
                return False
            record.status = "submitting"  # intermediate state
            record.claimed_at = datetime.now(timezone.utc)
            self._persist()
            return True

    def finalize_submit(self, form_id: str) -> FormRecord | None:
        """H4 fix: mark form as submitted after caller wrote the file."""
        with self._lock:
            record = self._forms.get(form_id)
            if record is None:
                return None
            record.status = "submitted"
            record.submitted_at = datetime.now(timezone.utc)
            self._persist()
            return record

    def list_pending(self) -> list[FormRecord]:
        with self._lock:
            now = datetime.now(timezone.utc)
            result = []
            for r in self._forms.values():
                if r.status == "pending":
                    result.append(r)
                elif r.status == "submitting":
                    # P1: auto-revert stale "submitting" forms (crash recovery).
                    # If a form has been in "submitting" for >10 minutes, the
                    # process that claimed it likely crashed. Revert to pending.
                    if r.claimed_at:
                        age = (now - r.claimed_at).total_seconds()
                    else:
                        age = 600  # no timestamp → assume stale
                    if age > 600:
                        r.status = "pending"
                        self._persist()
                        result.append(r)
            return result

    def next_form_id(self) -> str:
        """Generate a globally unique form_id.

        Format: FORM-YYYYMMDDHHMMSS-XXXX
        Where XXXX = 4 random alphanumeric chars.

        This is globally unique — no collision with stale files from previous
        sessions in .agentic/inputs/.
        """
        import secrets
        import string
        import time

        timestamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        # QA #9: secrets.choice instead of random.choices — form IDs are
        # security-relevant (predictable IDs could allow form hijacking).
        alphabet = string.ascii_lowercase + string.digits
        suffix = "".join(secrets.choice(alphabet) for _ in range(4))
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

# QA-B: was a local _atomic_write_text duplicate. Plugin depends on awf
# (from awf import api), so we use the canonical implementation.
from awf._atomic import atomic_write_text as _atomic_write_text  # noqa: E402
