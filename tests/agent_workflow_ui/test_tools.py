"""Tests for MCP tools: open_form, read_submit, cancel_form, list_pending_forms, list_templates."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from agent_workflow_ui.config import ensure_directories, load
from agent_workflow_ui.render.engine import create_env
from agent_workflow_ui.state import (
    get_registry,
    reset_registry,
    set_config,
    set_http_port,
    set_jinja_env,
)

import agent_workflow_ui as _awui

DEFAULT_TEMPLATES_DIR = Path(_awui.__file__).parent / "render" / "default_templates"


@pytest.fixture
def plugin_setup(tmp_path, monkeypatch):
    """Initialize plugin state in tmp_path.

    CRITICAL: monkeypatch browser.open_path to a no-op so tests don't actually
    open real browser tabs. Without this, every test that calls open_form
    spawns xdg-open and pollutes the user's browser.

    Also redirects temp_dir to tmp_path so rendered HTML files don't leak
    into /tmp/ after the test run.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_TEMP_DIR", str(tmp_path / "tmp"))
    config = load()
    ensure_directories(config)
    set_config(config)
    set_http_port(13747)
    set_jinja_env(create_env([config.templates_dir, DEFAULT_TEMPLATES_DIR]))
    reset_registry()

    # Mock browser.open_path so tests are hermetic — don't open real browser.
    def _fake_open_path(target, command="auto"):
        return True, f"mocked open for {target}"

    import agent_workflow_ui.tools.forms as forms_mod
    monkeypatch.setattr(forms_mod, "open_path", _fake_open_path, raising=True)
    # Also patch at browser module level in case other code imports it directly.
    import agent_workflow_ui.browser as browser_mod
    monkeypatch.setattr(browser_mod, "open_path", _fake_open_path, raising=True)

    return config


# --- open_form ---

def test_open_form_returns_form_id(plugin_setup):
    from agent_workflow_ui.tools.forms import open_form
    result = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    assert result["form_id"].startswith("FORM-")
    assert "browser_opened" in result
    assert "submit_url" in result
    assert result["form_id"] in result["submit_url"]


def test_open_form_increments_form_id(plugin_setup):
    from agent_workflow_ui.tools.forms import open_form
    r1 = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    r2 = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    assert r1["form_id"] != r2["form_id"]


def test_open_form_unknown_template(plugin_setup):
    from agent_workflow_ui.tools.forms import open_form
    result = asyncio.run(open_form(template="nonexistent", data={}))
    assert "error" in result
    assert "not found" in result["error"].lower() or "nonexistent" in result["error"]


def test_open_form_with_ttl(plugin_setup):
    from agent_workflow_ui.tools.forms import open_form
    result = asyncio.run(open_form(
        template="project-setup",
        data={"available_roles": ["worker"]},
        ttl_seconds=60,
    ))
    assert "expires_at" in result


def test_open_form_registers_in_registry(plugin_setup):
    from agent_workflow_ui.tools.forms import open_form
    result = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    record = get_registry().get(result["form_id"])
    assert record is not None
    assert record.template == "project-setup"
    assert record.status == "pending"


def test_open_form_no_data(plugin_setup):
    from agent_workflow_ui.tools.forms import open_form
    result = asyncio.run(open_form(template="project-setup"))
    assert result["form_id"].startswith("FORM-")
    assert "submit_url" in result


# --- read_submit ---

def test_read_submit_pending(plugin_setup):
    from agent_workflow_ui.tools.forms import open_form, read_submit
    open_result = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    r = asyncio.run(read_submit(open_result["form_id"]))
    assert r["submitted"] is False
    assert r["status"] == "pending"


def test_read_submit_after_yaml_written(plugin_setup):
    from agent_workflow_ui.tools.forms import open_form, read_submit
    open_result = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    form_id = open_result["form_id"]

    submit_data = {
        "form_id": form_id,
        "template": "project-setup",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "data": {"selected_roles": ["worker", "reviewer"]},
    }
    submit_file = plugin_setup.inputs_dir / f"{form_id}.yaml"
    submit_file.write_text(yaml.safe_dump(submit_data))
    get_registry().update_status(form_id, "submitted")

    r = asyncio.run(read_submit(form_id))
    assert r["submitted"] is True
    assert r["data"]["selected_roles"] == ["worker", "reviewer"]


def test_read_submit_unknown_form_id(plugin_setup):
    from agent_workflow_ui.tools.forms import read_submit
    r = asyncio.run(read_submit("FORM-999"))
    assert r["submitted"] is False
    assert r["status"] == "unknown"
    assert "error" in r


# --- cancel_form ---

def test_cancel_form(plugin_setup):
    from agent_workflow_ui.tools.forms import cancel_form, open_form, read_submit
    open_result = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    form_id = open_result["form_id"]

    r = asyncio.run(cancel_form(form_id))
    assert r["cancelled"] is True

    r = asyncio.run(read_submit(form_id))
    assert r["status"] == "cancelled"


def test_cancel_form_already_submitted(plugin_setup):
    from agent_workflow_ui.tools.forms import cancel_form, open_form
    open_result = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    form_id = open_result["form_id"]
    get_registry().update_status(form_id, "submitted")

    r = asyncio.run(cancel_form(form_id))
    assert r["cancelled"] is False
    assert r["reason"] == "already_submitted"


def test_cancel_form_unknown(plugin_setup):
    from agent_workflow_ui.tools.forms import cancel_form
    r = asyncio.run(cancel_form("FORM-999"))
    assert r["cancelled"] is False
    assert r["reason"] == "not_found"


# --- list_pending_forms ---

def test_list_pending_forms_initially_empty(plugin_setup):
    from agent_workflow_ui.tools.forms import list_pending_forms
    r = asyncio.run(list_pending_forms())
    assert r["count"] == 0
    assert r["pending"] == []


def test_list_pending_forms_after_open(plugin_setup):
    from agent_workflow_ui.tools.forms import list_pending_forms, open_form
    asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    asyncio.run(open_form(template="project-setup", data={"available_roles": ["reviewer"]}))

    r = asyncio.run(list_pending_forms())
    assert r["count"] == 2
    # Both forms use the same template name; verify count via instances.
    assert all(p["template"] == "project-setup" for p in r["pending"])
    assert all("age_seconds" in p for p in r["pending"])


def test_list_pending_forms_excludes_cancelled(plugin_setup):
    from agent_workflow_ui.tools.forms import cancel_form, list_pending_forms, open_form
    r1 = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    asyncio.run(open_form(template="project-setup", data={"available_roles": ["reviewer"]}))
    asyncio.run(cancel_form(r1["form_id"]))

    r = asyncio.run(list_pending_forms())
    assert r["count"] == 1
    assert r["pending"][0]["template"] == "project-setup"


# --- list_templates ---

def test_list_templates_returns_default_templates(plugin_setup):
    from agent_workflow_ui.tools.templates import list_templates
    r = asyncio.run(list_templates())
    names = {t["name"] for t in r["templates"]}
    # Only project-setup ships as a default template (audit fix: 5 reserved
    # templates removed — they had no submit handlers, risk of silent data loss).
    expected = {"project-setup"}
    assert expected.issubset(names), f"Missing: {expected - names}"


def test_list_templates_each_has_metadata(plugin_setup):
    from agent_workflow_ui.tools.templates import list_templates
    r = asyncio.run(list_templates())
    for t in r["templates"]:
        assert "description" in t
        assert t["description"]
        assert "source" in t
        assert t["source"] in ("default", "project")
        assert "required_data_keys" in t
        assert "optional_data_keys" in t


def test_list_templates_project_overrides_default(plugin_setup, tmp_path):
    """Project templates override default by name."""
    from agent_workflow_ui.tools.templates import list_templates

    project_template = plugin_setup.templates_dir / "project-setup.html.j2"
    project_template.write_text("""---
description: PROJECT OVERRIDE
required_data_keys:
  - foo
---
<html></html>
""")

    set_jinja_env(create_env([plugin_setup.templates_dir, DEFAULT_TEMPLATES_DIR]))

    r = asyncio.run(list_templates())
    role_template = next(t for t in r["templates"] if t["name"] == "project-setup")
    assert role_template["source"] == "project"
    assert role_template["description"] == "PROJECT OVERRIDE"


# --- Task 2: forms.py coverage gaps ---

def test_read_submit_lazy_expiration(plugin_setup):
    """read_submit returns status='expired' when TTL passed."""
    from datetime import timedelta

    from agent_workflow_ui.state import get_registry
    from agent_workflow_ui.tools.forms import open_form, read_submit

    result = asyncio.run(open_form(
        template="project-setup",
        data={"available_roles": ["worker"]},
        ttl_seconds=1,
    ))
    form_id = result["form_id"]

    record = get_registry().get(form_id)
    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)

    r = asyncio.run(read_submit(form_id))
    assert r["status"] == "expired"
    assert r["submitted"] is False


def test_list_pending_forms_excludes_expired(plugin_setup):
    """Expired forms filtered out from list_pending_forms."""
    from datetime import timedelta

    from agent_workflow_ui.state import get_registry
    from agent_workflow_ui.tools.forms import list_pending_forms, open_form

    asyncio.run(open_form(
        template="project-setup",
        data={"available_roles": ["worker"]},
        ttl_seconds=1,
    ))
    for rec in get_registry()._forms.values():
        rec.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)

    r = asyncio.run(list_pending_forms())
    assert r["count"] == 0


def test_open_form_no_http_port_returns_error(plugin_setup, monkeypatch):
    """open_form returns error when HTTP endpoint not started."""
    from agent_workflow_ui.state import get_http_port, set_http_port
    from agent_workflow_ui.tools.forms import open_form

    saved = get_http_port()
    set_http_port(None)
    result = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    set_http_port(saved)

    assert "error" in result
    assert "not started" in result["error"].lower()


def test_read_submit_malformed_yaml(plugin_setup):
    """read_submit returns error status when submit file has invalid YAML."""
    from agent_workflow_ui.tools.forms import open_form, read_submit

    open_result = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    form_id = open_result["form_id"]

    submit_file = plugin_setup.inputs_dir / f"{form_id}.yaml"
    submit_file.write_text("not: valid: yaml: [")

    r = asyncio.run(read_submit(form_id))
    assert r["submitted"] is False
    assert r["status"] == "error"
    assert "error" in r


def test_read_submit_non_dict_yaml(plugin_setup):
    """read_submit returns error when submit file is valid YAML but not a dict."""
    from agent_workflow_ui.tools.forms import open_form, read_submit

    open_result = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    form_id = open_result["form_id"]

    submit_file = plugin_setup.inputs_dir / f"{form_id}.yaml"
    submit_file.write_text("- just a list\n- not a dict")

    r = asyncio.run(read_submit(form_id))
    assert r["submitted"] is False
    assert r["status"] == "error"


def test_open_form_browser_open_fails(plugin_setup, monkeypatch):
    """open_form sets error when browser open fails."""
    from agent_workflow_ui.tools.forms import open_form

    def _fail_open(target, command="auto"):
        return False, "xdg-open not found"

    import agent_workflow_ui.browser as browser_mod
    monkeypatch.setattr(browser_mod, "open_path", _fail_open, raising=True)
    import agent_workflow_ui.tools.forms as forms_mod
    monkeypatch.setattr(forms_mod, "open_path", _fail_open, raising=True)

    result = asyncio.run(open_form(template="project-setup", data={"available_roles": ["worker"]}))
    assert result["browser_opened"] is False
    assert "error" in result
    assert "xdg-open" in result["error"]


# --- Task 3: templates.py coverage gaps ---

def test_list_templates_malformed_template_skipped(plugin_setup, monkeypatch):
    """Malformed template that raises during parsing is skipped silently."""
    from agent_workflow_ui.tools.templates import list_templates

    bad = plugin_setup.templates_dir / "malformed.html.j2"
    bad.write_text("<html></html>")

    import agent_workflow_ui.tools.templates as tpl_mod
    real_parse = tpl_mod.parse_frontmatter_from_file

    def _raise_on_path(path):
        if path.name == "malformed.html.j2":
            raise ValueError("simulated parse error")
        return real_parse(path)

    monkeypatch.setattr(tpl_mod, "parse_frontmatter_from_file", _raise_on_path)

    r = asyncio.run(list_templates())
    names = {t["name"] for t in r["templates"]}
    assert "malformed" not in names


def test_list_templates_no_frontmatter_in_project_template(plugin_setup):
    """Project template without frontmatter gets empty metadata."""
    from agent_workflow_ui.tools.templates import list_templates

    no_meta = plugin_setup.templates_dir / "no-meta.html.j2"
    no_meta.write_text("<html><body>no frontmatter</body></html>")

    r = asyncio.run(list_templates())
    found = next((t for t in r["templates"] if t["name"] == "no-meta"), None)
    assert found is not None
    assert found["source"] == "project"


# --- BD-2: _normalize_available_roles ---

def test_normalize_available_roles_list_of_strings():
    """List of strings → normalized to list of dicts."""
    from agent_workflow_ui.tools.forms import _normalize_available_roles

    result = _normalize_available_roles(["worker", "reviewer"])
    assert result == [
        {"id": "worker", "title": "worker", "description": ""},
        {"id": "reviewer", "title": "reviewer", "description": ""},
    ]


def test_normalize_available_roles_mixed_list():
    """Mixed list (str + dict) → normalized."""
    from agent_workflow_ui.tools.forms import _normalize_available_roles

    result = _normalize_available_roles([
        "worker",
        {"id": "reviewer", "title": "Reviewer", "description": "Reviews code"},
    ])
    assert result == [
        {"id": "worker", "title": "worker", "description": ""},
        {"id": "reviewer", "title": "Reviewer", "description": "Reviews code"},
    ]


def test_normalize_available_roles_single_string():
    """Single string → one-element list of dicts."""
    from agent_workflow_ui.tools.forms import _normalize_available_roles

    result = _normalize_available_roles("worker")
    assert result == [{"id": "worker", "title": "worker", "description": ""}]


def test_normalize_available_roles_already_dicts():
    """Already-correct list of dicts → unchanged."""
    from agent_workflow_ui.tools.forms import _normalize_available_roles

    input_val = [
        {"id": "worker", "title": "Worker", "description": "Does work"},
    ]
    result = _normalize_available_roles(input_val)
    assert result == input_val


def test_normalize_available_roles_none():
    """None input → empty list."""
    from agent_workflow_ui.tools.forms import _normalize_available_roles

    assert _normalize_available_roles(None) == []


def test_normalize_available_roles_none_in_list():
    """None items in list are filtered out."""
    from agent_workflow_ui.tools.forms import _normalize_available_roles

    result = _normalize_available_roles(["worker", None, "tester"])
    assert len(result) == 2
    assert result[0]["id"] == "worker"
    assert result[1]["id"] == "tester"


def test_normalize_available_roles_empty_string_in_list():
    """Empty strings in list are filtered out."""
    from agent_workflow_ui.tools.forms import _normalize_available_roles

    result = _normalize_available_roles(["worker", "", "tester"])
    assert len(result) == 2
    assert result[0]["id"] == "worker"
    assert result[1]["id"] == "tester"


def test_normalize_available_roles_dict_without_id():
    """Dicts without 'id' field are filtered out."""
    from agent_workflow_ui.tools.forms import _normalize_available_roles

    result = _normalize_available_roles([{"id": "a"}, {"name": "no-id"}])
    assert len(result) == 1
    assert result[0]["id"] == "a"


def test_normalize_available_roles_non_list_type():
    """Non-list, non-str, non-dict input (e.g. int) → empty list."""
    from agent_workflow_ui.tools.forms import _normalize_available_roles

    assert _normalize_available_roles(5) == []


def test_normalize_available_roles_mixed_invalid():
    """Mixed valid + invalid items → only valid kept."""
    from agent_workflow_ui.tools.forms import _normalize_available_roles

    result = _normalize_available_roles(["worker", 42, []])
    assert len(result) == 1
    assert result[0]["id"] == "worker"


def test_open_form_with_string_roles_project_setup(plugin_setup):
    """open_form with list-of-strings available_roles renders project-setup without crash."""
    from agent_workflow_ui.tools.forms import open_form

    result = asyncio.run(open_form(
        template="project-setup",
        data={"available_roles": ["worker", "reviewer"]},
    ))
    assert result["form_id"].startswith("FORM-")
    assert "error" not in result


def test_open_form_with_mixed_roles_project_setup(plugin_setup):
    """open_form with mixed str+dict roles renders project-setup without crash."""
    from agent_workflow_ui.tools.forms import open_form

    result = asyncio.run(open_form(
        template="project-setup",
        data={
            "available_roles": [
                "worker",
                {"id": "reviewer", "title": "Reviewer", "description": "Reviews code"},
            ],
        },
    ))
    assert result["form_id"].startswith("FORM-")
    assert "error" not in result


# --- BD-6: project_dir in open_form data ---


def test_open_form_project_dir_set(plugin_setup, tmp_path):
    """open_form with project_dir in data → FormRecord.project_dir is set."""
    from agent_workflow_ui.tools.forms import open_form

    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / ".agentic").mkdir()

    result = asyncio.run(open_form(
        template="project-setup",
        data={
            "available_roles": ["worker"],
            "project_dir": str(proj),
        },
    ))
    assert result["form_id"].startswith("FORM-")
    record = get_registry().get(result["form_id"])
    assert record is not None
    assert record.project_dir == proj.resolve()


def test_open_form_project_dir_no_agentic(plugin_setup, tmp_path, caplog):
    """open_form with project_dir that has no .agentic/ → project_dir=None, warning logged."""
    from agent_workflow_ui.tools.forms import open_form

    proj = tmp_path / "noagentic"
    proj.mkdir()
    # No .agentic/ directory

    result = asyncio.run(open_form(
        template="project-setup",
        data={
            "available_roles": ["worker"],
            "project_dir": str(proj),
        },
    ))
    assert result["form_id"].startswith("FORM-")
    record = get_registry().get(result["form_id"])
    assert record is not None
    assert record.project_dir is None
    assert "has no .agentic/" in caplog.text


def test_open_form_no_project_dir_compat(plugin_setup):
    """open_form without project_dir → FormRecord.project_dir is None (back-compat)."""
    from agent_workflow_ui.tools.forms import open_form

    result = asyncio.run(open_form(
        template="project-setup",
        data={"available_roles": ["worker"]},
    ))
    assert result["form_id"].startswith("FORM-")
    record = get_registry().get(result["form_id"])
    assert record is not None
    assert record.project_dir is None


def test_open_form_project_dir_not_leaked_to_template(plugin_setup, tmp_path):
    """project_dir is popped from data so it doesn't leak into template context."""
    from agent_workflow_ui.tools.forms import open_form

    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / ".agentic").mkdir()

    result = asyncio.run(open_form(
        template="project-setup",
        data={
            "available_roles": ["worker"],
            "project_dir": str(proj),
        },
    ))
    record = get_registry().get(result["form_id"])
    assert record is not None
    assert "project_dir" not in record.data_keys


def test_open_form_data_not_mutated(plugin_setup, tmp_path):
    """open_form does not mutate the caller's data dict."""
    from agent_workflow_ui.tools.forms import open_form

    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / ".agentic").mkdir()

    caller_data = {"project_dir": str(proj), "other": "x"}
    original_keys = set(caller_data.keys())

    asyncio.run(open_form(
        template="project-setup",
        data=caller_data,
    ))
    assert set(caller_data.keys()) == original_keys
    assert "project_dir" in caller_data


def test_read_submit_with_project_dir(plugin_setup, tmp_path):
    """read_submit reads from project/.agentic/inputs/ when FormRecord has project_dir."""
    from agent_workflow_ui.tools.forms import open_form, read_submit

    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / ".agentic" / "inputs").mkdir(parents=True)

    open_result = asyncio.run(open_form(
        template="project-setup",
        data={
            "available_roles": ["worker"],
            "project_dir": str(proj),
        },
    ))
    form_id = open_result["form_id"]

    submit_data = {
        "form_id": form_id,
        "template": "project-setup",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "data": {"selected_roles": ["worker"]},
    }
    submit_file = proj / ".agentic" / "inputs" / f"{form_id}.yaml"
    submit_file.write_text(yaml.safe_dump(submit_data))
    get_registry().update_status(form_id, "submitted")

    r = asyncio.run(read_submit(form_id))
    assert r["submitted"] is True
    assert r["data"]["selected_roles"] == ["worker"]
