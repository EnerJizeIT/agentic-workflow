"""Agent stage — spawn opencode run for each pipeline role.

Extracted from orchestrator.py (A6 refactor).
"""
from __future__ import annotations

import subprocess
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
) -> None:
    """Spawn opencode run for an agent stage.

    KAUD-4: ``hard_timeout`` overrides default 3600s timeout. Pass through
    from CLI --timeout or config. None = use BD20_HARD_TIMEOUT default.

    BD-15: ``prev_handoffs`` is a list of handoff .md files from previous
    pipeline stages (in pipeline order). Each is passed to the agent as
    ``--file`` so the agent sees what previous roles did.
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

    # BD-20: watch for DONE / BLOCKED signal in outbox.
    # BD-21: watch BOTH `.ready` and `.md.ready` (LLMs sometimes append
    # .ready to the .md filename).
    watch_paths: list[Path] = []
    if todo_id:
        watch_paths = [
            outbox / f"DONE-{todo_id}.ready",
            outbox / f"DONE-{todo_id}.md.ready",
            outbox / f"BLOCKED-{todo_id}.ready",
            outbox / f"BLOCKED-{todo_id}.md.ready",
        ]

    from ._env import awf_subprocess_env
    from .signal_watch import run_subprocess_until_signal

    result = run_subprocess_until_signal(
        cmd, cwd=project_dir, watch_paths=watch_paths, logs_dir=logs_dir,
        env=awf_subprocess_env(),
        hard_timeout=hard_timeout,
    )
    _log(logs_dir, f"Agent stage finished: {role} ({kind}) for {todo_id} (exit={result.returncode})")
    if result.returncode != 0:
        raise RuntimeError(
            f"Agent stage {role} ({kind}) subprocess exited with code {result.returncode}. "
            f"Cmd: {' '.join(cmd)}"
        )

    collect_handoff(role, todo_id, project_dir, logs_dir)


def collect_handoff(
    role: str,
    todo_id: str,
    project_dir: Path,
    logs_dir: Path,
) -> Path:
    """BD-15/19: gather PROGRESS/DONE + git diff summary into handoff .md.

    BD-19: filename is ``<role>-<todo_id>.md`` so retry on the same role
    doesn't overwrite the previous attempt's handoff.
    """
    handoff_dir = paths.agentic_dir(project_dir) / "handoff"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    outbox = paths.outbox(project_dir)

    progress = outbox / f"PROGRESS-{todo_id}.md"
    # H2 fix: support legacy short form DONE-{short_id}.md
    from .signals import find_signal_file
    done_path = find_signal_file(outbox, "DONE", todo_id, ".md") or (outbox / f"DONE-{todo_id}.md")

    parts: list[str] = [
        f"# Handoff from `{role}` (TODO {todo_id})",
        "",
        "**Generated:** by awf orchestrator (BD-15)",
        f"**Stage role:** {role}",
        f"**TODO:** {todo_id}",
        "",
    ]

    has_output = False
    done_has_body = False
    if done_path.is_file():
        body = done_path.read_text(encoding="utf-8").strip()
        if body:
            parts += ["## DONE summary (from worker)", "", body, ""]
            has_output = True
            done_has_body = True

    if progress.is_file():
        body = progress.read_text(encoding="utf-8").strip()
        if body:
            parts += ["## PROGRESS notes (from worker)", "", body, ""]
            has_output = True
    elif not done_has_body:
        # Only warn about missing PROGRESS if DONE summary is also absent.
        # DONE carries the same info — warning when DONE exists is noise.
        parts += [
            "## ⚠️ Worker did not leave progress notes",
            "",
            f"Role `{role}` did not write PROGRESS-{todo_id}.md.",
            "The work may still be valid — inspect git diff and DONE report.",
            "",
        ]

    if not has_output:
        parts += [
            "## ⚠️ NO OUTPUT FROM PREVIOUS STAGE",
            "",
            f"Role `{role}` did not write PROGRESS-{todo_id}.md or DONE-{todo_id}.md.",
            "Likely causes: worker crashed, hit turn-budget, or signalled",
            "BLOCKED without leaving notes. Treat prior stage work as",
            "unverified — inspect `git diff` against baseline before",
            "proceeding. If this is unexpected, escalate via BLOCKED signal.",
            "",
        ]

    # Git diff summary vs baseline
    context_dir = paths.context_dir(project_dir)
    sha_file = context_dir / f"BASELINE-{todo_id}.sha"
    if sha_file.is_file():
        base_sha = sha_file.read_text(encoding="utf-8").strip().split("\n")[0]
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
                if diff is not None and getattr(diff, "stdout", "").strip():
                    parts += ["## Git diff summary (vs baseline)", "", "```", diff.stdout.strip(), "```", ""]
            except (subprocess.TimeoutExpired, OSError) as e:
                _log(logs_dir, f"handoff git-diff failed: {e}")

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
        "- Write your own handoff at `.agentic/handoff/<your-role>.md` when finished.",
        "",
    ]

    out_file = handoff_dir / f"{role}-{todo_id}.md"
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
            result.append(handoff_dir / f"{st.role}-{todo_id}.md")
        else:
            candidates = sorted(
                handoff_dir.glob(f"{st.role}-*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                result.append(candidates[0])
    return result
