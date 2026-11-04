"""R1 (T1–T4, AC1–AC7, AC37, AC38): version probe, mode domain, argv.

The version is exercised through real fake binaries (a script answers
``--version``); the two spawns are exercised through their captured argv, so
a flag added by one builder and lost by the other fails here.
"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from awf import agent_stage, supervisor
from awf import opencode_version as ov
from awf.pipeline import Stage

SHIM = Path(__file__).resolve().parents[1] / "infra" / "opencode_shim" / "opencode"
STUB = Path(__file__).resolve().parents[1] / "stubs" / "opencode"


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ─── K1: probe_version ──────────────────────────────────────────────────


def test_probe_parses_real_v1_and_v2_formats(tmp_path):
    ov.reset_version_cache()
    v1 = _script(tmp_path, "oc1", "echo 1.19.4")
    v2 = _script(tmp_path, "oc2", "echo 'opencode v2.2.3'")
    assert ov.probe_version(str(v1)) == ov.Probe("ok", (1, 19, 4))
    assert ov.probe_version(str(v2)) == ov.Probe("ok", (2, 2, 3))


@pytest.mark.parametrize(
    "body, expected",
    [
        ("exit 0", "unknown"),          # empty output
        ("echo мусор", "unknown"),      # garbage
        ("echo 2.0.0; exit 1", "unknown"),  # non-zero code outranks the text
    ],
)
def test_probe_degrades_to_unknown(tmp_path, body, expected):
    ov.reset_version_cache()
    binary = _script(tmp_path, "oc", body)
    assert ov.probe_version(str(binary)).status == expected


def test_probe_missing_binary():
    ov.reset_version_cache()
    probe = ov.probe_version("/nonexistent/opencode-for-test")
    assert probe.status == "missing"


def test_probe_timeout_does_not_hang(tmp_path):
    ov.reset_version_cache()
    silent = _script(tmp_path, "oc-silent", "sleep 5")
    started = time.monotonic()
    probe = ov.probe_version(str(silent), timeout=0.3)
    assert probe.status == "unknown"
    assert time.monotonic() - started < 3
    # The production cap is the contract (AC38).
    assert ov.VERSION_TIMEOUT == 5.0


def test_probe_is_cached_once_per_process(tmp_path):
    ov.reset_version_cache()
    counter = tmp_path / "calls"
    binary = _script(
        tmp_path, "oc", f'echo x >> "{counter}"\necho 1.19.4'
    )
    assert ov.probe_version(str(binary)).status == "ok"
    assert ov.probe_version(str(binary)).status == "ok"
    assert counter.read_text() == "x\n"


def test_background_child_counts_its_own_probe(tmp_path):
    """AC6/D3: the cache is per process — the background child probes too."""
    import subprocess
    import sys

    counter = tmp_path / "calls"
    binary = _script(tmp_path, "oc", f'echo x >> "{counter}"\necho 2.2.3')
    ov.reset_version_cache()
    assert ov.probe_version(str(binary)).status == "ok"

    repo = Path(__file__).resolve().parents[2]
    code = (
        "import sys\n"
        "from awf import opencode_version as v\n"
        "v.reset_version_cache()\n"
        "assert v.probe_version(sys.argv[1]).status == 'ok'\n"
    )
    subprocess.run(
        [sys.executable, "-c", code, str(binary)],
        cwd=repo, check=True, capture_output=True,
    )
    assert counter.read_text().count("x") == 2


def test_probe_unknown_when_binary_is_not_executable(tmp_path):
    ov.reset_version_cache()
    path = tmp_path / "oc-noexec"
    path.write_text("#!/bin/sh\necho 1.0.0\n")
    path.chmod(0o644)
    assert ov.probe_version(str(path)).status == "unknown"


# ─── AC7: the two doubles answer --version without side effects ─────────


@pytest.mark.parametrize("binary", [SHIM, STUB])
def test_doubles_answer_version_without_signals(tmp_path, binary):
    env = dict(os.environ, AWF_SHIM_VERSION="2.1.4")
    import subprocess

    result = subprocess.run(
        [str(binary), "--version"], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "2.1.4"
    assert list(tmp_path.iterdir()) == []


# ─── R2: the flag is unconditional (managed is the only exception) ──────


def test_plan_always_adds_the_flag():
    """R2: there is no mode to choose — 2.x always runs isolated."""
    v1 = ov.Probe("ok", (1, 19, 4))
    v2 = ov.Probe("ok", (2, 2, 3))
    assert ov.resolve_plan({}, v2).add_flag is True
    assert ov.resolve_plan({}, v1).add_flag is True
    managed = ov.resolve_plan({"opencode": {"managed": True}}, v2)
    assert managed.add_flag is False
    assert ov.MANAGED_WARNING in managed.warning


def test_launch_plan_refuses_opencode_1(tmp_path):
    """R1/AC1: an older binary is a typed refusal with both versions named."""
    (tmp_path / ".agentic").mkdir()
    _probe, _plan, refusal = ov.launch_plan(
        tmp_path, config={}, probe=ov.Probe("ok", (1, 19, 4))
    )
    assert refusal is not None
    assert refusal.rc == 1
    assert "требует opencode 2.x" in refusal.message
    assert "1.19.4" in refusal.message


def test_launch_plan_unknown_version_is_fail_closed(tmp_path):
    """R1/AC2: an undetermined version refuses with its own text and rc 0."""
    (tmp_path / ".agentic").mkdir()
    _probe, _plan, refusal = ov.launch_plan(
        tmp_path, config={}, probe=ov.Probe("unknown")
    )
    assert refusal is not None
    assert refusal.rc == 0
    assert "версия не определена" in refusal.message


def test_launch_plan_missing_binary(tmp_path):
    (tmp_path / ".agentic").mkdir()
    _probe, _plan, refusal = ov.launch_plan(
        tmp_path, config={}, probe=ov.Probe("missing")
    )
    assert refusal is not None
    assert refusal.rc == 1
    assert "не найден" in refusal.message


def test_launch_plan_accepts_2x(tmp_path):
    (tmp_path / ".agentic").mkdir()
    _probe, plan, refusal = ov.launch_plan(
        tmp_path, config={}, probe=ov.Probe("ok", (2, 2, 3))
    )
    assert refusal is None
    assert plan.add_flag is True


def test_launch_plan_managed_skips_the_version_gate(tmp_path):
    """R1/AC4: the operator owns the server policy in managed mode."""
    (tmp_path / ".agentic").mkdir()
    _probe, plan, refusal = ov.launch_plan(
        tmp_path,
        config={"opencode": {"managed": True}},
        probe=ov.Probe("ok", (1, 19, 4)),
    )
    assert refusal is None
    assert plan.add_flag is False


# ─── K2: plan table ─────────────────────────────────────────────────────


def test_apply_standalone_inserts_after_subcommand():
    plan = ov.Plan(add_flag=True)
    assert ov.apply_standalone(["opencode", "run", "--auto", "--", "p"], plan) == [
        "opencode", "run", "--standalone", "--auto", "--", "p",
    ]
    assert ov.apply_standalone(["opencode", "models"], plan) == [
        "opencode", "models", "--standalone",
    ]
    assert ov.apply_standalone(["opencode", "run"], ov.Plan(add_flag=False)) == [
        "opencode", "run",
    ]


# ─── T1: both spawns use the same builder ───────────────────────────────


def _worker_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    for name in ("inbox", "outbox", "logs", "roles"):
        (proj / ".agentic" / name).mkdir(parents=True)
    (proj / ".agentic" / "roles" / "developer.md").write_text("# Dev\n")
    return proj


def _capture_worker(monkeypatch) -> dict:
    captured: dict = {}

    def fake_run(cmd, cwd, **kwargs):
        captured["cmd"] = cmd

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    return captured


@pytest.mark.parametrize(
    "cfg",
    [
        {},
        {"opencode": {"managed": False}},
        # R2/AC7: the removed key no longer refuses anything.
        {"opencode": {"standalone": "maybe"}},
    ],
)
def test_worker_argv_always_has_the_flag(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (2, 1, 4))
    )
    captured = _capture_worker(monkeypatch)
    proj = _worker_project(tmp_path)
    cfg = {**cfg, "models": {"developer": {"model": "p/test"}}}
    agent_stage.run_agent_stage(
        Stage(name="dev", role="developer", kind="execute"),
        "TODO-0001", proj, cfg, proj / ".agentic" / "logs",
    )
    cmd = captured["cmd"]
    assert cmd[:3] == ["opencode", "run", "--standalone"]
    assert cmd[3:] == [
        "--auto", "--print-logs",
        "--title", "awf-developer-TODO-0001",
        "--file", str(proj / ".agentic" / "roles" / "developer.md"),
        "--file", str(proj / ".agentic" / "inbox" / "TODO-0001.md"),
        "--model", "p/test",
        "--", cmd[-1],
    ]


def test_worker_managed_has_no_flag(tmp_path, monkeypatch):
    """R2: managed is the only path without ``--standalone``."""
    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (2, 2, 3))
    )
    captured = _capture_worker(monkeypatch)
    proj = _worker_project(tmp_path)
    agent_stage.run_agent_stage(
        Stage(name="dev", role="developer", kind="execute"),
        "TODO-0001", proj,
        {
            "opencode": {"managed": True},
            "models": {"developer": {"model": "p/test"}},
        },
        proj / ".agentic" / "logs",
    )
    assert "--standalone" not in captured["cmd"]


def test_supervisor_argv_v2_has_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (2, 2, 3))
    )
    captured: dict = {}

    def fake_run(cmd, cwd, **kwargs):
        captured["cmd"] = cmd
        holder = kwargs.get("signal_holder")
        if holder is not None:
            holder["signal"] = ""

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
    )
    proj = tmp_path / "sup"
    for d in ("inbox", "outbox", "logs", "roles"):
        (proj / ".agentic" / d).mkdir(parents=True)
    (proj / ".agentic" / "roles" / "supervisor.md").write_text("# Sup\n")
    (proj / ".agentic" / "phases").mkdir()
    phases = proj / ".agentic" / "phases" / "plan.md"
    phases.write_text("# Plan\n")

    supervisor.run_supervisor_via_subprocess(
        "plan", "", proj,
        {"models": {"supervisor": {"model": "p/test"}}},
        str(phases), proj / ".agentic" / "logs"
    )
    assert captured["cmd"][:3] == ["opencode", "run", "--standalone"]
