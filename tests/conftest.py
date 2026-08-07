"""Shared pytest fixtures for awf E2E tests."""
import os
import subprocess
from pathlib import Path

import pytest

# A10: disable FormRegistry persistence globally for all tests — keeps
# test isolation (no ~/.config/awf/state/forms_registry.yaml reads/writes).
os.environ.setdefault("AWF_DISABLE_FORM_PERSIST", "1")

# BD-36: disable plan checkpoint by default — tests that don't explicitly
# test the checkpoint would otherwise hang on browser.open / HTTP wait.
# test_plan_checkpoint.py re-enables per-test via monkeypatch.
os.environ.setdefault("AWF_PLAN_CHECKPOINT", "false")

REPO_ROOT = Path(__file__).resolve().parent.parent
AWF_BIN = REPO_ROOT / "bin" / "awf"
STUBS_DIR = REPO_ROOT / "tests" / "stubs"


@pytest.fixture(autouse=True)
def _isolate_xdg_env(monkeypatch):
    """A9: clear XDG_CONFIG_HOME so tests that patch Path.home() work.

    Also invalidates the models cache (QA-4) so subprocess mock results
    from one test don't leak into another.
    """
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    try:
        from agent_workflow_ui.opencode_config import _invalidate_models_cache
        _invalidate_models_cache()
    except ImportError:
        pass


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
    """empty_project after `awf init` (BD-28: no --template flag).

    Also creates a minimal pipeline.yaml so e2e tests can run `awf start`
    without needing the UI form. In real usage the form creates this file.
    """
    init_input = b"test-project\npytest\nruff\nmypy\n\nclaude-test-model\nn\nn\n"
    subprocess.run(
        [awf_bin, "init"],
        cwd=empty_project,
        env=awf_env,
        input=init_input,
        check=True,
        capture_output=True,
    )
    # BD-28: write a minimal pipeline.yaml (UI form normally does this).
    pipeline = empty_project / ".agentic" / "pipelines" / "default.yaml"
    pipeline.write_text(
        'name: "default"\n'
        'description: "Test pipeline (supervisor → worker → supervisor)"\n\n'
        'stages:\n'
        '  - name: "plan"\n'
        '    role: "supervisor"\n'
        '    action: "create_todo"\n'
        '  - name: "implement"\n'
        '    role: "worker"\n'
        '    action: "execute_todo"\n'
        '    on_blocked: "escalate"\n'
        '    max_retries: 3\n'
        '  - name: "verify"\n'
        '    role: "supervisor"\n'
        '    action: "verify_result"\n'
        '    on_approved: "commit_and_next"\n'
        '    on_rejected: "replan"\n',
        encoding="utf-8",
    )
    # Also need worker.md (UI form copies from skill content).
    worker_role = empty_project / ".agentic" / "roles" / "worker.md"
    if not worker_role.exists():
        worker_role.write_text("# Worker\n\nExecute the TODO.\n", encoding="utf-8")
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


# ─── plugin_setup fixture (shared by tests/agent_workflow_ui/*) ─────────
#
# Used to live in tests/agent_workflow_ui/test_tools.py as a local fixture,
# then briefly in tests/agent_workflow_ui/conftest.py — but the latter
# broke e2e tests which import `from conftest import run_awf` (pytest was
# resolving the subdir conftest first). Moved here as the single shared
# location.

DEFAULT_TEMPLATES_DIR = REPO_ROOT / "agent_workflow_ui" / "src" / "agent_workflow_ui" / "render" / "default_templates"


@pytest.fixture
def plugin_setup(tmp_path, monkeypatch):
    """Initialize plugin state in tmp_path + mock browser.open_path.

    CRITICAL: monkeypatch open_path at BOTH module levels (forms.py and
    browser.py). Without the forms.py patch, real subprocess.run fires
    and tests pollute the user's browser with form tabs.

    forms.py does `from ..browser import open_path` — this creates a
    separate reference in forms module's namespace. Patching only
    browser.open_path leaves forms.open_path untouched.
    """
    # Late imports (avoid affecting module-level state outside fixture)
    import agent_workflow_ui.browser as browser_mod
    import agent_workflow_ui.tools.forms as forms_mod
    from agent_workflow_ui.config import ensure_directories, load
    from agent_workflow_ui.render.engine import create_env
    from agent_workflow_ui.state import (
        reset_registry,
        set_config,
        set_http_port,
        set_jinja_env,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_TEMP_DIR", str(tmp_path / "tmp"))
    config = load()
    ensure_directories(config)
    set_config(config)
    set_http_port(13747)
    set_jinja_env(create_env([config.templates_dir, DEFAULT_TEMPLATES_DIR]))
    reset_registry()

    def _fake_open_path(target, command="auto"):
        return True, f"mocked open for {target}"

    monkeypatch.setattr(forms_mod, "open_path", _fake_open_path, raising=True)
    monkeypatch.setattr(browser_mod, "open_path", _fake_open_path, raising=True)

    return config
