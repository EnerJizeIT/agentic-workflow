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
    # Path traversal defense (defense-in-depth — registry.get is primary guard)
    assert not _is_valid_form_id("FORM-../../etc/passwd")
    assert not _is_valid_form_id("FORM-foo/bar")
    assert not _is_valid_form_id("FORM-foo\\bar")
    assert not _is_valid_form_id("FORM-..secret")


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


# --- BD-6: submit uses project_dir from FormRecord ---


def test_submit_with_project_dir_writes_to_project(http_setup, tmp_path):
    """POST submit writes YAML to project/.agentic/inputs/ when FormRecord has project_dir."""
    config, registry, port = http_setup

    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / ".agentic" / "inputs").mkdir(parents=True)

    registry.add(FormRecord(
        form_id="FORM-001",
        template="test",
        opened_at=datetime.now(timezone.utc),
        project_dir=proj,
    ))

    data = b"selected_roles=worker"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-001",
        data=data,
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200

    # YAML written to project's inputs_dir, NOT config.inputs_dir
    proj_yaml = proj / ".agentic" / "inputs" / "FORM-001.yaml"
    assert proj_yaml.exists(), f"YAML not in project: {proj_yaml}"
    assert "worker" in proj_yaml.read_text()

    # Should NOT be in default inputs_dir
    default_yaml = config.inputs_dir / "FORM-001.yaml"
    assert not default_yaml.exists(), f"YAML incorrectly in default dir: {default_yaml}"


def test_submit_without_project_dir_uses_default(http_setup):
    """POST submit without project_dir → writes to config.inputs_dir (back-compat)."""
    config, registry, port = http_setup

    registry.add(FormRecord(
        form_id="FORM-002",
        template="test",
        opened_at=datetime.now(timezone.utc),
        project_dir=None,
    ))

    data = b"selected_roles=reviewer"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-002",
        data=data,
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200

    default_yaml = config.inputs_dir / "FORM-002.yaml"
    assert default_yaml.exists(), f"YAML not in default dir: {default_yaml}"
    assert "reviewer" in default_yaml.read_text()


# ── A2 CSRF: Origin "null" from file:// pages ────────────────────────────────


class TestCSRFOriginNullFromFile:
    """A2 fix: forms opened via file:// send Origin: "null" (not "file://...").
    Browser standard behavior for sandboxed/local file origins.
    Must accept "null" only when Referer confirms file:// origin."""

    def test_origin_null_accepted_no_referer(self):
        """Origin: null + no Referer → accepted (Chrome strips Referer for file://)."""
        from agent_workflow_ui.http_endpoint import _is_origin_allowed
        assert _is_origin_allowed(origin="null", referer="")

    def test_origin_null_accepted_with_file_referer(self):
        """Origin: null + Referer: file:// → accepted."""
        from agent_workflow_ui.http_endpoint import _is_origin_allowed
        assert _is_origin_allowed(origin="null", referer="file:///tmp/form.html")

    def test_localhost_origin_accepted(self):
        """Origin: http://127.0.0.1:1234 → accepted."""
        from agent_workflow_ui.http_endpoint import _is_origin_allowed
        assert _is_origin_allowed(origin="http://127.0.0.1:1234", referer="")

    def test_evil_origin_rejected(self):
        """Origin: http://evil.com → rejected."""
        from agent_workflow_ui.http_endpoint import _is_origin_allowed
        assert not _is_origin_allowed(origin="http://evil.com", referer="")

    def test_no_origin_no_referer_accepted(self):
        """No Origin, no Referer (curl, non-browser) → accepted (backward compat)."""
        from agent_workflow_ui.http_endpoint import _is_origin_allowed
        assert _is_origin_allowed(origin="", referer="")
