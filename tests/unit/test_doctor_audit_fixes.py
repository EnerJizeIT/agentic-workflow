"""R5 (ops reliability): the AUD-0296 fixes to ``awf doctor``.

Split from ``test_doctor.py`` to keep both files under the ratchet's 400-line
bar. The fixtures mirror that file: every version surface is monkeypatched, so
these cases judge the report rules, not the machine.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from awf import versions
from awf.doctor import run_doctor

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


def _write_config(root, config) -> None:
    (root / ".agentic" / "config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".agentic").mkdir(parents=True)
    _write_config(root, CLEAN_CONFIG)
    return root


@pytest.fixture
def env(monkeypatch, tmp_path):
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


def test_newer_pinned_runner_does_not_warn_about_the_plugin(project, env, monkeypatch):
    """AUD-0296-3: runner 2.5.0, process and plugin 2.3.0 → only the runner WARNs."""
    runner = project.parent / "runner"
    (runner / "awf").mkdir(parents=True)
    (runner / "awf" / "__init__.py").write_text('__version__ = "2.5.0"\n')
    config = dict(CLEAN_CONFIG)
    config["automation"] = {"runner_dir": str(runner)}
    _write_config(project, config)
    _versions(monkeypatch)
    monkeypatch.setattr("awf.versions.plugin_requirement", lambda: "2.1.0")
    monkeypatch.setattr("awf.versions.repo_commit", lambda path: "run5000")

    report = run_doctor(project)
    assert report.rc == 1
    assert any(
        line.startswith("WARN  runner:") for line in report.warnings
    ), _lines(report)
    assert not any(
        line.startswith("WARN  plugin:") for line in report.lines
    ), _lines(report)
    assert any("plugin: 2.3.0" in line for line in report.lines), _lines(report)


def test_malformed_config_is_a_fail_not_a_healthy_empty_config(
    project, env, monkeypatch, capsys
):
    """AUD-0296-4: ``project: [unclosed`` is a WARN, not "everything is fine"."""
    (project / ".agentic" / "config.yaml").write_text(
        "project: [unclosed\n", encoding="utf-8"
    )
    _versions(monkeypatch)

    report = run_doctor(project)
    text = _lines(report)
    assert report.rc == 1
    assert any(
        line.startswith("WARN  config keys:") and "unreadable" in line
        for line in report.warnings
    ), text
    assert "ok  config keys" not in text
    assert any(
        line.startswith("WARN  models:") and "unknown" in line
        for line in report.warnings
    ), text
    assert "0 role(s) checked" not in text
    assert capsys.readouterr().err == ""


def test_runner_dir_mapping_keeps_its_secret(project, env, monkeypatch):
    """AUD-0296-5/AC17: a mapping value may carry an api_key — print no values."""
    config = dict(CLEAN_CONFIG)
    config["automation"] = {"runner_dir": {"api_key": "sk-live-SECRET123"}}
    _write_config(project, config)
    _versions(monkeypatch)

    report = run_doctor(project)
    text = _lines(report)
    assert "sk-live-SECRET123" not in text
    assert "automation.runner_dir" in text
    assert "launch will refuse" in text
    assert "sk-live-SECRET123" not in versions.runner_setting_problem(config)


def _owner_metadata_version(module_name: str) -> str:
    """Version of the nearest dist dir above the imported module (test oracle)."""
    module = importlib.import_module(module_name)
    top = module_name.replace("-", "_").split(".")[0].replace("_", "-").lower()
    base = Path(module.__file__).resolve().parent
    for parent in (base, *base.parents):
        for pattern in ("*.dist-info", "*.egg-info"):
            for dist in sorted(parent.glob(pattern)):
                stem = dist.name.rsplit(".", 1)[0]
                if stem.split("-")[0].replace("_", "-").lower() != top:
                    continue
                meta = dist / (
                    "METADATA" if dist.name.endswith(".dist-info") else "PKG-INFO"
                )
                text = meta.read_text(encoding="utf-8")
                line = next(
                    ln for ln in text.splitlines() if ln.startswith("Version:")
                )
                return line.split(":", 1)[1].strip()
    return versions.UNKNOWN


def test_metadata_follows_the_imported_module_not_the_process_cwd(tmp_path):
    """AUD-0296-2: started from a foreign cwd, awf's metadata is the tree's."""
    expected = _owner_metadata_version("awf")
    assert expected != versions.UNKNOWN

    code = "from awf import versions; print(versions.install_metadata('awf'))"
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == expected


def test_plugin_drift_uses_the_declared_awf_floor():
    """AUD-0296-3: the plugin's ``awf>=`` floor must be met by the process."""
    assert versions.awf_satisfies("2.1.0", "2.1.0") is True
    assert versions.awf_satisfies("2.0.9", "2.1.0") is False
    assert versions.awf_satisfies("2.5.0", "") is True
    assert versions.plugin_drift_kind("2.1.0", "2.1.0", "2.1.0") == ""
    assert versions.plugin_drift_kind("2.1.0", "2.1.0", "2.2.0") != ""
    assert versions.plugin_drift_kind("2.1.0", "2.0.0", "2.1.0") != ""


# ─── QA-0296: two rules the audit fixes left untested ───────────────────


def test_relative_runner_dir_resolves_against_the_project(project, env, monkeypatch):
    """R5: a relative ``runner_dir`` is the project's, as the launch resolves it."""
    (project / "runner" / "awf").mkdir(parents=True)
    (project / "runner" / "awf" / "__init__.py").write_text(
        '__version__ = "2.3.0"\n', encoding="utf-8"
    )
    config = dict(CLEAN_CONFIG)
    config["automation"] = {"runner_dir": "runner"}
    _write_config(project, config)
    _versions(monkeypatch)

    assert versions.runner_path(project, config) == project / "runner"
    report = run_doctor(project)
    runner_lines = [line for line in report.lines if line.split(":")[0].endswith(" runner")]
    assert runner_lines, _lines(report)
    assert str(project / "runner") in runner_lines[0], runner_lines[0]
    assert report.rc == 0, _lines(report)


def test_config_root_must_be_a_mapping(project, env, monkeypatch):
    """AUD-0296-4: a YAML root that is a list is a WARN, not a healthy config."""
    (project / ".agentic" / "config.yaml").write_text("- a\n- b\n", encoding="utf-8")
    _versions(monkeypatch)

    report = run_doctor(project)
    assert report.rc == 1
    assert any(
        line.startswith("WARN  config keys:") and "root is list" in line
        for line in report.warnings
    ), _lines(report)


# ─── QA-0296 (виток 2): CTX4 and the model-surface crash ───────────────


def test_unmet_floor_outranks_a_version_difference(project, env, monkeypatch):
    """AUD-0296 CTX4: an old awf is told to upgrade even when versions differ.

    A plugin 2.0.0 that declares ``awf>=2.2.0`` against a running 2.1.0 used to
    read as a plain version pair, and the report advised "restart the opencode
    session" — a restart cannot install a newer awf.
    """
    monkeypatch.setattr("awf.versions.process_version", lambda: "2.1.0")
    monkeypatch.setattr("awf.versions.plugin_version", lambda: "2.0.0")
    monkeypatch.setattr(
        "awf.versions.install_metadata",
        lambda name: "2.0.0" if name == "agent-workflow-ui" else "2.1.0",
    )
    monkeypatch.setattr("awf.versions.plugin_requirement", lambda: "2.2.0")

    report = run_doctor(project)
    plugin_warn = [line for line in report.warnings if line.startswith("WARN  plugin:")]
    assert len(plugin_warn) == 1, _lines(report)
    assert "requires awf>=2.2.0" in plugin_warn[0], plugin_warn[0]
    assert "upgrade awf" in plugin_warn[0], plugin_warn[0]
    assert "restart the opencode session" not in plugin_warn[0], plugin_warn[0]


def test_version_warning_unmet_floor_beats_a_version_difference():
    """CTX4: the handshake line names the floor instead of advising a restart."""
    line = versions.version_warning(
        process="2.1.0", runner="2.1.0", plugin="2.0.0", plugin_required="2.2.0"
    )
    assert "requires awf>=2.2.0" in line, line
    assert "restart the opencode session" not in line, line
    assert line.endswith("— run awf doctor; upgrade awf"), line
    assert (
        versions.plugin_drift_kind("2.1.0", "2.0.0", "2.2.0")
        == versions.DRIFT_FLOOR
    )


def test_non_mapping_model_result_is_unknown_not_a_crash(project, env, monkeypatch):
    """R5: a broken models surface degrades to a WARN, not an escaping error.

    The parsing (``result.get``) sat outside the ``try``: a surface that
    returned anything but a dict raised ``AttributeError`` out of
    ``run_doctor`` — the CLI printed an ``ERROR`` line, the MCP tool answered
    ``status: error`` and no report existed.
    """
    _versions(monkeypatch)
    monkeypatch.setattr("awf.api.model_check.check_model_config", lambda p: None)

    report = run_doctor(project)
    assert report.rc == 1, _lines(report)
    assert any(
        line.startswith("WARN  models:") and "unknown (TypeError)" in line
        for line in report.warnings
    ), _lines(report)


def test_model_warnings_turn_the_row_red(project, env, monkeypatch):
    """R5: a model surface with warnings is not reported as a healthy row."""
    _versions(monkeypatch)
    fake = dict(FAKE_MODELS)
    fake["warnings"] = ["role worker: provider p missing"]
    monkeypatch.setattr("awf.api.model_check.check_model_config", lambda p: fake)

    report = run_doctor(project)
    models = [line for line in report.warnings if line.startswith("WARN  models:")]
    assert len(models) == 1, _lines(report)
    assert "1 warning(s)" in models[0], models[0]


def test_process_without_a_commit_names_the_missing_evidence(project, env, monkeypatch):
    """AUD-0296 CTX1: the process row says "no commit evidence", like IMP6.

    A version string alone does not identify code; when the running module
    cannot name its commit the row must say so instead of looking healthy.
    """
    _versions(monkeypatch)
    monkeypatch.setattr("awf.versions.process_commit", lambda: "")

    report = run_doctor(project)
    awf_lines = [line for line in report.lines if line.startswith("ok  awf:")]
    assert len(awf_lines) == 1, _lines(report)
    assert "process has no commit evidence" in awf_lines[0], awf_lines[0]
    assert report.rc == 0, _lines(report)
