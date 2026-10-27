"""CARRY IMP2 (R13/K13): the raw full-suite gate.

T4 — a full suite run without a matching canonical artifact leaves a
``SUITE-WARN:`` and the ``suite_gate=raw-full-suite`` handoff fact; the
stage and the commit are not blocked. A matching artifact (same tree sha,
same command) silences it; somebody else's command or tree does not.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from awf import git_utils, suite_command, suite_guard
from awf.agent_stage import collect_handoff

PROJECT_KEY = "verification:\n  test_cmd: npm test\n"


def _repo(tmp_path: Path, config: str | None = PROJECT_KEY) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)
    (proj / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)
    (proj / ".agentic").mkdir()
    if config is not None:
        (proj / ".agentic" / "config.yaml").write_text(config, encoding="utf-8")
    exclude = proj / ".git" / "info" / "exclude"
    exclude.write_text(".agentic/context/\n", encoding="utf-8")
    return proj


def _artifact(proj: Path, command: str, sha: str | None = None) -> Path:
    sha = sha or git_utils.tree_fingerprint(proj)
    context = proj / ".agentic" / "context"
    context.mkdir(parents=True, exist_ok=True)
    path = context / f"SUITE-{sha}-abcd1234.json"
    path.write_text(json.dumps({"sha": sha, "command": command}), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "text",
    [
        "pytest tests/ -q",
        "python3 -m pytest tests --maxfail=1",
        "cd proj && pytest -q tests/",
        "bash scripts/run-all.sh",
        "./scripts/run-all.sh",
        # QA-0277 F1: the worker log is opencode's --print-logs echo.
        '\x1b[0m$ \x1b[0mcd "/p" && /venv/bin/python -m pytest tests/ -q -n auto',
        "$ cd /p && python3 -m pytest tests/ -q 2>&1 | tail -5",
        "grep -c x awf/_proc.py && python3 -m pytest tests/ -q",
        "timeout 600 python3 -m pytest tests/",
        "bash -c 'pytest tests/'",
        'bash -c "cd /p && pytest tests/"',
        "pytest tests -m slow",
    ],
)
def test_full_suite_lines_are_detected(text):
    assert suite_guard.detect_full_suite(text)


@pytest.mark.parametrize(
    "text",
    [
        "pytest tests/unit/test_x.py -q",
        "pytest tests -k smoke",
        "pytest tests/ --deselect x -k 'not slow'",
        "npm test",
        "Run `bash scripts/run-all.sh` before the final commit",
        "AGENTS.md: ворота — bash scripts/run-all.sh",
        "",
        # QA-0277 F2: reading the script is not running it.
        "cat scripts/run-all.sh",
        "$ sed -n '1,80p' scripts/run-all.sh",
        # QA-0277 F3: a quoted payload and metadata are not command echoes.
        'pgrep -af "suite-canonical|pytest tests/" | grep -v opencode',
        'for cmd in "python -m pytest tests/ -q"; do echo "$cmd"; done',
        'timestamp=2026-10-09T16:51:54.800Z level=INFO message=evaluated '
        'permission=bash pattern="cat scripts/run-all.sh"',
        "pytest -m tests",
        "bash -n scripts/run-all.sh",
    ],
)
def test_narrow_runs_are_not_full_suite(text):
    assert not suite_guard.detect_full_suite(text)


def test_raw_full_suite_warns_without_artifact(tmp_path):
    proj = _repo(tmp_path)
    warn = suite_guard.check(proj, "pytest tests/ -q\n")
    assert warn.startswith("SUITE-WARN:")


REAL_LOG = (
    "timestamp=2026-10-27T04:00:49.890Z level=INFO run=x message=loop\n"
    '\x1b[0m$ \x1b[0mcd "/home/u/proj" && /venv/bin/python -m pytest '
    "tests/ -q -n auto --timeout=300 -p no:cacheprovider 2>&1 | tail -4\n"
)


def test_real_print_logs_line_warns_and_artifact_silences(tmp_path):
    """QA-0277 F1/AC33: the actual log format must move the gate."""
    proj = _repo(tmp_path)
    assert suite_guard.check(proj, REAL_LOG).startswith("SUITE-WARN:")
    _artifact(proj, "npm test")
    assert suite_guard.check(proj, REAL_LOG) == ""


def test_reading_the_script_is_not_a_full_run(tmp_path):
    """QA-0277 F2: `cat`/`sed` of run-all.sh must not warn."""
    proj = _repo(tmp_path)
    assert suite_guard.check(proj, "$ cat scripts/run-all.sh\n") == ""
    assert suite_guard.check(
        proj, "$ sed -n '1,80p' scripts/run-all.sh\n",
    ) == ""


def test_matching_artifact_silences(tmp_path):
    proj = _repo(tmp_path)
    _artifact(proj, "npm test")
    assert suite_guard.check(proj, "pytest tests/ -q\n") == ""


def test_foreign_command_does_not_silence(tmp_path):
    proj = _repo(tmp_path)
    # The shell's AWF_SUITE_CMD override is not the engine's canonical string.
    _artifact(proj, "pytest tests -k smoke")
    assert suite_guard.check(proj, "pytest tests/ -q\n").startswith("SUITE-WARN:")


def test_foreign_tree_does_not_silence(tmp_path):
    proj = _repo(tmp_path)
    _artifact(proj, "npm test", sha="0" * 40)
    assert suite_guard.check(proj, "pytest tests/ -q\n").startswith("SUITE-WARN:")


def test_legacy_fallback_artifact_silences(tmp_path):
    proj = _repo(tmp_path, config=None)
    canonical = suite_command.resolve(proj)
    assert canonical is not None and canonical.source == "legacy"
    _artifact(proj, canonical.command)
    assert suite_guard.check(proj, "pytest tests/ -q\n") == ""


def test_narrow_override_does_not_silence_legacy(tmp_path, monkeypatch):
    proj = _repo(tmp_path, config=None)
    monkeypatch.setenv("AWF_SUITE_CMD", "pytest tests -k smoke")
    _artifact(proj, "pytest tests -k smoke")
    assert suite_guard.check(proj, "pytest tests/ -q\n").startswith("SUITE-WARN:")


def test_no_full_run_no_check(tmp_path):
    proj = _repo(tmp_path)
    assert suite_guard.check(proj, "pytest tests/unit/test_x.py -q\n") == ""
    assert suite_guard.check(proj, "") == ""


def test_handoff_carries_engine_fact(tmp_path):
    proj = _repo(tmp_path)
    logs = proj / ".agentic" / "logs"
    logs.mkdir()

    out = collect_handoff(
        "agent-implementer", "TODO-0001", proj, logs,
        engine_facts=[suite_guard.RAW_SUITE_FACT],
    )
    body = out.read_text(encoding="utf-8")
    assert "## Engine facts" in body
    assert "suite_gate=raw-full-suite" in body


def test_handoff_without_engine_facts_is_unchanged(tmp_path):
    proj = _repo(tmp_path)
    logs = proj / ".agentic" / "logs"
    logs.mkdir()

    out = collect_handoff("agent-implementer", "TODO-0001", proj, logs)
    assert "## Engine facts" not in out.read_text(encoding="utf-8")


def test_stage_wires_the_warning_into_the_handoff(tmp_path, monkeypatch):
    """K13: after the worker exits the gate reads its log and hands the
    fact to collect_handoff; the stage itself still succeeds."""
    from awf import agent_stage
    from awf.pipeline import Stage

    proj = _repo(tmp_path)
    (proj / ".agentic" / "inbox").mkdir()
    (proj / ".agentic" / "outbox").mkdir()
    (proj / ".agentic" / "logs").mkdir()
    roles = proj / ".agentic" / "roles"
    roles.mkdir()
    (roles / "developer.md").write_text("# Developer role\n", encoding="utf-8")

    captured: dict = {}
    warn_log: list[str] = []

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None, log_holder=None,
                 **kwargs):
        log = Path(logs_dir) / "worker.out"
        # QA-0277 F1: the real --print-logs command echo, ANSI included.
        log.write_text(
            '\x1b[0m$ \x1b[0mcd "/p" && python3 -m pytest tests/ -q\n',
            encoding="utf-8",
        )
        log_holder["log_path"] = str(log)
        log_holder["log_start_offset"] = "0"
        outbox = Path(cwd) / ".agentic" / "outbox"
        (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "_log", lambda logs_dir, msg: warn_log.append(str(msg)))
    monkeypatch.setattr(
        agent_stage, "collect_handoff",
        lambda *a, **kw: captured.update(kw),
    )

    agent_stage.run_agent_stage(
        Stage(name="agent-dev", role="developer", kind="execute"),
        "TODO-0001", proj, {}, proj / ".agentic" / "logs",
    )

    assert captured.get("engine_facts") == [suite_guard.RAW_SUITE_FACT]
    assert any(m.startswith("SUITE-WARN:") for m in warn_log)
