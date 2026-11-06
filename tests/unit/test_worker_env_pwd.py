"""A69 (TODO-0396): the worker's session directory is the project, not $HOME.

opencode takes the session directory from ``PWD``, not from the spawn's
``cwd``. The engine inherited its own ``PWD`` (wherever it was started —
usually the home directory), so every stage lived in ``$HOME`` and its
first shell call had to ``cd`` into the project.

``awf_subprocess_env(project_dir=...)`` now writes ``PWD`` and ``OLDPWD``
of the project. No project → the inherited ``PWD`` is left alone: callers
that spawn without one keep the old environment.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from awf import agent_stage
from awf._env import awf_subprocess_env
from awf.pipeline import Stage

MODEL_CONFIG = {"models": {"worker": {"model": "p/test"}}}

TODO_ID = "TODO-0001"

STAGE_TYPES = """\
stage_types:
  agent-dev: execute
"""

TODO_TEXT = """\
---
files: ["awf/x.py"]
verify: ["pytest -q tests/unit/test_x.py"]
---
# TODO — sample
"""


def _isolate_xdg(tmp_path: Path, monkeypatch) -> Path:
    xdg = tmp_path / "xdg"
    (xdg / "opencode").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return xdg


def test_project_dir_sets_pwd_and_oldpwd(tmp_path, monkeypatch):
    monkeypatch.setenv("PWD", "/home/somebody")
    monkeypatch.setenv("OLDPWD", "/home/somebody")
    _isolate_xdg(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()

    env = awf_subprocess_env(project_dir=proj)

    assert env["PWD"] == str(proj)
    assert env["OLDPWD"] == str(proj)


def test_no_project_dir_leaves_pwd_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("PWD", "/home/somebody")
    monkeypatch.setenv("OLDPWD", "/home/elsewhere")
    _isolate_xdg(tmp_path, monkeypatch)

    env = awf_subprocess_env()

    assert env["PWD"] == "/home/somebody"
    # Both keys move together with a project, so both must stay put without
    # one: a branch that rewrites OLDPWD only would otherwise pass.
    assert env["OLDPWD"] == "/home/elsewhere"


def test_path_with_spaces_and_cyrillic(tmp_path, monkeypatch):
    monkeypatch.setenv("PWD", "/home/somebody")
    _isolate_xdg(tmp_path, monkeypatch)
    proj = tmp_path / "проект с пробелами"
    proj.mkdir()

    env = awf_subprocess_env(project_dir=proj)

    assert env["PWD"] == str(proj)
    assert env["OLDPWD"] == str(proj)


def test_no_other_env_keys_are_touched(tmp_path, monkeypatch):
    """Boundary of the unit: only PWD/OLDPWD change.

    The TODO forbids touching HOME and TMPDIR; PATH is checked as well —
    a rewritten PATH would hide the opencode binary the stage spawns.
    """
    for key, value in (
        ("HOME", "/home/somebody"),
        ("TMPDIR", "/tmp/somebody"),
        ("PATH", "/usr/local/bin:/usr/bin:/bin"),
    ):
        monkeypatch.setenv(key, value)
    _isolate_xdg(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()

    env = awf_subprocess_env(project_dir=proj)

    assert env["HOME"] == "/home/somebody"
    assert env["TMPDIR"] == "/tmp/somebody"
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def test_parent_without_pwd_gets_it_created(tmp_path, monkeypatch):
    """The engine's own env may carry no PWD (a wrapper started it).

    opencode reads ``PWD`` and falls back to its cwd only when the key is
    absent — the project must be written, not merely overwritten. A ``str``
    project_dir is accepted too: the API passes ``Path``, scripts may not.
    """
    monkeypatch.delenv("PWD", raising=False)
    monkeypatch.delenv("OLDPWD", raising=False)
    _isolate_xdg(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()

    env = awf_subprocess_env(project_dir=str(proj))

    assert env["PWD"] == str(proj)
    assert env["OLDPWD"] == str(proj)


def test_pwd_reaches_a_real_child_process(tmp_path, monkeypatch):
    """The dict is only a promise until a child sees it.

    The stage hands this env to ``Popen``; here a real child reports what it
    got, so a future refactor that builds PWD but drops it before the spawn
    (a wrapper env, ``.update()`` on another dict) has to fail.
    """
    monkeypatch.setenv("PWD", "/home/somebody")
    _isolate_xdg(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()

    env = awf_subprocess_env(project_dir=proj)
    child = subprocess.run(
        [sys.executable, "-c",
         "import os; print(os.environ['PWD']); print(os.environ['OLDPWD'])"
         "; print(os.getcwd())"],
        cwd=str(proj), env=env, capture_output=True, text=True, check=True,
    )

    pwd, oldpwd, cwd = child.stdout.splitlines()
    assert pwd == str(proj)
    assert oldpwd == str(proj)
    # PWD and the real cwd must name one directory: opencode chdir's to PWD,
    # so a stage whose cwd and PWD disagree lands in the wrong tree.
    assert cwd == str(proj)


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    for name in ("inbox", "outbox", "logs", "context", "handoff", "roles"):
        (proj / ".agentic" / name).mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(STAGE_TYPES, encoding="utf-8")
    (proj / ".agentic" / "inbox" / f"{TODO_ID}.md").write_text(
        TODO_TEXT, encoding="utf-8"
    )
    (proj / ".agentic" / "roles" / "developer.md").write_text(
        "# Developer role\n", encoding="utf-8"
    )
    return proj


def test_agent_stage_spawn_env_carries_project_pwd(tmp_path, monkeypatch):
    """The stage hands the runner an env whose PWD is the project."""
    _isolate_xdg(tmp_path, monkeypatch)
    monkeypatch.setenv("PWD", "/home/somebody")
    proj = _project(tmp_path)
    captured: dict = {}

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None, signal_holder=None,
                 **kwargs):
        captured["env"] = env
        captured["cwd"] = cwd

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)

    agent_stage.run_agent_stage(
        Stage(name="agent-dev", role="developer", kind="execute"),
        TODO_ID, proj, MODEL_CONFIG, proj / ".agentic" / "logs",
    )

    assert captured["env"]["PWD"] == str(proj)
    assert captured["env"]["OLDPWD"] == str(proj)
    # cwd and PWD must name one directory — the stage spawns in the project.
    assert Path(captured["cwd"]) == proj
