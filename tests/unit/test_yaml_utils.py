"""Unit tests for awf.yaml_utils — yaml_get."""
from pathlib import Path

import yaml

from awf import yaml_utils


class TestYamlGet:

    def test_yaml_get_simple(self, tmp_path: Path) -> None:
        f = tmp_path / "cfg.yaml"
        f.write_text("project:\n  name: test-project\n")
        assert yaml_utils.yaml_get(f, "project.name") == "test-project"

    def test_yaml_get_top_level(self, tmp_path: Path) -> None:
        f = tmp_path / "cfg.yaml"
        f.write_text("default_pipeline: default\n")
        assert yaml_utils.yaml_get(f, "default_pipeline") == "default"

    def test_yaml_get_nested_deep(self, tmp_path: Path) -> None:
        f = tmp_path / "cfg.yaml"
        f.write_text("models:\n  worker:\n    agent_name: worker\n")
        assert yaml_utils.yaml_get(f, "models.worker.agent_name") == "worker"

    def test_yaml_get_missing_key_default(self, tmp_path: Path) -> None:
        f = tmp_path / "cfg.yaml"
        f.write_text("a: 1\n")
        assert yaml_utils.yaml_get(f, "no.such.key", "FALLBACK") == "FALLBACK"

    def test_yaml_get_partial_miss(self, tmp_path: Path) -> None:
        f = tmp_path / "cfg.yaml"
        f.write_text("models:\n  worker:\n    agent_name: w\n")
        assert yaml_utils.yaml_get(f, "models.ghost.agent", "?") == "?"

    def test_yaml_get_missing_file(self, tmp_path: Path) -> None:
        assert yaml_utils.yaml_get(tmp_path / "nope.yaml", "x", "default") == "default"

    def test_yaml_get_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "cfg.yaml"
        f.write_text("")
        assert yaml_utils.yaml_get(f, "x", "default") == "default"

    def test_yaml_get_non_dict_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "cfg.yaml"
        f.write_text("- item\n")
        assert yaml_utils.yaml_get(f, "x", "default") == "default"

    def test_yaml_get_value_is_zero(self, tmp_path: Path) -> None:
        f = tmp_path / "cfg.yaml"
        f.write_text("a:\n  b: 0\n")
        assert yaml_utils.yaml_get(f, "a.b") == 0

    def test_yaml_get_value_is_none(self, tmp_path: Path) -> None:
        f = tmp_path / "cfg.yaml"
        f.write_text("a:\n  b: null\n")
        assert yaml_utils.yaml_get(f, "a.b") is None

    def test_yaml_get_phases_current(self, tmp_path: Path) -> None:
        f = tmp_path / "cfg.yaml"
        f.write_text("phases:\n  current: .agentic/phases/plan.md\n")
        assert yaml_utils.yaml_get(f, "phases.current") == ".agentic/phases/plan.md"
