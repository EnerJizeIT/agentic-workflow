"""Tests for model discovery + validation — read_available_models + check_model_config.

CI-safe: all tests mock the opencode environment (CLI, opencode.json, opencode.db).
No dependency on real machine installation.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from awf import api
from awf.api.model_check import check_model_config

# Fake model lists for mocking
FAKE_CLI_MODELS = ["vllm/llm", "opencode/glm-5.2", "zai-coding-plan/glm-5.2"]
FAKE_RECENT_MODELS = ["zai-coding-plan/glm-5.2"]


@pytest.fixture
def awf_project(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


@pytest.fixture
def mock_model_env(monkeypatch, tmp_path):
    """Mock the entire model discovery environment.

    - subprocess.run returns fake 'opencode models' output
    - opencode.json created in fake home with vllm provider
    - opencode.db path points to nonexistent file (no recent sessions)
    """
    # Mock subprocess.run for 'opencode models' CLI
    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:1] == ["opencode"] and "models" in cmd:
            return type("R", (), {
                "returncode": 0,
                "stdout": "\n".join(FAKE_CLI_MODELS) + "\n",
                "stderr": "",
            })()
        # Default: real subprocess for other commands (git, etc.)
        return subprocess.run(cmd, **kwargs)

    monkeypatch.setattr("subprocess.run", fake_run)

    # Mock opencode.json in a fake home directory
    fake_home = tmp_path / "fake_home"
    oc_dir = fake_home / ".config" / "opencode"
    oc_dir.mkdir(parents=True)
    (oc_dir / "opencode.json").write_text(json.dumps({
        "provider": {
            "vllm": {
                "models": {
                    "llm": {"name": "LLM"},
                },
            },
        },
    }))

    import awf.xdg as xdg_mod
    monkeypatch.setattr(xdg_mod, "opencode_config_file", lambda: oc_dir / "opencode.json")
    monkeypatch.setattr(Path, "home", lambda: fake_home)


def _set_models(project: Path, models: dict) -> None:
    """Update config.yaml models section."""
    config_path = project / ".agentic" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["models"] = models
    config_path.write_text(yaml.safe_dump(config))


def _mock_cli_empty(monkeypatch):
    """Mock 'opencode models' CLI to return empty (force fallback)."""
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
    )


# ─── read_available_models ──────────────────────────────────────────────


class TestReadAvailableModels:
    """Model discovery from 'opencode models' CLI + opencode.json fallback."""

    def test_cli_returns_models(self, mock_model_env):
        """When 'opencode models' works → returns models from CLI."""
        from agent_workflow_ui.opencode_config import read_available_models

        models = read_available_models()
        assert len(models) > 0
        assert all("/" in m for m in models)  # all have provider/model format

    def test_cli_skips_non_model_lines(self, monkeypatch, tmp_path):
        """CLI output may contain non-model lines (page-assist notices)."""
        from agent_workflow_ui import opencode_config

        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: type("R", (), {
                "returncode": 0,
                "stdout": "[page-assist] CLI mode\nopencode/glm-5.2\nvllm/llm\n\n",
                "stderr": "",
            })(),
        )
        models = opencode_config.read_available_models()
        assert "opencode/glm-5.2" in models
        assert "vllm/llm" in models
        assert not any("[page-assist]" in m for m in models)
        assert not any("" == m for m in models)

    def test_fallback_to_json_when_cli_fails(self, monkeypatch, tmp_path):
        """When CLI unavailable → falls back to opencode.json parsing."""
        from agent_workflow_ui import opencode_config

        _mock_cli_empty(monkeypatch)
        fake_home = tmp_path / "home"
        oc_dir = fake_home / ".config" / "opencode"
        oc_dir.mkdir(parents=True)
        (oc_dir / "opencode.json").write_text(json.dumps({
            "provider": {"vllm": {"models": {"llm": {"name": "LLM"}}}},
        }))
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        models = opencode_config.read_available_models()
        assert "vllm/llm" in models

    def test_fallback_to_json_agents(self, monkeypatch, tmp_path):
        """Fallback: agent.<name>.model also collected."""
        from agent_workflow_ui import opencode_config

        _mock_cli_empty(monkeypatch)
        fake_home = tmp_path / "home"
        oc_dir = fake_home / ".config" / "opencode"
        oc_dir.mkdir(parents=True)
        (oc_dir / "opencode.json").write_text(json.dumps({
            "agent": {"worker": {"model": "custom/model"}},
        }))
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        models = opencode_config.read_available_models()
        assert "custom/model" in models

    def test_cli_timeout_falls_back(self, monkeypatch):
        """CLI timeout → falls back to opencode.json (no crash)."""
        from agent_workflow_ui import opencode_config

        def slow_cli(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="opencode", timeout=10)

        monkeypatch.setattr("subprocess.run", slow_cli)
        # Should not raise — falls back to opencode.json (may return [])
        models = opencode_config.read_available_models()
        assert isinstance(models, list)

    def test_cli_not_found_falls_back(self, monkeypatch):
        """opencode binary not on PATH → falls back (no crash)."""
        from agent_workflow_ui import opencode_config

        def missing_binary(*a, **kw):
            raise FileNotFoundError("opencode not found")

        monkeypatch.setattr("subprocess.run", missing_binary)
        models = opencode_config.read_available_models()
        assert isinstance(models, list)


# ─── read_recent_models ─────────────────────────────────────────────────


class TestReadRecentModels:
    """Recent models from opencode.db sessions."""

    def test_returns_list(self):
        """Returns a list (may be empty if no DB)."""
        from agent_workflow_ui.opencode_config import read_recent_models

        models = read_recent_models()
        assert isinstance(models, list)


# ─── check_model_config: validation paths ───────────────────────────────


class TestCheckModelConfigValidationPaths:
    """Each validation path in check_model_config."""

    def test_provider_in_opencode_json_valid(self, awf_project, mock_model_env):
        """Model in opencode.json provider → valid."""
        _set_models(awf_project, {
            "agent-test": {"agent_name": "worker", "model": "vllm/llm"},
        })
        result = check_model_config(awf_project)
        test_role = [m for m in result["models"] if m["role"] == "agent-test"][0]
        assert test_role["valid"] is True

    def test_cli_model_valid(self, awf_project, mock_model_env):
        """Model available via 'opencode models' but not in opencode.json → valid."""
        _set_models(awf_project, {
            "agent-test": {"agent_name": "worker", "model": "opencode/glm-5.2"},
        })
        result = check_model_config(awf_project)
        test_role = [m for m in result["models"] if m["role"] == "agent-test"][0]
        assert test_role["valid"] is True
        assert "opencode models" in test_role["note"]

    def test_completely_invalid_model_warns(self, awf_project, mock_model_env):
        """Model nowhere (not in CLI, providers, recent) → invalid + warning."""
        _set_models(awf_project, {
            "agent-test": {"agent_name": "worker", "model": "nonexistent/fake-model"},
        })
        result = check_model_config(awf_project)
        test_role = [m for m in result["models"] if m["role"] == "agent-test"][0]
        assert test_role["valid"] is False
        assert len(result["warnings"]) > 0

    def test_no_model_specified_is_valid(self, awf_project, mock_model_env):
        """Role without model → valid (uses opencode default)."""
        _set_models(awf_project, {
            "agent-test": {"agent_name": "worker"},  # no model key
        })
        result = check_model_config(awf_project)
        test_role = [m for m in result["models"] if m["role"] == "agent-test"][0]
        assert test_role["valid"] is True
        assert "default" in test_role["note"].lower()

    def test_multiple_roles_mixed_validity(self, awf_project, mock_model_env):
        """Some models valid, some not — correct per-role result."""
        _set_models(awf_project, {
            "agent-good": {"agent_name": "worker", "model": "vllm/llm"},
            "agent-bad": {"agent_name": "worker", "model": "fake/nonexistent"},
        })
        result = check_model_config(awf_project)
        roles = {m["role"]: m for m in result["models"]}
        assert roles["agent-good"]["valid"] is True
        assert roles["agent-bad"]["valid"] is False


# ─── check_model_config: edge cases ─────────────────────────────────────


class TestCheckModelConfigEdgeCases:
    """Edge cases and error handling."""

    def test_missing_agentic_raises(self, tmp_git_repo):
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            check_model_config(tmp_git_repo)

    def test_empty_models_section(self, awf_project, mock_model_env):
        """No models configured → empty result, no crash."""
        result = check_model_config(awf_project)
        # supervisor model always exists
        assert isinstance(result["models"], list)

    def test_opencode_json_missing_all_valid_via_cli(self, awf_project, monkeypatch):
        """No opencode.json at all — models still valid via CLI."""
        import awf.xdg as xdg_mod

        # Mock CLI to return models
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: type("R", (), {
                "returncode": 0,
                "stdout": "\n".join(FAKE_CLI_MODELS) + "\n",
                "stderr": "",
            })() if "models" in (a[0] if a and isinstance(a[0], list) else [])
            else subprocess.run(*a, **kw),
        )
        monkeypatch.setattr(
            xdg_mod,
            "opencode_config_file",
            lambda: Path("/nonexistent/opencode.json"),
        )
        _set_models(awf_project, {
            "agent-test": {"agent_name": "worker", "model": "vllm/llm"},
        })
        result = check_model_config(awf_project)
        test_role = [m for m in result["models"] if m["role"] == "agent-test"][0]
        assert test_role["valid"] is True

    def test_list_models_in_provider_handled(self, awf_project, monkeypatch, tmp_path):
        """Provider with models as LIST (not dict) — no AttributeError."""
        _mock_cli_empty(monkeypatch)
        # Create temp opencode.json with list-form models
        fake_home = tmp_path / "home"
        oc_dir = fake_home / ".config" / "opencode"
        oc_dir.mkdir(parents=True)
        (oc_dir / "opencode.json").write_text(json.dumps({
            "provider": {"custom": {"models": ["model-a", "model-b"]}},
        }))
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        _set_models(awf_project, {
            "agent-test": {"agent_name": "worker", "model": "custom/model-a"},
        })
        result = check_model_config(awf_project)
        test_role = [m for m in result["models"] if m["role"] == "agent-test"][0]
        assert test_role["valid"] is True

    def test_result_has_providers_available(self, awf_project, mock_model_env):
        """Result includes providers_available list."""
        result = check_model_config(awf_project)
        assert "providers_available" in result
        assert isinstance(result["providers_available"], list)

    def test_as_dict_serializable(self, awf_project, mock_model_env):
        """Full result is JSON-serializable for MCP."""
        result = check_model_config(awf_project)
        json.dumps(result)


# ─── Forms integration ──────────────────────────────────────────────────


class TestFormsModelCollection:
    """Forms.py _collect_opencode_models + _collect_recent_models."""

    def test_collect_opencode_models_returns_list(self, mock_model_env):
        """_collect_opencode_models returns a list of model strings."""
        from agent_workflow_ui.tools.forms import _collect_opencode_models

        models = _collect_opencode_models()
        assert isinstance(models, list)
        assert len(models) > 0

    def test_collect_recent_models_returns_list(self):
        """_collect_recent_models returns a list."""
        from agent_workflow_ui.tools.forms import _collect_recent_models

        models = _collect_recent_models()
        assert isinstance(models, list)

    def test_collect_opencode_no_duplicates(self, mock_model_env):
        """Result has no duplicates."""
        from agent_workflow_ui.tools.forms import _collect_opencode_models

        models = _collect_opencode_models()
        assert len(models) == len(set(models)), "Duplicate models in list"

    def test_all_models_contain_slash(self, mock_model_env):
        """All model IDs have provider/model format."""
        from agent_workflow_ui.tools.forms import _collect_opencode_models

        models = _collect_opencode_models()
        for m in models:
            assert "/" in m, f"Model '{m}' missing provider/model separator"


# ─── MCP tool wrapper ───────────────────────────────────────────────────


class TestAwfCheckModelConfigTool:
    """MCP tool awf_check_model_config returns proper format."""

    def test_returns_ok_status(self, awf_project, mock_model_env):
        import asyncio

        from agent_workflow_ui.tools import awf

        result = asyncio.run(awf.awf_check_model_config(project_dir=str(awf_project)))
        assert result["status"] == "ok"
        assert "models" in result
        assert "warnings" in result

    def test_error_status_for_missing_agentic(self, tmp_git_repo):
        import asyncio

        from agent_workflow_ui.tools import awf

        result = asyncio.run(awf.awf_check_model_config(project_dir=str(tmp_git_repo)))
        assert result["status"] == "error"
