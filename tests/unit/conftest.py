"""Shared fixtures for unit tests."""
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """A tmp_path with git init, user config, and an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def sample_simple_config() -> dict:
    """Config dict matching what awf init --template simple produces."""
    return {
        "project": {"name": "test-project"},
        "default_pipeline": "default",
        "phases": {"current": ".agentic/phases/plan.md"},
        "verification": {
            "test_cmd": "pytest",
            "lint_cmd": "ruff check .",
            "typecheck_cmd": "mypy .",
            "build_cmd": "",
        },
        "automation": {"auto_done": True},
        "models": {
            "worker": {"agent_name": "worker"},
            "reviewer": {"agent_name": "rev1"},
        },
    }


@pytest.fixture
def sample_full_config() -> dict:
    """Config dict matching what awf init --template full produces."""
    return {
        "project": {"name": "test-project"},
        "default_pipeline": "full",
        "phases": {"current": ".agentic/phases/plan.md"},
        "verification": {
            "test_cmd": "pytest",
            "lint_cmd": "ruff check .",
            "typecheck_cmd": "mypy .",
            "build_cmd": "",
        },
        "automation": {"auto_done": True},
        "models": {
            "worker": {"agent_name": "worker"},
            "reviewer": {"agent_name": "rev1"},
            "tester": {"agent_name": "tester"},
        },
    }


@pytest.fixture
def tmp_pipeline_file(tmp_path: Path) -> tuple[Path, dict]:
    """Write the simple pipeline YAML into tmp and return (Path, config_dict)."""
    agentic = tmp_path / ".agentic"
    pipelines = agentic / "pipelines"
    pipelines.mkdir(parents=True)

    simple_yaml = REPO_ROOT / "templates" / "pipelines" / "simple.yaml"
    full_yaml = REPO_ROOT / "templates" / "pipelines" / "full.yaml"

    (pipelines / "simple.yaml").write_text(simple_yaml.read_text())
    (pipelines / "full.yaml").write_text(full_yaml.read_text())
    (pipelines / "default.yaml").write_text(simple_yaml.read_text())

    config = {
        "project": {"name": "test-project"},
        "default_pipeline": "default",
        "models": {
            "worker": {"agent_name": "worker"},
            "reviewer": {"agent_name": "rev1"},
        },
    }
    (agentic / "config.yaml").write_text(
        "project:\n  name: test-project\ndefault_pipeline: default\n"
    )

    return tmp_path, config
