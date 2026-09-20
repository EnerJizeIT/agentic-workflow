"""Unit tests for agent_workflow_ui.state (FormRegistry)."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from agent_workflow_ui.state import (
    FormRecord,
    FormRegistry,
    _atomic_write_text,
)


def test_next_form_id_format():
    """next_form_id returns timestamp-based ID: FORM-YYYYMMDDHHMMSS-XXXX."""
    registry = FormRegistry()
    form_id = registry.next_form_id()
    assert form_id.startswith("FORM-")
    parts = form_id.split("-")
    assert len(parts) == 3  # FORM, timestamp, suffix
    assert len(parts[1]) == 14  # YYYYMMDDHHMMSS
    assert len(parts[2]) == 4   # random suffix


def test_next_form_id_unique():
    """Each call produces a unique form_id."""
    registry = FormRegistry()
    ids = {registry.next_form_id() for _ in range(20)}
    assert len(ids) == 20, "Duplicate form_ids generated"


def test_add_and_get():
    registry = FormRegistry()
    record = FormRecord(
        form_id="FORM-001",
        template="project-setup",
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


def test_http_port_accessor():
    """set_http_port / get_http_port roundtrip."""
    import agent_workflow_ui.state as state_mod
    from agent_workflow_ui.state import get_http_port, set_http_port
    state_mod._http_port = None

    assert get_http_port() is None
    # AUD12-09: value is arbitrary — accessor roundtrip, no port is bound.
    port = 4711
    set_http_port(port)
    assert get_http_port() == port

    state_mod._http_port = None


def test_update_status_unknown_form_returns_none():
    """update_status for unknown form_id returns None."""
    registry = FormRegistry()
    assert registry.update_status("FORM-999", "submitted") is None


def test_update_status_cancelled_sets_cancelled_at():
    """update_status to 'cancelled' sets cancelled_at timestamp."""
    registry = FormRegistry()
    registry.add(FormRecord(
        form_id="FORM-001",
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))

    updated = registry.update_status("FORM-001", "cancelled")
    assert updated.status == "cancelled"
    assert updated.cancelled_at is not None
    assert updated.submitted_at is None


def test_next_form_id_ignores_registry_contents():
    """next_form_id is timestamp-based, not affected by existing entries."""
    registry = FormRegistry()
    registry.add(FormRecord(
        form_id="FORM-99999999999999-zzzz",
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))
    new_id = registry.next_form_id()
    assert new_id != "FORM-99999999999999-zzzz"
    assert new_id.startswith("FORM-")


def test_get_config_raises_when_not_set():
    """get_config raises RuntimeError when config not initialized."""
    import agent_workflow_ui.state as state_mod
    state_mod._config = None
    from agent_workflow_ui.state import get_config
    with pytest.raises(RuntimeError, match="Config not initialized"):
        get_config()


def test_get_jinja_env_lazy_init_a3():
    """A3: get_jinja_env lazily initializes a default env (no RuntimeError)."""
    import agent_workflow_ui.state as state_mod
    state_mod._jinja_env = None
    from agent_workflow_ui.state import get_jinja_env
    env = get_jinja_env()
    assert env is not None
    # Second call returns the same instance
    env2 = get_jinja_env()
    assert env is env2


# ── Thread-safety (F4) ──────────────────────────────────────────────────────


def test_concurrent_add_no_loss():
    """10 threads x 100 adds = exactly 1000 entries, no corruption."""
    registry = FormRegistry()
    barrier = threading.Barrier(10)

    def worker(thread_id: int) -> None:
        barrier.wait()
        for i in range(100):
            fid = f"FORM-T{thread_id}-{i:04d}"
            registry.add(FormRecord(
                form_id=fid,
                template="test",
                opened_at=datetime.now(timezone.utc),
            ))

    threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(registry._forms) == 1000


def test_concurrent_update_status_not_corrupted():
    """5 threads update the same form's status concurrently — final status is valid."""
    registry = FormRegistry()
    registry.add(FormRecord(
        form_id="FORM-X",
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))
    barrier = threading.Barrier(5)
    statuses = ["submitted", "cancelled", "pending", "review", "error"]

    def worker(idx: int) -> None:
        barrier.wait()
        for _ in range(50):
            registry.update_status("FORM-X", statuses[idx])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    record = registry.get("FORM-X")
    assert record is not None
    assert record.status in statuses


# ── Atomic write (F10) ──────────────────────────────────────────────────────


def test_atomic_write_text_produces_valid_content(tmp_path: Path) -> None:
    """_atomic_write_text writes content correctly, no .tmp left behind."""
    target = tmp_path / "output.txt"
    _atomic_write_text(target, "hello world\n")
    assert target.read_text() == "hello world\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_text_creates_parent_dirs(tmp_path: Path) -> None:
    """_atomic_write_text creates intermediate directories."""
    target = tmp_path / "a" / "b" / "c" / "file.txt"
    _atomic_write_text(target, "content")
    assert target.read_text() == "content"


# ── A10: disk persistence ───────────────────────────────────────────────────


class TestFormRegistryPersistence:
    """A10/P2: load-persist roundtrip, terminal/expired filtering, garbage."""

    def test_persist_and_load_roundtrip(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("AWF_DISABLE_FORM_PERSIST", raising=False)
        monkeypatch.setattr(FormRegistry, "PERSIST_FILE", tmp_path / "reg.yaml")
        registry = FormRegistry()
        registry.add(
            FormRecord(
                form_id="FORM-A",
                template="project-setup",
                opened_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                project_dir=tmp_path,
            )
        )
        assert (tmp_path / "reg.yaml").is_file()

        reloaded = FormRegistry()  # __init__ → _load_persisted
        record = reloaded.get("FORM-A")
        assert record is not None
        assert record.template == "project-setup"
        assert record.project_dir == tmp_path
        assert record.expires_at is not None

    def test_load_filters_terminal_and_expired(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("AWF_DISABLE_FORM_PERSIST", raising=False)
        import yaml

        monkeypatch.setattr(FormRegistry, "PERSIST_FILE", tmp_path / "reg.yaml")
        now = datetime.now(timezone.utc)
        past = (now - timedelta(days=1)).isoformat()
        data = {
            "F-pending": {"template": "t", "opened_at": now.isoformat(), "status": "pending"},
            "F-submitted": {"template": "t", "opened_at": now.isoformat(), "status": "submitted"},
            "F-cancelled": {"template": "t", "opened_at": now.isoformat(), "status": "cancelled"},
            "F-expired-status": {"template": "t", "opened_at": now.isoformat(), "status": "expired"},
            "F-expired-date": {
                "template": "t", "opened_at": now.isoformat(),
                "status": "pending", "expires_at": past,
            },
            "F-bad-record": "not-a-dict",
            "F-bad-date": {"template": "t", "opened_at": "garbage", "status": "pending"},
        }
        (tmp_path / "reg.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

        registry = FormRegistry()
        assert registry.get("F-pending") is not None
        for form_id in (
            "F-submitted", "F-cancelled", "F-expired-status",
            "F-expired-date", "F-bad-record", "F-bad-date",
        ):
            assert registry.get(form_id) is None, form_id

    def test_load_tolerates_corrupt_yaml(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("AWF_DISABLE_FORM_PERSIST", raising=False)
        monkeypatch.setattr(FormRegistry, "PERSIST_FILE", tmp_path / "reg.yaml")
        (tmp_path / "reg.yaml").write_text("::: not yaml :::\n", encoding="utf-8")
        registry = FormRegistry()  # must not raise
        assert registry.list_pending() == []

    def test_load_non_dict_root(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("AWF_DISABLE_FORM_PERSIST", raising=False)
        monkeypatch.setattr(FormRegistry, "PERSIST_FILE", tmp_path / "reg.yaml")
        (tmp_path / "reg.yaml").write_text("- a\n- b\n", encoding="utf-8")
        registry = FormRegistry()
        assert registry.list_pending() == []

    def test_env_disables_persistence(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(FormRegistry, "PERSIST_FILE", tmp_path / "reg.yaml")
        monkeypatch.setenv("AWF_DISABLE_FORM_PERSIST", "1")
        registry = FormRegistry()
        assert registry.persist_enabled is False

        registry.add(
            FormRecord(
                form_id="FORM-B",
                template="t",
                opened_at=datetime.now(timezone.utc),
            )
        )
        assert not (tmp_path / "reg.yaml").exists()  # nothing written

        registry.persist_enabled = True
        assert registry.persist_enabled is True


class TestPersistOutsideLock:
    """QA .25: disk I/O must happen AFTER the registry lock is released."""

    def test_write_happens_outside_lock(self, tmp_path: Path, monkeypatch) -> None:
        import agent_workflow_ui.state as state_mod

        monkeypatch.setattr(FormRegistry, "PERSIST_FILE", tmp_path / "reg.yaml")
        monkeypatch.delenv("AWF_DISABLE_FORM_PERSIST", raising=False)
        registry = FormRegistry()

        lock_states: list[bool] = []
        orig = state_mod._atomic_write_text

        def spy(path, text, **kw):
            lock_states.append(registry._lock.locked())
            return orig(path, text, **kw)

        monkeypatch.setattr(state_mod, "_atomic_write_text", spy)
        registry.add(
            FormRecord(form_id="FORM-L", template="t", opened_at=datetime.now(timezone.utc))
        )

        assert lock_states == [False], "disk write must not hold the registry lock"
        assert (tmp_path / "reg.yaml").is_file()

    def test_claim_and_finalize_still_persist(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(FormRegistry, "PERSIST_FILE", tmp_path / "reg.yaml")
        monkeypatch.delenv("AWF_DISABLE_FORM_PERSIST", raising=False)
        registry = FormRegistry()
        registry.add(
            FormRecord(form_id="FORM-C", template="t", opened_at=datetime.now(timezone.utc))
        )

        assert registry.claim_for_submit("FORM-C") is True
        registry.finalize_submit("FORM-C")

        # Persistence is verified on disk: a reloaded registry intentionally
        # filters terminal (submitted) records, so check the file directly.
        import yaml

        data = yaml.safe_load((tmp_path / "reg.yaml").read_text(encoding="utf-8"))
        assert data["FORM-C"]["status"] == "submitted"
