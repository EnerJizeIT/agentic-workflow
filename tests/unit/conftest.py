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
    """Write inline pipeline YAMLs (simple + full) into tmp.

    BD-28: templates/pipelines/{simple,full}.yaml were removed (UI form
    generates pipeline now). Tests embed the YAML inline to stay independent
    of templates/.
    """
    agentic = tmp_path / ".agentic"
    pipelines = agentic / "pipelines"
    pipelines.mkdir(parents=True)

    simple_yaml = (
        'name: "default"\n'
        'description: "Supervisor plans, Worker implements, Supervisor verifies"\n\n'
        'stages:\n'
        '  - name: "plan"\n'
        '    role: "supervisor"\n'
        '    action: "create_todo"\n'
        '    description: "Supervisor studies the plan and creates a TODO"\n\n'
        '  - name: "implement"\n'
        '    role: "worker"\n'
        '    action: "execute_todo"\n'
        '    description: "Worker executes the TODO"\n'
        '    on_blocked: "escalate"\n'
        '    max_retries: 3\n\n'
        '  - name: "verify"\n'
        '    role: "supervisor"\n'
        '    action: "verify_result"\n'
        '    description: "Supervisor verifies the result"\n'
        '    on_approved: "commit_and_next"\n'
        '    on_rejected: "replan"\n'
    )

    full_yaml = (
        'name: "full"\n'
        'description: "Plan → implement → review → test → finalize"\n\n'
        'stages:\n'
        '  - name: "plan"\n'
        '    role: "supervisor"\n'
        '    action: "create_todo"\n'
        '  - name: "implement"\n'
        '    role: "worker"\n'
        '    action: "execute_todo"\n'
        '    on_blocked: "escalate"\n'
        '    max_retries: 3\n'
        '  - name: "review"\n'
        '    role: "reviewer"\n'
        '    action: "review_code"\n'
        '    on_rejected: "rollback_to:implement"\n'
        '    max_retries: 2\n'
        '  - name: "test"\n'
        '    role: "tester"\n'
        '    action: "run_tests"\n'
        '    on_failed: "rollback_to:implement"\n'
        '    max_retries: 2\n'
        '  - name: "finalize"\n'
        '    role: "supervisor"\n'
        '    action: "final_verify"\n'
        '    on_approved: "commit_and_report"\n'
        '    on_rejected: "replan"\n'
    )

    (pipelines / "simple.yaml").write_text(simple_yaml)
    (pipelines / "full.yaml").write_text(full_yaml)
    (pipelines / "default.yaml").write_text(simple_yaml)

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
