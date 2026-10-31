"""R6 (K8): the ``config`` row of ``awf doctor``.

The doctor says whether the opencode config is in the 2.x form and names
the 1.x keys when it is not. It never converts and never writes: the
conversion is the owner's manual step (R11), so the file hash has to be
the same before and after.

A string ``model`` is valid 2.x (the schema accepts it in the root and in
``agents.<name>.model``), so it must not produce a WARN.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from awf.doctor import run_doctor

CLEAN_CONFIG = {
    "project": {"name": "test"},
}

FAKE_MODELS = {
    "models": [{"role": "worker", "model": "p/one", "provider": "p", "valid": True}],
    "warnings": [],
    "providers_available": ["p"],
    "role_files": [],
}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".agentic").mkdir(parents=True)
    (root / ".agentic" / "config.yaml").write_text(
        yaml.safe_dump(CLEAN_CONFIG), encoding="utf-8"
    )
    return root


@pytest.fixture
def opencode_file(tmp_path, monkeypatch):
    """A path the doctor will read as the opencode config."""
    path = tmp_path / "opencode.json"

    def write(data) -> Path:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    monkeypatch.setattr("awf.doctor.xdg.opencode_config_file", lambda: path)
    monkeypatch.setattr(
        "awf.api.model_check.check_model_config", lambda project_dir: dict(FAKE_MODELS)
    )
    return write


def _lines(report) -> str:
    return "\n".join(report.lines)


def _config_line(report) -> str:
    lines = [line for line in report.lines if " config: " in line]
    assert len(lines) == 1, _lines(report)
    return lines[0]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_v2_file_is_ok(project, opencode_file):
    opencode_file(
        {
            "providers": {"p": {"settings": {"baseURL": "http://x"}}},
            "agents": {"worker": {"description": "w"}},
            "mcp": {"servers": {"agent-workflow-ui": {"type": "local"}}},
            "permissions": [
                {"action": "shell", "resource": "*", "effect": "allow"}
            ],
        }
    )

    report = run_doctor(project)

    line = _config_line(report)
    assert line.startswith("ok  ")
    assert not any("сконвертируй" in warning for warning in report.warnings)


def test_string_model_is_valid_v2(project, opencode_file):
    """FR2: a string ``model`` is 2.x — no WARN, nothing to convert."""
    opencode_file(
        {
            "model": "vllm/llm",
            "agents": {"worker": {"model": "vllm/llm"}},
        }
    )

    report = run_doctor(project)

    assert _config_line(report).startswith("ok  ")
    assert not any("model" in line and "WARN" in line for line in report.lines)


def test_v1_file_warns_and_stays_untouched(project, opencode_file):
    path = opencode_file(
        {
            "provider": {"p": {}},
            "agent": {"worker": {}},
            "permission": {"edit": "allow"},
            "mcp": {"agent-workflow-ui": {"type": "local"}},
        }
    )
    before = _sha(path)

    report = run_doctor(project)

    line = _config_line(report)
    assert line.startswith("WARN  ")
    assert "сконвертируй в V2" in line
    for key in ("provider", "agent", "permission", "mcp.agent-workflow-ui"):
        assert key in line, line
    assert _sha(path) == before


def test_mixed_file_counts_as_v1(project, opencode_file):
    """A mixed file counts as 1.x: one old key is enough to stop."""
    opencode_file(
        {
            "providers": {},
            "agents": {},
            "mcp": {"servers": {}},
            "permission": {"edit": "deny"},
        }
    )

    report = run_doctor(project)

    line = _config_line(report)
    assert "сконвертируй в V2" in line
    assert "permission" in line
    assert " provider," not in line  # no key that is not in the file


def test_missing_file_is_not_a_warning(project, opencode_file):
    report = run_doctor(project)

    line = _config_line(report)
    assert line.startswith("ok  ")
    assert "no opencode.json" in line


def test_broken_json_is_a_warning(project, opencode_file):
    path = Path(opencode_file({}))
    path.write_text("{not json", encoding="utf-8")
    before = _sha(path)

    report = run_doctor(project)

    assert _config_line(report).startswith("WARN  ")
    assert _sha(path) == before
