"""Supervisor stage handling — plan / verify / replan / salvage.

Extracted from orchestrator.py (A6 refactor). Two execution paths:

- Interactive (BD-30, auto=False): current opencode in user's chat IS the
  supervisor. awf prints explicit instructions and waits for a signal file.
- Auto (BD-14, auto=True): spawn ``opencode run`` subprocess that loads
  supervisor.md and does the work autonomously.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import config as cfg_mod
from . import paths
from ._log import log as _log
from .pipeline import Stage
from .xdg import awf_roles_dir

log = logging.getLogger(__name__)


def get_agent_name(config: dict, role: str) -> str:
    """Get the agent_name override for a role, defaulting to the role itself."""
    return cfg_mod.get(config, f"models.{role}.agent_name", role) or role


def get_role_model(config: dict, role: str) -> str | None:
    """BD-24: get the model override for a role from config.yaml.

    Returns the model string (e.g. "vllm/llm") if set, else None.
    """
    val = cfg_mod.get(config, f"models.{role}.model", "")
    return val if val else None


def build_prompt(kind: str, todo_id: str) -> str:
    """BD-29: build prompt for a stage kind.

    Three kinds: plan / execute / verify.
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


def global_roles_dir() -> Path:
    """Return the global roles directory: $XDG_CONFIG_HOME/awf/roles/."""
    return awf_roles_dir()


def resolve_role_file(role: str, project_dir: Path) -> Path:
    """Resolve role file with project-local → global fallback."""
    project_role = paths.agentic_dir(project_dir) / "roles" / f"{role}.md"
    global_role = global_roles_dir() / f"{role}.md"

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


def wait_for_supervisor_signal(
    kind: str,
    todo_id: str,
    project_dir: Path,
    logs_dir: Path,
    poll_interval: int = 3,
    timeout: int = 3600,
) -> str:
    """BD-30: poll for the signal file the interactive supervisor produces.

    kind="plan"   → waits for new TODO-*.ready in inbox (snapshot-based)
    kind="verify" → waits for ACK-{todo_id}.ready or APPROVE-{todo_id}.ready
                    in inbox, OR REVIEW-{todo_id}.md in outbox

    C1 v2 fix: timeout (default 3600s = 1 hour). Without this, in --background
    mode a forgotten interactive supervisor would hang forever (zombie process).
    Raises TimeoutError on expiry.
    """
    import time

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    existing_todo_signals: set[str] = set()
    if kind in ("plan", "replan") and inbox.is_dir():
        existing_todo_signals = {p.name for p in inbox.glob("TODO-*.ready")}

    deadline_log_interval = 60
    start = time.monotonic()
    deadline = start + timeout
    last_log = start

    while True:
        if kind in ("plan", "replan"):
            if inbox.is_dir():
                current = {p.name for p in inbox.glob("TODO-*.ready")}
                new_ones = current - existing_todo_signals
                if new_ones:
                    sig = sorted(new_ones)[0].replace(".ready", "")
                    _log(logs_dir, f"BD-30: interactive supervisor signal detected: {sig}")
                    return sig

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
            waited = int(now - start)
            _log(
                logs_dir,
                f"BD-30: interactive supervisor still waiting for {kind} signal "
                f"({waited}s elapsed, deadline in {int(deadline - now)}s)",
            )
            last_log = now

        if now >= deadline:
            waited = int(now - start)
            _log(logs_dir, f"BD-30: supervisor signal timeout after {waited}s (kind={kind})")
            raise TimeoutError(
                f"Supervisor {kind} signal not received within {timeout}s. "
                f"In --background mode this prevents zombie processes. "
                f"To extend: set AWF_SUPERVISOR_TIMEOUT env var or kill the awf process."
            )

        time.sleep(poll_interval)


def print_interactive_supervisor_instructions(
    kind: str,
    todo_id: str,
    project_dir: Path,
    phases_file: str,
) -> None:
    """BD-30: print explicit instructions for current opencode (supervisor)."""
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    handoff_dir = paths.agentic_dir(project_dir) / "handoff"
    phases_path = (
        project_dir / phases_file if not Path(phases_file).is_absolute() else Path(phases_file)
    )

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


def run_supervisor_stage(
    stage: Stage,
    todo_id: str,
    auto: bool,
    project_dir: Path,
    logs_dir: Path,
) -> str:
    """Dispatch a supervisor stage.

    Interactive (auto=False): print instructions + wait for signal file (BD-30).
    Auto (auto=True): spawn opencode subprocess (BD-14).

    Returns the signal name produced by supervisor:
    - For plan/replan: "TODO-NNNN" (the new TODO id)
    - For verify: "ACK-TODO-NNNN" or "APPROVE-TODO-NNNN" (approved)
                    or "REVIEW-TODO-NNNN" (rejected — C1: caller must check)
    - Empty string if signal detection failed or stage skipped.
    """
    kind = stage.kind
    config = cfg_mod.load(project_dir)
    phases_file = cfg_mod.get(config, "phases.current", ".agentic/phases/plan.md")

    print()
    print("=" * 41)
    print(f"  SUPERVISOR STAGE: {kind}")
    print("=" * 41)
    print()

    if not auto:
        print_interactive_supervisor_instructions(kind, todo_id, project_dir, phases_file)
        _log(logs_dir, f"BD-30: interactive supervisor {kind} — waiting for signal")
        # C1 v2: configurable timeout via env (default 3600s = 1 hour)
        import os
        timeout = int(os.environ.get("AWF_SUPERVISOR_TIMEOUT", "3600"))
        signal = wait_for_supervisor_signal(kind, todo_id, project_dir, logs_dir, timeout=timeout)
        _log(logs_dir, f"BD-30: interactive supervisor {kind} completed by user (opencode): {signal}")
        return signal

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
    return run_supervisor_via_subprocess(kind, todo_id, project_dir, config, phases_file, logs_dir)


def run_supervisor_via_subprocess(
    kind: str,
    todo_id: str,
    project_dir: Path,
    config: dict,
    phases_file: str,
    logs_dir: Path,
) -> str:
    """BD-14/29: spawn ``opencode run --auto --agent ... --file supervisor.md``.

    kind is one of: plan / verify / replan / salvage.
    salvage is NOT automatable — needs human judgement.

    Returns the signal name produced by supervisor subprocess (C1 fix):
    - plan/replan: new TODO-NNNN
    - verify: ACK-TODO-NNNN, APPROVE-TODO-NNNN, or REVIEW-TODO-NNNN
    - empty string if skipped.
    """
    try:
        role_file = resolve_role_file("supervisor", project_dir)
    except RuntimeError as e:
        print(f"[auto mode] supervisor.md not found — skipping. ({e})")
        _log(logs_dir, f"Supervisor stage {kind} auto-skipped (no supervisor.md)")
        return ""

    extra_files: list[str] = []
    prompt = ""

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    phases_path = (
        project_dir / phases_file if not Path(phases_file).is_absolute() else Path(phases_file)
    )

    if kind == "plan":
        if phases_path.is_file():
            extra_files.append(str(phases_path))
        prompt = build_prompt("plan", todo_id)
    elif kind == "verify":
        if not todo_id:
            print("[auto mode] No todo_id for verify — skip.")
            _log(logs_dir, "Supervisor verify auto-skipped (no todo_id)")
            return ""
        done_md = outbox / f"DONE-{todo_id}.md"
        if done_md.is_file():
            extra_files.append(str(done_md))
        progress = outbox / f"PROGRESS-{todo_id}.md"
        if progress.is_file():
            extra_files.append(str(progress))
        # BD-29 aggregate verify: forward ALL role handoffs.
        handoff_dir = paths.agentic_dir(project_dir) / "handoff"
        if handoff_dir.is_dir():
            for hf in sorted(handoff_dir.glob(f"*-{todo_id}.md")):
                if hf.is_file():
                    extra_files.append(str(hf))
        prompt = build_prompt("verify", todo_id)
    elif kind == "replan":
        if not todo_id:
            print("[auto mode] No todo_id for replan — skip.")
            _log(logs_dir, "Supervisor replan auto-skipped (no todo_id)")
            return ""
        blocked = outbox / f"BLOCKED-{todo_id}.md"
        if blocked.is_file():
            extra_files.append(str(blocked))
        prompt = (
            f"Worker reported BLOCKED on {todo_id}. Read the BLOCKED note, analyze the problem, "
            "create a refined TODO at .agentic/inbox/TODO-{NNNN}.md (next sequential ID), "
            "baseline it, and create the .ready signal."
        )
    elif kind == "salvage":
        print("[auto mode] salvage not automated — skipping.")
        _log(logs_dir, "Supervisor salvage auto-skipped (not automatable)")
        return ""
    else:
        print(f"[auto mode] Unknown kind {kind!r} — skipping.")
        _log(logs_dir, f"Supervisor stage {kind} auto-skipped (unknown kind)")
        return ""

    agent_name = get_agent_name(config, "supervisor")
    cmd = [
        "opencode", "run", "--auto",
        "--agent", agent_name,
        "--title", f"awf-supervisor-{kind}",
    ]
    role_model = get_role_model(config, "supervisor")
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

    from .orchestrator import _awf_subprocess_env
    from .signal_watch import run_subprocess_until_signal

    result = run_subprocess_until_signal(
        cmd,
        cwd=str(project_dir),
        watch_paths=watch_paths,
        watch_new_glob=watch_new_glob,
        logs_dir=logs_dir,
        env=_awf_subprocess_env(),
    )

    _log(logs_dir, f"Supervisor {kind} subprocess finished (exit={result.returncode})")
    if result.returncode != 0:
        raise RuntimeError(
            f"Supervisor {kind} subprocess exited with code {result.returncode}. "
            f"Cmd: {' '.join(cmd)}"
        )

    # C1 fix: determine which signal actually fired.
    signal_name = _detect_supervisor_signal(kind, todo_id, inbox, outbox)
    _log(logs_dir, f"Supervisor {kind} produced signal: {signal_name!r}")
    return signal_name


def _detect_supervisor_signal(
    kind: str,
    todo_id: str,
    inbox: Path,
    outbox: Path,
) -> str:
    """C1 fix: detect which signal the supervisor actually produced.

    For verify: prefers REVIEW (rejection) over ACK/APPROVE — if supervisor
    wrote REVIEW-{todo_id}.md, that's the most recent decision and should
    override any stale ACK.
    """
    if kind == "verify" and todo_id:
        # Check REVIEW first (most recent decision wins)
        review = outbox / f"REVIEW-{todo_id}.md"
        if review.exists():
            return f"REVIEW-{todo_id}"
        # Then ACK and APPROVE
        ack = inbox / f"ACK-{todo_id}.ready"
        if ack.exists():
            return f"ACK-{todo_id}"
        approve = inbox / f"APPROVE-{todo_id}.ready"
        if approve.exists():
            return f"APPROVE-{todo_id}"
    elif kind in ("plan", "replan"):
        # Newest TODO-*.ready that didn't exist at start (already filtered
        # by signal_watch snapshot). Return first found by mtime.
        todos = sorted(inbox.glob("TODO-*.ready"), key=lambda p: p.stat().st_mtime, reverse=True)
        if todos:
            return todos[0].stem  # "TODO-NNNN.ready" → "TODO-NNNN"
    return ""
