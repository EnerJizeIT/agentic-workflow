"""Unit tests for awf.api.check_model_config.

Validates that models declared in .agentic/config.yaml have matching
providers in opencode.json — prevents silent fallback to wrong model.

Critical regression test: provider.models may be a LIST (not dict);
old impl crashed with AttributeError when generating the warning for
"model not in provider's models list".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf import api


@pytest.fixture
def project_with_models(tmp_git_repo: Path) -> Path:
    """Init awf project + config.yaml with several role→model mappings."""
    api.init_project(tmp_git_repo, project_name="Test")
    config = tmp_git_repo / ".agentic" / "config.yaml"
    config.write_text(
        "project:\n"
        "  name: Test\n"
        "models:\n"
        "  worker:\n"
        "    model: 'anthropic/claude-3.5'\n"
        "  reviewer:\n"
        "    model: 'myprov/missing-model'\n"
        "  ghost:\n"
        "    model: 'ghostprov/anything'\n"
        "  nomodel:\n"
        "    agent_name: 'bare-agent'\n"
        "  direct:\n"
        "    model: 'gpt-4'\n"
    )
    return tmp_git_repo


def _write_opencode_json(xdg_root: Path, payload: dict) -> None:
    oc_dir = xdg_root / "opencode"
    oc_dir.mkdir(parents=True, exist_ok=True)
    (oc_dir / "opencode.json").write_text(json.dumps(payload))


class TestCheckModelConfig:
    def test_requires_agentic(self, tmp_path: Path):
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            api.check_model_config(tmp_path)

    def test_empty_opencode_json_marks_all_invalid(self, project_with_models, monkeypatch):
        """No opencode.json → every model lacks a provider → invalid + warning."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(project_with_models / "fake_xdg"))
        result = api.check_model_config(project_with_models)

        by_role = {m["role"]: m for m in result["models"]}
        # 'nomodel' role has no model key → treated as valid (uses opencode default)
        assert by_role["nomodel"]["valid"] is True
        assert by_role["nomodel"]["model"] == "(none)"
        # Roles with explicit models → invalid (no providers available)
        assert by_role["worker"]["valid"] is False
        assert by_role["reviewer"]["valid"] is False
        # warnings exist for invalid models
        assert any("worker" in w for w in result["warnings"])
        assert result["providers_available"] == []

    def test_dict_models_validates_correctly(self, project_with_models, monkeypatch):
        """opencode.json with provider.models as DICT → match works."""
        fake_xdg = project_with_models / "fake_xdg"
        _write_opencode_json(fake_xdg, {
            "provider": {
                "anthropic": {"models": {"claude-3.5": {}, "claude-3.7": {}}},
                "myprov": {"models": {"other-model": {}}},
            },
        })
        monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_xdg))

        result = api.check_model_config(project_with_models)
        by_role = {m["role"]: m for m in result["models"]}

        assert by_role["worker"]["valid"] is True
        assert by_role["worker"]["provider"] == "anthropic"
        # provider exists, model missing → invalid + warning lists available
        assert by_role["reviewer"]["valid"] is False
        assert "other-model" in " ".join(result["warnings"])

    def test_list_models_regression_no_crash(self, project_with_models, monkeypatch):
        """REGRESSION: provider.models as LIST used to crash with AttributeError
        when generating the warning for a missing model.

        Before fix: list(...).keys() on a list → AttributeError, breaks the tool.
        After fix: warning lists available models from the list correctly.
        """
        fake_xdg = project_with_models / "fake_xdg"
        _write_opencode_json(fake_xdg, {
            "provider": {
                "myprov": {"models": ["m1", "m2", "m3"]},
            },
        })
        monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_xdg))

        # Must not raise
        result = api.check_model_config(project_with_models)
        by_role = {m["role"]: m for m in result["models"]}

        # 'myprov/missing-model' → provider exists, model not in list → invalid + warning
        assert by_role["reviewer"]["valid"] is False
        warning_text = " ".join(result["warnings"])
        assert "myprov" in warning_text
        # Available models are listed from the list (not crashed)
        assert "m1" in warning_text or "m2" in warning_text

    def test_list_models_match_when_present(self, project_with_models, monkeypatch):
        """provider.models as LIST and model IS present → valid."""
        fake_xdg = project_with_models / "fake_xdg"
        _write_opencode_json(fake_xdg, {
            "provider": {
                "anthropic": {"models": ["claude-3.5", "claude-3.7"]},
            },
        })
        monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_xdg))

        result = api.check_model_config(project_with_models)
        by_role = {m["role"]: m for m in result["models"]}
        assert by_role["worker"]["valid"] is True

    def test_agent_config_match_treated_as_valid(self, project_with_models, monkeypatch):
        """Model referenced by opencode agent config → valid even without provider."""
        fake_xdg = project_with_models / "fake_xdg"
        _write_opencode_json(fake_xdg, {
            "agent": {
                "myagent": {"model": "anthropic/claude-3.5"},
            },
        })
        monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_xdg))

        result = api.check_model_config(project_with_models)
        by_role = {m["role"]: m for m in result["models"]}
        assert by_role["worker"]["valid"] is True
        assert "agent config" in by_role["worker"]["note"]

    def test_invalid_opencode_json_does_not_crash(self, project_with_models, monkeypatch):
        """Malformed opencode.json → treated as no providers (no crash)."""
        fake_xdg = project_with_models / "fake_xdg"
        oc_dir = fake_xdg / "opencode"
        oc_dir.mkdir(parents=True)
        (oc_dir / "opencode.json").write_text("{not valid json")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_xdg))

        result = api.check_model_config(project_with_models)
        assert result["providers_available"] == []
        # All roles with models → invalid
        assert all(m["valid"] is False for m in result["models"] if m["model"] != "(none)")
