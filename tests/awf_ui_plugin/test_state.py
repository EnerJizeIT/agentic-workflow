"""Unit tests for agent_workflow_ui.state (FormRegistry)."""
from __future__ import annotations

from datetime import datetime, timezone

from agent_workflow_ui.state import FormRecord, FormRegistry


def test_next_form_id_first():
    """First form_id is FORM-001."""
    registry = FormRegistry()
    assert registry.next_form_id() == "FORM-001"


def test_next_form_id_sequence():
    """form_ids increment correctly."""
    registry = FormRegistry()
    assert registry.next_form_id() == "FORM-001"
    assert registry.next_form_id() == "FORM-002"

    registry.add(FormRecord(
        form_id="FORM-005",
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))
    assert registry.next_form_id() == "FORM-006"


def test_add_and_get():
    registry = FormRegistry()
    record = FormRecord(
        form_id="FORM-001",
        template="role-assignment",
        opened_at=datetime.now(timezone.utc),
    )
    registry.add(record)

    assert registry.get("FORM-001") is record
    assert registry.get("FORM-999") is None


def test_update_status():
    registry = FormRegistry()
    record = FormRecord(
        form_id="FORM-001",
        template="test",
        opened_at=datetime.now(timezone.utc),
    )
    registry.add(record)

    updated = registry.update_status("FORM-001", "submitted")
    assert updated is not None
    assert updated.status == "submitted"
    assert updated.submitted_at is not None

    assert registry.update_status("FORM-999", "submitted") is None


def test_list_pending():
    registry = FormRegistry()
    now = datetime.now(timezone.utc)

    registry.add(FormRecord(form_id="FORM-001", template="a", opened_at=now))
    registry.add(FormRecord(form_id="FORM-002", template="b", opened_at=now))
    registry.add(FormRecord(form_id="FORM-003", template="c", opened_at=now))
    registry.add(FormRecord(form_id="FORM-004", template="d", opened_at=now, status="submitted"))
    registry.add(FormRecord(form_id="FORM-005", template="e", opened_at=now, status="cancelled"))

    pending = registry.list_pending()
    assert len(pending) == 3
    pending_ids = {r.form_id for r in pending}
    assert pending_ids == {"FORM-001", "FORM-002", "FORM-003"}
