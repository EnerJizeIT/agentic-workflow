"""Unit tests for awf.config — load and get."""
from pathlib import Path

import yaml

from awf import config


class TestConfigLoad:

    def test_load_returns_dict(self, tmp_path: Path) -> None:
        agentic = tmp_path / ".agentic"
        agentic.mkdir()
        (agentic / "config.yaml").write_text("project:\n  name: test\n")
        result = config.load(tmp_path)
        assert isinstance(result, dict)
        assert result["project"]["name"] == "test"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        result = config.load(tmp_path)
        assert result == {}

    def test_load_empty_file(self, tmp_path: Path) -> None:
        agentic = tmp_path / ".agentic"
        agentic.mkdir()
        (agentic / "config.yaml").write_text("")
        result = config.load(tmp_path)
        assert result == {}

    def test_load_non_dict_yaml(self, tmp_path: Path) -> None:
        agentic = tmp_path / ".agentic"
        agentic.mkdir()
        (agentic / "config.yaml").write_text("- item1\n- item2\n")
        result = config.load(tmp_path)
        assert result == {}

    def test_load_complex_config(self, tmp_path: Path) -> None:
        agentic = tmp_path / ".agentic"
        agentic.mkdir()
        cfg = {
            "project": {"name": "x"},
            "models": {"worker": {"agent_name": "w1"}},
            "verification": {"test_cmd": "pytest"},
        }
        (agentic / "config.yaml").write_text(yaml.safe_dump(cfg))
        result = config.load(tmp_path)
        assert result["models"]["worker"]["agent_name"] == "w1"


class TestConfigGet:

    def test_get_top_level(self, sample_simple_config: dict) -> None:
        assert config.get(sample_simple_config, "default_pipeline") == "default"

    def test_get_nested(self, sample_simple_config: dict) -> None:
        assert config.get(sample_simple_config, "project.name") == "test-project"

    def test_get_deep_nested(self, sample_simple_config: dict) -> None:
        assert config.get(sample_simple_config, "models.worker.agent_name") == "worker"

    def test_get_reviewer(self, sample_simple_config: dict) -> None:
        assert config.get(sample_simple_config, "models.reviewer.agent_name") == "rev1"

    def test_get_missing_key_returns_default(self, sample_simple_config: dict) -> None:
        assert config.get(sample_simple_config, "no.such.key", "FALLBACK") == "FALLBACK"

    def test_get_missing_key_none_default(self, sample_simple_config: dict) -> None:
        assert config.get(sample_simple_config, "no.such.key") is None

    def test_get_partial_miss(self, sample_simple_config: dict) -> None:
        assert config.get(sample_simple_config, "models.ghost.agent_name", "?") == "?"

    def test_get_value_is_none(self, sample_simple_config: dict) -> None:
        cfg = {"a": {"b": None}}
        assert config.get(cfg, "a.b") is None

    def test_get_value_is_zero(self, sample_simple_config: dict) -> None:
        cfg = {"a": {"b": 0}}
        assert config.get(cfg, "a.b") == 0

    def test_get_value_is_empty_string(self, sample_simple_config: dict) -> None:
        cfg = {"a": {"b": ""}}
        assert config.get(cfg, "a.b") == ""

    def test_get_value_is_false(self, sample_simple_config: dict) -> None:
        cfg = {"a": {"b": False}}
        assert config.get(cfg, "a.b") is False

    def test_get_non_dict_intermediate(self, sample_simple_config: dict) -> None:
        cfg = {"a": "string", "b": 123}
        assert config.get(cfg, "a.deep.key", "default") == "default"
        assert config.get(cfg, "b.deep.key", "default") == "default"
