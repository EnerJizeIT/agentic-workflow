"""Pipeline execution engine — the core state machine."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import git_utils, paths, todos, verify
from .pipeline import Stage, load_stages, resolve_pipeline_file
from .signals import (
    clean_stage_signals,
    expected_signal_prefixes,
    read_signal_for_todo,
    signal_type,
    wait_for_signal,
)
from .transitions import resolve_transition

APPROVE_TIMEOUT_SECONDS: int = int(os.environ.get("AWF_APPROVE_TIMEOUT_SECONDS", "1800"))
APPROVE_POLL_INTERVAL: int = 2


def _log(logs_dir: Path, message: str) -> None:
    """Append a timestamped line to orchestrator.log."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_file = logs_dir / "orchestrator.log"
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")


# BD-20: opencode run --auto sometimes doesn't exit after task is done
# (keeps looping). Awf watches for signal files that indicate "work is
# complete" and terminates the subprocess if it doesn't exit on its own.
BD20_SIGNAL_GRACE_SECONDS = 10  # grace after signal before terminate
BD20_POLL_INTERVAL = 2          # how often to check for signals
BD20_HARD_TIMEOUT = 1800        # absolute cap (30 min)


def _awf_subprocess_env() -> dict[str, str]:
    """BD-22: env for opencode subprocess spawned by awf.

    Sets ``OPENCODE_CONFIG_CONTENT`` to override global permission rules so
    the subprocess can run ``edit``/``bash``/``write`` without prompting
    the user (who isn't watching the subprocess anyway). Without this,
    every awf-launched opencode hangs on permission prompts → BD-20
    terminates it before any real work happens.

    Inline config has higher precedence than ``~/.config/opencode/opencode.json``
    (per opencode docs precedence order: remote < global < custom < project
    < .opencode < INLINE < managed).
    """
    import json
    import os

    env = os.environ.copy()
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
        "permission": {
            "edit": "allow",
            "bash": "allow",
            "write": "allow",
            "webfetch": "allow",
        }
    })
    return env


# BD-13/26: when normalize_skills runs in background (no human at the wheel),
# awf checks if local skills already exist (created by user via form).
# No mapping/guessing — the form is responsible for putting the right
# content into .agentic/roles/<role>.md. This module just checks completeness.


def _run_subprocess_until_signal(
    cmd: list[str],
    cwd: str | Path,
    watch_paths: list[Path] | None = None,
    watch_new_glob: tuple[Path, str] | None = None,
    logs_dir: Path | None = None,
    hard_timeout: int = BD20_HARD_TIMEOUT,
    grace_seconds: int = BD20_SIGNAL_GRACE_SECONDS,
) -> subprocess.CompletedProcess:
    """BD-20: run subprocess, watch for signal files, terminate if it lingers.

    Polls the subprocess every ``BD20_POLL_INTERVAL`` seconds. As soon as
    ANY of these conditions fires, gives ``grace_seconds`` to exit, then
    SIGTERM (and SIGKILL after 5s if still alive):

    - any path in ``watch_paths`` exists AFTER subprocess start (BD-22 fix:
      snapshot-based — stale files that existed before subprocess launch
      are ignored; only NEW appearances count as signal)
    - any NEW file matching ``watch_new_glob`` appears in the snapshot
      taken at start (used when the filename is picked by the subprocess
      itself, e.g. ``TODO-*.ready`` for supervisor create_todo)

    Args:
        cmd: command list.
        cwd: working directory.
        watch_paths: concrete paths whose NEW existence means "work is done".
        watch_new_glob: (directory, glob_pattern) — detect NEW files matching
            the pattern that didn't exist at subprocess start.
        logs_dir: optional, for logging.
        hard_timeout: absolute cap.
        grace_seconds: grace period after signal detected.
    """
    import time

    watch_paths = watch_paths or []
    # BD-22: snapshot which watch_paths already exist at start (stale signals
    # from previous runs). Only paths that DON'T exist at start, or that
    # appear AFTER start, count as a valid signal.
    pre_existing: set[str] = {str(p) for p in watch_paths if p.exists()}
    if pre_existing and logs_dir:
        _log(
            logs_dir,
            f"BD-22: ignoring {len(pre_existing)} stale watch_paths "
            f"(exist before subprocess start)",
        )

    snapshot: set[str] = set()
    if watch_new_glob is not None:
        watch_dir, pattern = watch_new_glob
        if watch_dir.is_dir():
            snapshot = {p.name for p in watch_dir.glob(pattern)}

    proc = subprocess.Popen(cmd, cwd=str(cwd), env=_awf_subprocess_env())
    deadline = time.monotonic() + hard_timeout
    signal_seen_at: float | None = None

    while True:
        rc = proc.poll()
        if rc is not None:
            if logs_dir:
                _log(logs_dir, f"subprocess exited naturally with code {rc}")
            return subprocess.CompletedProcess(cmd, rc)

        now = time.monotonic()

        if signal_seen_at is None:
            # BD-22: a path counts as signal only if it was NOT in pre_existing
            # snapshot (i.e., appeared DURING subprocess execution).
            triggered = any(
                str(p) not in pre_existing and p.exists() for p in watch_paths
            )
            if not triggered and watch_new_glob is not None:
                watch_dir, pattern = watch_new_glob
                if watch_dir.is_dir():
                    current = {p.name for p in watch_dir.glob(pattern)}
                    if current - snapshot:
                        triggered = True
            if triggered:
                signal_seen_at = now
                if logs_dir:
                    _log(
                        logs_dir,
                        f"BD-20: signal detected, granting {grace_seconds}s grace "
                        f"for subprocess to exit (pid={proc.pid})",
                    )
            elif now > deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                if logs_dir:
                    _log(logs_dir, "BD-20: hard timeout reached, subprocess killed")
                raise TimeoutError(
                    f"Subprocess (cmd: {cmd[0]}...) exceeded hard timeout "
                    f"of {hard_timeout}s without producing a signal"
                )
        else:
            if now - signal_seen_at >= grace_seconds:
                if logs_dir:
                    _log(
                        logs_dir,
                        f"BD-20: grace expired, terminating subprocess (pid={proc.pid})",
                    )
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                return subprocess.CompletedProcess(cmd, 0)

        time.sleep(BD20_POLL_INTERVAL)


def _get_agent_name(config: dict, role: str) -> str:
    """Get the agent_name override for a role, defaulting to the role itself."""
    return cfg_mod.get(config, f"models.{role}.agent_name", role) or role


def _get_role_model(config: dict, role: str) -> str | None:
    """BD-24: get the model override for a role from config.yaml.

    Returns the model string (e.g. "vllm/llm") if set, else None (let
    opencode pick its default).
    """
    val = cfg_mod.get(config, f"models.{role}.model", "")
    return val if val else None


def _build_prompt(kind: str, todo_id: str) -> str:
    """BD-29: build prompt for a stage kind.

    Only 3 kinds:
    - "plan"    — supervisor creates/refines TODO from phases/plan.md
    - "execute" — agent does its part of the TODO (any role, any skill)
    - "verify"  — supervisor verifies result, writes ACK or REVIEW

    Args:
        kind: stage.kind from pipeline.py
        todo_id: e.g. "TODO-0001"

    Returns:
        Prompt string passed to opencode run as the final positional arg.
    """
    if kind == "plan":
        return (
            "You are the supervisor. Read the phases/plan file. Determine the next step "
            "that is not yet completed. Create a TODO file at "
            ".agentic/inbox/TODO-{NNNN}.md (use next sequential ID), create baseline "
            "via `awf baseline TODO-{NNNN}`, then create the .ready signal at "
            ".agentic/inbox/TODO-{NNNN}.ready. If a TODO already exists in inbox, review "
            "it — refine, accept, or replace as needed (do NOT blindly skip). Keep the TODO "
            "at the goal level (what success looks like), do NOT micromanage individual "
            "roles — each role's skill.md already defines its zone."
        )
    if kind == "verify":
        return (
            f"You are the supervisor. Verify TODO {todo_id}: read the DONE report and PROGRESS notes, "
            "check `git diff --stat` against the baseline SHA in "
            f".agentic/context/BASELINE-{todo_id}.sha. Decide: is the work complete and correct? "
            "If yes, write ACK signal at "
            f".agentic/inbox/ACK-{todo_id}.ready. If no, do NOT ack — leave a note in "
            f".agentic/outbox/REVIEW-{todo_id}.md explaining what's wrong."
        )
    # execute (default)
    return (
        f"Execute your part of {todo_id} according to your role/skill instructions. "
        f"You see the TODO goal and handoffs from previous roles (if any). Add YOUR contribution — "
        f"don't redo prior work. When done, write .agentic/outbox/PROGRESS-{todo_id}.md (running notes), "
        f".agentic/outbox/DONE-{todo_id}.md (summary), and create the sentinel "
        f".agentic/outbox/DONE-{todo_id}.ready file (NOTE: .ready extension, NOT .md.ready). "
        f"If blocked, write BLOCKED-{todo_id}.md + BLOCKED-{todo_id}.ready instead. "
        f"Do not commit unless the TODO explicitly asks for it."
    )


log = logging.getLogger(__name__)


def _global_roles_dir() -> Path:
    """Return the global roles directory: ~/.config/awf/roles/."""
    return Path.home() / ".config" / "awf" / "roles"


def _resolve_role_file(role: str, project_dir: Path) -> Path:
    """Resolve role file with project-local → global fallback.

    Returns the existing ``<role>.md`` path or raises ``RuntimeError``
    listing both checked locations.
    """
    project_role = paths.agentic_dir(project_dir) / "roles" / f"{role}.md"
    global_role = _global_roles_dir() / f"{role}.md"

    if project_role.exists():
        return project_role

    if global_role.exists():
        log.info("Role '%s' not in project, falling back to global: %s", role, global_role)
        return global_role

    raise RuntimeError(
        f"Role file '{role}' not found.\n"
        f"  Checked: {project_role}\n"
        f"  Checked: {global_role}"
    )


def _wait_for_supervisor_signal(
    kind: str,
    todo_id: str,
    project_dir: Path,
    logs_dir: Path,
    poll_interval: int = 3,
) -> str:
    """BD-30: wait for the signal file the interactive supervisor should produce.

    In interactive mode (auto=False), the current opencode (the one in the
    user's chat) IS the supervisor — it reads the instructions printed by
    awf, does the work, and creates a signal file. This function polls
    for that file and returns the signal name when it appears.

    kind="plan"   → waits for new TODO-*.ready in inbox (snapshot-based)
    kind="verify" → waits for ACK-{todo_id}.ready or APPROVE-{todo_id}.ready
                    in inbox, OR REVIEW-{todo_id}.md in outbox
    """
    import time

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    # Snapshot what already exists so stale files don't trigger immediately.
    existing_todo_signals: set[str] = set()
    if kind in ("plan", "replan") and inbox.is_dir():
        existing_todo_signals = {p.name for p in inbox.glob("TODO-*.ready")}

    deadline_log_interval = 60  # log "still waiting" every minute
    last_log = time.monotonic()
    waited_total = 0

    while True:
        # plan/replan: new TODO-*.ready that didn't exist at start
        if kind in ("plan", "replan"):
            if inbox.is_dir():
                current = {p.name for p in inbox.glob("TODO-*.ready")}
                new_ones = current - existing_todo_signals
                if new_ones:
                    sig = sorted(new_ones)[0].replace(".ready", "")
                    _log(logs_dir, f"BD-30: interactive supervisor signal detected: {sig}")
                    return sig

        # verify: ACK or APPROVE in inbox, or REVIEW in outbox
        if kind == "verify" and todo_id:
            for sig_path in (
                inbox / f"ACK-{todo_id}.ready",
                inbox / f"APPROVE-{todo_id}.ready",
            ):
                if sig_path.exists():
                    _log(logs_dir, f"BD-30: interactive supervisor signal detected: {sig_path.name}")
                    return sig_path.stem
            review = outbox / f"REVIEW-{todo_id}.md"
            if review.exists():
                _log(logs_dir, f"BD-30: interactive supervisor signal detected: REVIEW-{todo_id}.md")
                return f"REVIEW-{todo_id}"

        now = time.monotonic()
        if now - last_log >= deadline_log_interval:
            waited_total += int(now - last_log)
            _log(
                logs_dir,
                f"BD-30: interactive supervisor still waiting for {kind} signal "
                f"({waited_total}s elapsed)",
            )
            last_log = now

        time.sleep(poll_interval)


def _run_supervisor_stage(
    stage: Stage,
    todo_id: str,
    auto: bool,
    project_dir: Path,
    logs_dir: Path,
) -> None:
    """Handle a supervisor stage.

    BD-29: supervisor stages are determined by position, not action.
    kind="plan" → create/refine TODO (always runs, even if TODO exists).
    kind="verify" → check result, write ACK or REVIEW.

    BD-30: in interactive mode (auto=False), the CURRENT opencode (in user's
    chat) is the supervisor. awf prints explicit instructions to the log
    file, then polls for a signal file. The user's opencode reads the log,
    does the work, creates the signal — awf continues.

    In auto mode (BD-14, --auto flag or CI): spawn ``opencode run`` subprocess
    that loads supervisor.md as instruction and does the work autonomously.
    """
    kind = stage.kind  # "plan" or "verify" (computed from position)
    config = cfg_mod.load(project_dir)
    phases_file = cfg_mod.get(config, "phases.current", ".agentic/phases/plan.md")

    print()
    print("=" * 41)
    print(f"  SUPERVISOR STAGE: {kind}")
    print("=" * 41)
    print()

    if not auto:
        # BD-30: interactive mode — current opencode (in chat) IS supervisor.
        # Print explicit instructions it can follow from the log file.
        _print_interactive_supervisor_instructions(
            kind, todo_id, project_dir, phases_file
        )
        _log(logs_dir, f"BD-30: interactive supervisor {kind} — waiting for signal")
        _wait_for_supervisor_signal(kind, todo_id, project_dir, logs_dir)
        _log(logs_dir, f"BD-30: interactive supervisor {kind} completed by user (opencode)")
        return

    # Auto mode: spawn opencode subprocess to do supervisor work.
    print("Instructions: .agentic/roles/supervisor.md")
    print(f"Phases file: {phases_file}")
    print()
    if kind == "plan":
        print("What to do (plan):")
        print("  1. Study the project state and phases file")
        print("  2. Determine the next step (or review existing TODO if present)")
        print("  3. Create baseline: awf baseline TODO-{NNNN}")
        print("  4. Write task to .agentic/inbox/TODO-{NNNN}.md")
        print("  5. Create signal: .agentic/inbox/TODO-{NNNN}.ready")
    elif kind == "verify":
        print("What to do (verify):")
        print("  1. Read report from .agentic/outbox/")
        print("  2. Run verification commands independently")
        print("  3. Check git diff — changes must be in source files")
        print("  4. Decide: continue / fix / rollback")
        print("  5. If approved: create .agentic/inbox/ACK-{NNNN}.ready")
    else:
        print(f"What to do ({kind}): see supervisor.md instructions")

    print()
    _run_supervisor_via_subprocess(kind, todo_id, project_dir, config, phases_file, logs_dir)


def _print_interactive_supervisor_instructions(
    kind: str,
    todo_id: str,
    project_dir: Path,
    phases_file: str,
) -> None:
    """BD-30: print explicit instructions for the current opencode (supervisor).

    Output goes to awf-start.out log file. The user's opencode reads it via
    `tail -f` or `Read` tool, follows the instructions, and creates the
    expected signal file. awf detects the signal and continues the pipeline.
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    handoff_dir = paths.agentic_dir(project_dir) / "handoff"
    phases_path = project_dir / phases_file if not Path(phases_file).is_absolute() else Path(phases_file)

    print("BD-30: INTERACTIVE SUPERVISOR MODE")
    print("=" * 60)
    print("You (the current opencode in user's chat) are the supervisor.")
    print("Follow the instructions below, then create the signal file.")
    print("awf is waiting — it will continue automatically when the signal appears.")
    print("=" * 60)
    print()

    if kind == "plan":
        print("STAGE: plan (create/refine TODO for the next pipeline step)")
        print()
        print("STEPS:")
        print(f"  1. Read {phases_path} — find next unfinished step ([ ] checkbox)")
        print(f"  2. Read existing TODO-*.md in {inbox}/ (if any) — review/refine")
        print("  3. Create .agentic/inbox/TODO-NNNN.md with detailed task:")
        print("     - Goal (what success looks like)")
        print("     - Prohibitions (what NOT to do)")
        print("     - Context (links, references, prior work)")
        print("     - Tasks with Files / Description / Verify / Done-when")
        print("  4. Run: awf baseline TODO-NNNN  (records current git SHA)")
        print("  5. Create empty signal file: .agentic/inbox/TODO-NNNN.ready")
        print()
        print(f"SIGNAL TO CREATE: {inbox}/TODO-NNNN.ready")
        print("(Use next sequential TODO number — check existing files in inbox/)")
    elif kind == "verify":
        print(f"STAGE: verify (review work done by agents on {todo_id})")
        print()
        print("INPUTS TO READ:")
        print(f"  - {outbox}/DONE-{todo_id}.md  (last role's summary)")
        print(f"  - {outbox}/PROGRESS-{todo_id}.md  (last role's running notes)")
        if handoff_dir.is_dir():
            handoffs = sorted(handoff_dir.glob(f"*-{todo_id}.md"))
            if handoffs:
                print(f"  - {handoff_dir}/  (per-role handoffs: {len(handoffs)} files)")
                for hf in handoffs:
                    print(f"      {hf.name}")
        print(f"  - .agentic/context/BASELINE-{todo_id}.sha  (git baseline)")
        print()
        print("STEPS:")
        print("  1. Read all handoffs + DONE + PROGRESS")
        print(f"  2. Run: git diff --stat $(cat .agentic/context/BASELINE-{todo_id}.sha)")
        print("  3. Verify each Task in TODO against actual changes")
        print("  4. Decide:")
        print("     - APPROVED → create signal file below")
        print(f"     - REJECTED → write REVIEW-{todo_id}.md in outbox explaining what's wrong")
        print()
        print(f"SIGNAL TO CREATE: {inbox}/ACK-{todo_id}.ready")
        print(f"  (or write REVIEW to: {outbox}/REVIEW-{todo_id}.md)")
    else:
        # replan / salvage — interactive paths from escalation/rollback
        print(f"STAGE: {kind}")
        print()
        print("This is an internal supervisor path (replan/salvage).")
        print("Read supervisor.md for guidance, then decide:")
        print("  - replan: create a new refined TODO-NNNN.md + .ready signal")
        print("  - salvage: review the current state and either ACK or REVIEW")
        print()
        print(f"SIGNAL TO CREATE: {inbox}/TODO-NNNN.ready (for replan)")
        print(f"  or: {inbox}/ACK-{todo_id}.ready (for salvage ACK)")

    print()
    print("=" * 60)
    print("awf is waiting for the signal. Take your time.")
    print("=" * 60)
    print()


def _run_supervisor_via_subprocess(
    kind: str,
    todo_id: str,
    project_dir: Path,
    config: dict,
    phases_file: str,
    logs_dir: Path,
) -> None:
    """BD-14/29: spawn ``opencode run --auto --agent worker --file supervisor.md``
    to do supervisor work without human.

    kind is one of:
    - "plan"    — create/refine TODO. ALWAYS runs (Q3 — even if TODO exists,
                  supervisor reviews it).
    - "verify"  — read DONE/PROGRESS, write ACK or REVIEW.
    - "replan"  — internal: BLOCKED signal → new TODO (called from run_pipeline
                  escalation path, not from a pipeline stage directly).
    - "salvage" — internal: agent crashed without signal (called from
                  run_pipeline salvage path).

    Falls back to "auto-skip" if supervisor.md is missing or if subprocess fails.
    """
    try:
        role_file = _resolve_role_file("supervisor", project_dir)
    except RuntimeError as e:
        print(f"[auto mode] supervisor.md not found — skipping. ({e})")
        _log(logs_dir, f"Supervisor stage {kind} auto-skipped (no supervisor.md)")
        return

    extra_files: list[str] = []
    prompt = ""

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    phases_path = project_dir / phases_file if not Path(phases_file).is_absolute() else Path(phases_file)

    if kind == "plan":
        # BD-29 / Q3: do NOT skip if active TODO exists. Supervisor reviews it.
        if phases_path.is_file():
            extra_files.append(str(phases_path))
        prompt = _build_prompt("plan", todo_id)
    elif kind == "verify":
        if not todo_id:
            print("[auto mode] No todo_id for verify — skip.")
            _log(logs_dir, "Supervisor verify auto-skipped (no todo_id)")
            return
        done_md = outbox / f"DONE-{todo_id}.md"
        if done_md.is_file():
            extra_files.append(str(done_md))
        progress = outbox / f"PROGRESS-{todo_id}.md"
        if progress.is_file():
            extra_files.append(str(progress))
        # BD-29 aggregate verify context: forward ALL role handoffs for this
        # todo_id so supervisor sees the full picture, not just the last
        # role's PROGRESS/DONE (which overwrite each other between roles).
        handoff_dir = paths.agentic_dir(project_dir) / "handoff"
        if handoff_dir.is_dir():
            for hf in sorted(handoff_dir.glob(f"*-{todo_id}.md")):
                if hf.is_file():
                    extra_files.append(str(hf))
        prompt = _build_prompt("verify", todo_id)
    elif kind == "replan":
        if not todo_id:
            print("[auto mode] No todo_id for replan — skip.")
            _log(logs_dir, "Supervisor replan auto-skipped (no todo_id)")
            return
        blocked = outbox / f"BLOCKED-{todo_id}.md"
        if blocked.is_file():
            extra_files.append(str(blocked))
        prompt = (
            f"Worker reported BLOCKED on {todo_id}. Read the BLOCKED note, analyze the problem, "
            "create a refined TODO at .agentic/inbox/TODO-{NNNN}.md (next sequential ID), "
            "baseline it, and create the .ready signal."
        )
    elif kind == "salvage":
        # salvage is too risky to automate — needs human judgement.
        print("[auto mode] salvage not automated — skipping.")
        _log(logs_dir, "Supervisor salvage auto-skipped (not automatable)")
        return
    else:
        print(f"[auto mode] Unknown kind {kind!r} — skipping.")
        _log(logs_dir, f"Supervisor stage {kind} auto-skipped (unknown kind)")
        return

    agent_name = _get_agent_name(config, "supervisor")
    cmd = [
        "opencode", "run", "--auto",
        "--agent", agent_name,
        # BD-24: pass model from config.yaml so role uses correct LLM
        # (without this, opencode uses default model which may differ).
        "--title", f"awf-supervisor-{kind}",
    ]
    # BD-24: --model only if explicitly set in config.yaml
    role_model = _get_role_model(config, "supervisor")
    if role_model:
        cmd += ["--model", role_model]
    cmd += ["--file", str(role_file)]
    for f in extra_files:
        cmd += ["--file", f]
    cmd += ["--", prompt]

    print(f"[auto mode] Spawning supervisor subprocess: agent={agent_name}")
    print(f"  role: {role_file}")
    for f in extra_files:
        print(f"  ctx:  {f}")
    print()

    _log(logs_dir, f"Supervisor {kind} subprocess started (agent={agent_name})")

    # BD-20/29: watch for the signal this supervisor kind would produce.
    # plan / replan → new TODO-*.ready in inbox (snapshot-based glob watch)
    # verify       → ACK-{todo_id}.ready in inbox (or REVIEW-{todo_id}.md
    #                in outbox if supervisor chose not to ack)
    watch_paths: list[Path] = []
    watch_new_glob: tuple[Path, str] | None = None
    if kind in ("plan", "replan"):
        watch_new_glob = (inbox, "TODO-*.ready")
    elif kind == "verify" and todo_id:
        watch_paths = [
            inbox / f"ACK-{todo_id}.ready",
            inbox / f"APPROVE-{todo_id}.ready",
            outbox / f"REVIEW-{todo_id}.md",
        ]

    result = _run_subprocess_until_signal(
        cmd,
        cwd=str(project_dir),
        watch_paths=watch_paths,
        watch_new_glob=watch_new_glob,
        logs_dir=logs_dir,
    )

    _log(logs_dir, f"Supervisor {kind} subprocess finished (exit={result.returncode})")
    if result.returncode != 0:
        raise RuntimeError(
            f"Supervisor {kind} subprocess exited with code {result.returncode}. "
            f"Cmd: {' '.join(cmd)}"
        )


def _run_agent_stage(
    stage: Stage,
    todo_id: str,
    project_dir: Path,
    config: dict,
    logs_dir: Path,
    prev_handoffs: list[Path] | None = None,
) -> None:
    """Spawn opencode run for an agent stage.

    BD-15: ``prev_handoffs`` is a list of handoff .md files from previous
    pipeline stages (in pipeline order). Each is passed to the agent as
    ``--file`` so the agent sees what previous roles did and what is
    expected of it.
    """
    role = stage.role
    # BD-29: agents always execute (kind computed from position).
    kind = stage.kind
    agent_name = _get_agent_name(config, role)

    print()
    print("=" * 41)
    print(f"  AGENT STAGE: {role} ({kind})")
    print(f"  Task: {todo_id}")
    print("=" * 41)
    print()

    role_file = _resolve_role_file(role, project_dir)
    inbox = paths.inbox(project_dir)
    todo_file = inbox / f"{todo_id}.md"

    prompt = _build_prompt(kind, todo_id)

    print(f"Running agent: {agent_name}")
    print(f"Role file: {role_file}")
    print(f"Task: {todo_file}")
    print()

    # Clean stale signals this kind could produce
    prefixes = expected_signal_prefixes(kind)
    outbox = paths.outbox(project_dir)
    clean_stage_signals(outbox, todo_id, *prefixes)

    _log(logs_dir, f"Agent stage started: {role} ({kind}) for {todo_id}")

    cmd = [
        "opencode", "run", "--auto",
        "--agent", agent_name,
        # BD-23: unique title per stage isolates session from interactive opencode
        "--title", f"awf-{role}-{todo_id}",
        "--file", str(role_file),
        "--file", str(todo_file),
    ]
    # BD-24: pass --model from config.yaml if explicitly set
    role_model = _get_role_model(config, role)
    if role_model:
        cmd += ["--model", role_model]

    # BD-15: forward previous stages' handoffs as --file args
    if prev_handoffs:
        for hf in prev_handoffs:
            if hf.is_file():
                cmd += ["--file", str(hf)]
                print(f"Handoff in:  {hf}")
                _log(logs_dir, f"Forwarding handoff: {hf}")

    cmd += ["--", prompt]

    # BD-27: role.md already contains skill content (form writes it there
    # directly). No separate .agentic/skills/ lookup needed — that was
    # BD-13 legacy removed when form started embedding skill into role.md.

    # BD-20: watch for DONE / BLOCKED signal in outbox for this todo_id.
    # If subprocess finishes the task but doesn't exit (known opencode hang),
    # awf detects the signal and terminates after grace period.
    # BD-21: watch BOTH `.ready` and `.md.ready` — LLMs sometimes append
    # .ready to the .md filename instead of replacing .md with .ready.
    outbox = paths.outbox(project_dir)
    watch_paths: list[Path] = []
    if todo_id:
        watch_paths = [
            outbox / f"DONE-{todo_id}.ready",
            outbox / f"DONE-{todo_id}.md.ready",
            outbox / f"BLOCKED-{todo_id}.ready",
            outbox / f"BLOCKED-{todo_id}.md.ready",
        ]

    result = _run_subprocess_until_signal(
        cmd, cwd=project_dir, watch_paths=watch_paths, logs_dir=logs_dir,
    )
    _log(logs_dir, f"Agent stage finished: {role} ({kind}) for {todo_id} (exit={result.returncode})")
    if result.returncode != 0:
        raise RuntimeError(
            f"Agent stage {role} ({kind}) subprocess exited with code {result.returncode}. "
            f"Cmd: {' '.join(cmd)}"
        )

    # BD-15: collect this stage's output into a handoff for the next role
    _collect_handoff(role, todo_id, project_dir, logs_dir)


def _collect_handoff(
    role: str,
    todo_id: str,
    project_dir: Path,
    logs_dir: Path,
) -> Path:
    """BD-15/19: gather PROGRESS/DONE + git diff summary into handoff .md.

    BD-19: filename is ``<role>-<todo_id>.md`` (not just ``<role>.md``)
    so retry on the same role (after BLOCKED → replan → retry) doesn't
    overwrite the previous attempt's handoff. Each retry produces a new
    handoff file keeping the audit trail.

    Always writes a file (may contain only header + instructions if no
    PROGRESS/DONE exist). Returns the path written.
    """
    handoff_dir = paths.agentic_dir(project_dir) / "handoff"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    outbox = paths.outbox(project_dir)

    progress = outbox / f"PROGRESS-{todo_id}.md"
    done = outbox / f"DONE-{todo_id}.md"

    parts: list[str] = [
        f"# Handoff from `{role}` (TODO {todo_id})",
        "",
        "**Generated:** by awf orchestrator (BD-15)",
        f"**Stage role:** {role}",
        f"**TODO:** {todo_id}",
        "",
    ]

    has_output = False
    if progress.is_file():
        body = progress.read_text(encoding="utf-8").strip()
        if body:
            parts += ["## PROGRESS notes (from worker)", "", body, ""]
            has_output = True

    if done.is_file():
        body = done.read_text(encoding="utf-8").strip()
        if body:
            parts += ["## DONE summary (from worker)", "", body, ""]
            has_output = True

    if not has_output:
        # Explicit marker so the next role knows the previous stage produced
        # NO report (crash, timeout, BLOCKED without notes) — not just empty
        # sections that look like an oversight.
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

    # Git diff summary vs baseline (if available)
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

    # Last commit message if any (indicates what was committed)
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

    # BD-19: include todo_id in filename so retries don't overwrite prior handoffs.
    out_file = handoff_dir / f"{role}-{todo_id}.md"
    out_file.write_text("\n".join(parts), encoding="utf-8")
    print(f"Handoff out: {out_file}")
    _log(logs_dir, f"Handoff written: {out_file}")
    return out_file


def _resolve_prev_handoffs(
    pipeline_stages: list[Stage],
    current_stage_idx: int,
    project_dir: Path,
    todo_id: str = "",
) -> list[Path]:
    """BD-15/19: return handoff paths for all AGENT stages before ``current_stage_idx``.

    BD-19: handoff files are named ``<role>-<todo_id>.md``. When ``todo_id``
    is provided, returns those specific files. When ``todo_id`` is empty
    (legacy/unknown), falls back to scanning handoff_dir for any file
    starting with ``<role>-`` (newest first).

    Returns paths in pipeline order (oldest first). Missing files are
    filtered out by the caller.
    """
    handoff_dir = paths.agentic_dir(project_dir) / "handoff"
    result: list[Path] = []
    for i in range(current_stage_idx):
        st = pipeline_stages[i]
        # Only forward handoffs from agent stages (supervisor stages don't
        # produce handoffs; they produce TODOs/signals).
        if st.role == "supervisor":
            continue
        if todo_id:
            # BD-19: precise file path.
            result.append(handoff_dir / f"{st.role}-{todo_id}.md")
        else:
            # Fallback: find newest file matching <role>-*.md
            candidates = sorted(
                handoff_dir.glob(f"{st.role}-*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                result.append(candidates[0])
    return result


def _maybe_commit(
    stage_name: str,
    todo_id: str,
    policy: str,
    project_dir: Path,
    logs_dir: Path,
    auto: bool = False,
) -> None:
    """Auto-commit if policy is commit_and_next or commit_and_report.

    In auto mode, blocks waiting for APPROVE-TODO-NNNN.ready signal file
    before committing. This preserves supervisor approval gate.
    """
    if policy not in ("commit_and_next", "commit_and_report"):
        return

    if not git_utils.is_git_repo(project_dir):
        print(f"Not a git repo — skipping auto-commit for '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"No git repo; auto-commit skipped at {stage_name}")
        return

    if auto:
        inbox = paths.inbox(project_dir)
        # BD-17: accept either APPROVE-{todo}.ready (from `awf approve` /
        # human) or ACK-{todo}.ready (from supervisor verify subprocess in
        # auto mode). Both authorize the commit.
        approve_signal = inbox / f"APPROVE-{todo_id}.ready"
        ack_signal = inbox / f"ACK-{todo_id}.ready"
        print("Auto-mode: waiting for supervisor approval to commit.", file=sys.stderr)
        print(f"  Approve signal: awf approve {todo_id}", file=sys.stderr)
        print("  Or ACK from supervisor verify subprocess.", file=sys.stderr)
        _log(logs_dir, f"Auto-mode: waiting for APPROVE or ACK signal for {todo_id}")

        import time
        deadline = time.time() + APPROVE_TIMEOUT_SECONDS
        while not approve_signal.exists() and not ack_signal.exists():
            if time.time() > deadline:
                print(
                    f"ERROR: APPROVE/ACK signal not received within {APPROVE_TIMEOUT_SECONDS}s. "
                    f"Pipeline aborting.",
                    file=sys.stderr,
                )
                _log(logs_dir, f"APPROVE/ACK timeout for {todo_id}")
                raise TimeoutError(f"APPROVE/ACK signal not received for {todo_id}")
            time.sleep(APPROVE_POLL_INTERVAL)
        which = "APPROVE" if approve_signal.exists() else "ACK"
        _log(logs_dir, f"{which} signal received for {todo_id}")

    if git_utils.commit_all(project_dir, f"awf({stage_name}): {todo_id}"):
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_dir, capture_output=True, text=True,
        ).stdout.strip()
        print(f"Auto-committed: {todo_id} at '{stage_name}' ({sha}).", file=sys.stderr)
        print("Remember to push: git push origin HEAD", file=sys.stderr)
        _log(logs_dir, f"Auto-committed {todo_id} at {stage_name} ({sha})")
    else:
        print(f"No changes to auto-commit at '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"Nothing to auto-commit at {stage_name}")


def _find_stage_index(stages: list[Stage], name: str) -> int:
    """Find stage index by name. Returns -1 if not found."""
    for i, s in enumerate(stages):
        if s.name == name:
            return i
    return -1


def _find_active_todo(project_dir: Path) -> str:
    """Find the newest active TODO.

    Thin wrapper around ``todos.newest_active`` kept for backward compat
    with internal callers that already have a Path.
    """
    return todos.newest_active(project_dir)


def _read_baseline_sha(project_dir: Path, todo_id: str) -> str:
    """Return SHA from ``.agentic/context/BASELINE-<todo>.sha`` or empty string.

    Single source of truth for baseline SHA reads — used by auto-DONE and
    salvage paths. Avoids duplicating the file-read + parse logic.
    """
    if not todo_id:
        return ""
    sha_file = paths.context_dir(project_dir) / f"BASELINE-{todo_id}.sha"
    if not sha_file.exists():
        return ""
    return sha_file.read_text(encoding="utf-8").strip().split("\n")[0]


def _extract_step_id_from_todo(todo_path: Path) -> int | None:
    """BD-33: parse 'Step N' from TODO-NNNN.md frontmatter/body.

    Looks for patterns like:
      **Phase:** Неделя 1 — Step 1
      Step 1:
      step_id: 1
      **Step 1**

    Returns the integer step number, or None if not found.
    """
    import re

    if not todo_path.is_file():
        return None
    try:
        text = todo_path.read_text(encoding="utf-8")
    except OSError:
        return None

    # Try YAML frontmatter first (most reliable): step_id: 1
    m = re.search(r"^step_id:\s*(\d+)\s*$", text, re.MULTILINE)
    if m:
        return int(m.group(1))

    # Then 'Step N' anywhere in the first 20 lines (header area)
    head = "\n".join(text.splitlines()[:20])
    matches = re.findall(r"\bStep\s+(\d+)\b", head)
    if matches:
        return int(matches[0])

    return None


def _mark_plan_step_done(
    project_dir: Path,
    todo_id: str,
    logs_dir: Path,
) -> bool:
    """BD-33: after verify, mark the corresponding Step in phases/plan.md as done.

    Parses phases/plan.md for a line matching the Step number extracted from
    TODO-NNNN.md, replaces '- [ ]' with '- [x]' and appends the TODO id.

    Returns True if a line was updated, False otherwise (no Step in TODO,
    no plan.md, no matching line).
    """
    import re

    config = cfg_mod.load(project_dir)
    phases_rel = cfg_mod.get(config, "phases.current", ".agentic/phases/plan.md")
    plan_path = project_dir / phases_rel if not Path(phases_rel).is_absolute() else Path(phases_rel)

    if not plan_path.is_file():
        _log(logs_dir, f"BD-33: no plan file at {plan_path} — skip step update")
        return False

    todo_path = paths.inbox(project_dir) / f"{todo_id}.md"
    step_id = _extract_step_id_from_todo(todo_path)
    if step_id is None:
        _log(logs_dir, f"BD-33: no Step N found in {todo_id}.md — skip step update")
        return False

    try:
        content = plan_path.read_text(encoding="utf-8")
    except OSError as e:
        _log(logs_dir, f"BD-33: failed to read {plan_path}: {e}")
        return False

    # Match: '- [ ] Step N:' OR '- [ ] **Step N**:' (any whitespace)
    # Use [ \t] instead of \s to avoid matching across newlines.
    pattern = re.compile(
        r"^([ \t]*-[ \t]*\[[ \t]])(?:[ \t]|\*\*)*Step[ \t]+" + str(step_id) + r"\b",
        re.MULTILINE,
    )
    match = pattern.search(content)
    if not match:
        _log(logs_dir, f"BD-33: no '- [ ] Step {step_id}' in plan.md — skip")
        return False

    # Replace '[ ]' with '[x]' and append TODO id at end of line
    line_start = match.start()
    line_end = content.find("\n", line_start)
    if line_end == -1:
        line_end = len(content)
    original_line = content[line_start:line_end]

    updated_line = original_line.replace("[ ]", "[x]", 1)
    # Append TODO marker if not already present
    if todo_id not in updated_line:
        updated_line = updated_line.rstrip() + f"  — {todo_id}"

    new_content = content[:line_start] + updated_line + content[line_end:]
    try:
        plan_path.write_text(new_content, encoding="utf-8")
        _log(logs_dir, f"BD-33: marked Step {step_id} done in plan.md (TODO {todo_id})")
        return True
    except OSError as e:
        _log(logs_dir, f"BD-33: failed to write {plan_path}: {e}")
        return False


def _print_progress_report(project_dir: Path, logs_dir: Path) -> None:
    """BD-34: print a progress summary after pipeline completes.

    Counts [ ] vs [x] in phases/plan.md, lists next 3 unfinished steps.
    """
    import re

    config = cfg_mod.load(project_dir)
    phases_rel = cfg_mod.get(config, "phases.current", ".agentic/phases/plan.md")
    plan_path = project_dir / phases_rel if not Path(phases_rel).is_absolute() else Path(phases_rel)

    print()
    print("=" * 51)
    print("  PROGRESS REPORT")
    print("=" * 51)

    if not plan_path.is_file():
        print(f"  (no plan file at {plan_path})")
        return

    try:
        content = plan_path.read_text(encoding="utf-8")
    except OSError:
        print(f"  (failed to read {plan_path})")
        return

    done = re.findall(r"^\s*-\s*\[x\]", content, re.MULTILINE)
    todo = re.findall(r"^\s*-\s*\[\s\]", content, re.MULTILINE)

    total = len(done) + len(todo)
    print(f"  Steps done:     {len(done)} / {total}")
    print(f"  Steps remaining: {len(todo)}")
    print()

    # List next 3 unfinished steps
    unfinished = []
    for line in content.splitlines():
        if re.match(r"^\s*-\s*\[\s\]", line):
            # Strip checkbox and leading whitespace
            clean = re.sub(r"^\s*-\s*\[\s\]\s*", "", line)
            clean = clean.strip()
            if len(clean) > 100:
                clean = clean[:97] + "..."
            unfinished.append(clean)

    if unfinished:
        print("  Next steps:")
        for i, step in enumerate(unfinished[:3], 1):
            print(f"    {i}. {step}")
        if len(unfinished) > 3:
            print(f"    ... and {len(unfinished) - 3} more")
    else:
        print("  All steps complete! 🎉")

    print()
    _log(logs_dir, f"BD-34: progress report: {len(done)}/{total} steps done")


def run_pipeline(args: Any) -> int:
    """Execute the pipeline and return exit code."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    agentic = paths.agentic_dir(project_dir)

    if not agentic.is_dir():
        print("No .agentic/ found. Run 'awf init' first.")
        return 1

    config_file = paths.config_file(project_dir)
    if not config_file.exists():
        print("No .agentic/config.yaml found. Run 'awf init' first.")
        return 1

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    context_dir = paths.context_dir(project_dir)
    logs_dir = paths.agentic_dir(project_dir) / "logs"
    for d in (inbox, outbox, context_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    config = cfg_mod.load(project_dir)

    pipeline_name = getattr(args, "pipeline", None)
    from_stage = getattr(args, "from_stage", None)
    auto = getattr(args, "auto", False)
    # NOTE: --timeout (default 3600s) is accepted for backward compat but is
    # intended for the agent subprocess execution limit, which is not yet
    # implemented (subprocess.run is blocking). The 30s fallback poll below
    # is hard-coded on purpose: agent subprocess is blocking so signal
    # should already exist when run() returns — 30s covers a small race only.

    try:
        pipeline_file = resolve_pipeline_file(project_dir, pipeline_name, config)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    stages = load_stages(pipeline_file)
    if not stages:
        print(f"ERROR: No stages found in {pipeline_file}")
        return 1

    total = len(stages)
    print("=== Agentic Workflow: Starting Pipeline ===")
    print(f"Pipeline: {pipeline_file}")
    print(f"Stages: {' '.join(s.name for s in stages)}")
    print()
    _log(logs_dir, f"Pipeline started with {total} stages: {' '.join(s.name for s in stages)}")

    # Retry counts — one per stage
    retry_counts = [0] * total

    # Determine starting stage
    stage_idx = 0
    if from_stage:
        stage_idx = _find_stage_index(stages, from_stage)
        if stage_idx == -1:
            print(f"ERROR: Stage '{from_stage}' not found in pipeline")
            return 1
        _log(logs_dir, f"Starting from stage: {from_stage} (index {stage_idx})")

    current_todo = ""

    while 0 <= stage_idx < total:
        stage = stages[stage_idx]
        s_name = stage.name
        s_role = stage.role
        s_kind = stage.kind  # BD-29: kind computed from position
        s_desc = stage.description

        print()
        print("-" * 43)
        print(f"  Stage {stage_idx + 1}/{total}: {s_name} ({s_role} :: {s_kind})")
        if s_desc:
            print(f"  {s_desc}")
        print("-" * 43)
        _log(logs_dir, f"Stage {stage_idx}: {s_name} ({s_role} :: {s_kind})")

        # --- Supervisor stage ---
        if s_role == "supervisor":
            try:
                _run_supervisor_stage(stage, current_todo, auto, project_dir, logs_dir)
            except RuntimeError as e:
                print(
                    f"ERROR: supervisor stage '{s_name}' crashed. Pipeline stopped.",
                    file=sys.stderr,
                )
                print(f"  Details: {e}", file=sys.stderr)
                print(f"  See {logs_dir / 'orchestrator.log'} for full context.", file=sys.stderr)
                _log(logs_dir, f"Pipeline stopped at stage {s_name}: {e}")
                return 1

            # BD-29: kind-based flow (was action-based).
            if s_kind == "plan":
                current_todo = _find_active_todo(project_dir)
                if not current_todo:
                    print("No active TODO found. Create one first, then continue.")
                    _log(logs_dir, "No active TODO after supervisor stage")
                    return 1
                print(f"Active TODO: {current_todo}")

            if s_kind == "verify":
                print("Supervisor verification complete.")
                _maybe_commit(s_name, current_todo, stage.on_approved, project_dir, logs_dir, auto=auto)
                # BD-33: mark the Step in plan.md as done after successful verify
                _mark_plan_step_done(project_dir, current_todo, logs_dir)
                stage_idx += 1
                continue

            stage_idx += 1
            continue

        # --- Agent stage ---
        if not current_todo:
            current_todo = _find_active_todo(project_dir)
            if not current_todo:
                print(f"No active TODO for agent stage '{s_name}'. Run supervisor stage first.")
                return 1

        # BD-15/19: forward handoffs from previous agent stages (named with todo_id)
        prev_handoffs = _resolve_prev_handoffs(stages, stage_idx, project_dir, todo_id=current_todo)
        try:
            _run_agent_stage(stage, current_todo, project_dir, config, logs_dir, prev_handoffs=prev_handoffs)
        except RuntimeError as e:
            print(
                f"ERROR: agent stage '{s_name}' (role={s_role}) crashed. Pipeline stopped.",
                file=sys.stderr,
            )
            print(f"  Details: {e}", file=sys.stderr)
            print(f"  See {logs_dir / 'orchestrator.log'} for full context.", file=sys.stderr)
            _log(logs_dir, f"Pipeline stopped at stage {s_name}: {e}")
            return 1

        prefixes = expected_signal_prefixes(s_kind)

        # Read signal (opencode run is blocking; signal should already exist)
        signal = read_signal_for_todo(outbox, current_todo, *prefixes)

        if not signal:
            # Brief fallback poll (30s) — agent subprocess is blocking, signal
            # should already exist; this covers a small race.
            try:
                signal = wait_for_signal(outbox, current_todo, *prefixes, timeout=30)
            except TimeoutError:
                pass

        if not signal:
            # No signal: try auto-DONE
            baseline_sha = _read_baseline_sha(project_dir, current_todo)
            if baseline_sha and verify.attempt_auto_done(project_dir, current_todo, config, baseline_sha):
                signal = read_signal_for_todo(outbox, current_todo, *prefixes)

        if not signal:
            # Still no signal — escalate
            print(
                f"WARNING: No signal after agent stage '{s_name}' "
                f"(worker ran but didn't signal).",
                file=sys.stderr,
            )
            _log(logs_dir, f"No signal after {s_name} — salvage path")

            if auto:
                baseline_sha = _read_baseline_sha(project_dir, current_todo)

                if baseline_sha and verify.detect_work_evidence(project_dir, baseline_sha):
                    print(
                        f"  Worker left changes vs baseline. TODO {current_todo} left ACTIVE "
                        f"for manual salvage.",
                        file=sys.stderr,
                    )
                    print(
                        f"  Inspect: git diff ; awf status ; then write "
                        f"DONE-{current_todo}.ready or replan.",
                        file=sys.stderr,
                    )
                    _log(logs_dir, f"Auto: work detected; {current_todo} left active for manual salvage")
                else:
                    print("  No worker changes and no signal — treating as failure.", file=sys.stderr)
                    _log(logs_dir, f"Auto: no work + no signal — stop at {s_name}")
                return 1

            # Interactive salvage — treat as verify semantics (BD-30):
            # current opencode reviews the situation and decides ACK/REVIEW.
            salvage_stage = Stage(
                name="salvage",
                role="supervisor",
                kind="verify",  # BD-30: salvage waits for ACK/REVIEW like verify
            )
            _run_supervisor_stage(salvage_stage, current_todo, auto=False, project_dir=project_dir, logs_dir=logs_dir)
            signal = read_signal_for_todo(outbox, current_todo, *prefixes)
            if not signal:
                print("No signal after supervisor salvage. Stopping.", file=sys.stderr)
                _log(logs_dir, f"Stopped: salvage produced no signal at {s_name}")
                return 1
            print(f"Salvaged signal: {signal}", file=sys.stderr)
            _log(logs_dir, f"Salvaged signal: {signal} via supervisor")

        sig_type = signal_type(signal)
        print(f"Signal classified as: {sig_type}")

        action, target = resolve_transition(stage, sig_type)
        _log(logs_dir, f"Transition: stage={stage_idx} signal={sig_type} -> action={action} target={target}")

        if action in ("next", "commit_and_next", "commit_and_report"):
            _maybe_commit(s_name, current_todo, action, project_dir, logs_dir, auto=auto)
            print("Moving to next stage.")
            retry_counts[stage_idx] = 0
            stage_idx += 1

        elif action == "escalate":
            max_r = stage.max_retries
            if retry_counts[stage_idx] < max_r:
                retry_counts[stage_idx] += 1
                print(f"BLOCKED — escalating to supervisor (attempt {retry_counts[stage_idx]}/{max_r})")
                _log(logs_dir, "Escalating to supervisor for retry")

                # BD-29 fix: pass kind="replan" so supervisor gets the correct
                # prompt and signal watch paths (was passing agent stage with
                # kind="execute", causing supervisor to skip in auto mode).
                replan_stage = Stage(
                    name="replan",
                    role="supervisor",
                    kind="replan",
                )
                _run_supervisor_stage(replan_stage, current_todo, auto, project_dir, logs_dir)

                new_todo = _find_active_todo(project_dir)
                if new_todo:
                    current_todo = new_todo
                    print(f"New TODO: {current_todo} — retrying stage '{s_name}'")
                else:
                    print("Supervisor did not create a new TODO. Stopping.")
                    return 1
            else:
                print(f"BLOCKED — max retries reached ({max_r}). Pipeline stopped.")
                _log(logs_dir, f"Max retries reached for stage {s_name}")
                return 1

        elif action == "rollback":
            target_idx = _find_stage_index(stages, target)
            if target_idx >= 0:
                print(f"Rolling back to stage: {stages[target_idx].name}")
                _log(logs_dir, f"Rollback to stage {stages[target_idx].name} (index {target_idx})")
                stage_idx = target_idx

                # BD-29 fix: same as escalation — use kind="replan" for supervisor.
                replan_stage = Stage(
                    name="replan",
                    role="supervisor",
                    kind="replan",
                )
                _run_supervisor_stage(replan_stage, current_todo, auto, project_dir, logs_dir)
                new_todo = _find_active_todo(project_dir)
                if new_todo:
                    current_todo = new_todo
            else:
                print(f"ERROR: Rollback target '{target}' not found in pipeline")
                return 1

        elif action == "stop":
            print("Pipeline stopped by policy.")
            _log(logs_dir, f"Pipeline stopped by policy at stage {s_name}")
            return 0

        else:
            print(f"Unknown transition: {action}")
            return 1

    # All stages completed
    print()
    print("=" * 41)
    print("  Pipeline complete!")
    print("=" * 41)
    print()
    # BD-34: auto-report after verify (was: user had to run 'awf report' manually)
    _print_progress_report(project_dir, logs_dir)
    print("Run 'awf start' for the next iteration.")
    _log(logs_dir, "Pipeline complete")
    return 0
