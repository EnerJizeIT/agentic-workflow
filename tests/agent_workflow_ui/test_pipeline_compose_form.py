"""M7.1 (TODO-0150): pipeline-compose form — render, whitelist, prefill, apply.

Render: the template renders from the plugin defaults (full and minimal
context), hostile stage data round-trips through the <script> JSON
literal (AUD09-03 class), policy datalists mirror the awf/pipeline.py
registry. Whitelist: open_form accepts the template and keeps rejecting
unknown ones. Prefill: the form opens prefilled from the selected (or
active) pipeline file; a traversal name falls back to the active
pipeline, a broken file degrades to an empty stage list. Apply: the
submit is materialized through awf.api.write_pipeline (the single stage
registry — no duplicated validation) with the force checkbox, and every
result carries the active-run warning (awf_run_revise).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from agent_workflow_ui.http_endpoint import apply_form_submit
from agent_workflow_ui.render.engine import create_default_env, render_template
from agent_workflow_ui.tools.forms import _populate_pipeline_compose, open_form

import agent_workflow_ui as _awui

DEFAULT_TEMPLATES_DIR = Path(_awui.__file__).parent / "render" / "default_templates"


def _render(**overrides) -> str:
    env = create_default_env()
    context = {
        "form_id": "FORM-test",
        "submit_url": "http://127.0.0.1:1/submit/FORM-test",
        "pipeline_names": ["audit-llm", "default"],
        "active_pipeline": "default",
        "initial_pipeline": "default",
        "initial_stages": [
            {"role": "supervisor", "name": "plan", "on_approved": "next"},
            {"role": "agent-implementer", "id": "impl", "task": "does the work", "max_retries": 3},
        ],
        "existing_pipeline": True,
        "project_roles": [{"id": "agent-implementer", "title": "agent-implementer"}],
        "policy_options": {"on_blocked": ["escalate", "stop"], "on_approved": ["next", "commit_and_next", "commit_and_report"]},
        "pipelines_dir": "/proj/.agentic/pipelines",
    }
    context.update(overrides)
    return render_template(env, "pipeline-compose", context)


def _script_json(rendered: str, var: str):
    script = rendered.split("<script>", 1)[1].split("</script>", 1)[0]
    for line in script.splitlines():
        if line.strip().startswith(f"var {var} ="):
            return json.loads(line.strip()[len(f"var {var} ="):].rstrip(";").strip())
    pytest.fail(f"var {var} = not found as a single-line assignment")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal awf project: two pipelines + one role file."""
    (tmp_path / ".agentic" / "pipelines").mkdir(parents=True)
    (tmp_path / ".agentic" / "roles").mkdir()
    default = tmp_path / ".agentic" / "pipelines" / "default.yaml"
    default.write_text(yaml.safe_dump({
        "name": "default",
        "stages": [
            {"name": "plan", "role": "supervisor", "on_approved": "next"},
            {"name": "impl", "role": "agent-implementer", "task": "does the work"},
        ],
    }, sort_keys=False), encoding="utf-8")
    (tmp_path / ".agentic" / "pipelines" / "audit-llm.yaml").write_text(yaml.safe_dump({
        "name": "audit-llm",
        "stages": [{"role": "agent-security-auditor", "max_retries": 2}],
    }, sort_keys=False), encoding="utf-8")
    (tmp_path / ".agentic" / "roles" / "agent-implementer.md").write_text("# role\n", encoding="utf-8")
    return tmp_path


# ── Render ──────────────────────────────────────────────────────────────


def test_template_renders_form_body():
    html = _render()
    assert 'id="compose-form"' in html
    assert 'id="preview-btn"' in html
    assert 'id="submit-btn"' in html
    assert "awf_run_revise" in html
    assert len(html) > 5000


def test_template_renders_with_minimal_context():
    env = create_default_env()
    html = render_template(env, "pipeline-compose", {
        "form_id": "FORM-min",
        "submit_url": "http://127.0.0.1:1/submit/FORM-min",
    })
    assert 'id="compose-form"' in html
    assert len(html) > 5000


def test_initial_stages_round_trip():
    stages = _script_json(_render(), "initialStages")
    assert stages == [
        {"role": "supervisor", "name": "plan", "on_approved": "next"},
        {"role": "agent-implementer", "id": "impl", "task": "does the work", "max_retries": 3},
    ]


def test_hostile_stage_data_survives_script():
    """Quotes / backslashes / newlines in stage values must not break the
    <script> block — the array is a single tojson literal (AUD09-03 class)."""
    html = _render(initial_stages=[
        {"role": "qa'", "task": "x\\y\nz\"w", "on_blocked": "escalate"},
    ])
    stages = _script_json(html, "initialStages")
    assert stages[0]["role"] == "qa'"
    assert stages[0]["task"] == "x\\y\nz\"w"


def test_policy_datalists_come_from_registry():
    """The datalists are rendered from the awf/pipeline.py registry — the
    form offers exactly the words the shared validator accepts."""
    from awf.pipeline import POLICY_ALLOWED

    html = _render(policy_options={k: list(v) for k, v in POLICY_ALLOWED.items()})
    for key, options in POLICY_ALLOWED.items():
        assert f'id="pol-{key}"' in html
        for word in options:
            assert f'<option value="{word}">' in html


def test_existing_pipeline_marks_force_checked():
    def force_attr(html: str) -> str:
        return html.split('id="force-cb"', 1)[1].split(">", 1)[0]

    assert "checked" in force_attr(_render(existing_pipeline=True))
    assert "checked" not in force_attr(_render(existing_pipeline=False))


# ── Whitelist + prefill ─────────────────────────────────────────────────


def test_open_form_whitelist_accepts_pipeline_compose(plugin_initialized, project):
    result = asyncio.run(open_form("pipeline-compose", {"pipeline": "default"}, str(project)))
    assert not result.get("error"), result
    assert result["form_id"]


def test_open_form_whitelist_still_rejects_unknown(plugin_initialized, project):
    result = asyncio.run(open_form("pipeline-compose-evil", {}, str(project)))
    assert "Unknown template" in str(result.get("error", ""))


def test_populate_prefills_active_pipeline(project):
    ctx = _populate_pipeline_compose({}, project)
    assert ctx["pipeline_names"] == ["audit-llm", "default"]
    assert ctx["active_pipeline"] == "default"
    assert ctx["initial_pipeline"] == "default"
    assert ctx["existing_pipeline"] is True
    assert ctx["initial_stages"][0]["role"] == "supervisor"
    assert ctx["project_roles"] == [{"id": "agent-implementer", "title": "agent-implementer"}]


def test_populate_explicit_pipeline(project):
    ctx = _populate_pipeline_compose({"pipeline": "audit-llm"}, project)
    assert ctx["initial_pipeline"] == "audit-llm"
    assert ctx["existing_pipeline"] is True
    assert ctx["initial_stages"] == [{"role": "agent-security-auditor", "max_retries": 2}]


def test_populate_new_name_is_not_existing(project):
    ctx = _populate_pipeline_compose({"pipeline": "brand-new"}, project)
    assert ctx["initial_pipeline"] == "brand-new"
    assert ctx["existing_pipeline"] is False
    assert ctx["initial_stages"] == []


def test_populate_traversal_name_falls_back_to_active(project):
    """The prefill name is public input — a traversal must not be read."""
    ctx = _populate_pipeline_compose({"pipeline": "../evil"}, project)
    assert ctx["initial_pipeline"] == "default"
    assert ctx["existing_pipeline"] is True


def test_populate_broken_yaml_degrades_to_empty(project):
    (project / ".agentic" / "pipelines" / "default.yaml").write_text("stages: [unclosed", encoding="utf-8")
    ctx = _populate_pipeline_compose({}, project)
    assert ctx["existing_pipeline"] is True
    assert ctx["initial_stages"] == []


def test_populate_policy_options_match_registry():
    from awf.pipeline import POLICY_ALLOWED

    ctx = _populate_pipeline_compose({}, None)
    assert ctx["policy_options"] == {k: list(v) for k, v in POLICY_ALLOWED.items()}


# ── Apply (submit → write_pipeline) ─────────────────────────────────────


def _apply(data: dict, project: Path):
    return apply_form_submit("pipeline-compose", data, project)


def test_apply_writes_pipeline_file(project):
    result = _apply({
        "pipeline_name": "audit-llm-2",
        "force": "",
        "stages_json": json.dumps([
            {"role": "supervisor"},
            {"role": "agent-implementer", "id": "impl", "task": "does the work", "max_retries": 3},
        ]),
    }, project)
    assert result.ok, result.errors
    target = project / ".agentic" / "pipelines" / "audit-llm-2.yaml"
    assert target.is_file()
    doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert doc["name"] == "audit-llm-2"
    assert doc["stages"][1]["task"] == "does the work"
    # The written file is a valid pipeline document (same loader).
    from awf.pipeline import load_stages
    assert [st.role for st in load_stages(target)] == ["supervisor", "agent-implementer"]
    # The active-run warning is always present.
    assert any("awf_run_revise" in w for w in result.warnings)


def test_apply_refuses_existing_without_force(project):
    target = project / ".agentic" / "pipelines" / "default.yaml"
    before = target.read_bytes()
    result = _apply({
        "pipeline_name": "default",
        "force": "",
        "stages_json": json.dumps([{"role": "supervisor"}]),
    }, project)
    assert not result.ok
    assert any("already exists" in e for e in result.errors)
    assert target.read_bytes() == before


def test_apply_overwrites_with_force(project):
    result = _apply({
        "pipeline_name": "default",
        "force": "on",
        "stages_json": json.dumps([{"role": "agent-security-auditor"}]),
    }, project)
    assert result.ok, result.errors
    doc = yaml.safe_load((project / ".agentic" / "pipelines" / "default.yaml").read_text(encoding="utf-8"))
    # write_pipeline normalizes: role slug + name defaults to the role slug.
    assert doc["stages"] == [{"role": "agent-security-auditor", "name": "agent-security-auditor"}]


def test_apply_no_project_dir_warns():
    result = _apply({"pipeline_name": "x", "stages_json": "[{\"role\": \"supervisor\"}]"}, None)
    assert result.ok
    assert any("project_dir" in w for w in result.warnings)
    assert any("awf_run_revise" in w for w in result.warnings)


def test_apply_unknown_key_rejected_by_shared_validator(project):
    """No duplicated validation: an unknown stage key is refused by the
    same shared registry the write path uses."""
    result = _apply({
        "pipeline_name": "bad",
        "stages_json": json.dumps([{"role": "supervisor", "action": "build"}]),
    }, project)
    assert not result.ok
    assert any("unknown keys" in e and "action" in e for e in result.errors)
    assert not (project / ".agentic" / "pipelines" / "bad.yaml").exists()


def test_apply_invalid_json_rejected(project):
    result = _apply({"pipeline_name": "x", "stages_json": "{not json"}, project)
    assert not result.ok
    assert any("not valid JSON" in e for e in result.errors)


def test_apply_empty_stages_rejected(project):
    result = _apply({"pipeline_name": "x", "stages_json": "[]"}, project)
    assert not result.ok
    assert any("no stages" in e for e in result.errors)


def test_apply_traversal_name_rejected(project):
    result = _apply({
        "pipeline_name": "../evil",
        "stages_json": json.dumps([{"role": "supervisor"}]),
    }, project)
    assert not result.ok
    assert not (project / ".agentic" / "pipelines" / ".." / "evil.yaml").exists()


def test_apply_dispatch_routes_pipeline_compose(project):
    """apply_form_submit is the single dispatch point for the template."""
    direct = _apply({"pipeline_name": "via-dispatch", "stages_json": "[{\"role\": \"supervisor\"}]"}, project)
    from agent_workflow_ui.http_endpoint import _apply_pipeline_compose_submit
    (project / ".agentic" / "pipelines" / "via-dispatch.yaml").unlink()
    ref = _apply_pipeline_compose_submit({"pipeline_name": "via-dispatch", "stages_json": "[{\"role\": \"supervisor\"}]"}, project)
    assert direct.to_dict() == ref.to_dict()


# ── End-to-end: open_form renders the prefilled HTML ────────────────────


@pytest.fixture
def plugin_initialized(tmp_path, monkeypatch):
    """Plugin state so open_form runs headless (browser mocked, temp in tmp)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_TEMP_DIR", str(tmp_path / "tmp"))
    from agent_workflow_ui.config import ensure_directories, load
    from agent_workflow_ui.http_endpoint import _find_free_port
    from agent_workflow_ui.render.engine import create_env
    from agent_workflow_ui.state import reset_registry, set_config, set_http_port, set_jinja_env

    config = load()
    ensure_directories(config)
    set_config(config)
    set_http_port(_find_free_port())
    set_jinja_env(create_env([config.templates_dir, DEFAULT_TEMPLATES_DIR]))
    reset_registry()

    import agent_workflow_ui.browser as browser_mod
    import agent_workflow_ui.tools.forms as forms_mod

    def _fake_open_path(target, command="auto"):
        return True, "mocked"

    monkeypatch.setattr(browser_mod, "open_path", _fake_open_path, raising=True)
    monkeypatch.setattr(forms_mod, "open_path", _fake_open_path, raising=True)
    return config


def test_open_form_renders_prefilled_html(plugin_initialized, project):
    result = asyncio.run(open_form(
        "pipeline-compose", {"pipeline": "audit-llm"}, str(project),
    ))
    assert not result.get("error"), result
    # The rendered temp file: config.temp_dir / agent-workflow-ui-<form_id>.html
    from agent_workflow_ui.state import get_config

    rendered = (get_config().temp_dir / f"agent-workflow-ui-{result['form_id']}.html").read_text(encoding="utf-8")
    stages = _script_json(rendered, "initialStages")
    assert stages == [{"role": "agent-security-auditor", "max_retries": 2}]
    assert "value=\"audit-llm\"" in rendered
