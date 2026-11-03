"""A42: the verify stage's prompt — pack facts instead of handoff history.

Before A42 the verify prompt carried the supervisor role, the doctrine and
every handoff of the unit (35–50k chars for a green unit) as ``--file``
attachments, and the model spent 7–10 minutes of its own time re-reading
them and re-running checks the pack had already run. The verdict itself is
a fact on disk: ``context/GATES-<id>.md``.

What this module assembles is ONE bounded prompt:

- the verify pack verdict inline — verdict, red sections, first reason
  (``verify_pack.read_gates_facts`` is the single parser);
- ``git diff --stat`` + the changed/new file list (``verify.diff_stat_for_todo``);
- TODO, ``DONE`` and ``PROGRESS`` as short excerpts;
- the stage's own instructions from ``supervisor.build_prompt("verify")``.

Handoff history never enters the prompt, and the text forbids re-running
the checks. The one narration line (prompt size, files, duration,
commands) is written by the supervisor stage after the subprocess ends —
``format_verify_timing`` is that line's one home.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import paths
from ._log import log as _log
from .verify_pack import read_gates_facts

# The whole assembled prompt (instructions + facts) must fit this. Measured
# against the pre-A42 shape: handoffs alone were 35k chars for TODO-0354.
VERIFY_PROMPT_BUDGET_CHARS = 12000

# A green pack should reach a verdict within this window (A42 target). The
# timing line names the prompt size and the file count when it does not.
VERIFY_STAGE_TARGET_SECONDS = 180.0

# Excerpt caps for the two narrative sources ("кратко", not archives).
_DONE_EXCERPT_CHARS = 1500
_PROGRESS_EXCERPT_CHARS = 800
_TODO_EXCERPT_CHARS = 4000


def verify_budget_chars() -> int:
    """The verify prompt budget in characters (substitutable in tests)."""
    return VERIFY_PROMPT_BUDGET_CHARS


def _read_brief(path: Path, limit: int) -> str:
    """First ``limit`` chars of a file, or "" — never raises."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[... truncated ...]"


def _pack_block(project_dir: Path, todo_id: str) -> str:
    """The pack verdict as inline text; an absent pack is a line of its own."""
    report = paths.context_dir(project_dir) / f"GATES-{todo_id}.md"
    facts = read_gates_facts(report)
    if facts is None:
        return (
            f"verify pack: EMPTY — {report.name} is not on disk. "
            "No pack facts are collected for this unit; judge the diff and "
            "the TODO only."
        )

    head = [f"verdict: {facts.get('verdict') or 'unreadable'}"]
    exit_code = facts.get("exit_code")
    if exit_code is not None:
        head.append(f"exit {exit_code}")
    measured = facts.get("measured")
    if measured is not None:
        head.append(f"{measured} section(s) measured")
    lines = ["; ".join(head)]

    failed = [str(name) for name in (facts.get("failed_sections") or [])]
    if failed:
        lines.append("red sections: " + ", ".join(failed))
    first = str(facts.get("first_failure") or "").strip()
    if first:
        lines.append("first reason: " + first)
    note = str(facts.get("note") or "").strip()
    if note:
        lines.append("pack note: " + note)
    if not failed and not note:
        # The verdict line above is the claim; this line must not upgrade
        # "nothing-measured" or "error" into "all green" by accident.
        lines.append("no failing sections in the report")
    return "\n".join(lines)


def _diff_block(project_dir: Path, todo_id: str) -> str:
    """Diffstat + changed/new file list — the same facts the gate reads."""
    from .verify import diff_stat_for_todo

    text = diff_stat_for_todo(project_dir, todo_id).strip()
    if not text:
        return (
            "no diff stat available (missing BASELINE-<id>.sha, not a git "
            "repo, or git failed)"
        )
    return text


def _sections(
    todo_id: str, project_dir: Path
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The facts in two tiers: what the verdict needs, then the narrative.

    The first tier always enters the prompt — the pack block (the verdict),
    the diff (what changed) and the TODO (the contract). The second tier is
    narrative: it enters only while the whole prompt fits and gives way
    first ("DONE/PROGRESS — кратко").
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    required: list[tuple[str, str]] = [
        ("verify pack", _pack_block(project_dir, todo_id)),
        ("diff vs baseline", _diff_block(project_dir, todo_id)),
    ]
    todo_text = _read_brief(inbox / f"{todo_id}.md", _TODO_EXCERPT_CHARS)
    if todo_text:
        required.append(("TODO (the contract)", todo_text))
    optional: list[tuple[str, str]] = []
    progress = _read_brief(
        outbox / f"PROGRESS-{todo_id}.md", _PROGRESS_EXCERPT_CHARS
    )
    if progress:
        optional.append(("PROGRESS (short)", progress))
    done = _read_brief(outbox / f"DONE-{todo_id}.md", _DONE_EXCERPT_CHARS)
    if done:
        optional.append(("DONE (short)", done))
    return required, optional


def _render(base: str, kept: list[tuple[str, str]]) -> str:
    body = "\n\n".join(f"### {label}\n\n{text}" for label, text in kept)
    head = (
        "## Pack facts (already collected — do NOT run tests or verify commands)\n\n"
        "The verdict below is decided from these facts. Do not re-run the pack, "
        "the contract commands or the test suite."
    )
    return f"{base}\n\n{head}\n\n{body}" if body else f"{base}\n\n{head}"


def build_verify_prompt(
    todo_id: str,
    project_dir: str | Path,
    *,
    config: dict | None = None,
    pipeline_name: str | None = None,
    logs_dir: Path | None = None,
    budget: int | None = None,
) -> tuple[str, list[str]]:
    """A42: the bounded verify prompt — pack facts, no handoffs.

    Returns ``(prompt, dropped)`` where ``dropped`` names every section that
    was cut or truncated to stay inside the budget. The cut rules are
    :func:`awf.bounded_prompt.fit_prompt`: the PROGRESS/DONE excerpts give
    way first, then the TODO is truncated, then the diff — by label, never
    off the tail, so the pack verdict and the closing instruction survive.
    A missing or unreadable source never breaks the prompt: the section is
    simply absent.
    """
    from .bounded_prompt import fit_prompt
    from .supervisor import build_prompt

    project = Path(project_dir)
    base = build_prompt(
        "verify",
        todo_id,
        config=config,
        project_dir=project,
        pipeline_name=pipeline_name,
    )
    limit = verify_budget_chars() if budget is None else int(budget)

    required, optional = _sections(todo_id, project)
    prompt, dropped = fit_prompt(
        lambda items: _render(base, items),
        required=required,
        optional=optional,
        budget=limit,
        truncate_order=("TODO (the contract)", "diff vs baseline"),
    )
    if dropped and logs_dir is not None:
        _log(
            logs_dir,
            f"A42: verify prompt over {limit} chars — cut: " + ", ".join(dropped),
        )
    return prompt, dropped


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def count_logged_commands(log_path: str | Path | None, start_offset: int = 0) -> int:
    """Bash invocations the verify subprocess printed after ``start_offset``.

    ``opencode run`` echoes every shell tool call as a ``$ <cmd>`` line in
    the worker log. The count is best-effort narration for the timing line:
    an unreadable/absent log or a bad offset is ``-1`` ("unknown"), never an
    exception inside the stage.
    """
    if not log_path:
        return -1
    try:
        raw = Path(log_path).read_bytes()
    except OSError:
        return -1
    if start_offset < 0 or start_offset > len(raw):
        return -1
    text = _ANSI_RE.sub("", raw[start_offset:].decode("utf-8", errors="replace"))
    return sum(1 for line in text.splitlines() if line.startswith("$ "))


def format_verify_timing(
    todo_id: str,
    *,
    prompt_chars: int,
    files: int,
    duration: float,
    commands: int,
    target: float | None = None,
) -> str:
    """A42: the one narration line for a verify stage.

    Shape: ``A42: verify <id>: prompt <n> chars in <m> file(s), duration
    <x>s, commands <k>``. Over the target window the line appends the cause
    it can name — the prompt size and the file count.
    """
    window = VERIFY_STAGE_TARGET_SECONDS if target is None else float(target)
    command_text = "unknown" if commands < 0 else str(commands)
    line = (
        f"A42: verify {todo_id}: prompt {prompt_chars} chars in {files} file(s), "
        f"duration {duration:.1f}s, commands {command_text}"
    )
    if duration > window:
        line += (
            f" — over {window:.0f}s target; cause: prompt {prompt_chars} chars, "
            f"{files} file(s)"
        )
    return line


def log_verify_timing(logs_dir: Path | None, line: str) -> None:
    """Write the timing line to ``orchestrator.log`` (best-effort)."""
    if logs_dir is not None:
        _log(logs_dir, line)


__all__ = [
    "VERIFY_PROMPT_BUDGET_CHARS",
    "VERIFY_STAGE_TARGET_SECONDS",
    "build_verify_prompt",
    "count_logged_commands",
    "format_verify_timing",
    "log_verify_timing",
    "verify_budget_chars",
]
