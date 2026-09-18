"""Agent stage — spawn opencode run for each pipeline role.

Extracted from orchestrator.py (A6 refactor).
"""
from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from ._atomic import atomic_write_text
from ._log import log as _log
from .pipeline import Stage
from .signals import clean_stage_signals, expected_signal_prefixes
from .supervisor import (
    build_prompt,
    get_agent_name,
    get_role_model,
    resolve_role_file,
)


def run_agent_stage(
    stage: Stage,
    todo_id: str,
    project_dir: Path,
    config: dict,
    logs_dir: Path,
    prev_handoffs: list[Path] | None = None,
    hard_timeout: int | None = None,
    retry_note: str | None = None,
    attempt: int = 1,
) -> None:
    """Spawn opencode run for an agent stage.

    KAUD-4: ``hard_timeout`` overrides default 3600s timeout. Pass through
    from CLI --timeout or config. None = use BD20_HARD_TIMEOUT default.

    BD-15: ``prev_handoffs`` is a list of handoff .md files from previous
    pipeline stages (in pipeline order). Each is passed to the agent as
    ``--file`` so the agent sees what previous roles did.

    dogfood-11: ``retry_note`` is appended to the prompt when the stage is
    being retried after a silent exit (worker answered with text / hit its
    output-token limit) — pushes it to act instead of re-researching.
    ``attempt`` is the 1-based worker run number for this stage entry and
    lands in the handoff facts.
    """
    role = stage.role
    kind = stage.kind
    agent_name = get_agent_name(config, role)

    print()
    print("=" * 41)
    print(f"  AGENT STAGE: {role} ({kind})")
    print(f"  Task: {todo_id}")
    print("=" * 41)
    print()

    role_file = resolve_role_file(role, project_dir)
    inbox = paths.inbox(project_dir)
    todo_file = inbox / f"{todo_id}.md"

    prompt = build_prompt(kind, todo_id, project_dir=project_dir)
    if retry_note:
        prompt = prompt + "\n\n" + retry_note
        _log(logs_dir, f"dogfood-11: retry note appended to {role} prompt")

    print(f"Running agent: {agent_name}")
    print(f"Role file: {role_file}")
    print(f"Task: {todo_file}")
    print()

    # Clean stale signals this kind could produce
    prefixes = expected_signal_prefixes(kind)
    outbox = paths.outbox(project_dir)
    clean_stage_signals(outbox, todo_id, *prefixes)

    # DF5-5: clean stale PROGRESS from previous stage so collect_handoff
    # doesn't pick up the previous worker's notes as this stage's output.
    # PROGRESS is per-stage running notes, not cumulative. Previous stage's
    # PROGRESS is already captured in its handoff .md file (BD-19).
    stale_progress = outbox / f"PROGRESS-{todo_id}.md"
    if stale_progress.exists():
        stale_progress.unlink()
        _log(logs_dir, f"DF5-5: cleaned stale PROGRESS-{todo_id}.md from previous stage")

    _log(logs_dir, f"Agent stage started: {role} ({kind}) for {todo_id}")

    cmd = [
        "opencode", "run", "--auto",
        "--print-logs",
        "--agent", agent_name,
        "--title", f"awf-{role}-{todo_id}",
        "--file", str(role_file),
        "--file", str(todo_file),
    ]
    role_model = get_role_model(config, role)
    if role_model:
        cmd += ["--model", role_model]

    if prev_handoffs:
        for hf in prev_handoffs:
            if hf.is_file():
                cmd += ["--file", str(hf)]
                print(f"Handoff in:  {hf}")
                _log(logs_dir, f"Forwarding handoff: {hf}")

    cmd += ["--", prompt]

    # BD-20: watch for expected signal files in outbox.
    # P1: watch ALL expected prefixes, not just DONE/BLOCKED — otherwise
    # REVIEW-APPROVED, TEST-PASSED etc. signals aren't detected until
    # subprocess exits, causing unnecessary delays.
    watch_paths: list[Path] = []
    if todo_id:
        for prefix in expected_signal_prefixes("execute"):
            watch_paths.append(outbox / f"{prefix}-{todo_id}.ready")
            watch_paths.append(outbox / f"{prefix}-{todo_id}.md.ready")

    from ._env import awf_subprocess_env
    from .signal_watch import run_subprocess_until_signal

    agent_start = time.monotonic()
    result = run_subprocess_until_signal(
        cmd, cwd=project_dir, watch_paths=watch_paths, logs_dir=logs_dir,
        env=awf_subprocess_env(),
        hard_timeout=hard_timeout,
    )
    agent_elapsed = time.monotonic() - agent_start
    _log(logs_dir, f"Agent stage finished: {role} ({kind}) for {todo_id} (exit={result.returncode})")
    if result.returncode != 0:
        raise RuntimeError(
            f"Agent stage {role} ({kind}) subprocess exited with code {result.returncode}. "
            f"Cmd: {' '.join(cmd)}"
        )

    collect_handoff(
        role, todo_id, project_dir, logs_dir,
        exit_code=result.returncode, duration_sec=agent_elapsed, attempt=attempt,
        stage_name=stage.name,
    )


def collect_handoff(
    role: str,
    todo_id: str,
    project_dir: Path,
    logs_dir: Path,
    exit_code: int | None = None,
    duration_sec: float | None = None,
    attempt: int = 1,
    stage_name: str = "",
) -> Path:
    """BD-15/19: gather PROGRESS/DONE + git diff summary into handoff .md.

    BD-19: filename is ``<role>-<todo_id>.md`` so retry on the same role
    doesn't overwrite the previous attempt's handoff.

    Handoff v2 (dogfood-11): the file is a *fact sheet* for the NEXT WORKER —
    run metadata, signal/notes presence, changes vs baseline — instead of
    alarm prose addressed to the supervisor (that lives in the SALVAGE note).
    """
    handoff_dir = paths.agentic_dir(project_dir) / "handoff"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    outbox = paths.outbox(project_dir)

    progress = outbox / f"PROGRESS-{todo_id}.md"
    # H2 fix: support legacy short form DONE-{short_id}.md
    from .signals import find_signal_file
    done_path = find_signal_file(outbox, "DONE", todo_id, ".md") or (outbox / f"DONE-{todo_id}.md")

    progress_body = ""
    if progress.is_file():
        progress_body = progress.read_text(encoding="utf-8").strip()
    done_body = ""
    if done_path.is_file():
        done_body = done_path.read_text(encoding="utf-8").strip()

    def _signal(prefix: str, suffix: str) -> str:
        return "yes" if (outbox / f"{prefix}-{todo_id}{suffix}").is_file() else "no"

    # Changes vs baseline — a deterministic fact used both in the fact block
    # and in the diff section below.
    context_dir = paths.context_dir(project_dir)
    sha_file = context_dir / f"BASELINE-{todo_id}.sha"
    base_sha = ""
    if sha_file.is_file():
        base_sha = sha_file.read_text(encoding="utf-8").strip().split("\n")[0]
    diff_stat_text = ""
    changed_files: list[str] | None = None
    if base_sha:
        try:
            diff = subprocess.run(
                ["git", "diff", "--stat", base_sha],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if diff is not None:
                diff_stat_text = (getattr(diff, "stdout", "") or "").strip()
        except (subprocess.TimeoutExpired, OSError) as e:
            _log(logs_dir, f"handoff git-diff failed: {e}")
        try:
            names = subprocess.run(
                ["git", "diff", "--name-only", base_sha],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if names is not None:
                changed_files = [
                    line.strip()
                    for line in (getattr(names, "stdout", "") or "").splitlines()
                    if line.strip()
                ]
        except (subprocess.TimeoutExpired, OSError):
            pass

    if not base_sha:
        changes_fact = "- changes vs baseline: no baseline recorded"
    elif changed_files is None:
        changes_fact = "- changes vs baseline: unknown (git diff failed)"
    elif changed_files:
        listed = ", ".join(f"`{p}`" for p in changed_files[:5])
        more = f" … +{len(changed_files) - 5}" if len(changed_files) > 5 else ""
        changes_fact = f"- changes vs baseline: {len(changed_files)} file(s) — {listed}{more}"
    else:
        changes_fact = "- changes vs baseline: none"

    facts: list[str] = [
        "## Run facts",
        "",
        f"- generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"- stage role: `{role}`",
        f"- worker run: {attempt}",
    ]
    if exit_code is not None:
        facts.append(f"- worker exit code: {exit_code}")
    if duration_sec is not None:
        facts.append(f"- worker duration: {duration_sec:.0f}s")
    facts += [
        f"- signals: DONE={_signal('DONE', '.ready')}, "
        f"BLOCKED={_signal('BLOCKED', '.ready')}, "
        f"REVIEW={'yes' if (outbox / f'REVIEW-{todo_id}.md').is_file() else 'no'}",
        f"- worker notes: PROGRESS={'present' if progress_body else 'absent'}, "
        f"DONE-report={'present' if done_body else 'absent'}",
        changes_fact,
    ]

    parts: list[str] = [
        f"# Handoff from `{role}` (TODO {todo_id})",
        "",
        *facts,
        "",
    ]

    if done_body:
        parts += ["## DONE summary (from worker)", "", done_body, ""]

    if progress_body:
        parts += ["## PROGRESS notes (from worker)", "", progress_body, ""]
    elif not done_body:
        # No notes from the worker. What that MEANS depends on the changes
        # fact — never guess a cause at the next worker (handoff v2).
        if changed_files:
            parts += [
                "## ⚠️ No worker notes",
                "",
                f"Role `{role}` left no PROGRESS-{todo_id}.md and no DONE-{todo_id}.md",
                "summary. The file changes listed below are the only evidence of",
                "its work — inspect them directly.",
                "",
            ]
        else:
            parts += [
                "## ⚠️ No worker notes and no file changes",
                "",
                f"Role `{role}` left no notes and no changes vs baseline.",
                "Treat its work as absent — proceed from the TODO as written.",
                "",
            ]

    if diff_stat_text:
        parts += ["## Git diff summary (vs baseline)", "", "```", diff_stat_text, "```", ""]

    try:
        last = subprocess.run(
            ["git", "log", "-1", "--pretty=%h %s"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if last is not None and (getattr(last, "stdout", "") or "").strip():
            parts += ["## Latest commit", "", f"`{last.stdout.strip()}`", ""]
    except (subprocess.TimeoutExpired, OSError):
        pass

    parts += [
        "## What the next role should know",
        "",
        "- Read this handoff first. The TODO file declares the goal; this file shows what's done.",
        "- Pick up where this role left off. Do NOT redo work already done.",
        (
            f"- Write your notes to `.agentic/outbox/PROGRESS-{todo_id}.md` (and the "
            f"DONE report) — awf assembles the handoff from them automatically. "
            f"Do NOT create extra files in .agentic/handoff/."
        ),
        "",
    ]

    # Day-3 (dashboard review): key by STAGE name, not role — two QA stages
    # share the role 'agent-qa-review' and used to overwrite each other's
    # handoff (and borrow the wrong stage duration in the chat).
    out_file = handoff_dir / f"{stage_name or role}-{todo_id}.md"
    # QA-C: handoff write atomic (was direct write_text — inconsistent with
    # rest of codebase which uses atomic_write_text for crash safety).
    atomic_write_text(out_file, "\n".join(parts))
    print(f"Handoff out: {out_file}")
    _log(logs_dir, f"Handoff written: {out_file}")
    return out_file


def resolve_prev_handoffs(
    pipeline_stages: list[Stage],
    current_stage_idx: int,
    project_dir: Path,
    todo_id: str = "",
) -> list[Path]:
    """BD-15/19: return handoff paths for all AGENT stages before current_stage_idx."""
    handoff_dir = paths.agentic_dir(project_dir) / "handoff"
    result: list[Path] = []
    for i in range(current_stage_idx):
        st = pipeline_stages[i]
        if st.role == "supervisor":
            continue
        if todo_id:
            primary = handoff_dir / f"{st.name}-{todo_id}.md"
            legacy = handoff_dir / f"{st.role}-{todo_id}.md"
            if not primary.is_file() and legacy.is_file():
                result.append(legacy)  # in-flight runs from before the rename
            else:
                result.append(primary)
        else:
            # P2: sort by name (numeric ID) instead of mtime — deterministic
            # even when rapid retry creates files in the same second.
            import re
            def _sort_key(p: Path) -> tuple[int, str]:
                m = re.search(r"(\d+)", p.name)
                return (int(m.group(1)) if m else 0, p.name)
            candidates = sorted(
                handoff_dir.glob(f"{st.role}- *.md".replace(" ", "")),
                key=_sort_key,
                reverse=True,
            )
            if candidates:
                result.append(candidates[0])
    return result
