"""Unit tests for agent_workflow_ui.config."""
from __future__ import annotations

import tempfile
from pathlib import Path

from agent_workflow_ui.config import detect_project_dir, ensure_directories, load


def test_load_defaults(tmp_path, monkeypatch):
    """Config loads with defaults when env vars are unset."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWF_INPUTS_DIR", raising=False)
    monkeypatch.delenv("AWF_TEMPLATES_DIR", raising=False)
    monkeypatch.delenv("AWF_DASHBOARDS_DIR", raising=False)
    monkeypatch.delenv("AWF_HTTP_PORT", raising=False)
    monkeypatch.delenv("AWF_OPEN_BROWSER_CMD", raising=False)
    monkeypatch.delenv("AWF_TEMP_DIR", raising=False)
    monkeypatch.delenv("AWF_DEFAULT_TTL_SECONDS", raising=False)

    config = load()

    assert config.inputs_dir == (tmp_path / ".agentic" / "inputs").resolve()
    assert config.templates_dir == (tmp_path / ".agentic" / "templates").resolve()
    assert config.dashboards_dir == (tmp_path / ".agentic" / "dashboards").resolve()
    assert config.http_port == 0
    assert config.open_browser_cmd == "auto"
    assert config.temp_dir == Path(tempfile.gettempdir())
    assert config.default_ttl_seconds == 86400


def test_load_env_override(tmp_path, monkeypatch):
    """Config respects env vars."""
    monkeypatch.chdir(tmp_path)
    custom_inputs = tmp_path / "custom-inputs"
    monkeypatch.setenv("AWF_INPUTS_DIR", str(custom_inputs))
    monkeypatch.setenv("AWF_HTTP_PORT", "9999")

    config = load()

    assert config.inputs_dir == custom_inputs.resolve()
    assert config.http_port == 9999


def test_load_absolute_paths(tmp_path, monkeypatch):
    """Absolute paths in env are preserved as-is."""
    monkeypatch.chdir(tmp_path)
    abs_path = "/tmp/some-abs-path"
    monkeypatch.setenv("AWF_INPUTS_DIR", abs_path)

    config = load()

    assert config.inputs_dir == Path(abs_path)


def test_ensure_directories_creates_missing(tmp_path, monkeypatch):
    """ensure_directories creates dirs idempotently."""
    monkeypatch.chdir(tmp_path)
    config = load()

    assert not config.inputs_dir.exists()
    ensure_directories(config)
    assert config.inputs_dir.is_dir()
    assert config.templates_dir.is_dir()
    assert config.dashboards_dir.is_dir()

    ensure_directories(config)


def test_ensure_directories_partial_existing(tmp_path, monkeypatch):
    """Doesn't fail if some dirs already exist."""
    monkeypatch.chdir(tmp_path)
    config = load()
    config.inputs_dir.mkdir(parents=True)
    (config.inputs_dir / "existing.yaml").write_text("test")

    ensure_directories(config)

    assert (config.inputs_dir / "existing.yaml").read_text() == "test"


def test_detect_project_dir_with_agentic(tmp_path, monkeypatch):
    """Returns cwd when .agentic/ exists."""
    (tmp_path / ".agentic").mkdir()
    monkeypatch.chdir(tmp_path)
    assert detect_project_dir() == tmp_path


def test_detect_project_dir_without_agentic(tmp_path, monkeypatch):
    """Returns None when .agentic/ is missing."""
    monkeypatch.chdir(tmp_path)
    assert detect_project_dir() is None


def test_detect_project_dir_agentic_file_not_dir(tmp_path, monkeypatch):
    """Returns None when .agentic is a file, not a directory."""
    (tmp_path / ".agentic").write_text("not a dir")
    monkeypatch.chdir(tmp_path)
    assert detect_project_dir() is None
