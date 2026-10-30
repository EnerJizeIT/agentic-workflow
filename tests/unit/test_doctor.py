"""R5 (ops reliability): ``awf doctor`` — report, rc and secrecy.

The doctor is judged on a fixture project, not on the user's install: every
version surface is monkeypatched (T13–T16). What is exercised is the report
assembly, the WARN rules of R5/AC14–AC17/AC23 and the exit-code contract:
0 — no WARN, 1 — WARNs, 2 — no ``.agentic/``.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from awf import cmd_doctor, versions
from awf.doctor import run_doctor

#: A config whose every key is read — the "healthy project" half of AC14.
CLEAN_CONFIG = {
    "project": {"name": "Test"},
    "config_version": 2,
    "discipline": {"dirty_policy": "isolate"},
    "verification": {"test_cmd": "pytest -q", "lint_cmd": "ruff check ."},
    "automation": {"stage_timeout_seconds": 3600},
    "models": {"worker": {"model": "p/one", "agent_name": "worker"}},
}

FAKE_MODELS = {
    "models": [{"role": "worker", "model": "p/one", "provider": "p", "valid": True}],
    "warnings": [],
    "providers_available": ["p"],
    "role_files": [],
}


@pytest.fixture
def project(tmp_path):
    """A minimal awf project: ``.agentic/config.yaml`` and nothing else."""
    root = tmp_path / "project"
    (root / ".agentic").mkdir(parents=True)
    _write_config(root, CLEAN_CONFIG)
    return root


def _write_config(root, config) -> None:
    (root / ".agentic" / "config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Hermetic surfaces: opencode.json, the process commit, the model check."""
    opencode = tmp_path / "opencode.json"
    opencode.write_text(json.dumps({"provider": {}}), encoding="utf-8")
    monkeypatch.setattr("awf.doctor.xdg.opencode_config_file", lambda: opencode)
    monkeypatch.setattr("awf.versions.process_commit", lambda: "proc0000")
    monkeypatch.setattr(
        "awf.api.model_check.check_model_config", lambda project_dir: dict(FAKE_MODELS)
    )
    return opencode


def _versions(monkeypatch, *, process="2.3.0", plugin="2.3.0", metadata="2.3.0"):
    monkeypatch.setattr("awf.versions.process_version", lambda: process)
    monkeypatch.setattr("awf.versions.plugin_version", lambda: plugin)
    monkeypatch.setattr("awf.versions.install_metadata", lambda name: metadata)


def _lines(report) -> str:
    return "\n".join(report.lines)


def test_healthy_project_is_ok(project, env, monkeypatch):
    """AC14: everything agrees, no runner_dir → rc 0 and only ok lines."""
    _versions(monkeypatch)
    report = run_doctor(project)
    assert report.rc == 0
    assert report.warnings == []
    assert report.lines
    assert all(line.startswith("ok  ") for line in report.lines), _lines(report)


def test_runner_version_drift_is_a_warn(project, env, monkeypatch):
    """AC14: a pinned checkout with another version → rc 1, WARN names both."""
    runner = project.parent / "runner"
    (runner / "awf").mkdir(parents=True)
    (runner / "awf" / "__init__.py").write_text('__version__ = "2.0.0"\n')
    config = dict(CLEAN_CONFIG)
    config["automation"] = {"runner_dir": str(runner)}
    _write_config(project, config)
    _versions(monkeypatch)

    report = run_doctor(project)
    assert report.rc == 1
    runner_warns = [line for line in report.warnings if line.startswith("WARN  runner:")]
    assert len(runner_warns) == 1, _lines(report)
    assert "2.0.0" in runner_warns[0]
    assert "2.3.0" in runner_warns[0]
    assert str(runner) in runner_warns[0]


def test_same_version_different_code_is_a_warn(project, env, monkeypatch):
    """AUD-0293-2: equal versions do not identify code — commits do."""
    runner = project.parent / "runner"
    (runner / "awf").mkdir(parents=True)
    (runner / "awf" / "__init__.py").write_text('__version__ = "2.3.0"\n')
    config = dict(CLEAN_CONFIG)
    config["automation"] = {"runner_dir": str(runner)}
    _write_config(project, config)
    _versions(monkeypatch)
    monkeypatch.setattr("awf.versions.repo_commit", lambda path: "run1234")

    report = run_doctor(project)
    assert report.rc == 1
    assert any(
        "same version, different code" in line and "run1234" in line
        for line in report.warnings
    ), _lines(report)


def test_no_agentic_is_rc_2(tmp_path, env, monkeypatch):
    """AC15: no ``.agentic/`` → rc 2, one line, no traceback."""
    _versions(monkeypatch)
    report = run_doctor(tmp_path)
    assert report.rc == 2
    assert len(report.lines) == 1
    assert "no .agentic/" in report.lines[0]
    assert report.warnings == []


def test_unreadable_opencode_json_warns_and_other_sections_stay(project, env, monkeypatch):
    """AC15: a broken opencode.json is a WARN, the other sections still report."""
    env.write_text("{not json", encoding="utf-8")
    _versions(monkeypatch)

    report = run_doctor(project)
    assert report.rc == 1
    assert any("opencode.json" in line for line in report.warnings), _lines(report)
    text = _lines(report)
    assert "awf:" in text
    assert "config keys:" in text


def test_dead_keys_are_named_and_values_stay_secret(project, env, monkeypatch):
    """AC16/AC17: dead keys are named; values and tokens are never printed."""
    config = dict(CLEAN_CONFIG)
    config["retry"] = {"max_attempts": 3, "backoff_seconds": 0}
    config["api_key"] = "sk-secret"
    _write_config(project, config)
    _versions(monkeypatch)

    report = run_doctor(project)
    text = _lines(report)
    assert report.rc == 1
    assert "retry.max_attempts" in text
    assert "retry.backoff_seconds" in text
    assert "api_key" in text
    assert "sk-secret" not in text


def test_config_without_dead_keys_has_no_key_warn(project, env, monkeypatch):
    """AC16: a clean config gives no config-key WARN."""
    _versions(monkeypatch)
    report = run_doctor(project)
    assert not any(line.startswith("WARN  config keys:") for line in report.lines)


def test_plugin_and_metadata_warnings_are_separate(project, env, monkeypatch):
    """AC23: plugin vs awf is one WARN, metadata vs module is another."""
    monkeypatch.setattr("awf.versions.process_version", lambda: "2.3.0")
    monkeypatch.setattr("awf.versions.plugin_version", lambda: "2.0.0")
    monkeypatch.setattr(
        "awf.versions.install_metadata",
        lambda name: "1.6.0" if name == "agent-workflow-ui" else "2.3.0",
    )

    report = run_doctor(project)
    assert report.rc == 1
    plugin_warn = [line for line in report.warnings if line.startswith("WARN  plugin:")]
    meta_warn = [
        line
        for line in report.warnings
        if line.startswith("WARN  agent-workflow-ui metadata:")
    ]
    assert len(plugin_warn) == 1, _lines(report)
    assert "2.0.0" in plugin_warn[0] and "2.3.0" in plugin_warn[0]
    assert len(meta_warn) == 1, _lines(report)
    assert "1.6.0" in meta_warn[0] and "2.0.0" in meta_warn[0]
    assert "stale, reinstall the package" in meta_warn[0]


def test_plugin_floor_unmet_with_equal_versions_names_the_floor(
    project, env, monkeypatch
):
    """AUD-0296-3: equal versions + unmet ``awf>=`` floor is not "!=" advice."""
    _versions(monkeypatch, process="2.1.0", plugin="2.1.0", metadata="2.1.0")
    monkeypatch.setattr("awf.versions.plugin_requirement", lambda: "2.2.0")

    report = run_doctor(project)
    plugin_warn = [line for line in report.warnings if line.startswith("WARN  plugin:")]
    assert len(plugin_warn) == 1, _lines(report)
    assert "requires awf>=2.2.0" in plugin_warn[0], plugin_warn[0]
    assert "upgrade awf" in plugin_warn[0], plugin_warn[0]
    assert "!=" not in plugin_warn[0], plugin_warn[0]
    assert "restart the opencode session" not in plugin_warn[0], plugin_warn[0]


def test_version_warning_floor_unmet_does_not_claim_equal_versions_differ():
    """K5: the handshake line names the floor instead of "plugin != process"."""
    line = versions.version_warning(
        process="2.1.0", runner="2.1.0", plugin="2.1.0", plugin_required="2.2.0"
    )
    assert "plugin 2.1.0 requires awf>=2.2.0 (process 2.1.0)" in line, line
    assert "!=" not in line, line
    assert line.endswith("— run awf doctor; upgrade awf"), line


def test_unknown_plugin_forms_no_drift_pair(project, env, monkeypatch):
    """AC27: an unimportable plugin is ``unknown`` — no pair WARN, rc stays 0."""
    _versions(monkeypatch, plugin="unknown")

    report = run_doctor(project)
    assert report.rc == 0
    assert not any(line.startswith("WARN  plugin:") for line in report.lines)
    assert any("plugin: unknown" in line for line in report.lines), _lines(report)


def test_model_check_exception_degrades_to_unknown(project, env, monkeypatch):
    """R5: a broken models surface is one ``unknown`` WARN, not a traceback."""
    _versions(monkeypatch)

    def boom(project_dir):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr("awf.api.model_check.check_model_config", boom)

    report = run_doctor(project)
    assert report.rc == 1
    assert any(
        line.startswith("WARN  models:") and "unknown (RuntimeError)" in line
        for line in report.lines
    ), _lines(report)
    assert "provider exploded" not in _lines(report)


def test_cmd_doctor_prints_lines_and_returns_rc(project, env, monkeypatch, capsys):
    """``awf doctor`` prints the report and returns its rc (D8)."""
    _versions(monkeypatch)
    rc = cmd_doctor.run(SimpleNamespace(project_dir=str(project)))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("\n") == len(run_doctor(project).lines)


# ─── K5: the version surface the launch handshake (IMP6) composes ───────


def test_version_warning_is_empty_when_versions_agree():
    """K5: matching versions produce no handshake warning."""
    assert versions.version_warning(
        process="2.2.0", runner="2.2.0", plugin="2.2.0"
    ) == ""


def test_version_warning_lists_only_drifted_participants():
    """K5: the runner is named with its pinned path, the process always."""
    line = versions.version_warning(
        process="2.2.0",
        runner="2.0.0",
        plugin="2.2.0",
        pinned_dir="/opt/awf",
    )
    assert line.startswith("version drift: runner 2.0.0 (pinned: /opt/awf), ")
    assert "process 2.2.0" in line
    assert "plugin" not in line
    assert line.endswith("— run awf doctor; restart the opencode session")


def test_version_warning_does_not_blame_the_plugin_for_a_newer_runner():
    """AUD-0296-3: the plugin follows the process, not the pinned runner."""
    line = versions.version_warning(
        process="2.2.0", runner="2.5.0", plugin="2.2.0"
    )
    assert "runner 2.5.0" in line
    assert "process 2.2.0" in line
    assert "plugin" not in line
    assert versions.plugin_drift_kind("2.2.0", "2.2.0") == ""


def test_version_warning_names_a_drifting_plugin():
    """K5: a plugin that lags the runner is named; unknown forms no pair."""
    line = versions.version_warning(
        process="2.2.0", runner="2.2.0", plugin="2.0.0"
    )
    assert "plugin 2.0.0" in line
    assert "runner" not in line
    assert versions.version_warning(
        process="2.2.0", runner="2.2.0", plugin=versions.UNKNOWN
    ) == ""
    assert versions.plugin_drift_kind("2.2.0", versions.UNKNOWN) == ""


def test_repo_commit_is_empty_outside_a_repository(tmp_path, monkeypatch):
    """AUD-0293-2: no commit evidence degrades to ``""``, never an error."""
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    assert versions.repo_commit(tmp_path) == ""
    assert versions.runner_drift("2.2.0", "2.0.0") is True
    assert versions.runner_drift("2.2.0", "2.2.0") is False


@pytest.mark.parametrize(
    ("dirname", "expected"),
    [
        ("awf-2.2.0.egg-info", "awf"),
        ("awf.egg-info", "awf"),
        ("agent_workflow_ui-2.2.0.dist-info", "agent-workflow-ui"),
        ("agent-workflow-ui-2.2.0.egg-info", "agent-workflow-ui"),
        ("agent-workflow-ui.egg-info", "agent-workflow-ui"),
    ],
)
def test_dist_project_cuts_the_version_not_the_name(tmp_path, dirname, expected):
    """AUD-0296-2: a dashed name must survive version stripping."""
    assert versions._dist_project(tmp_path / dirname) == expected


def test_unreadable_pinned_runner_does_not_blame_the_plugin(project, env, monkeypatch):
    """AC27: an unreadable pinned runner is ``unknown`` — no plugin pair.

    The runner row already reports the unreadable checkout; comparing the
    plugin against the string ``unknown`` would add a false "restart the
    session" diagnosis for a plugin that matches the running process.
    """
    config = dict(CLEAN_CONFIG)
    config["automation"] = {"runner_dir": str(project.parent / "missing")}
    _write_config(project, config)
    _versions(monkeypatch)

    report = run_doctor(project)
    assert report.rc == 1
    runner_warns = [
        line for line in report.warnings if line.startswith("WARN  runner:")
    ]
    assert len(runner_warns) == 1, _lines(report)
    assert "unknown" in runner_warns[0]
    plugin_warns = [
        line for line in report.lines if line.startswith("WARN  plugin:")
    ]
    assert not plugin_warns, _lines(report)
    assert any("plugin: 2.3.0" in line for line in report.lines), _lines(report)


def test_plugin_drift_ignores_an_unknown_runner():
    """AC27: ``unknown`` on the runner side forms no pair either."""
    assert versions.plugin_drift_kind(versions.UNKNOWN, "2.2.0") == ""
    line = versions.version_warning(
        process="2.2.0", runner=versions.UNKNOWN, plugin="2.2.0"
    )
    assert "runner unknown" in line
    assert "plugin" not in line


def test_cli_routes_doctor_and_returns_its_rc(project, env, monkeypatch, capsys):
    """R5: ``awf doctor`` is wired into the parser and the dispatcher."""
    from awf import cli

    _versions(monkeypatch)
    rc = cli.main(["doctor", "--project-dir", str(project)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("\n") == len(run_doctor(project).lines)

    missing = project.parent / "no-agentic"
    missing.mkdir()
    assert cli.main(["doctor", "--project-dir", str(missing)]) == 2
    assert "no .agentic/" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["", 42])
def test_invalid_runner_dir_is_a_warn_not_not_pinned(project, env, monkeypatch, bad):
    """R5: a value the launch refuses must not read as "not pinned" (healthy)."""
    config = dict(CLEAN_CONFIG)
    config["automation"] = {"runner_dir": bad}
    _write_config(project, config)
    _versions(monkeypatch)

    report = run_doctor(project)
    runner_warns = [
        line for line in report.warnings if line.startswith("WARN  runner:")
    ]
    assert len(runner_warns) == 1, _lines(report)
    assert "automation.runner_dir" in runner_warns[0]
    assert "launch will refuse" in runner_warns[0]
    assert "not pinned" not in _lines(report)


def test_socks_proxy_is_a_warn_without_the_value(project, env, monkeypatch):
    """A11: opencode 2.x fetch rejects socks; doctor names the variable only."""
    _versions(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "socks5h://127.0.0.1:10808")

    report = run_doctor(project)
    assert report.rc == 1
    warns = [line for line in report.warnings if line.startswith("WARN  proxy:")]
    assert len(warns) == 1, _lines(report)
    assert "HTTPS_PROXY" in warns[0]
    assert "10808" not in "\n".join(report.lines)
