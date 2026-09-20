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


def _build_pipeline_context(
    project_dir: Path,
    todo_id: str,
    pipeline_name: str | None = None,
) -> str:
    """BD-10: Build pipeline context for worker prompt.

    Tells the worker:
    - What stage it is and what comes before/after in the pipeline
    - What prior TODOs produced (done/ directory)
    - Explicit scope boundary: don't do other stages' work

    This prevents system-analyst from implementing code when
    REQUIREMENTS.md already exists, implementer from writing
    requirements, etc.
    """
    import yaml as _yaml

    parts: list[str] = []

    # 1. Read pipeline stages — KAUD-2: respect explicit pipeline_name,
    #    then config.default_pipeline, then "default".
    from . import config as _cfg_mod
    from .pipeline import resolve_pipeline_file

    config_data = _cfg_mod.load(project_dir)
    if not pipeline_name:
        pipeline_name = _cfg_mod.get(config_data, "default_pipeline", "default") or "default"
    try:
        pipeline_file = resolve_pipeline_file(project_dir, pipeline_name, config_data)
    except FileNotFoundError:
        return ""
    if not pipeline_file.is_file():
        return ""

    try:
        pipeline = _yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
    except (OSError, _yaml.YAMLError):
        return ""

    stages = pipeline.get("stages", []) if pipeline else []
    if not stages:
        return ""

    # 2. Find current stage from state file
    from .pipeline_state import read_state

    state = read_state(project_dir)
    current_stage_name = state.get("stage_name", "") if state else ""
    if not current_stage_name:
        return ""

    # 3. Find position in pipeline
    current_idx = None
    for i, s in enumerate(stages):
        if s.get("name") == current_stage_name:
            current_idx = i
            break

    if current_idx is None:
        return ""

    total = len(stages)
    parts.append(f"## Pipeline context (you are stage {current_idx + 1} of {total})")

    # 4. List other stages
    before = [s["name"] for s in stages[:current_idx]]
    after = [s["name"] for s in stages[current_idx + 1:]]

    if before:
        parts.append(f"Before you: {', '.join(before)}")
    if after:
        parts.append(f"After you: {', '.join(after)}")

    # 5. Explicit scope boundary — match common role name patterns
    role_lower = current_stage_name.lower()
    if "analyst" in role_lower and after:
        parts.append("\nYour scope: requirements analysis. Implementation, architecture, QA — other stages handle those.")
    elif "architect" in role_lower and after:
        parts.append("\nYour scope: architecture/design. Requirements, implementation, QA — other stages handle those.")
    elif "implement" in role_lower and before:
        parts.append("\nYour scope: implementation. Requirements and architecture should already exist — use them, don't recreate.")
    elif "qa" in role_lower or "review" in role_lower:
        parts.append("\nYour scope: quality review. Don't implement — review what others built.")
    elif "audit" in role_lower:
        parts.append("\nYour scope: project audit. Don't implement — assess overall quality.")

    # 6. Prior completed work from done/
    done_dir = project_dir / ".agentic" / "done"
    if done_dir.is_dir():
        completed = sorted(d.name for d in done_dir.iterdir() if d.is_dir())
        if completed:
            parts.append(f"\nPrior completed TODOs: {', '.join(completed)}")
            parts.append("Their output files already exist — review them, don't redo.")

    return "\n".join(parts)


## ─── Stage-specific snippets (focused injection) ───────────────────────
# Each snippet is ~15-20 lines of DIRECTLY RELEVANT instructions for one
# stage. Appended to build_prompt() output so Qwen sees focused rules at
# the END of the prompt (recency bias → higher compliance).

_SNIPPET_ALWAYS = """\
## ⚡ Critical rules (always apply)
- DO NOT edit source files, awf tooling, or templates. All changes through pipeline.
- DO NOT git commit/push manually — pipeline auto-commits after verify approve.
- You ARE the decision maker. Do NOT ask user "should I approve?" — decide yourself.
- After ANY action (approve, dispatch, start) → call awf_wait_for_event to verify result.
- If MCP tool times out → bash fallback: `python3 -m awf status --project-dir <path>`
"""

_SNIPPET_PLAN = """\
## Plan stage — write the TODO (single contract)
1. Study project vision (above) + phases file (.agentic/phases/plan.md)
2. Determine next uncompleted step toward goal
3. Write .agentic/inbox/TODO-NNNN.md:
   - Goal: what this increment achieves (1-3 sentences)
   - Tasks: specific, testable steps for the first agent stage
   - Context: what the agent needs to know about existing code
   - Verify: commands to check success
   - Optional: TODO может нести машиночитаемый блок в самом верху (--- / ---, ключи verify/gates/prove_red) — валидируется при dispatch, формат: docs/unit-contract.md
4. Create signal: .agentic/inbox/TODO-NNNN.ready
5. The checkpoint form shows this TODO to the user — approve, edit, or reject
6. Workers are capable — give autonomy, don't over-specify
"""

_SNIPPET_VERIFY = """\
## Verify stage — YOU are the reviewer, not a relay
1. Read ALL handoffs in .agentic/handoff/
2. Read TODO-{todo_id}.md — this is the contract (Goal, Success criteria, Verify).
   For EACH success criterion in the TODO:
   - Mark ✅ met or ❌ not met
   - If ❌ → you MUST write REVIEW with specifics, not approve
3. Run: git diff --stat — check what actually changed
4. Read the actual code changes for correctness
5. Run verify commands from the TODO independently
6. DECIDE YOURSELF (do NOT ask user):
   - ALL criteria met → create .agentic/inbox/ACK-{todo_id}.ready
   - ANY criterion not met → write .agentic/outbox/REVIEW-{todo_id}.md
     listing which criteria failed and what to fix
7. DO NOT relay "pipeline waits for your decision" — that's YOUR call.
"""

_SNIPPET_SALVAGE = """\
## Salvage — worker didn't signal
The worker ran but didn't create DONE-{todo_id}.ready. Common with smaller models.
1. Read .agentic/inbox/SALVAGE-{todo_id}.md for details on what happened.
2. Check git diff — did worker produce useful work?
3. If yes → create .agentic/inbox/ACK-{todo_id}.ready (accept)
4. If no → call awf_retry_stage(project_dir) to retry the stage
5. If retry also fails → create .agentic/outbox/REVIEW-{todo_id}.md (reject)
6. Do NOT git commit manually — pipeline auto-commits after ACK.

If the diff is EMPTY and this is a REPEAT salvage (see "ATTEMPT" in the
SALVAGE file): do NOT retry the same scope again. Split the work into
smaller TODOs (one file / one function per TODO) and require incremental
writes — a model with a limited output budget cannot finish a big task
in a single reply.
"""

_STAGE_SNIPPETS = {
    "plan": _SNIPPET_PLAN,
    "verify": _SNIPPET_VERIFY,
    "salvage": _SNIPPET_SALVAGE,
}


def _stage_snippet(kind: str, todo_id: str = "") -> str:
    """Load stage-specific focused instructions.

    Returns the snippet text with {todo_id} substituted, or empty string
    if no snippet for this stage kind.
    """
    snippet = _STAGE_SNIPPETS.get(kind, "")
    if snippet and todo_id:
        snippet = snippet.replace("{todo_id}", todo_id)
    return snippet


def get_salvage_snippet(todo_id: str = "") -> str:
    """Public API: salvage instructions for orchestrator."""
    snippet = _SNIPPET_ALWAYS + "\n" + _SNIPPET_SALVAGE
    if todo_id:
        snippet = snippet.replace("{todo_id}", todo_id)
    return snippet


def build_prompt(
    kind: str,
    todo_id: str,
    config: dict | None = None,
    project_dir: Path | None = None,
    pipeline_name: str | None = None,
) -> str:
    """BD-29: build prompt for a stage kind.

    Three kinds: plan / execute / verify.
    UI-2/UI-3: if config has context.message or supervisor.instructions,
    they are appended to the prompt so supervisor sees user's input.

    П6: if project_dir is provided AND kind is "plan", auto-inject the
    project's vision/README excerpt so supervisor (LLM) has concrete
    context instead of guessing where to look.
    """
    # Base prompt per kind
    if kind == "plan":
        base = (
            "You are the supervisor. Read the phases/plan file. Determine the next step "
            "that is not yet completed. Create a TODO file at "
            ".agentic/inbox/TODO-NNNN.md (use next sequential ID). "
            "Baseline is created automatically by awf (П3) — no need to run "
            "`awf baseline` manually. Then create the .ready signal at "
            ".agentic/inbox/TODO-NNNN.ready. If a TODO already exists in inbox, review "
            "it — refine, accept, or replace as needed (do NOT blindly skip). Keep the TODO "
            "at the goal level (what success looks like), do NOT micromanage individual "
            "roles — each role's skill.md already defines its zone."
        )
    elif kind == "verify":
        base = (
            f"You are the supervisor. Verify TODO {todo_id}: read the DONE report and PROGRESS notes, "
            "check `git diff --stat` against the baseline SHA in "
            f".agentic/context/BASELINE-{todo_id}.sha. Decide: is the work complete and correct? "
            "If yes, write ACK signal at "
            f".agentic/inbox/ACK-{todo_id}.ready. If no, do NOT ack — leave a note in "
            f".agentic/outbox/REVIEW-{todo_id}.md explaining what's wrong."
        )
    elif kind == "salvage":
        base = (
            f"You are the supervisor. The worker at a previous stage ran but did NOT create "
            f"DONE-{todo_id}.ready. This is a SALVAGE situation. Two common causes: "
            f"(1) the worker finished but forgot the signal — git diff then shows work; "
            f"(2) the worker hit its output-token limit mid-reply (truncated → no tool call → "
            f"clean exit) — git diff is then EMPTY and no progress notes exist. "
            f"Read .agentic/inbox/SALVAGE-{todo_id}.md FIRST: it lists what happened and, "
            f"for repeat salvages, what to change before retrying. "
            f"If the work in `git diff --stat` (baseline .agentic/context/BASELINE-{todo_id}.sha) "
            f"is acceptable → create .agentic/inbox/ACK-{todo_id}.ready. "
            f"If not → create .agentic/outbox/REVIEW-{todo_id}.md with feedback — do NOT just "
            f"retry the same scope when the diff is empty."
        )
    else:
        # execute (default) — no context/instructions for agent stages.
        # DF5-1: signal contract FIRST, before skill content. Workers
        # (especially Qwen/vLLM) skip the one-liner buried at the end
        # when skill file is 100+ lines of detail. This anchor goes first.
        base = (
            f"## CRITICAL completion contract (read this BEFORE your skill)\n"
            f"Pipeline BLOCKS until you create the signal file. This is non-negotiable.\n\n"
            f"  ✅ Done?   → touch .agentic/outbox/DONE-{todo_id}.ready\n"
            f"  🚫 Blocked? → touch .agentic/outbox/BLOCKED-{todo_id}.ready\n\n"
            f"Also write a 1-line summary: .agentic/outbox/DONE-{todo_id}.md\n"
            f"Optional machine facts for the next role: .agentic/outbox/DONE-{todo_id}.json (keys: files_changed, tests_run, gates, notes — format in docs/unit-contract.md).\n\n"
            f"## OUTPUT DISCIPLINE (dogfood-11: works with any model, any output limit)\n"
            f"Your reply has a limited token budget. Work in small pieces:\n"
            f"- Write code straight into files (write/edit tools). NEVER draft whole\n"
            f"  files inside your reasoning or reply text.\n"
            f"- Create the file skeleton first, then fill in one function or section\n"
            f"  per step.\n"
            f"- One file per step. Keep replies short — no long explanations.\n"
            f"- If a reply is growing large, STOP, save what you have, and continue\n"
            f"  in the next step.\n\n"
            f"## WORKSPACE DISCIPLINE (SPEC-2)\n"
            f"- Temporary files: ONLY /tmp/opencode/** or the test framework's\n"
            f"  tmp_path. Never invent your own /tmp paths — external paths are\n"
            f"  permission-gated and a headless run cannot answer an ask prompt.\n"
            f"- Live runs of external processes (real API calls, smoke runs): ONLY\n"
            f"  when the task explicitly asks for them; ordinary checks = tests with\n"
            f"  markers. Expensive commands: prefer reporting over repeated runs.\n\n"
            f"## Task\n"
            f"Execute your part of {todo_id} according to your role/skill instructions. "
            f"You see the TODO goal and handoffs from previous roles (if any). Add YOUR contribution — "
            f"don't redo prior work. "
            f"Do not commit unless the TODO explicitly asks for it."
        )
        # BD-10: pipeline context — tell worker its place in the team.
        # Without this, system-analyst tries to implement code, implementer
        # tries to write requirements, etc. Worker needs to know:
        # - What stage it is (position in pipeline)
        # - What comes before/after (don't do others' work)
        # - What prior TODOs produced (don't redo)
        if project_dir is not None:
            ctx = _build_pipeline_context(project_dir, todo_id, pipeline_name)
            if ctx:
                base += "\n\n" + ctx
        return base

    # П6: auto-inject project vision/README excerpt for plan stage.
    # Without this, supervisor (LLM) sees only generic instructions and
    # has to guess where project context lives.
    if kind == "plan" and project_dir is not None:
        from .paths import find_vision_file
        vision = find_vision_file(project_dir)
        if vision is not None:
            try:
                content = vision.read_text(encoding="utf-8")
                # Truncate to keep prompt manageable (LLM context budget).
                # 4000 chars ~ 1000 tokens — enough for vision summary.
                if len(content) > 4000:
                    excerpt = content[:4000] + "\n\n[... truncated ...]"
                else:
                    excerpt = content
                try:
                    rel = vision.relative_to(project_dir)
                except ValueError:
                    rel = vision
                base += f"\n\n## Project vision (auto-injected from {rel})\n{excerpt}"
            except OSError:
                pass  # non-fatal — supervisor falls back to generic prompt

    # UI-2/UI-3: append user's context + instructions for supervisor stages
    if config and kind in ("plan", "verify", "salvage"):
        ctx_msg = cfg_mod.get(config, "context.message", "") or ""
        sup_instr = cfg_mod.get(config, "supervisor.instructions", "") or ""
        if ctx_msg:
            base += f"\n\n## Project context (from user)\n{ctx_msg}"
        if sup_instr:
            base += f"\n\n## Additional instructions (from user)\n{sup_instr}"

    # Stage-specific focused snippet injection.
    # Order: always rules first (context), then stage-specific (actionable LAST).
    # Recency bias: Qwen pays most attention to the END of the prompt.
    snippet = _stage_snippet(kind, todo_id)
    if snippet:
        base += "\n\n" + _SNIPPET_ALWAYS + "\n" + snippet

    return base


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


def _safe_supervisor_timeout() -> int:
    """Parse AWF_SUPERVISOR_TIMEOUT with safe fallback on invalid value.

    Without this, ``AWF_SUPERVISOR_TIMEOUT=abc`` raised ValueError at
    runtime inside run_supervisor_stage — pipeline crashed mid-flight
    with an opaque traceback. Now falls back to 3600.
    """
    import os

    raw = os.environ.get("AWF_SUPERVISOR_TIMEOUT", "3600")
    try:
        return max(1, int(raw))
    except (ValueError, TypeError):
        return 3600


def _decision_signal_fresh(
    path: Path,
    wall_start: float,
    stale_logged: set[str],
    logs_dir: Path,
) -> bool:
    """AUD04-04: mtime gate for verify decision signals (ACK/APPROVE/REVIEW).

    A decision file is fresh when it appeared no earlier than the second the
    wait started. Compared at whole-second resolution on purpose: filesystem
    mtime granularity can be 1s, and a supervisor answering in the first
    microseconds of the wait must not be rejected by a strict
    ``st_mtime > wall_start`` compare (FU-03 QA note). Anything older is a
    leftover from a previous cycle (commit-fail, kill+continue) and is
    ignored — otherwise a dead approval would re-open the commit gate.
    Stale hits are logged once per filename so the poll loop stays quiet.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    if int(mtime) >= int(wall_start):
        return True
    if path.name not in stale_logged:
        stale_logged.add(path.name)
        _log(
            logs_dir,
            f"AUD04-04: stale decision signal ignored: {path.name} "
            f"(mtime predates this wait — previous cycle's decision)",
        )
    return False


def _decision_is_stale(
    path: Path,
    signal: str,
    accepted_decision: str | None,
    accepted_decision_mtime: float | None,
) -> bool:
    """U6b: cycle-aware gate for the auto-verify fallback.

    ``accepted_decision``/``accepted_decision_mtime`` is the decision the
    engine ACCEPTED in a previous verify cycle (recorded at acceptance,
    cleared at consumption — pipeline_engine._record_verify_decision /
    _consume_verify_decision). A kill between acceptance and consumption
    leaves the decision file on disk; the next cycle's fallback would
    re-accept it by mere existence (auto-commit on a dead approval).

    The leftover is the SAME physical file as the accepted one: same signal
    name, mtime no newer than the recorded one (whole-second resolution,
    same convention as _decision_signal_fresh). A newer mtime means the
    owner re-approved — that is a fresh decision, not the leftover.
    """
    if not accepted_decision or accepted_decision_mtime is None:
        return False
    if accepted_decision != signal:
        return False
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return int(mtime) <= int(accepted_decision_mtime)


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

    Dogfood-1 fix: snapshot filter only excludes TODOs that already have a
    matching DONE-<id>.ready in outbox (truly stale). Active orphan TODOs
    (created by supervisor before `awf start`, no DONE yet) are picked up
    immediately. Without this, the common workflow "create TODO → awf_start"
    would hang forever waiting for a "new" signal that never arrives.

    AUD04-04: for verify/salvage/replan, decision signals (ACK/APPROVE/REVIEW)
    must be fresh for THIS wait (mtime >= wait start, whole-second resolution).
    A file left over from a previous cycle no longer satisfies the wait; it
    is also consumed by the engine at the end of the cycle that accepted it
    (pipeline_engine._consume_verify_decision) — the mtime gate is the safety
    net for files the dead process never got to consume.
    """
    import time

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    # BD-30: snapshot which TODO signals existed BEFORE pipeline start.
    # Used to distinguish "new signal from interactive supervisor" from
    # "stale file from previous runs". But only filter as stale if the
    # TODO already has a matching DONE-<id>.ready in outbox (truly completed)
    # OR is archived in done/ (DF6-1/DF6-3); otherwise it's an active orphan.
    from .todos import is_archived

    existing_todo_signals: set[str] = set()
    active_orphan_signals: list[str] = []
    if kind in ("plan", "replan") and inbox.is_dir():
        for p in inbox.glob("TODO-*.ready"):
            existing_todo_signals.add(p.name)
            todo_name = p.stem  # "TODO-0001"
            has_done = (outbox / f"DONE-{todo_name}.ready").exists()
            is_done = has_done or is_archived(project_dir, todo_name)
            if not is_done:
                active_orphan_signals.append(p.name)
        # If there are active orphans (not-yet-done), take the newest
        # immediately on first poll iteration. This avoids the UX trap
        # where "create TODO then awf_start" hangs forever.
        active_orphan_signals.sort()

    deadline_log_interval = 60
    start = time.monotonic()
    wall_start = time.time()  # for mtime comparisons (replan signals)
    deadline = start + timeout
    last_log = start
    # AUD04-04: filenames already logged as stale, to keep the poll quiet.
    stale_decision_logged: set[str] = set()

    while True:
        if kind == "plan":
            # Dogfood-1: active orphan TODOs (no DONE) picked up immediately.
            # AUD04-09: the NEWEST orphan (list is sorted ascending, so the
            # last element) — the engine executes the newest active TODO
            # (_find_active_todo), so the logged id must match the one run.
            if active_orphan_signals:
                sig = active_orphan_signals.pop(-1).replace(".ready", "")
                _log(
                    logs_dir,
                    f"BD-30/dogfood-1: picked up active orphan TODO signal: {sig} "
                    f"(created before pipeline start, no matching DONE in outbox)",
                )
                return sig
            if inbox.is_dir():
                current = {p.name for p in inbox.glob("TODO-*.ready")}
                new_ones = current - existing_todo_signals
                if new_ones:
                    sig = sorted(new_ones)[0].replace(".ready", "")
                    _log(logs_dir, f"BD-30: interactive supervisor signal detected: {sig}")
                    return sig

        if kind in ("verify", "salvage", "replan") and todo_id:
            # AUD04-04: existence alone is not acceptance — the decision must
            # be fresh for this wait (a previous cycle's ACK/APPROVE/REVIEW
            # must not be accepted again).
            for sig_path in (
                inbox / f"ACK-{todo_id}.ready",
                inbox / f"APPROVE-{todo_id}.ready",
            ):
                if sig_path.exists() and _decision_signal_fresh(
                    sig_path, wall_start, stale_decision_logged, logs_dir
                ):
                    _log(logs_dir, f"BD-30: interactive supervisor signal detected: {sig_path.name}")
                    return sig_path.stem
            review = outbox / f"REVIEW-{todo_id}.md"
            if review.exists() and _decision_signal_fresh(
                review, wall_start, stale_decision_logged, logs_dir
            ):
                _log(logs_dir, f"BD-30: interactive supervisor signal detected: REVIEW-{todo_id}.md")
                return f"REVIEW-{todo_id}"

        if kind == "replan" and inbox.is_dir():
            # Day-2 B2: the replan supervisor answers AFTER the escalation —
            # accept only TODO signals created since the wait started. The old
            # orphan-pickup instantly "completed" the wait with the very TODO
            # being retried (its .ready pre-dated the escalation), and the
            # pipeline stopped as if the supervisor had answered nothing.
            for todo_sig in sorted(inbox.glob("TODO-*.ready")):
                try:
                    if todo_sig.stat().st_mtime > wall_start:
                        sig = todo_sig.stem
                        _log(
                            logs_dir,
                            f"BD-30: replan signal detected: {sig} (created after escalation)",
                        )
                        return sig
                except OSError:
                    continue

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
        # П6: point supervisor to project vision/README if found
        from .paths import find_vision_file
        vision = find_vision_file(project_dir)
        if vision is not None:
            try:
                rel = vision.relative_to(project_dir)
            except ValueError:
                rel = vision
            print(f"PROJECT CONTEXT: read {rel} for vision/architecture.")
            print("  Extract relevant steps for the current iteration.")
            print()
        else:
            print("PROJECT CONTEXT: no vision/README found in project root.")
            print("  Ask the user about project goals before planning.")
            print()
        print("STEPS:")
        print(f"  1. Read {phases_path} — find next unfinished step ([ ] checkbox)")
        print(f"  2. Read existing TODO-*.md in {inbox}/ (if any) — review/refine")
        print("  3. Create .agentic/inbox/TODO-NNNN.md with detailed task:")
        print("     - Goal (what success looks like)")
        print("     - Prohibitions (what NOT to do)")
        print("     - Context (links, references, prior work)")
        print("     - Tasks with Files / Description / Verify / Done-when")
        print("  4. Create empty signal file: .agentic/inbox/TODO-NNNN.ready")
        print("     (awf auto-creates git baseline on next stage — no manual `awf baseline`)")
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
        if kind == "salvage":
            print(f"Salvage details: read {inbox}/SALVAGE-{todo_id}.md — it lists what")
            print("happened and, for repeat attempts, what to change before retrying.")
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
    pipeline_name: str | None = None,
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
        # C1 v2: configurable timeout via env (default 3600s = 1 hour).
        # Invalid env value falls back to default (was: ValueError crash).
        timeout = _safe_supervisor_timeout()
        signal = wait_for_supervisor_signal(kind, todo_id, project_dir, logs_dir, timeout=timeout)
        _log(logs_dir, f"BD-30: interactive supervisor {kind} completed by user (opencode): {signal}")
        return signal

    print("Instructions: .agentic/roles/supervisor.md")
    print(f"Phases file: {phases_file}")
    print()
    if kind == "plan":
        print("What to do (plan — write the TODO):")
        print("  1. Study the project state and phases file")
        print("  2. Determine the next step (or review existing TODO if present)")
        print("  3. Write .agentic/inbox/TODO-NNNN.md:")
        print("     - Goal: what this increment achieves (1-3 sentences)")
        print("     - Tasks: specific, testable steps for the first agent stage")
        print("     - Context: what the agent needs to know about existing code")
        print("     - Verify: commands to check success")
        print("  4. Create signal: .agentic/inbox/TODO-NNNN.ready")
        print("  5. The checkpoint form shows the TODO to the user —")
        print("     they approve, edit, or reject before agents start")
    elif kind == "verify":
        print("What to do (verify):")
        print("  1. Read report from .agentic/outbox/")
        print("  2. Run verification commands independently")
        print("  3. Check git diff — changes must be in source files")
        print("  4. Decide: continue / fix / rollback")
        print("  5. If approved: create .agentic/inbox/ACK-NNNN.ready")
    else:
        print(f"What to do ({kind}): see supervisor.md instructions")

    print()
    return run_supervisor_via_subprocess(
        kind, todo_id, project_dir, config, phases_file, logs_dir, pipeline_name
    )


def _prepare_supervisor_stage(
    kind: str,
    todo_id: str,
    project_dir: Path,
    config: dict,
    phases_file: str,
    logs_dir: Path,
    pipeline_name: str | None,
) -> tuple[list[str], str] | None:
    """Build extra_files + prompt for a supervisor stage kind.

    Returns None to indicate the stage should be skipped.
    Returns (extra_files, prompt) otherwise.
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    phases_path = (
        project_dir / phases_file if not Path(phases_file).is_absolute() else Path(phases_file)
    )

    extra_files: list[str] = []
    prompt = ""

    if kind == "plan":
        if phases_path.is_file():
            extra_files.append(str(phases_path))
        prompt = build_prompt(
            "plan", todo_id, config=config, project_dir=project_dir, pipeline_name=pipeline_name
        )

    elif kind == "verify":
        if not todo_id:
            print("[auto mode] No todo_id for verify — skip.")
            _log(logs_dir, "Supervisor verify auto-skipped (no todo_id)")
            return None
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
        # R8: the TODO is the contract — forward it as an extra file so the
        # supervisor sees it without opening the file manually.
        todo_md = inbox / f"{todo_id}.md"
        if todo_md.is_file():
            extra_files.append(str(todo_md))
        prompt = build_prompt(
            "verify", todo_id, config=config, project_dir=project_dir, pipeline_name=pipeline_name
        )

    elif kind == "replan":
        if not todo_id:
            print("[auto mode] No todo_id for replan — skip.")
            _log(logs_dir, "Supervisor replan auto-skipped (no todo_id)")
            return None
        blocked = outbox / f"BLOCKED-{todo_id}.md"
        if blocked.is_file():
            extra_files.append(str(blocked))
        prompt = (
            f"Worker reported BLOCKED on {todo_id}. Read the BLOCKED note, analyze the problem, "
            "create a refined TODO at .agentic/inbox/TODO-NNNN.md (next sequential ID), "
            "baseline it, and create the .ready signal."
        )

    elif kind == "salvage":
        print("[auto mode] salvage not automated — skipping.")
        _log(logs_dir, "Supervisor salvage auto-skipped (not automatable)")
        return None

    else:
        print(f"[auto mode] Unknown kind {kind!r} — skipping.")
        _log(logs_dir, f"Supervisor stage {kind} auto-skipped (unknown kind)")
        return None

    return extra_files, prompt


def run_supervisor_via_subprocess(
    kind: str,
    todo_id: str,
    project_dir: Path,
    config: dict,
    phases_file: str,
    logs_dir: Path,
    pipeline_name: str | None = None,
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

    prepared = _prepare_supervisor_stage(
        kind, todo_id, project_dir, config, phases_file, logs_dir, pipeline_name
    )
    if prepared is None:
        return ""
    extra_files, prompt = prepared

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

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

    from ._env import awf_subprocess_env
    from .signal_watch import run_subprocess_until_signal

    signal_holder: dict[str, str] = {}

    result = run_subprocess_until_signal(
        cmd,
        cwd=str(project_dir),
        watch_paths=watch_paths,
        watch_new_glob=watch_new_glob,
        logs_dir=logs_dir,
        env=awf_subprocess_env(),
        signal_holder=signal_holder,
        # U6a/U6c are worker-only (TODO-0017): the supervisor subprocess can
        # be legitimately long-silent (verify waits), so no watchdog; no
        # preflight either — keep the supervisor flow exactly as before.
        preflight_timeout=0,
        no_output_timeout=0,
    )

    _log(logs_dir, f"Supervisor {kind} subprocess finished (exit={result.returncode})")
    if result.returncode != 0:
        raise RuntimeError(
            f"Supervisor {kind} subprocess exited with code {result.returncode}. "
            f"Cmd: {' '.join(cmd)}"
        )

    fired = signal_holder.get("signal", "")
    if fired:
        signal_name = fired.rsplit(".", 1)[0] if "." in fired else fired
        _log(logs_dir, f"Supervisor {kind} produced signal (from watcher): {signal_name!r}")
        return signal_name

    # U6b: pass the previous cycle's accepted decision (pipeline state) so
    # the fallback can reject a leftover from a kill between acceptance and
    # consumption. The watcher path above already cannot fire on it (BD-22
    # pre-existing snapshot) — the fallback is the only hole.
    from .pipeline_state import read_state

    prev_state = read_state(project_dir) or {}
    signal_name = _detect_supervisor_signal(
        kind, todo_id, inbox, outbox,
        accepted_decision=prev_state.get("accepted_decision"),
        accepted_decision_mtime=prev_state.get("accepted_decision_mtime"),
    )
    _log(logs_dir, f"Supervisor {kind} produced signal (fallback): {signal_name!r}")
    return signal_name


def _detect_supervisor_signal(
    kind: str,
    todo_id: str,
    inbox: Path,
    outbox: Path,
    accepted_decision: str | None = None,
    accepted_decision_mtime: float | None = None,
) -> str:
    """C1 fix: detect which signal the supervisor actually produced.

    For verify: prefers REVIEW (rejection) over ACK/APPROVE — if supervisor
    wrote REVIEW-{todo_id}.md, that's the most recent decision and should
    override any stale ACK.

    U6b: existence alone is NOT acceptance. ``accepted_decision``/
    ``accepted_decision_mtime`` (from the pipeline state) carry the decision
    the engine already accepted in a previous cycle; a decision file matching
    that record at the same or older mtime is the leftover of a killed cycle
    and is rejected (see _decision_is_stale). A pre-approval with no record
    (BD-8) and a fresh re-approval (newer mtime) are accepted as before.
    """
    if kind == "verify" and todo_id:
        # Check REVIEW first (most recent decision wins)
        review = outbox / f"REVIEW-{todo_id}.md"
        if review.exists() and not _decision_is_stale(
            review, f"REVIEW-{todo_id}", accepted_decision, accepted_decision_mtime
        ):
            return f"REVIEW-{todo_id}"
        # Then ACK and APPROVE
        ack = inbox / f"ACK-{todo_id}.ready"
        if ack.exists() and not _decision_is_stale(
            ack, f"ACK-{todo_id}", accepted_decision, accepted_decision_mtime
        ):
            return f"ACK-{todo_id}"
        approve = inbox / f"APPROVE-{todo_id}.ready"
        if approve.exists() and not _decision_is_stale(
            approve, f"APPROVE-{todo_id}", accepted_decision, accepted_decision_mtime
        ):
            return f"APPROVE-{todo_id}"
    elif kind in ("plan", "replan"):
        # Newest TODO-*.ready by mtime. signal_watch already confirmed at
        # least one NEW TODO-*.ready appeared during supervisor execution
        # (its snapshot filtered out pre-existing files). Here we just
        # pick the freshest one — stale TODOs from previous runs are
        # older by mtime. If signal_watch is bypassed and stale files
        # exist, this could return an outdated TODO; safe today because
        # this function is only called from run_supervisor_stage which is
        # always wrapped by run_subprocess_until_signal.
        todos = sorted(inbox.glob("TODO-*.ready"), key=lambda p: p.stat().st_mtime, reverse=True)
        if todos:
            return todos[0].stem  # "TODO-NNNN.ready" → "TODO-NNNN"
    return ""
