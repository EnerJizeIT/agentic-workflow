"""Shared pytest fixtures for awf E2E tests."""
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

import pytest

# A10: disable FormRegistry persistence globally for all tests — keeps
# test isolation (no ~/.config/awf/state/forms_registry.yaml reads/writes).
os.environ.setdefault("AWF_DISABLE_FORM_PERSIST", "1")

# BD-36: disable plan checkpoint by default — tests that don't explicitly
# test the checkpoint would otherwise hang on browser.open / HTTP wait.
# test_plan_checkpoint.py re-enables per-test via monkeypatch.
os.environ.setdefault("AWF_PLAN_CHECKPOINT", "false")

# AUD-2: cap commit_gate approve timeout at 5s in tests (was 1800s).
# If a test reaches commit_gate without pre-created APPROVE file,
# it would sleep for 30 minutes.
os.environ.setdefault("AWF_APPROVE_TIMEOUT_SECONDS", "5")

# U7a: hermetic git — the suite must never read the user's ~/.gitconfig or
# the system git config. 2026-09-20 incident: a corporate ~/.gitconfig with
# commit.gpgsign=true hung full pytest runs on pinentry (gpg prompt).
# /dev/null is a valid (empty) config file. Hard assignment on purpose:
# inherited values would break hermeticity.
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
# Fixed test identity: commits in tmp test-repos work even without local
# user.name/user.email config.
os.environ["GIT_AUTHOR_NAME"] = "awf-test"
os.environ["GIT_AUTHOR_EMAIL"] = "test@example.invalid"
os.environ["GIT_COMMITTER_NAME"] = "awf-test"
os.environ["GIT_COMMITTER_EMAIL"] = "test@example.invalid"

# Suppress webbrowser.open during tests (deterministic dashboard opening
# would spawn browser windows on every test that triggers awf_start).
webbrowser.open = lambda *a, **kw: True

# SAFETY (2026-09-20): never let the suite signal pid/pgid <= 1.
# `os.killpg(1, sig)` is a libc wrapper over `kill(2)` with argument
# `-pgid` — on Linux that is `kill(-1, sig)`: SIGTERM/SIGKILL to every
# process the caller may signal (the whole uid session). Full
# `pytest tests/` runs killed the owner's graphical session three times
# (fake Popen with pid=1 + hard timeout in tests/integration; analysis:
# ~/Desktop/session-crash-report-2026-09-20.md). The guard in
# awf/_proc.py::kill_process_tree is `proc.pid > 1`; this tripwire is the
# belt-and-suspenders for the whole suite: any REAL broad-kill attempt
# fails loudly instead of wiping the session.
# Refusal marker at runtime (green-light grep target):
# "SAFETY: os.killpg(1, 15) targets pid/pgid <= 1 ..." (name comes from the
# wrapped function, see _forbid_session_kill call sites below).
def _forbid_session_kill(real, name):
    def guarded(target, sig, *args, **kwargs):
        if isinstance(target, int) and target <= 1:
            raise RuntimeError(
                f"SAFETY: {name}({target!r}, {sig}) targets pid/pgid <= 1 — "
                "kill(-1) would signal the entire user session. Refusing to "
                "call through (2026-09-20 session-kill incident)."
            )
        return real(target, sig, *args, **kwargs)

    return guarded


os.kill = _forbid_session_kill(os.kill, "os.kill")
if hasattr(os, "killpg"):
    os.killpg = _forbid_session_kill(os.killpg, "os.killpg")

REPO_ROOT = Path(__file__).resolve().parent.parent
AWF_BIN = REPO_ROOT / "bin" / "awf"
STUBS_DIR = REPO_ROOT / "tests" / "stubs"


@pytest.fixture(autouse=True)
def _isolate_xdg_env(monkeypatch):
    """A9: clear XDG_CONFIG_HOME so tests that patch Path.home() work.

    AUD-2: intercept 'opencode models' subprocess so tests that call
    read_available_models() without explicit mock get a fast empty response
    instead of spawning a real 10s subprocess.

    Also invalidates the models cache (QA-4) so results from one test
    don't leak into another.
    """
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    # AUD-2: mock 'opencode models' → empty (tests that need real behavior
    # override via their own monkeypatch.setattr).
    _real_run = subprocess.run

    def _intercept_opencode_models(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[:2] == ["opencode", "models"]:
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return _real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("subprocess.run", _intercept_opencode_models)

    # P0.3: also intercept subprocess.Popen to prevent real pipeline subprocess
    # spawns in unit tests (test_api.py background tests). Returns a fake Popen
    # that looks "already exited" so callers don't hang.
    _real_popen = subprocess.Popen

    class _FakePopen:
        def __init__(self, cmd, *args, **kwargs):
            self.pid = 99999
            self.returncode = 0
            self.stdout = None
            self.stderr = None
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass
        def communicate(self, timeout=None):
            return ("", "")
        def terminate(self):
            pass

    def _intercept_popen(cmd, *args, **kwargs):
        # Allow real Popen for tests that explicitly need it (they set
        # monkeypatch.setattr back to _real_popen or use their own mock).
        if isinstance(cmd, list) and len(cmd) >= 1:
            cmd0 = str(cmd[0])
            if cmd0 in ("python3", "python", sys.executable) and "orchestrator" in " ".join(str(c) for c in cmd):
                return _FakePopen()
        return _real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr("subprocess.Popen", _intercept_popen)

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


def _free_port() -> int:
    """Ask the OS for a free TCP port (AUD12-09).

    No test may depend on a specific port being free (13747 used to be
    hardcoded in three places — parallel runs or a busy CI box crashed
    them). This is the shared home of the pattern; test_plan_checkpoint
    imports it from here.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _git_init_bare(proj: Path) -> None:
    """git init + test identity, NO initial commit (AUD12-08).

    For scenarios that need an empty repo without HEAD (e.g. testing
    current_sha failure on a fresh repo).
    """
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)


def _git_init(proj: Path) -> None:
    """git init + identity + README + initial commit (AUD12-08).

    The single home of the 5-line git boilerplate — every test that needs
    a committed repo goes through here (fixtures tmp_git_repo/empty_project
    or a direct call).
    """
    _git_init_bare(proj)
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
def tmp_git_repo(tmp_path) -> Path:
    """A tmp_path/repo with git init, user config, and an initial commit.

    AUD12-08: single home for the 5-line git boilerplate — previously
    duplicated in tests/unit/conftest.py, tests/negative/conftest.py and
    ~18 inline copies across the suite.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    return repo


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
    # AUD12-09: no hardcoded port — the suite must not depend on a
    # specific port being free (parallel runs / busy CI box).
    set_http_port(_free_port())
    set_jinja_env(create_env([config.templates_dir, DEFAULT_TEMPLATES_DIR]))
    reset_registry()

    def _fake_open_path(target, command="auto"):
        return True, f"mocked open for {target}"

    monkeypatch.setattr(forms_mod, "open_path", _fake_open_path, raising=True)
    monkeypatch.setattr(browser_mod, "open_path", _fake_open_path, raising=True)

    return config
