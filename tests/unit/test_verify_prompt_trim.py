"""IMP2b: the verify prompt is built by the stage context builder.

The verify stage no longer assembles its own text or owns a budget: the
``supervisor`` family of :func:`awf.stage_context.build_stage_context`
carries the ``GATES`` verdict, the ``DIFF`` and the ``DONE`` report, and the
stage caps the whole text at 6 000 chars. This file owns three facts:

1. the prompt carries the pack verdict inline and no handoff history;
2. the text fits the 6 000 cap and the ``CTX`` line names the supervisor
   type and the cap;
3. the stage narrates itself in one line — size, files, duration, commands.
"""
from __future__ import annotations

import re
from pathlib import Path

from awf import config as cfg_mod
from awf import supervisor
from awf.stage_context import build_stage_context
from awf.verify_context import (
    VERIFY_STAGE_TARGET_SECONDS,
    count_logged_commands,
    format_verify_timing,
    log_verify_timing,
)

_TODO_ID = "TODO-0001"

_PACK = """\
# GATES-TODO-0001 — verify-pack report

Generated: 2026-11-03T08:09:51+00:00

## Verdict: **failed** (exit 1) — 4 section(s) measured

## 1. gates: FAIL
- ratchet: FAIL (rc=1) — marker-reason: counts above baseline

## 2. diff: PASS
changed vs baseline: 1 tracked file, +2/-0

```json
{"verdict": "failed", "exit_code": 1, "measured": 4,
 "sections": {"gates": "fail", "diff": "pass"}, "first_failure": "marker-reason"}
```
"""

_STAGE_TYPES = """\
stage_types:
  plan: plan
  verify: supervisor
  agent-implementer: execute
models:
  supervisor:
    model: p/test
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(tmp_path: Path) -> Path:
    agentic = tmp_path / ".agentic"
    for name in ("inbox", "outbox", "context", "handoff", "logs", "roles"):
        (agentic / name).mkdir(parents=True)
    _write(agentic / "config.yaml", f"project:\n  name: T\n{_STAGE_TYPES}")
    _write(
        agentic / "inbox" / f"{_TODO_ID}.md",
        "# TODO-0001 — unit\n\n## Success criteria\n- marker-criterion\n",
    )
    _write(agentic / "outbox" / f"DONE-{_TODO_ID}.md", "# DONE\n\nmarker-done-summary\n")
    _write(agentic / "context" / f"GATES-{_TODO_ID}.md", _PACK)
    _write(agentic / "context" / f"BASELINE-{_TODO_ID}.sha", "deadbeef\n")
    _write(agentic / "roles" / "supervisor.md", "# Role\n\n## Роль\nВеди пайплайн.\n")
    _write(
        agentic / "handoff" / f"agent-qa-review-{_TODO_ID}.md",
        "HANDOFF-SECRET-MARKER " * 200,
    )
    return tmp_path


def _verify(root: Path):
    return build_stage_context(
        root, _TODO_ID, "verify", "supervisor", cap=6000,
    )


def test_prompt_embeds_pack_verdict_and_done(tmp_path):
    prompt = _verify(_project(tmp_path)).text

    assert "verdict: failed" in prompt
    assert "marker-reason" in prompt
    assert "marker-done-summary" in prompt


def test_prompt_excludes_handoff_history(tmp_path):
    """The live rule: verify drops ``PREVLINK``, so no handoff text enters.

    ``<= 1`` used to pass because the first line of the handoff leaked
    through ``PREVLINK``. The live path excludes the block outright; the
    test asks for that exact outcome.
    """
    root = _project(tmp_path)
    prompt = build_stage_context(
        root, _TODO_ID, "verify", "supervisor", cap=6000, exclude=("PREVLINK",),
    ).text

    assert "HANDOFF-SECRET-MARKER" not in prompt


def test_verify_prompt_fits_the_6000_cap(tmp_path):
    result = _verify(_project(tmp_path))

    assert len(result.text) <= 6000
    assert result.cap == 6000


def test_ctx_line_names_supervisor_and_cap(tmp_path):
    root = _project(tmp_path)
    _verify(root)

    log = (root / ".agentic" / "logs" / "orchestrator.log").read_text(encoding="utf-8")
    ctx = next(line for line in log.splitlines() if " CTX " in line)
    assert f"{_TODO_ID} verify" in ctx
    assert "type=supervisor" in ctx
    assert "cap=6000" in ctx
    assert "file=" in ctx


def test_verify_stage_prepares_no_extra_files(tmp_path):
    proj = _project(tmp_path)

    prompt = supervisor._prepare_supervisor_stage(
        "verify",
        _TODO_ID,
        proj,
        proj / ".agentic" / "logs",
    )

    assert prompt is not None
    assert "verdict: failed" in prompt
    assert "HANDOFF-SECRET-MARKER" not in prompt


def test_timing_line_names_size_and_duration(tmp_path):
    """The stage's one narration line carries numbers, not a feeling."""
    logs = tmp_path / ".agentic" / "logs"
    logs.mkdir(parents=True)

    fast = format_verify_timing(
        _TODO_ID, prompt_chars=4321, duration=12.5, commands=1
    )
    slow = format_verify_timing(
        _TODO_ID, prompt_chars=9876, duration=205.0, commands=7
    )
    log_verify_timing(logs, slow)

    assert "prompt 9876 chars" in slow
    assert "duration 205.0s" in slow
    assert "commands 7" in slow
    assert "over 180s target" in slow
    assert "prompt 9876 chars" in slow
    assert "over 180s target" not in fast
    assert "duration 12.5s" in fast
    assert VERIFY_STAGE_TARGET_SECONDS == 180.0

    text = (logs / "orchestrator.log").read_text(encoding="utf-8")
    assert "A42: verify TODO-0001" in text
    assert "9876" in text
    assert "205.0s" in text


def test_count_logged_commands_reads_the_delta(tmp_path):
    """Only shell calls printed after the stage start count — best effort."""
    log = tmp_path / "worker-output.out"
    log.write_text(
        "\x1b[0m$ old-command\n"
        "===== awf run =====\n"
        "\x1b[0m$ new-command\n"
        "answer text\n"
        "$ another-command\n",
        encoding="utf-8",
    )
    offset = len("\x1b[0m$ old-command\n===== awf run =====\n")
    assert count_logged_commands(log, offset) == 2
    assert count_logged_commands(tmp_path / "missing.out", 0) == -1
    assert count_logged_commands(log, -5) == -1


def test_stage_writes_the_timing_line(tmp_path, monkeypatch):
    """The wiring, not just the formatter: the stage logs size + duration."""
    from subprocess import CompletedProcess

    from tests.unit._signal_fakes import log_text, make_project

    proj = make_project(tmp_path)
    agentic = proj / ".agentic"
    _write(agentic / "config.yaml", _STAGE_TYPES)
    _write(agentic / "inbox" / f"{_TODO_ID}.md", "# TODO\n")
    worker_log = agentic / "logs" / "worker-output.out"

    def fake_run(cmd, cwd=None, **kwargs):
        worker_log.write_text("\x1b[0m$ cmd-one\nanswer\n", encoding="utf-8")
        holder = kwargs.get("log_holder")
        if holder is not None:
            holder["log_path"] = str(worker_log)
            holder["log_start_offset"] = 0
        return CompletedProcess(cmd, 0)

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
    )
    monkeypatch.setattr(
        supervisor, "_wait_for_verify_decision", lambda *a, **kw: "", raising=True
    )

    supervisor.run_supervisor_via_subprocess(
        "verify",
        _TODO_ID,
        proj,
        cfg_mod.load(proj),
        str(proj / ".agentic" / "phases" / "plan.md"),
        proj / ".agentic" / "logs",
    )

    line = next(
        row for row in log_text(proj).splitlines() if "A42: verify TODO-0001" in row
    )
    # IMP2b: the prompt is one text with no attachments, so the line names
    # its size and duration — an "in N file(s)" count would be a constant.
    assert re.search(r"prompt \d+ chars,", line), line
    assert "file(s)" not in line
    assert re.search(r"duration \d+\.\ds", line), line
    assert "commands 1" in line


def test_verify_stage_attaches_nothing(tmp_path, monkeypatch):
    """At the SPAWN level: no ``--file`` reaches the verify subprocess."""
    from subprocess import CompletedProcess

    from tests.unit._signal_fakes import make_project

    proj = make_project(tmp_path)
    agentic = proj / ".agentic"
    _write(agentic / "config.yaml", _STAGE_TYPES)
    _write(agentic / "inbox" / f"{_TODO_ID}.md", "# TODO\n")
    captured: dict = {}

    def fake_run(cmd, cwd=None, **kwargs):
        captured["cmd"] = cmd
        return CompletedProcess(cmd, 0)

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
    )
    monkeypatch.setattr(
        supervisor, "_wait_for_verify_decision", lambda *a, **kw: "", raising=True
    )

    supervisor.run_supervisor_via_subprocess(
        "verify",
        _TODO_ID,
        proj,
        cfg_mod.load(proj),
        str(proj / ".agentic" / "phases" / "plan.md"),
        proj / ".agentic" / "logs",
    )

    assert "--file" not in captured["cmd"]


def test_partial_in_memory_config_still_resolves_the_stage_type(tmp_path):
    """A caller passes the keys the spawn reads (model/agent/wait windows).

    The stage type lives in the project's ``config.yaml``; rejecting a
    partial dict as "no stage_types" broke every supervisor-path fixture and
    would break an API caller that never owned the project's type table.
    """
    from awf import supervisor
    from tests.unit._signal_fakes import make_project

    proj = make_project(tmp_path)
    _write(proj / ".agentic" / "config.yaml", _STAGE_TYPES)
    _write(proj / ".agentic" / "inbox" / f"{_TODO_ID}.md", "# TODO\n")

    prompt = supervisor._prepare_supervisor_stage(
        "verify",
        _TODO_ID,
        proj,
        proj / ".agentic" / "logs",
        config={"models": {"supervisor": {"model": "p/test"}}},
    )

    assert prompt is not None
    assert len(prompt) <= 6000
