"""IMP2b: the worker prompt is the stage context text and nothing else.

``run_agent_stage`` no longer attaches files: the argv carries one prompt
after ``--``, that prompt is byte-for-byte the file the builder wrote, and
``orchestrator.log`` gets the ``CTX`` line. ``retry_note`` still lands last
(recency — weak models weight the tail).
"""
from __future__ import annotations

import re
from pathlib import Path

from awf import agent_stage
from awf.pipeline import Stage
from awf.stage_context import CAP

MODEL_CONFIG = {"models": {"worker": {"model": "p/test"}}}

TODO_ID = "TODO-0001"

STAGE_TYPES = """\
stage_types:
  plan: plan
  agent-dev: execute
  verify: supervisor
"""

TODO_TEXT = """\
---
files: ["awf/x.py"]
verify: ["pytest -q tests/unit/test_x.py"]
gates: ["tests"]
---
# TODO — sample

## Что сделать

1. Сделать `x`.

## Приёмка (факты)

- AC1: `x` есть.

## Границы

- `awf/y.py` не трогать.
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    for name in ("inbox", "outbox", "logs", "context", "handoff", "roles", "doctrine"):
        (proj / ".agentic" / name).mkdir(parents=True)
    _write(proj / ".agentic" / "config.yaml", STAGE_TYPES)
    _write(proj / ".agentic" / "inbox" / f"{TODO_ID}.md", TODO_TEXT)
    _write(proj / ".agentic" / "roles" / "developer.md", "# Developer role\n")
    return proj


def _capture_run(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None, signal_holder=None,
                 **kwargs):
        captured["cmd"] = cmd

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    return captured


def _stage() -> Stage:
    return Stage(name="agent-dev", role="developer", kind="execute", task="Do x.")


def test_prompt_is_the_written_stage_file(tmp_path, monkeypatch):
    captured = _capture_run(monkeypatch)
    proj = _project(tmp_path)

    agent_stage.run_agent_stage(
        _stage(), TODO_ID, proj, MODEL_CONFIG, proj / ".agentic" / "logs",
    )

    cmd = captured["cmd"]
    prompt = cmd[-1]
    stage_file = proj / ".agentic" / "context" / f"STAGE-{TODO_ID}-agent-dev.md"
    assert stage_file.read_text(encoding="utf-8") == prompt


def test_no_file_flag_in_stage_argv(tmp_path, monkeypatch):
    captured = _capture_run(monkeypatch)
    proj = _project(tmp_path)

    agent_stage.run_agent_stage(
        _stage(), TODO_ID, proj, MODEL_CONFIG, proj / ".agentic" / "logs",
    )

    assert "--file" not in captured["cmd"]


def test_ctx_line_written_for_the_stage(tmp_path, monkeypatch):
    _capture_run(monkeypatch)
    proj = _project(tmp_path)

    agent_stage.run_agent_stage(
        _stage(), TODO_ID, proj, MODEL_CONFIG, proj / ".agentic" / "logs",
    )

    log = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(encoding="utf-8")
    ctx_lines = [line for line in log.splitlines() if " CTX " in line]
    # IMP2b: one stage, one text, one measurement — a second build would
    # leave a second line naming a text that is no longer on disk.
    assert len(ctx_lines) == 1
    ctx = ctx_lines[0]
    assert f"{TODO_ID} agent-dev" in ctx
    assert "type=execute" in ctx
    assert "file=" in ctx
    assert "role=" in ctx
    assert "role_pct=" in ctx


def test_retry_note_appended_to_prompt(tmp_path, monkeypatch):
    captured = _capture_run(monkeypatch)
    proj = _project(tmp_path)

    agent_stage.run_agent_stage(
        _stage(), TODO_ID, proj, MODEL_CONFIG, proj / ".agentic" / "logs",
        retry_note="## RETRY 1: test push note",
    )

    prompt = captured["cmd"][-1]
    assert "RETRY 1: test push note" in prompt
    # Appended at the very end — after the context and the stage identity.
    assert prompt.rindex("RETRY 1") > prompt.rindex("## Stage identity")
    stage_file = proj / ".agentic" / "context" / f"STAGE-{TODO_ID}-agent-dev.md"
    assert stage_file.read_text(encoding="utf-8") == prompt


def test_retry_note_survives_the_tail_cap(tmp_path, monkeypatch):
    """The retry push is this run's own text; the static tail gives way.

    A retry adds ~800 characters to a tail already near its 1 500 cap. The
    blind cut at the end removed exactly the note the retry exists for.
    """
    captured = _capture_run(monkeypatch)
    proj = _project(tmp_path)
    note = "## ⚠️ RETRY 1\n" + "act now, do not re-read. " * 30

    agent_stage.run_agent_stage(
        _stage(), TODO_ID, proj, MODEL_CONFIG, proj / ".agentic" / "logs",
        retry_note=note,
    )

    prompt = captured["cmd"][-1]
    # The builder strips the suffix's outer whitespace; the note's own text
    # is what must survive, not its trailing space.
    assert note.rstrip() in prompt
    assert "## Stage identity" in prompt
    assert len(prompt) <= CAP


def test_without_retry_note_prompt_has_no_retry(tmp_path, monkeypatch):
    captured = _capture_run(monkeypatch)
    proj = _project(tmp_path)

    agent_stage.run_agent_stage(
        _stage(), TODO_ID, proj, MODEL_CONFIG, proj / ".agentic" / "logs",
    )

    assert "RETRY" not in captured["cmd"][-1]


def test_explicit_log_name_for_any_role(tmp_path, monkeypatch):
    """AUD16-06: run_agent_stage passes awf-{role}-{todo}.out explicitly —
    the worker log is findable by the dashboard for ANY role name."""
    captured: dict = {}

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None, signal_holder=None,
                 **kwargs):
        captured.update(kwargs)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)

    proj = _project(tmp_path)
    agent_stage.run_agent_stage(
        _stage(), TODO_ID, proj, MODEL_CONFIG, proj / ".agentic" / "logs",
    )

    assert captured["log_name"] == "awf-developer-TODO-0001.out"


def test_ctx_total_is_the_written_stage_text(tmp_path, monkeypatch):
    """T13: ``total`` is the length of the text the stage really gets.

    The tail (assignment, stage identity, retry note) is appended to the
    same file and the same argv, so a ``total`` measured before the tail
    understates the prompt — and no longer equals ``len(text)``.
    """
    captured = _capture_run(monkeypatch)
    proj = _project(tmp_path)

    agent_stage.run_agent_stage(
        _stage(), TODO_ID, proj, MODEL_CONFIG, proj / ".agentic" / "logs",
        retry_note="## RETRY 1: a note long enough to move the number",
    )

    prompt = captured["cmd"][-1]
    assert len(prompt) <= CAP
    ctx = next(
        line
        for line in (proj / ".agentic" / "logs" / "orchestrator.log")
        .read_text(encoding="utf-8").splitlines()
        if " CTX " in line
    )
    total = int(re.search(r"total=(\d+)", ctx).group(1))
    assert total == len(prompt)
