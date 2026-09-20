"""Tests for HTTP endpoint: form submits, validation, idempotency."""
from __future__ import annotations

import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

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


def test_submit_write_failure_rolls_back_to_pending(http_setup, monkeypatch):
    """If YAML write fails after claim, form status rolls back to pending.

    Regression: without rollback, claim_for_submit puts the form in
    "submitting" state, and any subsequent POST is rejected with 409
    forever — user can never resubmit.
    """
    import agent_workflow_ui.http_endpoint as ep

    config, registry, port = http_setup
    registry.add(FormRecord(
        form_id="FORM-rollback",
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))

    def _fail_write(path, data):
        raise OSError("simulated disk full")

    monkeypatch.setattr(ep, "_atomic_write_yaml", _fail_write)

    data = b"foo=bar"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-rollback",
        data=data,
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 500

    # Critical: status must be back to "pending", not stuck in "submitting"
    record = registry.get("FORM-rollback")
    assert record.status == "pending", (
        f"Expected rollback to 'pending', got '{record.status}'. "
        "Form is stuck — user can't resubmit."
    )


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


class TestBuildPlanMdFromVariant:
    """Dogfood-7: increment-planning submit renders the chosen variant to plan.md."""

    def test_full_variant_rendered(self):
        from agent_workflow_ui.http_endpoint import _build_plan_md_from_variant

        selected = {
            "id": "A",
            "title": "Vertical slice",
            "strategy": "vertical",
            "description": "Each increment delivers user-visible value.",
            "estimated_todos": 3,
            "estimated_time": "2 weeks",
            "increments": [
                {"name": "I1: storyboard", "goal": "storyboard only", "artefacts": ["storyboard.md"]},
                {"goal": "jira integration"},
            ],
            "pros": ["fast feedback"],
            "cons": ["refactor overhead"],
        }
        variants = [selected, {"id": "B", "title": "Horizontal"}]

        md = _build_plan_md_from_variant(selected, variants)

        assert md.startswith("# Plan: Vertical slice")
        assert "**Strategy:** vertical" in md
        assert "Each increment delivers user-visible value." in md
        assert "_Estimated: ~3 TODOs, ~2 weeks_" in md
        assert "### 1. I1: storyboard" in md
        assert "**Artefacts:** storyboard.md" in md
        assert "### 2. Increment 2" in md
        assert "**Pros:**" in md and "- fast feedback" in md
        assert "**Cons:**" in md and "- refactor overhead" in md
        assert "## Other variants considered" in md
        assert "**B**: Horizontal" in md

    def test_minimal_variant_defaults(self):
        from agent_workflow_ui.http_endpoint import _build_plan_md_from_variant

        md = _build_plan_md_from_variant({}, [])

        assert "# Plan: Untitled plan" in md
        assert "## Increments" in md
        assert "Estimated" not in md
        assert "Trade-offs" not in md
        assert "Other variants" not in md

    def test_pros_without_cons(self):
        from agent_workflow_ui.http_endpoint import _build_plan_md_from_variant

        md = _build_plan_md_from_variant({"pros": ["speed"]}, [])
        assert "**Pros:**" in md
        assert "- speed" in md
        assert "**Cons:**" not in md


# ── FU-16 / AUD09-01: TTL enforced on the HTTP endpoint ───────────────────


def test_submit_after_ttl_rejected(http_setup):
    """POST after expires_at — with nobody having called read_submit/
    list_pending — must get 410, no submit file, status flipped to
    'expired'."""
    config, registry, port = http_setup
    registry.add(FormRecord(
        form_id="FORM-ttl",
        template="test",
        opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    ))

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-ttl",
        data=b"foo=bar",
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 410
    body = exc_info.value.read().decode()
    assert "expired" in body.lower()

    assert not (config.inputs_dir / "FORM-ttl.yaml").exists()
    assert registry.get("FORM-ttl").status == "expired"


def test_submit_already_expired_status_says_expired(http_setup):
    """Form whose status was already flipped to 'expired' (by a prior
    read_submit) → 410 with expiry text, not the misleading 409
    'being submitted by another request'."""
    config, registry, port = http_setup
    registry.add(FormRecord(
        form_id="FORM-exp",
        template="test",
        opened_at=datetime.now(timezone.utc),
        status="expired",
    ))

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-exp",
        data=b"foo=bar",
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 410
    body = exc_info.value.read().decode()
    assert "expired" in body.lower()
    assert "another request" not in body.lower()
    assert not (config.inputs_dir / "FORM-exp.yaml").exists()


# ── FU-16 / AUD09-07: malformed Content-Length ───────────────────────────


def _raw_post(port: int, raw_request: bytes, half_close: bool = False,
              timeout: float = 5.0) -> bytes:
    """Send a raw request and return the full response (read to EOF)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(raw_request)
        if half_close:
            s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def test_negative_content_length_rejected(http_setup):
    """Content-Length: -1 → 400 immediately (old code: read(-1) blocked
    the handler thread until the client closed the connection)."""
    config, registry, port = http_setup
    registry.add(FormRecord(
        form_id="FORM-neg",
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))

    raw = (
        b"POST /submit/FORM-neg HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\n"
        b"Content-Length: -1\r\n"
        b"\r\n"
        b"foo=bar"
    )
    resp = _raw_post(port, raw)

    assert b" 400 " in resp.split(b"\r\n", 1)[0], resp.split(b"\r\n", 1)[0]
    assert not (config.inputs_dir / "FORM-neg.yaml").exists()
    assert registry.get("FORM-neg").status == "pending"


def test_short_body_not_submitted(http_setup):
    """Declared Content-Length: 5, only 1 byte sent + EOF → 400 'Short
    body'. Old code continued with the truncated body and submitted the
    form with an empty payload."""
    config, registry, port = http_setup
    registry.add(FormRecord(
        form_id="FORM-short",
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))

    raw = (
        b"POST /submit/FORM-short HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\n"
        b"Content-Length: 5\r\n"
        b"\r\n"
        b"a"
    )
    resp = _raw_post(port, raw, half_close=True)

    assert b" 400 " in resp.split(b"\r\n", 1)[0], resp.split(b"\r\n", 1)[0]
    assert b"Short body" in resp
    assert not (config.inputs_dir / "FORM-short.yaml").exists()
    assert registry.get("FORM-short").status == "pending"


# ── FU-16 / AUD09-05: crash-resubmit through the HTTP path itself ─────────


def test_stale_submitting_reclaimable_via_http(http_setup):
    """AUD09-05 completion criterion at the HTTP level (audit proposal
    ``test_stale_submitting_reclaimable_via_http``): the claiming process
    crashed between claim and finalize 11 minutes ago and nobody called
    list_pending. A browser resubmit must be accepted (200 + file), not
    409'ed — the registry-level reclaim is what do_POST relies on."""
    config, registry, port = http_setup
    registry.add(FormRecord(
        form_id="FORM-crash",
        template="test",
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=12),
        status="submitting",
        claimed_at=datetime.now(timezone.utc) - timedelta(minutes=11),
    ))

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/FORM-crash",
        data=b"foo=bar",
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200

    assert (config.inputs_dir / "FORM-crash.yaml").exists()
    assert registry.get("FORM-crash").status == "submitted"
