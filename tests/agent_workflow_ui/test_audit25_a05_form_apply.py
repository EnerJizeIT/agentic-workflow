"""A-05 (аудит 2026-09-25, слой 6): статус формы различает «принято» и
«применено».

До фикса HTTP submit сохранял payload, вызывал finalize_submit, а ошибка
apply_project_setup / apply_increment_plan только логировалась: браузер
получал 200 «Форма отправлена», read_submit ошибку не показывал, повторная
отправка уже применённой формы материализацию не запускала.

После фикса:
- submit-файл несёт apply_status (received → applied/failed) и apply_result
  (ok, applied, errors, warnings, attempts);
- ошибка видна в read_submit и в ответе браузеру;
- повторный submit завершает применение из сохранённого payload ровно один
  раз (маркер applied не задваивается).
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pytest
import yaml
from agent_workflow_ui.config import load
from agent_workflow_ui.http_endpoint import start_http_server
from agent_workflow_ui.state import (
    FormRecord,
    FormRegistry,
    get_registry,
    reset_registry,
    set_config,
)


@pytest.fixture
def a05_setup(tmp_path, monkeypatch):
    """HTTP server + global config/registry, so read_submit works too."""
    monkeypatch.chdir(tmp_path)
    config = load()
    config.inputs_dir.mkdir(parents=True, exist_ok=True)
    set_config(config)
    reset_registry()
    registry = FormRegistry()
    server, port = start_http_server(config, registry)
    yield config, registry, port
    server.shutdown()


def _make_project(tmp_path):
    """Minimal awf project: .agentic/ + config.yaml + supervisor.md."""
    proj = tmp_path / "proj"
    (proj / ".agentic" / "roles").mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "test", "root": "."},
                "models": {
                    "supervisor": {"description": "current session"},
                    "worker": {"agent_name": "worker", "model": "vllm/llm"},
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (proj / ".agentic" / "roles" / "supervisor.md").write_text("# Supervisor\n", encoding="utf-8")
    return proj


def _add_form(registry, form_id, template, project_dir):
    record = FormRecord(
        form_id=form_id,
        template=template,
        opened_at=datetime.now(timezone.utc),
        project_dir=project_dir,
    )
    registry.add(record)
    # read_submit reads the GLOBAL registry — the handler works with the
    # bound one; the same object in both keeps them in sync.
    get_registry().add(record)
    return record


def _post(port, form_id, fields: dict):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/{form_id}",
        data=body,
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _read_submit(form_id):
    from agent_workflow_ui.tools.forms import read_submit

    return asyncio.run(read_submit(form_id))


def _submit_fields() -> dict:
    return {
        "team_config": json.dumps([{"type": "default", "agent": "worker"}]),
        "context_message": "ctx",
    }


def _submit_file(proj, form_id):
    return proj / ".agentic" / "inputs" / f"{form_id}.yaml"


def test_apply_failure_surfaces_in_submit_status(a05_setup, tmp_path, monkeypatch):
    """A failed materialization is recorded as failed with the reason —
    visible in the submit file, in read_submit and in the browser page.
    Baseline: 200 + «Форма отправлена», no apply info anywhere."""
    config, registry, port = a05_setup
    proj = _make_project(tmp_path)
    form_id = "FORM-a05fail"
    _add_form(registry, form_id, "project-setup", proj)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated apply failure")

    monkeypatch.setattr("awf.api.apply_project_setup", _boom)

    status, page = _post(port, form_id, _submit_fields())
    assert status == 200

    payload = yaml.safe_load(_submit_file(proj, form_id).read_text(encoding="utf-8"))
    assert payload["apply_status"] == "failed"
    assert any("simulated apply failure" in e for e in payload["apply_result"]["errors"])

    r = _read_submit(form_id)
    assert r["submitted"] is True
    assert r["apply_status"] == "failed"
    assert any("simulated apply failure" in e for e in r["apply_result"]["errors"])

    assert "Форма отправлена!" not in page
    assert "не удалось" in page


def test_reapply_from_payload_completes_once(a05_setup, tmp_path, monkeypatch):
    """After a failed apply, a resubmit completes the apply from the SAVED
    payload exactly once; a further resubmit reports the completed apply
    without re-running it (no doubled marker, no extra call)."""
    config, registry, port = a05_setup
    proj = _make_project(tmp_path)
    form_id = "FORM-a05replay"
    _add_form(registry, form_id, "project-setup", proj)

    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        from awf.api.setup import apply_project_setup as real

        return real(*args, **kwargs)

    monkeypatch.setattr("awf.api.apply_project_setup", _flaky)

    # 1st submit: apply fails → failed with attempts=1
    status1, _ = _post(port, form_id, _submit_fields())
    assert status1 == 200
    p1 = yaml.safe_load(_submit_file(proj, form_id).read_text(encoding="utf-8"))
    assert p1["apply_status"] == "failed"
    assert p1["apply_result"]["attempts"] == 1

    # 2nd submit: re-apply from the saved payload completes it.
    # The new body is irrelevant — the saved data is used.
    status2, page2 = _post(port, form_id, {"unrelated": "body"})
    assert status2 == 200
    p2 = yaml.safe_load(_submit_file(proj, form_id).read_text(encoding="utf-8"))
    assert p2["apply_status"] == "applied"
    assert p2["apply_result"]["ok"] is True
    assert p2["apply_result"]["attempts"] == 2
    assert p2["apply_result"]["errors"] == []
    assert "worker" in json.dumps(p2["data"]), "saved payload's data must be applied"
    assert (proj / ".agentic" / "pipelines" / "default.yaml").exists()
    assert "уже применена" in page2

    # 3rd submit: already applied — no extra apply run, marker not doubled.
    status3, _ = _post(port, form_id, {"unrelated": "again"})
    assert status3 == 200
    p3 = yaml.safe_load(_submit_file(proj, form_id).read_text(encoding="utf-8"))
    assert p3["apply_status"] == "applied"
    assert p3["apply_result"]["attempts"] == 2
    assert calls["n"] == 2, "apply must run exactly once after the failure"


def test_normal_submit_is_applied(a05_setup, tmp_path):
    """Happy path: submit → applied with the parts that were materialized."""
    config, registry, port = a05_setup
    proj = _make_project(tmp_path)
    form_id = "FORM-a05ok"
    _add_form(registry, form_id, "project-setup", proj)

    status, page = _post(port, form_id, _submit_fields())
    assert status == 200

    payload = yaml.safe_load(_submit_file(proj, form_id).read_text(encoding="utf-8"))
    assert payload["apply_status"] == "applied"
    assert payload["apply_result"]["ok"] is True
    assert payload["apply_result"]["attempts"] == 1
    assert payload["apply_result"]["errors"] == []
    assert payload["apply_result"]["applied"], "materialized parts must be listed"

    r = _read_submit(form_id)
    assert r["submitted"] is True
    assert r["apply_status"] == "applied"
    assert r["apply_result"]["ok"] is True

    assert "Форма применена" in page


def test_resubmit_of_applied_form_does_not_reapply(a05_setup, tmp_path, monkeypatch):
    """A resubmit of an already-APPLIED form reports the completed apply
    and does not run the materialization again."""
    config, registry, port = a05_setup
    proj = _make_project(tmp_path)
    form_id = "FORM-a05dup"
    _add_form(registry, form_id, "project-setup", proj)

    calls = {"n": 0}

    def _count(*args, **kwargs):
        calls["n"] += 1
        from awf.api.setup import apply_project_setup as real

        return real(*args, **kwargs)

    monkeypatch.setattr("awf.api.apply_project_setup", _count)

    status1, _ = _post(port, form_id, _submit_fields())
    assert status1 == 200
    assert calls["n"] == 1

    status2, page2 = _post(port, form_id, {"unrelated": "body"})
    assert status2 == 200
    payload = yaml.safe_load(_submit_file(proj, form_id).read_text(encoding="utf-8"))
    assert payload["apply_status"] == "applied"
    assert payload["apply_result"]["attempts"] == 1, "no duplicated apply marker"
    assert calls["n"] == 1, "apply must not re-run for an applied form"
    assert "уже применена" in page2


def test_increment_plan_failure_surfaces_in_submit_status(a05_setup, tmp_path, monkeypatch):
    """increment-planning: a failed apply_increment_plan is recorded as
    failed (the same defect at the second call site)."""
    config, registry, port = a05_setup
    proj = _make_project(tmp_path)
    (proj / ".agentic" / "phases").mkdir(parents=True, exist_ok=True)
    form_id = "FORM-a05inc"
    _add_form(registry, form_id, "increment-planning", proj)

    variants = [{"id": "A", "title": "Vertical slice", "strategy": "vertical"}]

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated plan failure")

    monkeypatch.setattr("awf.api.apply_increment_plan", _boom)

    status, page = _post(
        port,
        form_id,
        {"selected_variant": "A", "variants_json": json.dumps(variants)},
    )
    assert status == 200

    payload = yaml.safe_load(_submit_file(proj, form_id).read_text(encoding="utf-8"))
    assert payload["apply_status"] == "failed"
    assert any("simulated plan failure" in e for e in payload["apply_result"]["errors"])

    r = _read_submit(form_id)
    assert r["apply_status"] == "failed"
    assert any("simulated plan failure" in e for e in r["apply_result"]["errors"])
    assert "не удалось" in page


def test_read_submit_legacy_file_reports_applied(a05_setup, tmp_path):
    """Legacy submit file (no apply_status key, written before A-05):
    read_submit keeps working and reports the historical default."""
    config, registry, port = a05_setup
    proj = _make_project(tmp_path)
    form_id = "FORM-a05legacy"
    _add_form(registry, form_id, "project-setup", proj)
    (proj / ".agentic" / "inputs").mkdir(parents=True, exist_ok=True)
    _submit_file(proj, form_id).write_text(
        yaml.safe_dump(
            {
                "form_id": form_id,
                "template": "project-setup",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "data": {"selected_roles": ["worker"]},
            }
        ),
        encoding="utf-8",
    )
    get_registry().update_status(form_id, "submitted")

    r = _read_submit(form_id)
    assert r["submitted"] is True
    assert r["apply_status"] == "applied"
    assert "apply_result" not in r
