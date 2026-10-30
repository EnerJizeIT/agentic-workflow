"""R2 (T5–T8, AC8–AC13, AC39, AC40): fail-closed launch without isolation.

Every entry point is exercised with a 2.x probe and ``standalone: off``: the
refusal must be typed, must name the key, and must leave no trace (file
hashes, git status, TODO id, lease, spawned stage).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from awf import opencode_version as ov
from awf.api import pipeline
from awf.api import run as run_api
from awf.api._results import RunNextResult, RunStartResult, ServiceRunResult, StartResult


def _project(tmp_path: Path, config_text: str = "opencode:\n  standalone: off\n") -> Path:
    proj = tmp_path / "proj"
    for name in ("inbox", "outbox", "context", "state", "logs", "roles", "pipelines"):
        (proj / ".agentic" / name).mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(config_text, encoding="utf-8")
    return proj


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _probe(version: tuple[int, int, int] | None, status: str = "ok"):
    return lambda *a, **kw: ov.Probe(status, version)


def test_start_refused_on_v2_off(tmp_path, monkeypatch):
    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((2, 2, 3)))
    proj = _project(tmp_path)
    before = _tree_hash(proj / ".agentic")

    result = pipeline.start_pipeline(proj, background=False)

    assert isinstance(result, StartResult)
    assert result.run_mode == "noop"
    assert result.exit_code == 0
    assert "opencode.standalone" in result.message
    assert _tree_hash(proj / ".agentic") == before
    assert not (proj / ".agentic" / "state" / "awf-launch.lease").exists()


def test_start_allowed_on_v1_and_v2_auto(tmp_path, monkeypatch):
    proj = _project(tmp_path, "project:\n  name: x\n")
    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((2, 2, 3)))
    message, rc, warning = ov.gate(proj)
    assert (message, rc, warning) == ("", 0, "")
    _probe_obj, plan, refusal = ov.launch_plan(proj)
    assert plan.add_flag is True and refusal is None

    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((1, 19, 4)))
    message, rc, warning = ov.gate(proj)
    assert (message, rc, warning) == ("", 0, "")


@pytest.mark.parametrize(
    "entry",
    ["continue", "run_next", "run_start", "run_service_start"],
)
def test_every_entry_refuses_without_trace(tmp_path, monkeypatch, entry):
    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((2, 2, 3)))
    proj = _project(tmp_path)
    before = _tree_hash(proj / ".agentic")

    if entry == "continue":
        result = pipeline.continue_pipeline(proj, background=False)
        assert isinstance(result, StartResult) and result.run_mode == "noop"
    elif entry == "run_next":
        result = run_api.run_next(proj, background=False)
        assert isinstance(result, RunNextResult) and result.action == "refused"
    elif entry == "run_start":
        result = run_api.run_start(proj, queue=["TODO-0001"])
        assert isinstance(result, RunStartResult) and result.active is False
    else:
        result = run_api.run_service_start(
            proj, slug="auditor", description="x", background=False
        )
        assert isinstance(result, ServiceRunResult) and result.action == "refused"

    assert "opencode.standalone" in result.message
    assert _tree_hash(proj / ".agentic") == before
    assert list((proj / ".agentic" / "inbox").iterdir()) == []


def test_unknown_version_refused_even_with_on(tmp_path, monkeypatch):
    monkeypatch.setattr("awf.opencode_version.probe_version", _probe(None, "unknown"))
    proj = _project(tmp_path, "opencode:\n  standalone: on\n")
    result = pipeline.start_pipeline(proj, background=False)
    assert result.run_mode == "noop"
    assert result.exit_code == 0
    assert "версия не определена" in result.message


def test_unknown_version_on_v1_is_refused_fail_closed(tmp_path, monkeypatch):
    # AC39: the accepted behavior change — an unreadable version is refused
    # for 1.x too (fail-closed).
    monkeypatch.setattr("awf.opencode_version.probe_version", _probe(None, "unknown"))
    proj = _project(tmp_path)
    refusal = ov.preflight(proj)
    assert refusal is not None and refusal.rc == 0
    assert "версия не определена" in refusal.message


def test_missing_binary_has_its_own_text(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    monkeypatch.setenv("PATH", "")
    ov.reset_version_cache()
    monkeypatch.setattr(
        "awf.opencode_version.probe_version", _probe(None, "missing")
    )
    result = pipeline.start_pipeline(proj, background=False)
    assert result.run_mode == "noop"
    assert result.exit_code == 1
    assert "opencode не найден" in result.message
    assert "standalone" not in result.message


def test_managed_opt_in_warns_instead_of_refusing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((2, 2, 3)))
    proj = _project(
        tmp_path,
        "opencode:\n  standalone: off\n  managed: true\n",
    )
    message, rc, warning = ov.gate(proj)
    assert message == ""
    assert "managed" in warning
    _probe_obj, plan, refusal = ov.launch_plan(proj)
    assert plan.add_flag is False and refusal is None


def test_managed_env_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((2, 2, 3)))
    monkeypatch.setenv("AWF_OPENCODE_MANAGED", "1")
    proj = _project(tmp_path, "opencode:\n  standalone: off\n")
    message, _rc, _warning = ov.gate(proj)
    assert message == ""


def test_doctor_prints_managed_line(tmp_path, monkeypatch):
    from awf import doctor

    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((2, 2, 3)))
    monkeypatch.setattr("awf.opencode_db.available_models", lambda *a, **kw: [])
    proj = _project(
        tmp_path,
        "opencode:\n  standalone: off\n  managed: true\n",
    )
    report = doctor.run_doctor(proj)
    assert any("managed: on" in line for line in report.lines), report.lines
    assert any("opencode" in line and "2.2.3" in line for line in report.lines)
