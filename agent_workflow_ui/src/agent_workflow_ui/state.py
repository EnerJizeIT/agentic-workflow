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


# AUD09-05: a "submitting" claim older than this (or without claimed_at)
# means the claiming process crashed between claim and finalize. The claim
# is re-taken instead of 409'ing forever.
STALE_CLAIM_SECONDS = 600


def _is_stale_claim(record: FormRecord) -> bool:
    """AUD09-05: stale = older than STALE_CLAIM_SECONDS, or no claimed_at
    at all (legacy/corrupt persisted record). The old `age = 600` +
    `if age > 600` combination made the no-timestamp branch dead code."""
    if record.claimed_at is None:
        return True
    return (datetime.now(timezone.utc) - record.claimed_at).total_seconds() >= STALE_CLAIM_SECONDS


class FormRegistry:
    """Thread-safe registry of forms opened in current plugin session.

    A10: persists forms so MCP subprocess crash/restart doesn't lose
    pending forms.

    AUD09-04: persistence is PER-FORM files
    (``<state>/forms/FORM-*.yaml``, one record per file, atomic rename).
    The old design wrote the WHOLE registry snapshot on every mutation —
    with two parallel MCP processes that was last-write-wins and forms
    opened in one session vanished from the other. Per-form files are
    independent: each process only ever writes its own record.

    ``PERSIST_FILE`` (legacy whole-registry snapshot) is kept as a
    read-only fallback for installs that predate the split; on first load
    its records are back-filled as per-form files and it is ignored from
    then on.
    """

    # AUD-12: uses consolidated xdg_config_home from awf.xdg
    # AUD09-04: legacy whole-registry snapshot — read-only fallback now.
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

    @property
    def persist_dir(self) -> Path:
        """AUD09-04: per-form record files live in ``<state>/forms/``."""
        return self.PERSIST_FILE.parent / "forms"

    def _record_payload(self, record: FormRecord) -> dict:
        """Snapshot a record's fields as a plain dict.

        Call this INSIDE the lock (CPU-only) and hand the payload to the
        disk writers AFTER releasing it — disk I/O must not block the lock
        while a form request waits (QA .25).
        """
        return {
            "template": record.template,
            "opened_at": record.opened_at.isoformat() if record.opened_at else None,
            "status": record.status,
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            "claimed_at": record.claimed_at.isoformat() if record.claimed_at else None,
            "project_dir": str(record.project_dir) if record.project_dir else None,
        }

    def _write_form_file(self, form_id: str, payload: dict) -> None:
        """AUD09-04: atomically write ONE record's file, outside the lock."""
        if payload is None or not self._persist_enabled:
            return
        # form_id becomes a filename — refuse anything that could escape
        # persist_dir (server-generated IDs are already safe; belt).
        if not form_id or not form_id.isascii() or "/" in form_id or "\\" in form_id:
            return
        import yaml

        try:
            _atomic_write_text(
                self.persist_dir / f"{form_id}.yaml",
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            )
        except OSError:
            pass

    def _remove_form_file(self, form_id: str) -> None:
        """AUD09-04: drop a record's file (terminal states), outside the lock."""
        if not self._persist_enabled:
            return
        try:
            (self.persist_dir / f"{form_id}.yaml").unlink()
        except OSError:
            pass

    def _record_from_dict(self, form_id: str, rec_dict: dict) -> FormRecord | None:
        """Parse + filter one persisted record dict.

        P2: terminal (submitted/cancelled/expired) and date-expired forms
        are skipped — prevents memory/disk growth across restarts.
        Returns None when the record is filtered out or unparseable.
        """
        status = rec_dict.get("status", "pending")
        if status in ("submitted", "cancelled", "expired"):
            return None
        expires = rec_dict.get("expires_at")
        if expires:
            try:
                if datetime.fromisoformat(expires) < datetime.now(timezone.utc):
                    return None
            except (ValueError, TypeError):
                pass
        try:
            return FormRecord(
                form_id=form_id,
                template=rec_dict.get("template", ""),
                opened_at=datetime.fromisoformat(rec_dict["opened_at"]) if rec_dict.get("opened_at") else datetime.now(timezone.utc),
                status=status,
                expires_at=datetime.fromisoformat(rec_dict["expires_at"]) if rec_dict.get("expires_at") else None,
                claimed_at=datetime.fromisoformat(rec_dict["claimed_at"]) if rec_dict.get("claimed_at") else None,
                project_dir=Path(rec_dict["project_dir"]) if rec_dict.get("project_dir") else None,
            )
        except (KeyError, ValueError, TypeError):
            return None

    def _parse_record_file(self, path: Path) -> FormRecord | None:
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return None
        if not isinstance(data, dict):
            return None
        return self._record_from_dict(path.stem, data)

    def _parse_legacy_file(self) -> list[FormRecord]:
        """Read the pre-AUD09-04 whole-registry snapshot (fallback)."""
        try:
            import yaml
            data = yaml.safe_load(self.PERSIST_FILE.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return []
        if not isinstance(data, dict):
            return []
        records = []
        for form_id, rec_dict in data.items():
            if not isinstance(rec_dict, dict):
                continue
            record = self._record_from_dict(form_id, rec_dict)
            if record is not None:
                records.append(record)
        return records

    def _load_persisted(self) -> None:
        """A10 + AUD09-04: load registry from disk on startup (if exists).

        Per-form files are canonical (no cross-process overwrite). The
        legacy whole-registry file is a one-time fallback: its surviving
        records are loaded AND back-filled as per-form files, so the next
        start reads only the per-form directory.
        """
        if not self._persist_enabled:
            return
        records: list[FormRecord] = []
        backfill: list[tuple[str, dict]] = []
        if self.persist_dir.is_dir():
            for path in sorted(self.persist_dir.glob("*.yaml")):
                record = self._parse_record_file(path)
                if record is not None:
                    records.append(record)
        if not records and self.PERSIST_FILE.is_file():
            for record in self._parse_legacy_file():
                records.append(record)
                backfill.append((record.form_id, self._record_payload(record)))
        with self._lock:
            for record in records:
                self._forms[record.form_id] = record
        for form_id, payload in backfill:
            self._write_form_file(form_id, payload)

    def add(self, record: FormRecord) -> None:
        with self._lock:
            self._forms[record.form_id] = record
            payload = self._record_payload(record)
        self._write_form_file(record.form_id, payload)

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
            # AUD09-04: terminal states are dropped from disk (the submit
            # yaml itself is the durable artifact); others update the file.
            if status in ("submitted", "cancelled", "expired"):
                payload = None
            else:
                payload = self._record_payload(record)
        if payload is None:
            self._remove_form_file(form_id)
        else:
            self._write_form_file(form_id, payload)
        return record

    def claim_for_submit(self, form_id: str) -> bool:
        """H4 fix: atomic check-and-set for TOCTOU race protection.

        Returns True if the form was pending and this call successfully
        claimed it (caller may proceed to write submit file). Returns
        False if form was already submitted/cancelled/expired/missing —
        caller must abort.

        AUD09-05: a "submitting" form is re-claimable when its claim is
        stale (crash between claim and finalize, >= STALE_CLAIM_SECONDS
        or no claimed_at) — otherwise the HTTP path 409s forever.

        Without this, two parallel POSTs in ThreadingHTTPServer could
        both pass the `status == 'pending'` check and both write to
        the same submit file (last wins, first lost silently).
        """
        with self._lock:
            record = self._forms.get(form_id)
            if record is None:
                return False
            if record.status == "submitting":
                if not _is_stale_claim(record):
                    return False
            elif record.status != "pending":
                return False
            record.status = "submitting"  # intermediate state
            record.claimed_at = datetime.now(timezone.utc)
            payload = self._record_payload(record)
        self._write_form_file(form_id, payload)
        return True

    def finalize_submit(self, form_id: str) -> FormRecord | None:
        """H4 fix: mark form as submitted after caller wrote the file."""
        with self._lock:
            record = self._forms.get(form_id)
            if record is None:
                return None
            record.status = "submitted"
            record.submitted_at = datetime.now(timezone.utc)
        self._remove_form_file(form_id)
        return record

    def list_pending(self) -> list[FormRecord]:
        with self._lock:
            result = []
            reverted: list[tuple[str, dict]] = []
            for r in self._forms.values():
                if r.status == "pending":
                    result.append(r)
                elif r.status == "submitting":
                    # P1/AUD09-05: auto-revert stale "submitting" forms
                    # (crash recovery): the claimer likely crashed.
                    if _is_stale_claim(r):
                        r.status = "pending"
                        result.append(r)
                        reverted.append((r.form_id, self._record_payload(r)))
        for form_id, payload in reverted:
            self._write_form_file(form_id, payload)
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
