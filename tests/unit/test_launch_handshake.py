"""OPS IMP6 (R6, AC18–AC20, AC27, INV7): the launch handshake.

T17 — the version pair and the drift warning reach ``start_pipeline`` /
``continue_pipeline`` / ``run_next``; T18 — the CLI lines; T19 — an
unimportable plugin is ``unknown`` with no pair. The warning is advisory:
the launch runs and the outcome does not change (INV7). The commit line
covers the IMP5 audit note (CTX1): equal versions, different code — or a
pinned runner whose process has no commit evidence — is still named.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf import api, cmd_start, versions
from awf.api import pipeline as api_pipeline
from awf.api._results import StartResult


@pytest.fixture
def facts(monkeypatch):
    """A hermetic version surface: no real plugin import, no real git reads."""
    monkeypatch.setattr(versions, "process_version", lambda: "2.1.0")
    monkeypatch.setattr(versions, "plugin_version", lambda: "2.1.0")
    monkeypatch.setattr(versions, "plugin_requirement", lambda: "")
    monkeypatch.setattr(versions, "process_commit", lambda: "proc1234")
    monkeypatch.setattr(versions, "repo_commit", lambda path: "proc1234")


def _pinned(tmp_path: Path, version: str = "2.1.0") -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "awf").mkdir(parents=True)
    (checkout / "awf" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    return checkout


# ── T17/T19: the version facts themselves ───────────────────────────────


def test_matching_versions_have_no_warning(facts) -> None:
    """AC18: the pair matches → both fields filled, ``warning`` empty."""
    facts_ = versions.handshake(Path("/nonexistent"), {})
    assert facts_ == {
        "runner_version": "2.1.0",
        "plugin_version": "2.1.0",
        "warning": "",
    }


def test_plugin_drift_names_both_versions(facts, monkeypatch) -> None:
    """AC18: plugin 2.0.0 against awf 2.1.0 → warning with both."""
    monkeypatch.setattr(versions, "plugin_version", lambda: "2.0.0")
    warning = versions.handshake(Path("/nonexistent"), {})["warning"]
    assert warning == (
        "version drift: process 2.1.0, plugin 2.0.0 — "
        "run awf doctor; restart the opencode session"
    )


def test_without_runner_dir_runner_version_is_process(facts) -> None:
    """AC19: no pin → the process version, and no runner in the warning."""
    facts_ = versions.handshake(Path("/nonexistent"), {})
    assert facts_["runner_version"] == "2.1.0"
    assert "runner" not in facts_["warning"]


def test_pinned_runner_drift_names_the_dir(facts, tmp_path) -> None:
    """K7: the pinned version wins, and the warning carries its path."""
    checkout = _pinned(tmp_path, "2.0.0")
    config = {"automation": {"runner_dir": str(checkout)}}
    facts_ = versions.handshake(tmp_path, config)
    assert facts_["runner_version"] == "2.0.0"
    assert "runner 2.0.0" in facts_["warning"]
    assert str(checkout) in facts_["warning"]
    assert "process 2.1.0" in facts_["warning"]


def test_unknown_plugin_makes_no_pair(facts, monkeypatch) -> None:
    """AC27: an unimportable plugin is ``unknown`` and forms no drift pair."""
    monkeypatch.setattr(versions, "plugin_version", lambda: versions.UNKNOWN)
    facts_ = versions.handshake(Path("/nonexistent"), {})
    assert facts_["plugin_version"] == "unknown"
    assert facts_["runner_version"] == "2.1.0"
    assert facts_["warning"] == ""


def test_unimportable_plugin_is_unknown_through_the_import(
    monkeypatch,
) -> None:
    """AC27: the real import failure is ``unknown``, named but pairless."""
    monkeypatch.setitem(sys.modules, "agent_workflow_ui", None)
    assert versions.plugin_version() == versions.UNKNOWN
    facts_ = versions.handshake(Path("/nonexistent"), {})
    assert facts_["plugin_version"] == "unknown"
    assert facts_["runner_version"] == versions.process_version()
    assert facts_["warning"] == ""


def test_process_without_commit_evidence_is_named(facts, monkeypatch, tmp_path) -> None:
    """CTX1 (audit IMP5): runner has a commit, the process has none."""
    checkout = _pinned(tmp_path)
    monkeypatch.setattr(versions, "process_commit", lambda: "")
    facts_ = versions.handshake(
        tmp_path, {"automation": {"runner_dir": str(checkout)}}
    )
    assert "process has no commit evidence" in facts_["warning"]
    assert "proc1234" in facts_["warning"]


def test_equal_versions_different_commit_is_named(facts, monkeypatch, tmp_path) -> None:
    """AUD-0293-2: the same version string, different code."""
    checkout = _pinned(tmp_path)
    monkeypatch.setattr(versions, "repo_commit", lambda path: "run5678")
    facts_ = versions.handshake(
        tmp_path, {"automation": {"runner_dir": str(checkout)}}
    )
    assert "runner commit run5678" in facts_["warning"]
    assert "process commit proc1234" in facts_["warning"]


# ── T17: the fields on the three launch results ─────────────────────────


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="OpsHandshake")
    inbox = tmp_git_repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "TODO-0001.md").write_text("# Task\n", encoding="utf-8")
    (inbox / "TODO-0001.ready").write_text("", encoding="utf-8")
    return tmp_git_repo


def _fake_background(proj: Path):
    log = proj / ".agentic" / "logs" / "pipeline.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    return lambda *a, **kw: (4242, log, None)


def _install_fake_spawn(monkeypatch, proj: Path) -> None:
    monkeypatch.setattr(api_pipeline, "start_in_background", _fake_background(proj))
    monkeypatch.setattr(api_pipeline, "_verify_child_alive", lambda pid, lf: True)


def test_start_result_carries_the_versions(facts, tmp_git_repo, monkeypatch) -> None:
    proj = _project(tmp_git_repo)
    _install_fake_spawn(monkeypatch, proj)
    result = api.start_pipeline(proj, background=True, todo_id="TODO-0001")
    assert result.run_mode == "background"
    assert result.runner_version == "2.1.0"
    assert result.plugin_version == "2.1.0"
    assert result.warning == ""


def test_drift_warning_does_not_change_the_launch(
    facts, tmp_git_repo, monkeypatch
) -> None:
    """AC18/AC20: a real launch with drift still launches; rc untouched."""
    proj = _project(tmp_git_repo)
    monkeypatch.setattr(versions, "plugin_version", lambda: "2.0.0")
    _install_fake_spawn(monkeypatch, proj)
    result = api.start_pipeline(proj, background=True, todo_id="TODO-0001")
    assert result.run_mode == "background"
    assert result.exit_code is None
    assert "2.0.0" in result.warning and "2.1.0" in result.warning


def test_unknown_plugin_does_not_block_the_launch(
    facts, tmp_git_repo, monkeypatch
) -> None:
    """AC27: ``unknown`` is not a drift and the launch runs."""
    proj = _project(tmp_git_repo)
    monkeypatch.setattr(versions, "plugin_version", lambda: versions.UNKNOWN)
    _install_fake_spawn(monkeypatch, proj)
    result = api.start_pipeline(proj, background=True, todo_id="TODO-0001")
    assert result.run_mode == "background"
    assert result.plugin_version == "unknown"
    assert result.warning == ""


def test_version_read_failure_keeps_the_launch(
    facts, tmp_git_repo, monkeypatch
) -> None:
    """INV7: a failing version read leaves the fields empty, launch intact."""
    proj = _project(tmp_git_repo)

    def _boom() -> str:
        raise RuntimeError("version surface unreadable")

    monkeypatch.setattr(versions, "process_version", _boom)
    _install_fake_spawn(monkeypatch, proj)
    result = api.start_pipeline(proj, background=True, todo_id="TODO-0001")
    assert result.run_mode == "background"
    assert result.runner_version == ""
    assert result.plugin_version == ""
    assert result.warning == ""


def test_continue_result_carries_the_versions(
    facts, tmp_git_repo, monkeypatch
) -> None:
    proj = _project(tmp_git_repo)
    _install_fake_spawn(monkeypatch, proj)
    result = api.continue_pipeline(proj, background=True, todo_id="TODO-0001")
    assert result.run_mode == "background"
    assert result.runner_version == "2.1.0"
    assert result.plugin_version == "2.1.0"


def test_run_next_result_carries_the_versions(
    facts, tmp_git_repo, monkeypatch
) -> None:
    proj = _project(tmp_git_repo)
    api.run_start(proj, queue=["TODO-0001"])
    log = proj / ".agentic" / "logs" / "pipeline.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        api_pipeline,
        "start_pipeline",
        lambda *a, **kw: StartResult(
            run_mode="background",
            run_id=4242,
            log_file=str(log),
            exit_code=None,
            message="awf start running in background (PID 4242). "
            "GO IDLE — wait for user.",
        ),
    )
    result = api.run_next(proj)
    assert result.action == "started"
    assert result.runner_version == "2.1.0"
    assert result.plugin_version == "2.1.0"


# ── T18: the CLI line and the log ───────────────────────────────────────


def test_cli_prints_versions_and_warns(facts, tmp_path, monkeypatch, capsys) -> None:
    log = tmp_path / "run.log"
    log.write_text("", encoding="utf-8")
    warning = (
        "version drift: runner 2.0.0, process 2.1.0 — "
        "run awf doctor; restart the opencode session"
    )
    result = StartResult(
        run_mode="background",
        run_id=1,
        log_file=str(log),
        exit_code=None,
        message="started",
        runner_version="2.0.0",
        plugin_version="2.1.0",
        warning=warning,
    )
    monkeypatch.setattr(cmd_start.api, "start_pipeline", lambda **kw: result)
    args = SimpleNamespace(
        command="start",
        project_dir=str(tmp_path),
        background=True,
        auto=False,
        pipeline=None,
        from_stage=None,
        timeout=None,
        todo="",
    )
    rc = cmd_start.run(args)
    err = capsys.readouterr().err
    assert "runner: 2.0.0, plugin: 2.1.0" in err
    assert "version drift" in err
    logged = log.read_text(encoding="utf-8")
    assert "runner: 2.0.0, plugin: 2.1.0" in logged  # T18: versions in the log
    assert "version drift" in logged
    assert rc == 0


def test_cli_logs_versions_without_drift(tmp_path, monkeypatch) -> None:
    """T18/K7: a healthy run still leaves the version pair in the launch log."""
    log = tmp_path / "run.log"
    log.write_text("", encoding="utf-8")
    result = StartResult(
        run_mode="background",
        run_id=1,
        log_file=str(log),
        exit_code=None,
        message="started",
        runner_version="2.1.0",
        plugin_version="2.1.0",
    )
    monkeypatch.setattr(cmd_start.api, "start_pipeline", lambda **kw: result)
    args = SimpleNamespace(
        command="start",
        project_dir=str(tmp_path),
        background=True,
        auto=False,
        pipeline=None,
        from_stage=None,
        timeout=None,
        todo="",
    )
    rc = cmd_start.run(args)
    logged = log.read_text(encoding="utf-8")
    assert "runner: 2.1.0, plugin: 2.1.0" in logged
    assert "version drift" not in logged
    assert rc == 0
