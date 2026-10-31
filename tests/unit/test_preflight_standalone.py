"""R1 (AC1–AC4): awf 3 refuses opencode < 2 at every entry, before any trace.

The refusal is typed (a result, not an exception), names both versions, and
leaves the project untouched: file hashes, no lease, no spawned stage. An
undetermined version is fail-closed with its own text and rc 0; managed is
the one mode where the operator owns the policy.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from awf import opencode_version as ov
from awf.api import pipeline
from awf.api import run as run_api
from awf.api._results import StartResult


def _project(tmp_path: Path, config_text: str = "project:\n  name: x\n") -> Path:
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


def test_start_refused_on_opencode_1(tmp_path, monkeypatch):
    """AC1: a typed refusal naming the found 1.x version, no side effects."""
    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((1, 19, 4)))
    proj = _project(tmp_path)
    before = _tree_hash(proj / ".agentic")

    result = pipeline.start_pipeline(proj, background=False)

    assert isinstance(result, StartResult)
    assert result.run_mode == "noop"
    assert result.exit_code == 1
    assert "требует opencode 2.x" in result.message
    assert "1.19.4" in result.message
    assert _tree_hash(proj / ".agentic") == before
    assert not (proj / ".agentic" / "state" / "awf-launch.lease").exists()


def test_start_allowed_on_2x(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((2, 2, 3)))
    message, rc, warning = ov.gate(proj)
    assert (message, rc, warning) == ("", 0, "")
    _probe_obj, plan, refusal = ov.launch_plan(proj)
    assert plan.add_flag is True and refusal is None


@pytest.mark.parametrize("entry", ["continue", "run_next", "run_service_start"])
def test_every_entry_refuses_without_trace(tmp_path, monkeypatch, entry):
    """AC3: the same refusal on every entry, with no file left behind."""
    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((1, 19, 4)))
    proj = _project(tmp_path)
    before = _tree_hash(proj / ".agentic")

    if entry == "continue":
        result = pipeline.continue_pipeline(proj, background=False)
    elif entry == "run_next":
        result = run_api.run_next(proj)
    else:
        result = run_api.run_service_start(proj, slug="x", description="y")

    message = getattr(result, "message", "")
    assert "требует opencode 2.x" in message, message
    assert _tree_hash(proj / ".agentic") == before


def test_unknown_version_is_refused_fail_closed(tmp_path, monkeypatch):
    """AC2: undetermined version — own text, rc 0, no exception."""
    monkeypatch.setattr(
        "awf.opencode_version.probe_version", _probe(None, status="unknown")
    )
    proj = _project(tmp_path)
    message, rc, _warning = ov.gate(proj)
    assert rc == 0
    assert "версия не определена" in message


def test_missing_binary_has_its_own_text(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "awf.opencode_version.probe_version", _probe(None, status="missing")
    )
    proj = _project(tmp_path)
    result = pipeline.start_pipeline(proj, background=False)
    assert result.exit_code == 1
    assert "не найден" in result.message
    assert "требует opencode" not in result.message


def test_managed_skips_the_version_gate(tmp_path, monkeypatch, capsys):
    """AC4: managed is the operator's policy — no version refusal."""
    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((1, 19, 4)))
    proj = _project(
        tmp_path, "project:\n  name: x\nopencode:\n  managed: true\n"
    )
    message, rc, warning = ov.gate(proj)
    assert (message, rc) == ("", 0)
    assert ov.MANAGED_WARNING in warning


def test_doctor_prints_managed_line(tmp_path, monkeypatch):
    from awf.doctor import run_doctor

    monkeypatch.setattr("awf.opencode_version.probe_version", _probe((2, 2, 3)))
    proj = _project(
        tmp_path, "project:\n  name: x\nopencode:\n  managed: true\n"
    )
    report = run_doctor(proj)
    assert any("managed" in line for line in report.lines)
