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

    - any path in ``watch_paths`` exists (concrete filenames — used when
      we know the signal name ahead of time, e.g. ``ACK-TODO-0001.ready``)
    - any NEW file matching ``watch_new_glob`` appears in the snapshot
      taken at start (used when the filename is picked by the subprocess
      itself, e.g. ``TODO-*.ready`` for supervisor create_todo)

    Args:
        cmd: command list.
        cwd: working directory.
        watch_paths: concrete paths whose existence means "work is done".
        watch_new_glob: (directory, glob_pattern) — detect NEW files matching
            the pattern that didn't exist at subprocess start.
        logs_dir: optional, for logging.
        hard_timeout: absolute cap.
        grace_seconds: grace period after signal detected.
    """
    import time

    watch_paths = watch_paths or []
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
            triggered = any(p.exists() for p in watch_paths)
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


def _build_prompt(action: str, todo_id: str) -> str:
    """Build the prompt string for an agent stage, matching bash run_agent_stage."""
    prompts = {
        "execute_todo": (
            f"Execute all Tasks in {todo_id} via edit tool. Run Verify after each task. "
            f"Run regression verify before any commit. "
            f"Write .agentic/outbox/DONE-{todo_id}.md or BLOCKED-{todo_id}.md and create "
            f"matching .ready signal. Do not commit unless the TODO explicitly includes a "
            f"final Git commit step."
        ),
        "review_code": (
            f"Review the code changes for {todo_id}. Compare TODO with actual git diff. "
            f"Check quality and correctness. Write .agentic/outbox/REVIEW-APPROVED-{todo_id}.md "
            f"or REVIEW-REJECTED-{todo_id}.md with .ready signal."
        ),
        "run_tests": (
            f"Run the full test suite and verification commands. Compare results with baseline. "
            f"Write .agentic/outbox/TEST-PASSED-{todo_id}.md or TEST-FAILED-{todo_id}.md "
            f"with .ready signal."
        ),
        "audit_code": (
            f"Audit the code changes for {todo_id} for security issues. "
            f"Write .agentic/outbox/REVIEW-APPROVED-{todo_id}.md or "
            f"REVIEW-REJECTED-{todo_id}.md with .ready signal."
        ),
    }
    return prompts.get(
        action,
        f"Execute action '{action}' for {todo_id} following role instructions. "
        f"Write result to .agentic/outbox/ with .ready signal.",
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


def _run_supervisor_stage(
    stage: Stage,
    todo_id: str,
    auto: bool,
    project_dir: Path,
    logs_dir: Path,
) -> None:
    """Handle a supervisor stage.

    In interactive mode (auto=False): print instructions, wait for Enter.
    In auto mode (BD-14): spawn ``opencode run`` subprocess that loads
    supervisor.md as instruction and does the work — writes TODO/.ready
    for plan, ACK for verify, etc.
    """
    action = stage.action
    config = cfg_mod.load(project_dir)
    phases_file = cfg_mod.get(config, "phases.current", ".agentic/phases/plan.md")

    print()
    print("=" * 41)
    print(f"  SUPERVISOR STAGE: {action}")
    print("=" * 41)
    print()
    print("Instructions: .agentic/roles/supervisor.md")
    print(f"Phases file: {phases_file}")
    print()

    if action == "create_todo":
        print("What to do:")
        print("  1. Study the project state and phases file")
        print("  2. Determine the next step")
        print("  3. Create baseline: awf baseline TODO-{NNNN}")
        print("  4. Write task to .agentic/inbox/TODO-{NNNN}.md")
        print("  5. Create signal: .agentic/inbox/TODO-{NNNN}.ready")
    elif action in ("verify_result", "final_verify"):
        print("What to do:")
        print("  1. Read report from .agentic/outbox/")
        print("  2. Run verification commands independently")
        print("  3. Check git diff — changes must be in source files")
        print("  4. Decide: continue / fix / rollback")
        print("  5. If approved: create .agentic/inbox/ACK-{NNNN}.ready")
    elif action == "replan":
        print("Worker returned BLOCKED. Resolve the issue:")
        print("  1. Read .agentic/outbox/BLOCKED-*.md")
        print("  2. Analyze the problem")
        print("  3. Create a new TODO with refined instructions")
        print("  4. Create .agentic/inbox/TODO-{NNNN}.ready")
    elif action == "salvage":
        context_dir = paths.context_dir(project_dir)
        base_sha = ""
        if todo_id:
            sha_file = context_dir / f"BASELINE-{todo_id}.sha"
            if sha_file.exists():
                base_sha = sha_file.read_text(encoding="utf-8").strip().split("\n")[0]
        outbox = paths.outbox(project_dir)
        print("Worker finished but wrote NO signal (timeout/crash). Work may be complete.")
        print()
        print("Investigate the orphaned work:")
        print(f"  1. See what changed:  git diff {base_sha} --stat" if base_sha else "  1. See what changed:  git diff --stat")
        print(f"  2. Worker progress:   cat {outbox}/PROGRESS-{todo_id}.md")
        print("  3. Verify changes independently (build, tests, review).")
        print()
        print("Decide and act:")
        print(f"  - good  -> salvage: write {outbox}/DONE-{todo_id}.md + {outbox}/DONE-{todo_id}.ready")
        print("  - bad   -> rollback to baseline, then create a new TODO (replan)")
        print("  - stuck -> leave as-is and stop")
        print()
        print("After writing a signal (or a new TODO), press Enter to continue.")

    print()
    if not auto:
        print("When done, press Enter to continue...")
        input()
        _log(logs_dir, f"Supervisor stage {action} completed by user")
        return

    # BD-14: auto mode — spawn opencode subprocess to do supervisor work.
    _run_supervisor_via_subprocess(action, todo_id, project_dir, config, phases_file, logs_dir)


def _run_supervisor_via_subprocess(
    action: str,
    todo_id: str,
    project_dir: Path,
    config: dict,
    phases_file: str,
    logs_dir: Path,
) -> None:
    """BD-14: spawn ``opencode run --auto --agent worker --file supervisor.md``
    to do supervisor work (create TODO, verify, replan) without human.

    Falls back to old "auto-skip" behavior if supervisor.md is missing or
    if subprocess fails (logged).
    """
    try:
        role_file = _resolve_role_file("supervisor", project_dir)
    except RuntimeError as e:
        print(f"[auto mode] supervisor.md not found — skipping. ({e})")
        _log(logs_dir, f"Supervisor stage {action} auto-skipped (no supervisor.md)")
        return

    extra_files: list[str] = []
    prompt = ""

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    phases_path = project_dir / phases_file if not Path(phases_file).is_absolute() else Path(phases_file)

    if action == "create_todo":
        # Skip if there is already an active TODO (supervisor would no-op).
        active = todos.list_active_todos(inbox, outbox)
        if active:
            print(f"[auto mode] Active TODO already exists: {active[0]} — skip create.")
            _log(logs_dir, f"Supervisor create_todo auto-skipped (active TODO {active[0]})")
            return
        if phases_path.is_file():
            extra_files.append(str(phases_path))
        prompt = (
            "You are the supervisor. Read the phases/plan file. Determine the next step "
            "that is not yet completed. Create a TODO file at "
            ".agentic/inbox/TODO-{NNNN}.md (use next sequential ID), create baseline "
            "via `awf baseline TODO-{NNNN}`, then create the .ready signal at "
            ".agentic/inbox/TODO-{NNNN}.ready. Do NOT implement the TODO yourself — "
            "later roles do that. Keep the TODO at the goal level (what success looks like), "
            "do NOT micromanage individual roles — each role's skill.md already defines its zone."
        )
    elif action in ("verify_result", "final_verify"):
        if not todo_id:
            print("[auto mode] No todo_id for verify — skip.")
            _log(logs_dir, f"Supervisor {action} auto-skipped (no todo_id)")
            return
        done_md = outbox / f"DONE-{todo_id}.md"
        if done_md.is_file():
            extra_files.append(str(done_md))
        progress = outbox / f"PROGRESS-{todo_id}.md"
        if progress.is_file():
            extra_files.append(str(progress))
        prompt = (
            f"You are the supervisor. Verify TODO {todo_id}: read the DONE report and PROGRESS notes, "
            "check `git diff --stat` against the baseline SHA in "
            f".agentic/context/BASELINE-{todo_id}.sha. Decide: is the work complete and correct? "
            "If yes, write ACK signal at "
            f".agentic/inbox/ACK-{todo_id}.ready. If no, do NOT ack — leave a note in "
            f".agentic/outbox/REVIEW-{todo_id}.md explaining what's wrong."
        )
    elif action == "replan":
        if not todo_id:
            print("[auto mode] No todo_id for replan — skip.")
            _log(logs_dir, f"Supervisor {action} auto-skipped (no todo_id)")
            return
        blocked = outbox / f"BLOCKED-{todo_id}.md"
        if blocked.is_file():
            extra_files.append(str(blocked))
        prompt = (
            f"Worker reported BLOCKED on {todo_id}. Read the BLOCKED note, analyze the problem, "
            "create a refined TODO at .agentic/inbox/TODO-{NNNN}.md (next sequential ID), "
            "baseline it, and create the .ready signal."
        )
    else:
        # salvage or unknown — skip in auto mode (too risky to automate).
        print(f"[auto mode] Action {action!r} not automated — skipping.")
        _log(logs_dir, f"Supervisor stage {action} auto-skipped (not automatable)")
        return

    agent_name = _get_agent_name(config, "supervisor")
    cmd = [
        "opencode", "run", "--auto",
        "--agent", agent_name,
        # BD-24: pass model from config.yaml so role uses correct LLM
        # (without this, opencode uses default model which may differ).
        "--title", f"awf-supervisor-{action}",
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

    _log(logs_dir, f"Supervisor {action} subprocess started (agent={agent_name})")

    # BD-20: watch for the signal this supervisor action would produce.
    # create_todo / replan → new TODO-*.ready in inbox (we don't know the
    #   ID ahead of time — use snapshot-based glob watch)
    # verify_result / final_verify → ACK-{todo_id}.ready in inbox (or
    #   REVIEW-{todo_id}.md in outbox if supervisor chose not to ack)
    watch_paths: list[Path] = []
    watch_new_glob: tuple[Path, str] | None = None
    if action in ("create_todo", "replan"):
        watch_new_glob = (inbox, "TODO-*.ready")
    elif action in ("verify_result", "final_verify") and todo_id:
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

    _log(logs_dir, f"Supervisor {action} subprocess finished (exit={result.returncode})")
    if result.returncode != 0:
        raise RuntimeError(
            f"Supervisor {action} subprocess exited with code {result.returncode}. "
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
    action = stage.action
    agent_name = _get_agent_name(config, role)

    print()
    print("=" * 41)
    print(f"  AGENT STAGE: {role} ({action})")
    print(f"  Task: {todo_id}")
    print("=" * 41)
    print()

    role_file = _resolve_role_file(role, project_dir)
    inbox = paths.inbox(project_dir)
    todo_file = inbox / f"{todo_id}.md"

    prompt = _build_prompt(action, todo_id)

    print(f"Running agent: {agent_name}")
    print(f"Role file: {role_file}")
    print(f"Task: {todo_file}")
    print()

    # Clean stale signals this action could produce
    prefixes = expected_signal_prefixes(action)
    outbox = paths.outbox(project_dir)
    clean_stage_signals(outbox, todo_id, *prefixes)

    _log(logs_dir, f"Agent stage started: {role} ({action}) for {todo_id}")

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

    skills_dir = paths.agentic_dir(project_dir) / "skills"
    local_skill = skills_dir / f"{role}.md"
    if local_skill.exists():
        cmd.insert(cmd.index("--"), "--file")
        cmd.insert(cmd.index("--"), str(local_skill))
        print(f"Local skill: {local_skill}")
        _log(logs_dir, f"Using local skill: {local_skill}")
    else:
        _log(logs_dir, f"No local skill for {role} (using role .md only)")

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
    _log(logs_dir, f"Agent stage finished: {role} ({action}) for {todo_id} (exit={result.returncode})")
    if result.returncode != 0:
        raise RuntimeError(
            f"Agent stage {role} ({action}) subprocess exited with code {result.returncode}. "
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


def _check_needs_normalize(project_dir: Path) -> tuple[bool, list[dict], Path | None]:
    """Check .agentic/state/needs_normalize.yaml.

    Returns (needed, team_list, state_file_path). Does NOT delete the file.
    """
    import yaml

    state_file = paths.agentic_dir(project_dir) / "state" / "needs_normalize.yaml"
    if not state_file.exists():
        return False, [], None
    try:
        data = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return False, [], None
    if not isinstance(data, dict) or not data.get("needed"):
        return False, [], None
    team = data.get("team", [])
    return True, team if isinstance(team, list) else [], state_file


def _consume_needs_normalize(state_file: Path | None) -> None:
    """Delete the needs_normalize state file after successful normalize."""
    if state_file and state_file.exists():
        state_file.unlink()


def _run_normalize_stage(
    team: list[dict],
    project_dir: Path,
    logs_dir: Path,
    *,
    background: bool = False,
) -> None:
    """Run normalize_skills stage — supervisor (current session) does the work.

    BD-13: in background mode this stage cannot run interactively (no human
    at the wheel). Instead of SystemExit, log a warning, preserve the
    needs_normalize state file, and let the pipeline continue. The supervisor
    can run `awf normalize` interactively later.
    """
    effective_team = team
    if not effective_team:
        roles_dir = paths.agentic_dir(project_dir) / "roles"
        if roles_dir.is_dir():
            for rf in sorted(roles_dir.glob("*.md")):
                effective_team.append({"role": rf.stem, "type": "local"})
        else:
            skills_dir = paths.agentic_dir(project_dir) / "skills"
            if skills_dir.is_dir():
                for sf in sorted(skills_dir.glob("*.md")):
                    effective_team.append({"role": sf.stem, "type": "skill-derived"})

    print()
    print("=" * 51)
    print("  NORMALIZE_SKILLS STAGE")
    print("=" * 51)
    print()
    print("Pipeline detected team selection from form submit.")
    print("Team roles (in pipeline order):")
    for i, member in enumerate(effective_team, 1):
        role = member.get("role", "")
        role_type = member.get("type", "")
        print(f"  {i}. {role} ({role_type})")
    print()

    # BD-16: print per-role pipeline contracts so supervisor knows what to write.
    try:
        from .skills_contract import render_pipeline_contract

        n = len(effective_team)
        if n > 0:
            print("Per-role pipeline contracts (BD-16):")
            print("-" * 51)
            for i, member in enumerate(effective_team, 1):
                role = str(member.get("role", ""))
                prev_role = effective_team[i - 2].get("role", "") if i >= 2 else None
                next_role = effective_team[i].get("role", "") if i < n else None
                print(f"\n[{i}/{n}] {role}:")
                print(render_pipeline_contract(role, i, n, prev_role, next_role))
            print("-" * 51)
            print()
    except ImportError:
        pass

    if background or not sys.stdin.isatty():
        # BD-13: don't exit, just defer. Also defer when stdin is not a tty
        # (e.g. detached background process re-launched without --background
        # flag — cmd_start strips --background before re-exec, so the child
        # sees background=False but has stdin=DEVNULL).
        print("WARNING: normalize_skills deferred (no interactive stdin).")
        print("Supervisor should run `awf normalize` interactively to update")
        print("local skills. Pipeline continues with whatever role .md files")
        print("currently exist in .agentic/roles/.")
        print()
        _log(
            logs_dir,
            "normalize_skills deferred (no interactive stdin, state preserved)",
        )
        return

    print("Your job as supervisor:")
    print("  1. Read global skills for each role:")
    print("     ~/.config/opencode/skills/<role-slug>/SKILL.md")
    print("     (use Read tool)")
    print("  2. Build conflict matrix: identify overlaps, contradictions")
    print("     in zones of responsibility across roles.")
    print("  3. For each role, write local skill:")
    print(f"     {paths.agentic_dir(project_dir) / 'skills' / '<role>.md'}")
    print("     Format: frontmatter (derived_from_global: true, global_path,")
    print("     global_sha, normalized_at, pipeline_context) + full copy of")
    print("     global skill + 'Project-specific adaptation' section.")
    print("  4. Heuristics for conflicts:")
    print("    - Priority = pipeline order (first wins).")
    print("    - Output contracts per role type (see supervisor.md).")
    print("    - Unresolved conflicts -> plan.md 'Open questions' section.")
    print("  5. When done, press Enter to continue pipeline.")
    print()
    _log(logs_dir, f"Normalize stage started for team of {len(effective_team)}")
    input()
    _log(logs_dir, "Normalize stage completed (supervisor released)")


def _check_skill_drift(project_dir: Path) -> bool:
    """Check if any local skill has stale global_sha.

    Returns True if any drift detected.
    """
    import hashlib

    import yaml

    skills_dir = paths.agentic_dir(project_dir) / "skills"
    if not skills_dir.is_dir():
        return False

    drifted = []
    for local in sorted(skills_dir.glob("*.md")):
        content = local.read_text(encoding="utf-8")
        if not content.startswith("---\n"):
            continue
        end = content.find("\n---\n", 4)
        if end == -1:
            continue
        try:
            fm = yaml.safe_load(content[4:end]) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict) or not fm.get("derived_from_global"):
            continue
        global_path = Path(str(fm.get("global_path", ""))).expanduser()
        stored_sha = str(fm.get("global_sha", ""))
        if not global_path.exists():
            continue
        current = hashlib.sha256(global_path.read_bytes()).hexdigest()
        if current != stored_sha:
            drifted.append((local.name, global_path.name))

    if drifted:
        print("WARNING: Skill drift detected:", file=sys.stderr)
        for local_name, global_name in drifted:
            print(f"  {local_name} <- {global_name}", file=sys.stderr)
        print("Run 'awf normalize --check-drift' for details, then 'awf normalize'.",
              file=sys.stderr)
        return True
    return False


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
    background = getattr(args, "background", False)
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

    # BD-10-D: drift detection at start
    if _check_skill_drift(project_dir):
        print("Pipeline will run normalize_skills stage due to drift.", file=sys.stderr)
        state_dir = paths.agentic_dir(project_dir) / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        (state_dir / "needs_normalize.yaml").write_text(
            f"needed: true\ntrigger: drift\nmarked_at: {datetime.now(timezone.utc).isoformat()}\n"
        )

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
        s_action = stage.action
        s_desc = stage.description

        print()
        print("-" * 43)
        print(f"  Stage {stage_idx + 1}/{total}: {s_name} ({s_role} :: {s_action})")
        if s_desc:
            print(f"  {s_desc}")
        print("-" * 43)
        _log(logs_dir, f"Stage {stage_idx}: {s_name} ({s_role} :: {s_action})")

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

            if s_action in ("create_todo", "replan"):
                current_todo = _find_active_todo(project_dir)
                if not current_todo:
                    print("No active TODO found. Create one first, then continue.")
                    _log(logs_dir, "No active TODO after supervisor stage")
                    return 1
                print(f"Active TODO: {current_todo}")

            if s_action in ("verify_result", "final_verify"):
                print("Supervisor verification complete.")
                _maybe_commit(s_name, current_todo, stage.on_approved, project_dir, logs_dir, auto=auto)
                stage_idx += 1
                continue

            stage_idx += 1

            # BD-10-C: insert normalize_skills stage after plan
            if s_name == "plan":
                needed, team, state_file = _check_needs_normalize(project_dir)
                if needed:
                    try:
                        _run_normalize_stage(team, project_dir, logs_dir, background=background)
                        # BD-13: only consume state file when normalize actually ran
                        # (i.e., not in background mode). In background, _run_normalize_stage
                        # returns immediately with a warning — state preserved for later.
                        if not background:
                            _consume_needs_normalize(state_file)
                    except Exception:
                        _log(logs_dir, "normalize_skills failed — state file preserved for retry")
                        raise

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

        prefixes = expected_signal_prefixes(s_action)

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

            # Interactive salvage
            _run_supervisor_stage(stage, current_todo, auto=False, project_dir=project_dir, logs_dir=logs_dir)
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

                _run_supervisor_stage(stage, current_todo, auto, project_dir, logs_dir)

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

                _run_supervisor_stage(stage, current_todo, auto, project_dir, logs_dir)
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
    print("Run 'awf status' to check state.")
    print("Run 'awf report' for summary.")
    print("Run 'awf start' for the next iteration.")
    _log(logs_dir, "Pipeline complete")
    return 0
