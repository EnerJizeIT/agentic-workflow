"""A42 (TODO-0355): the verify prompt is facts, not handoff history.

Owns three rules:

1. the prompt carries the pack verdict inline (verdict + red sections +
   first reason) and the diff, while handoff history never enters it;
2. the assembled prompt fits the character budget; overflow is cut and
   named, never silent;
3. the stage narrates itself in one line — prompt size, files, duration,
   commands — and the prompt forbids re-running the checks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import verify_context
from awf.verify_context import (
    VERIFY_PROMPT_BUDGET_CHARS,
    build_verify_prompt,
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
 "sections": {"gates": "fail", "diff": "pass"}}
```
"""


def _project(tmp_path: Path) -> Path:
    agentic = tmp_path / ".agentic"
    for name in ("inbox", "outbox", "context", "handoff"):
        (agentic / name).mkdir(parents=True)
    (agentic / "logs").mkdir()
    (agentic / "inbox" / f"{_TODO_ID}.md").write_text(
        "# TODO-0001 — unit\n\n## Success criteria\n- marker-criterion\n",
        encoding="utf-8",
    )
    (agentic / "outbox" / f"DONE-{_TODO_ID}.md").write_text(
        "# DONE\n\nmarker-done-summary\n", encoding="utf-8"
    )
    (agentic / "outbox" / f"PROGRESS-{_TODO_ID}.md").write_text(
        "## Task 1\n\nmarker-progress\n", encoding="utf-8"
    )
    (agentic / "context" / f"GATES-{_TODO_ID}.md").write_text(_PACK, encoding="utf-8")
    (agentic / "handoff" / f"agent-qa-review-{_TODO_ID}.md").write_text(
        "HANDOFF-SECRET-MARKER\n" * 200, encoding="utf-8"
    )
    return tmp_path


def test_prompt_excludes_handoffs_and_embeds_pack_verdict(tmp_path):
    """Handoff history is out; the pack verdict and its reason are inline."""
    project = _project(tmp_path)
    prompt, _dropped = build_verify_prompt(_TODO_ID, project)

    assert "HANDOFF-SECRET-MARKER" not in prompt
    assert "handoff" not in prompt.lower()
    assert "Read ALL handoffs" not in prompt

    # Facts, not a pointer to a file: verdict, red section and why.
    assert "verdict: failed" in prompt
    assert "red sections: gates" in prompt
    assert "marker-reason" in prompt
    assert "marker-criterion" in prompt
    assert "marker-done-summary" in prompt


def test_prompt_forbids_running_checks(tmp_path):
    """The pack already ran the checks — the verdict is decided from facts."""
    prompt, _dropped = build_verify_prompt(_TODO_ID, _project(tmp_path))
    assert "do NOT run tests" in prompt
    assert "Do not re-run the pack" in prompt


def test_prompt_fits_the_budget(tmp_path):
    """Overflow is cut with a named reason, never silently."""
    project = _project(tmp_path)
    todo = project / ".agentic" / "inbox" / f"{_TODO_ID}.md"
    todo.write_text(todo.read_text(encoding="utf-8") + "long-line\n" * 2000,
                    encoding="utf-8")
    prompt, dropped = build_verify_prompt(_TODO_ID, project, budget=4000)
    assert len(prompt) <= 4000
    assert any("TODO" in name for name in dropped), dropped

    full, no_cut = build_verify_prompt(_TODO_ID, project)
    assert no_cut == []
    assert len(full) <= VERIFY_PROMPT_BUDGET_CHARS


def test_overflow_names_the_section_it_cut(tmp_path):
    """The cut is attributed to the section, not blamed on the instructions.

    Regression: the old positional walk took the overflow off the end of the
    prompt, always landing ~20 chars over the budget, so the clamp then fired
    and reported "fixed instructions truncated" for an ordinary section cut.
    """
    project = _project(tmp_path)
    todo = project / ".agentic" / "inbox" / f"{_TODO_ID}.md"
    todo.write_text(todo.read_text(encoding="utf-8") + "pad-line\n" * 400,
                    encoding="utf-8")

    prompt, dropped = build_verify_prompt(_TODO_ID, project, budget=3500)

    assert len(prompt) <= 3500
    assert "TODO (the contract) truncated" in dropped, dropped
    assert "fixed text truncated" not in dropped, dropped
    # The verdict and the instruction to write the signal survive the cut.
    assert "verdict: failed" in prompt
    assert f"ACK-{_TODO_ID}.ready" in prompt
    assert f"REVIEW-{_TODO_ID}.md" in prompt


def test_prompt_does_not_send_the_model_to_read_or_run(tmp_path):
    """A42: the facts are inline — no file reads, no command runs.

    The base verify instructions used to say "read the DONE report and
    PROGRESS notes, check `git diff --stat`" while the snippet in the same
    prompt said the opposite. One contract, not two.
    """
    prompt, _dropped = build_verify_prompt(_TODO_ID, _project(tmp_path))

    assert "read the DONE report" not in prompt
    assert "Run verify commands" not in prompt
    assert "git diff --stat" not in prompt
    assert "do not run them again" in prompt


def test_nothing_measured_is_not_dressed_as_green(tmp_path):
    """A verdict of "nothing-measured" must not read as "all sections passed".

    The block always states the verdict; the helper line only says what the
    report contains, so a pack that measured nothing cannot be mistaken for
    a green one.
    """
    project = _project(tmp_path)
    (project / ".agentic" / "context" / f"GATES-{_TODO_ID}.md").write_text(
        "# GATES-TODO-0001\n\n## Verdict: **nothing-measured** (exit 0) — "
        "0 section(s) measured\n",
        encoding="utf-8",
    )

    prompt, _dropped = build_verify_prompt(_TODO_ID, project)

    assert "verdict: nothing-measured" in prompt
    assert "every measured section passed" not in prompt
    assert "no failing sections in the report" in prompt


def test_empty_pack_is_its_own_line(tmp_path):
    """No GATES report is a fact of its own, not a silent absence."""
    project = _project(tmp_path)
    (project / ".agentic" / "context" / f"GATES-{_TODO_ID}.md").unlink()
    prompt, _dropped = build_verify_prompt(_TODO_ID, project)
    assert "verify pack: EMPTY" in prompt


def test_timing_line_names_size_and_duration(tmp_path):
    """The stage's one narration line carries numbers, not a feeling."""
    logs = tmp_path / ".agentic" / "logs"
    logs.mkdir(parents=True)

    fast = format_verify_timing(
        _TODO_ID, prompt_chars=4321, files=2, duration=12.5, commands=1
    )
    slow = format_verify_timing(
        _TODO_ID, prompt_chars=9876, files=3, duration=205.0, commands=7
    )
    log_verify_timing(logs, slow)

    assert "prompt 9876 chars in 3 file(s)" in slow
    assert "duration 205.0s" in slow
    assert "commands 7" in slow
    assert "over 180s target" in slow
    assert "prompt 9876 chars" in slow
    assert "over 180s target" not in fast
    assert "duration 12.5s" in fast

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


@pytest.mark.parametrize("budget", [VERIFY_PROMPT_BUDGET_CHARS])
def test_budget_constant_is_a_real_cut(tmp_path, budget, monkeypatch):
    """Pre-A42 handoffs alone were ~35k chars; the budget must be far below."""
    assert budget < 20000
    monkeypatch.setattr(verify_context, "verify_budget_chars", lambda: budget)
    prompt, _dropped = build_verify_prompt(_TODO_ID, _project(tmp_path))
    assert len(prompt) <= budget


def test_stage_writes_the_timing_line(tmp_path, monkeypatch):
    """The wiring, not just the formatter: the stage logs size + duration."""
    import re
    from subprocess import CompletedProcess

    from awf import supervisor
    from tests.unit._signal_fakes import log_text, make_project

    proj = make_project(tmp_path)
    worker_log = proj / ".agentic" / "logs" / "worker-output.out"

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
        {"models": {"supervisor": {"model": "p/test"}}},
        str(proj / ".agentic" / "phases" / "plan.md"),
        proj / ".agentic" / "logs",
    )

    line = next(
        row for row in log_text(proj).splitlines() if "A42: verify TODO-0001" in row
    )
    assert re.search(r"prompt \d+ chars in 1 file\(s\)", line), line
    assert re.search(r"duration \d+\.\ds", line), line
    assert "commands 1" in line


def test_stage_attaches_nothing_but_the_role_to_verify(tmp_path):
    """A42 at the SPAWN level: no handoff file reaches the subprocess.

    The prompt test above proves the built text carries no handoff body; this
    one proves the stage does not re-attach the same history as ``--file``,
    which the prompt text cannot show. Handoffs exist here on purpose.
    """
    from awf import supervisor
    from tests.unit._signal_fakes import make_project

    proj = make_project(tmp_path)
    handoff = proj / ".agentic" / "handoff"
    handoff.mkdir()
    (handoff / f"agent-qa-review-{_TODO_ID}.md").write_text(
        "HANDOFF-SECRET-MARKER " * 200, encoding="utf-8"
    )
    (proj / ".agentic" / "inbox" / f"{_TODO_ID}.md").write_text(
        f"# {_TODO_ID}\n\n## Success criteria\n- marker-criterion\n",
        encoding="utf-8",
    )

    extra_files, prompt = supervisor._prepare_supervisor_stage(
        "verify",
        _TODO_ID,
        proj,
        {},
        str(proj / ".agentic" / "phases" / "plan.md"),
        proj / ".agentic" / "logs",
        None,
    )

    assert extra_files == [], "verify must not attach handoffs/TODO as files"
    assert "HANDOFF-SECRET-MARKER" not in prompt
    assert "marker-criterion" in prompt, "the TODO still reaches the model inline"
    assert "verify pack: EMPTY" in prompt, "a missing pack is stated, not silent"
