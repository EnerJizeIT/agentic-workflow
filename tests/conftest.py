"""Shared pytest fixtures for awf E2E tests."""
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AWF_BIN = REPO_ROOT / "bin" / "awf"
STUBS_DIR = REPO_ROOT / "tests" / "stubs"


@pytest.fixture
def awf_bin() -> str:
    """Absolute path to the awf binary in this repo."""
    assert AWF_BIN.exists(), f"bin/awf not found at {AWF_BIN}"
    return str(AWF_BIN)


@pytest.fixture
def awf_env(tmp_path, monkeypatch) -> dict:
    """Environment for subprocess invocations of awf.

    - PATH has tests/stubs/ first → `opencode` resolves to our mock.
    - HOME is redirected under tmp_path → awf init won't touch real opencode.json.
    - AWF_TEST_OPENCODE_BEHAVIOR defaults to "done"; tests override via setenv.
    """
    env = os.environ.copy()
    env["PATH"] = f"{STUBS_DIR}:{env.get('PATH', '')}"
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)
    # Pre-create an empty opencode config so create_opencode_agents' proposal
    # code path doesn't bail out with "no opencode config found".
    oc_dir = fake_home / ".config" / "opencode"
    oc_dir.mkdir(parents=True, exist_ok=True)
    (oc_dir / "opencode.json").write_text('{"agent": {}}\n')
    env["AWF_TEST_OPENCODE_BEHAVIOR"] = "done"
    return env


def _git_init(proj: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)
    (proj / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)


@pytest.fixture
def empty_project(tmp_path) -> Path:
    """A git-initialized empty project. awf init has NOT been run yet."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _git_init(proj)
    return proj


@pytest.fixture
def initialized_project(empty_project: Path, awf_bin: str, awf_env: dict) -> Path:
    """empty_project after `awf init --template simple` (non-interactive input)."""
    init_input = b"test-project\npytest\nruff\nmypy\n\nclaude-test-model\nn\n"
    subprocess.run(
        [awf_bin, "init", "--template", "simple"],
        cwd=empty_project,
        env=awf_env,
        input=init_input,
        check=True,
        capture_output=True,
    )
    return empty_project


def run_awf(awf_bin: str, args: list, cwd: Path, env: dict, input_data: bytes = None,
            timeout: int = 60) -> subprocess.CompletedProcess:
    """Helper — runs bin/awf with given args, returns result."""
    return subprocess.run(
        [awf_bin] + list(args),
        cwd=cwd, env=env, input=input_data,
        capture_output=True, text=False,
        timeout=timeout, check=False,
    )
