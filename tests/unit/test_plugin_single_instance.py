"""A45 (TODO-0360): один экземпляр MCP-плагина на проект, без дублей.

Дефект (03.11): под одним ``opencode serve`` жили две копии
``agent_workflow_ui``, каждая со своим портом (наблюдались 47451 и 34405),
плюс копия на каждый ``opencode run``. Инварианты юнита:

1. Второй запуск на том же проекте не поднимает второй порт — он отвечает
   «использую экземпляр <pid> на порту <n>».
2. Воркер (``opencode run``, маркер ``AWF_BACKGROUND_CHILD``) — tool-only:
   без дашборд-порта и без вкладок.
3. ``awf doctor`` показывает число экземпляров проекта.

QA (TODO-0360, stage agent-qa-review) added the regression block at the end:
``release()`` must keep the lock file (unlinking it lets two processes own
two different inodes), a leftover file with a dead pid must not block a new
owner, and the reuse path must be honest while the owner has not published a
port yet.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import webbrowser
from pathlib import Path

import pytest
from agent_workflow_ui.config import Config
from agent_workflow_ui.state import get_registry, serves_forms, set_instance_mode

from agent_workflow_ui import instance


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".agentic" / "state").mkdir(parents=True, exist_ok=True)
    return root


def _config(tmp_path: Path) -> Config:
    tmp = tmp_path / "run"
    inputs = tmp / "inputs"
    templates = tmp / "templates"
    dashboards = tmp / "dashboards"
    for path in (inputs, templates, dashboards, tmp / "scratch"):
        path.mkdir(parents=True, exist_ok=True)
    return Config(
        inputs_dir=inputs,
        templates_dir=templates,
        dashboards_dir=dashboards,
        http_port=0,
        open_browser_cmd="false",
        temp_dir=tmp / "scratch",
        default_ttl_seconds=60,
    )


@pytest.fixture(autouse=True)
def _owner_mode():
    """Instance mode is process-global; every test starts and ends as owner."""
    set_instance_mode("owner")
    yield
    set_instance_mode("owner")


def test_second_launch_reuses_owner_port(tmp_path):
    """Second plugin process: no second bind, honest pid/port line."""
    project = _project(tmp_path)
    config = _config(tmp_path)
    first = instance.boot(config, project, get_registry(), tool_only=False)
    try:
        assert first.mode == "owner"
        assert first.server is not None
        assert first.port and first.port > 0

        second = instance.boot(config, project, get_registry(), tool_only=False)

        assert second.mode == "reuse"
        assert second.server is None, "second launch must not bind another port"
        assert second.port == first.port
        assert f"использую экземпляр {os.getpid()}" in second.message
        assert f"на порту {first.port}" in second.message
        # The owner published its port into the lock for other instances.
        published = json.loads(instance.lock_path(project).read_text(encoding="utf-8"))
        assert published["port"] == first.port
        assert published["pid"] == os.getpid()
    finally:
        if first.server is not None:
            first.server.shutdown()
        if first.lock is not None:
            first.lock.release()


def test_worker_is_tool_only_without_port_or_tabs(tmp_path, monkeypatch):
    """A worker serves tools only: no dashboard port, no browser tab."""
    monkeypatch.setenv(instance.WORKER_MARKER_ENV, "1")
    opened: list[tuple] = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **kw: opened.append(a))

    project = _project(tmp_path)
    plan = instance.plan_startup(project)
    assert plan.mode == "tool-only"
    assert plan.http_port is None
    assert not plan.starts_server

    result = instance.boot(_config(tmp_path), project, get_registry(), tool_only=True)

    assert result.mode == "tool-only"
    assert result.server is None
    assert result.port is None
    assert opened == []
    # A tool-only worker never takes the lock: the main instance can start.
    assert instance.read_owner(instance.lock_path(project)) is None
    # The mode the entry point records decides whether forms are served at
    # all — a worker has no registry behind the owner's URL.
    set_instance_mode(result.mode)
    assert not serves_forms(), "tool-only must not serve the owner's form URL"


@pytest.mark.parametrize("mode", ["tool-only", "reuse"])
def test_non_owner_open_form_refuses_instead_of_dead_url(plugin_setup, mode):
    """Neither a worker nor a reuse client has a registry behind the URL —
    both must refuse with the mode named, not hand out a dead submit link."""
    from agent_workflow_ui.tools.forms import open_form

    set_instance_mode(mode)
    result = asyncio.run(
        open_form(template="project-setup", data={"available_roles": ["worker"]})
    )

    assert result["form_id"] == ""
    assert result["submit_url"] == ""
    assert mode in result["error"]


def _doctor_surfaces(monkeypatch, tmp_path: Path) -> None:
    """Keep the doctor fast and hermetic: no `opencode models`, no version drift."""
    opencode = tmp_path / "opencode.json"
    opencode.write_text(json.dumps({"providers": {}}), encoding="utf-8")
    monkeypatch.setattr("awf.doctor.xdg.opencode_config_file", lambda: opencode)
    monkeypatch.setattr("awf.versions.process_commit", lambda: "proc0000")
    monkeypatch.setattr("awf.versions.process_version", lambda: "3.0.0")
    monkeypatch.setattr("awf.versions.plugin_version", lambda: "3.0.0")
    monkeypatch.setattr("awf.versions.install_metadata", lambda name: "3.0.0")
    monkeypatch.setattr(
        "awf.api.model_check.check_model_config",
        lambda project_dir: {
            "models": [],
            "warnings": [],
            "providers_available": [],
            "role_files": [],
        },
    )


def test_doctor_reports_instance_count(tmp_path, monkeypatch):
    """`awf doctor` names how many plugin instances serve the project."""
    from awf.doctor import run_doctor

    _doctor_surfaces(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    project = _project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: T\n", encoding="utf-8"
    )
    (project / ".agentic" / "state" / "plugin.lock").write_text(
        json.dumps({"pid": os.getpid(), "port": 47451, "project": str(project)}),
        encoding="utf-8",
    )
    xdg_dir = tmp_path / "xdg" / "awf-plugin"
    xdg_dir.mkdir(parents=True)
    (xdg_dir / "deadbeef.lock").write_text(
        json.dumps({"pid": os.getpid(), "port": 34405, "project": str(project)}),
        encoding="utf-8",
    )

    report = run_doctor(project)

    line = next(ln for ln in report.lines if "plugin instances" in ln)
    assert "экземпляров 2" in line, line
    assert "47451" in line and "34405" in line, line
    assert "владелец — да" in line, line


# --- QA (TODO-0360): regression tests for the lock's failure modes -----------


@pytest.mark.skipif(instance.fcntl is None, reason="flock is POSIX-only")
def test_release_keeps_one_lock_inode(tmp_path):
    """The released lock must stay the *same* inode for the next starter.

    ``release()`` used to unlink the file after unlocking. Then a process
    that had already opened the path held an fd on the orphan inode and
    could win the lock there, while the next starter created a new file and
    won too — two owners, two dashboard servers, exactly the defect A45
    fixes. This reproduces the window without timing luck.
    """
    fcntl = instance.fcntl
    project = _project(tmp_path)
    path = instance.lock_path(project)
    owner = instance.acquire(project, timeout=0)
    assert owner.owned
    contender = os.open(path, os.O_RDWR)  # a second start that lost the lock
    later = None
    try:
        with pytest.raises(OSError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

        owner.release()
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)  # wins that inode

        later = instance.acquire(project, timeout=0)
        assert not later.owned, (
            "release() unlinked the lock file: a second inode became lockable "
            "while the contender still owns the first one"
        )
    finally:
        if later is not None and later.owned:
            later.release()
        os.close(contender)


def test_leftover_lock_with_dead_pid_does_not_block_owner(tmp_path):
    """A crashed owner leaves a file; the kernel dropped its lock, so a new
    start must become the owner instead of reusing a dead port."""
    project = _project(tmp_path)
    path = instance.lock_path(project)
    proc = subprocess.Popen(["true"])
    proc.wait()
    path.write_text(
        json.dumps({"pid": proc.pid, "port": 34405, "project": str(project)}),
        encoding="utf-8",
    )
    assert instance.read_owner(path) is None, "dead pid must not read as owner"

    plan = instance.plan_startup(project, tool_only=False, timeout=0)
    try:
        assert plan.mode == "owner"
        assert plan.starts_server
    finally:
        plan.lock.release()


def test_reuse_before_owner_published_port_is_honest(tmp_path):
    """Owner took the lock but has not bound yet: reuse must not invent a
    port and must say so instead of claiming a ready instance."""
    project = _project(tmp_path)
    holder = instance.acquire(project, timeout=0)
    try:
        assert holder.owned
        plan = instance.plan_startup(project, tool_only=False, timeout=0)
        assert plan.mode == "reuse"
        assert plan.http_port is None
        assert not plan.starts_server
        assert "порт ещё не опубликован" in plan.message
    finally:
        holder.release()


def test_corrupt_pid_in_lock_is_not_an_owner(tmp_path):
    """Architect review R2: a pid outside the OS range must not raise."""
    path = tmp_path / "corrupt.lock"
    path.write_text(
        json.dumps({"pid": 2**40, "port": 1, "project": str(tmp_path)}),
        encoding="utf-8",
    )
    assert instance.read_owner(path) is None
    path.write_text("{not json", encoding="utf-8")
    assert instance.read_owner(path) is None


def test_doctor_ignores_dead_owner(tmp_path, monkeypatch):
    """A corpse in the lock file is not an instance: doctor must count 0."""
    from awf.doctor import run_doctor

    _doctor_surfaces(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    project = _project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: T\n", encoding="utf-8"
    )
    proc = subprocess.Popen(["true"])
    proc.wait()
    (project / ".agentic" / "state" / "plugin.lock").write_text(
        json.dumps({"pid": proc.pid, "port": 47451, "project": str(project)}),
        encoding="utf-8",
    )

    report = run_doctor(project)

    line = next(ln for ln in report.lines if "plugin instances" in ln)
    assert "экземпляров 0" in line, line
    assert "владелец — нет" in line, line


def test_tool_only_env_gate(monkeypatch):
    """The env gate both modes read; a truthy value wins, '0' does not.

    The suite itself inherits AWF_BACKGROUND_CHILD=1 from the awf worker, so
    this gate is not a corner case — it decides what every ``main()`` in a
    test does.
    """
    monkeypatch.delenv(instance.TOOL_ONLY_ENV, raising=False)
    monkeypatch.setenv(instance.WORKER_MARKER_ENV, "1")
    assert instance.tool_only_requested()

    monkeypatch.delenv(instance.WORKER_MARKER_ENV)
    monkeypatch.setenv(instance.TOOL_ONLY_ENV, "0")
    assert not instance.tool_only_requested()

    monkeypatch.setenv(instance.TOOL_ONLY_ENV, "true")
    assert instance.tool_only_requested()
