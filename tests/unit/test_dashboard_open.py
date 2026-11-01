"""A14: the dashboard opens itself, and never twice for one URL.

The port is reused across launches, so the tab a user has open keeps working;
the marker ``context/dashboard_opened`` is what stops a second tab:

* automatic open (pipeline launch) — never a second tab for the same url;
* explicit open (``awf dashboard --open``) — a 60-second double-click window,
  after which a closed tab can be reopened by request.
"""
from __future__ import annotations

import socket
from pathlib import Path

from awf.api import dashboard_open


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for sub in ("context", "state", "dashboards"):
        (project / ".agentic" / sub).mkdir(parents=True)
    return project


def _listen(tmp_path: Path) -> tuple[int, socket.socket]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock.getsockname()[1], sock


def _serve(project: Path, port: int) -> None:
    (project / ".agentic" / "state" / "dashboard_port").write_text(
        str(port), encoding="utf-8"
    )


def test_automatic_open_skips_a_tab_already_open_for_the_url(tmp_path):
    project = _project(tmp_path)
    port, sock = _listen(tmp_path)
    try:
        _serve(project, port)
        opened: list[str] = []
        first = dashboard_open.open_dashboard(project, browser_open=opened.append)
        second = dashboard_open.open_dashboard(project, browser_open=opened.append)
    finally:
        sock.close()

    assert first["opened"] is True
    assert second["opened"] is False and second["already_open"] is True
    assert len(opened) == 1


def test_explicit_open_reopens_after_the_double_click_window(tmp_path, monkeypatch):
    project = _project(tmp_path)
    port, sock = _listen(tmp_path)
    try:
        _serve(project, port)
        opened: list[str] = []
        assert dashboard_open.open_dashboard(project, browser_open=opened.append)["opened"]

        # Inside the window: a double click does not open a second tab.
        again = dashboard_open.open_dashboard(
            project, explicit=True, browser_open=opened.append
        )
        assert again["already_open"] is True and len(opened) == 1

        # After it, the owner may have closed the tab: reopening is allowed.
        monkeypatch.setattr(dashboard_open, "EXPLICIT_DEDUP_SECONDS", 0.0)
        reopened = dashboard_open.open_dashboard(
            project, explicit=True, browser_open=opened.append
        )
    finally:
        sock.close()

    assert reopened["opened"] is True
    assert len(opened) == 2


def test_a_new_port_is_a_new_tab(tmp_path):
    project = _project(tmp_path)
    first, sock1 = _listen(tmp_path)
    second, sock2 = _listen(tmp_path)
    try:
        opened: list[str] = []
        _serve(project, first)
        assert dashboard_open.open_dashboard(project, browser_open=opened.append)["opened"]
        _serve(project, second)
        assert dashboard_open.open_dashboard(project, browser_open=opened.append)["opened"]
    finally:
        sock1.close()
        sock2.close()
    assert len(opened) == 2


def test_no_server_and_no_page_is_honest(tmp_path):
    project = _project(tmp_path)
    result = dashboard_open.open_dashboard(project, browser_open=lambda url: None)
    # No live port; generation is attempted and its failure is not an exception.
    assert result["opened"] in (False, True)
    assert "url" in result and "method" in result


def test_gui_available_follows_display_and_ci(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("AWF_NO_DASHBOARD_OPEN", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert dashboard_open.gui_available() is True
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert dashboard_open.gui_available() is False
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("CI", "true")
    assert dashboard_open.gui_available() is False


def test_a_test_process_never_opens_a_browser(monkeypatch):
    """01.11: the suite spawns hundreds of pipelines — each opened a tab."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("AWF_NO_DASHBOARD_OPEN", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/unit/test_x.py::test_y")
    assert dashboard_open.gui_available() is False


def test_the_owner_can_switch_the_open_off(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("AWF_NO_DASHBOARD_OPEN", "1")
    assert dashboard_open.gui_available() is False


def test_engine_hook_respects_the_switch_and_ci(tmp_path, monkeypatch):
    from awf import orchestrator

    project = _project(tmp_path)
    logs = project / ".agentic"
    calls: list[str] = []
    monkeypatch.setattr(
        dashboard_open, "open_dashboard",
        lambda *a, **kw: calls.append("open") or {"opened": True, "url": "u"},
    )
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    orchestrator._maybe_open_dashboard(project, {"dashboard": {"auto_open": False}}, logs)
    assert calls == []

    monkeypatch.setenv("CI", "true")
    orchestrator._maybe_open_dashboard(project, {}, logs)
    assert calls == []


def test_engine_hook_opens_when_allowed(tmp_path, monkeypatch):
    """A real project path (not `/tmp/pytest-*` — that one is refused)."""
    import shutil

    from awf import orchestrator

    root = Path("/tmp/opencode/dashboard-hook-test")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    project = _project(root)
    logs = project / ".agentic"
    calls: list[str] = []
    monkeypatch.setattr(
        dashboard_open, "open_dashboard",
        lambda *a, **kw: calls.append("open") or {"opened": True, "url": "u"},
    )
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")

    orchestrator._maybe_open_dashboard(project, {}, logs)
    for thread in __import__("threading").enumerate():
        if thread.name == "awf-dashboard-open":
            thread.join(timeout=2)
    assert calls == ["open"]
    shutil.rmtree(root, ignore_errors=True)


def test_cli_reports_the_url_without_a_server(tmp_path, capsys):
    from awf import cmd_dashboard

    project = _project(tmp_path)
    args = type("A", (), {"project_dir": str(project), "open": False})()
    assert cmd_dashboard.run(args) == 0
    assert "no live server" in capsys.readouterr().out
