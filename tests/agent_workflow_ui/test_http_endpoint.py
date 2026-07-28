"""Tests for HTTP endpoint: form submits, validation, idempotency."""
from __future__ import annotations

import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest
from agent_workflow_ui.config import load
from agent_workflow_ui.http_endpoint import (
    _ack_page,
    _find_free_port,
    _is_valid_form_id,
    start_http_server,
)
from agent_workflow_ui.state import FormRecord, FormRegistry


@pytest.fixture
def http_setup(tmp_path, monkeypatch):
    """Spin up HTTP server with tmp config and registry."""
    monkeypatch.chdir(tmp_path)
    config = load()
    config.inputs_dir.mkdir(parents=True, exist_ok=True)
    registry = FormRegistry()
    server, port = start_http_server(config, registry)
    yield config, registry, port
    server.shutdown()


def test_find_free_port():
    """_find_free_port returns a usable port."""
    port = _find_free_port()
    assert 1024 < port < 65536


def test_is_valid_form_id():
    assert _is_valid_form_id("FORM-001")
    assert _is_valid_form_id("FORM-999")
    assert not _is_valid_form_id("form-001")
    assert not _is_valid_form_id("FORM-1")
    assert not _is_valid_form_id("FOO-001")
    assert not _is_valid_form_id("")


def test_health_endpoint(http_setup):
    """GET /health returns 200 ok."""
    config, registry, port = http_setup
    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health")
    assert resp.status == 200
    body = resp.read().decode()
    assert "ok" in body


def test_submit_writes_yaml(http_setup):
    """POST /submit/FORM-001 writes YAML file."""
    config, registry, port = http_setup
    registry.add(FormRecord(
        form_id="FORM-001",
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))

    data = b"selected_roles=worker&selected_roles=reviewer&comment=hello"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-001",
        data=data,
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200

    yaml_file = config.inputs_dir / "FORM-001.yaml"
    assert yaml_file.exists(), f"YAML not written: {yaml_file}"
    content = yaml_file.read_text()
    assert "FORM-001" in content
    assert "test" in content
    assert "worker" in content
    assert "reviewer" in content
    assert "hello" in content


def test_submit_unknown_form_returns_404(http_setup):
    """POST to unknown form_id returns 404."""
    config, registry, port = http_setup

    data = b"foo=bar"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-999",
        data=data,
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 404


def test_submit_invalid_form_id_returns_404(http_setup):
    """POST with malformed form_id returns 404."""
    config, registry, port = http_setup

    data = b"foo=bar"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/invalid",
        data=data,
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 404


def test_submit_already_submitted_is_idempotent(http_setup):
    """Duplicate submit returns 200 (not error), doesn't overwrite."""
    config, registry, port = http_setup
    registry.add(FormRecord(
        form_id="FORM-001",
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))

    data1 = b"choice=option1"
    req1 = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-001",
        data=data1,
        method="POST",
    )
    resp1 = urllib.request.urlopen(req1)
    assert resp1.status == 200

    data2 = b"choice=option2"
    req2 = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-001",
        data=data2,
        method="POST",
    )
    resp2 = urllib.request.urlopen(req2)
    assert resp2.status == 200

    content = (config.inputs_dir / "FORM-001.yaml").read_text()
    assert "option1" in content
    assert "option2" not in content


def test_get_submit_unknown_path_returns_404(http_setup):
    """GET unknown path returns 404."""
    config, registry, port = http_setup
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/unknown-path")
    assert exc_info.value.code == 404


def test_submit_too_large_body_returns_413(http_setup):
    """Body > 1MB returns 413."""
    config, registry, port = http_setup
    registry.add(FormRecord(
        form_id="FORM-001",
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))

    big_data = b"x" * (2 * 1024 * 1024)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-001",
        data=big_data,
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 413


# --- Task 4: http_endpoint.py coverage gaps ---

def test_get_submit_path_returns_404(http_setup):
    """GET /submit/... returns 404 (only POST allowed)."""
    config, registry, port = http_setup
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/submit/FORM-001")
    assert exc_info.value.code == 404


def test_submit_cancelled_form_returns_410(http_setup):
    """POST to cancelled form returns 410 Gone."""
    config, registry, port = http_setup
    registry.add(FormRecord(
        form_id="FORM-001",
        template="test",
        opened_at=datetime.now(timezone.utc),
        status="cancelled",
    ))

    data = b"foo=bar"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-001",
        data=data,
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 410


def test_submit_empty_body_returns_400(http_setup):
    """POST with Content-Length: 0 returns 400."""
    config, registry, port = http_setup
    registry.add(FormRecord(
        form_id="FORM-001",
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-001",
        data=b"",
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400


def test_post_non_submit_path_returns_404(http_setup):
    """POST to non-/submit/ path returns 404."""
    config, registry, port = http_setup

    data = b"foo=bar"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/other-path",
        data=data,
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 404


# --- BD-4: ack page Russian + dark theme ---

def test_ack_page_russian_dark_theme():
    page = _ack_page("FORM-test-1234", already_submitted=False)
    assert "Форма отправлена!" in page
    assert "Эта форма уже была отправлена" not in page
    assert 'lang="ru"' in page
    assert "Подтверждение отправки" in page
    assert "ID формы:" in page
    assert "Что дальше — вернись в CLI" in page
    assert "Переключись на терминал" in page
    assert "я отправил форму" in page
    assert "Агент прочитает твои ответы" in page
    assert "не блокируется в ожидании" in page
    assert "#1565c0" not in page
    assert "#e3f2fd" not in page
    assert "#f14c4c" in page
    assert "#3d1a1a" in page
    assert "#1e1e1e" in page
    assert "#d4d4d4" in page
    assert "FORM-test-1234" in page


def test_ack_page_already_submitted():
    page = _ack_page("FORM-abc-5678", already_submitted=True)
    assert "Эта форма уже была отправлена ранее." in page
    assert "Форма отправлена!" not in page
    assert 'lang="ru"' in page
    assert "FORM-abc-5678" in page
    assert "#f14c4c" in page
