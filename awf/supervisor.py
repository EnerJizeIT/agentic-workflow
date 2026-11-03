"""Supervisor stage handling — plan / verify / replan / salvage.

Extracted from orchestrator.py (A6 refactor). Two execution paths:

- Interactive (BD-30, auto=False): current opencode in user's chat IS the
  supervisor. awf prints explicit instructions and waits for a signal file.
- Auto (BD-14, auto=True): spawn ``opencode run`` subprocess that loads
  supervisor.md and does the work autonomously.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from pathlib import Path

from . import config as cfg_mod
from . import paths, worker_tree
from ._errors import AwfApiError
from ._log import log as _log
from .arbiter import (
    decision_is_stale,
    discard_overruled,
    human_verify_decision,
    overruled_auto_verdicts,
)
from .doctrine import materialize_doctrine
from .pipeline import Stage, load_stages, resolve_pipeline_file
from .todo_ids import is_valid_todo_id
from .xdg import awf_roles_dir

log = logging.getLogger(__name__)

# R1/AC1: prompt names this many latest completed units, never the archive.
_PRIOR_TODO_LIMIT = 8

# TODO-0338 (A35): after the verify verdict is on disk the supervisor
# subprocess gets this long to exit on its own before the engine TERMs it.
# Overridable via `wait.verify_grace_seconds`; 0 means TERM as soon as the
# signal is seen, a negative value falls back to this default.
VERIFY_GRACE_DEFAULT = 90.0

# Archive directory names, not public IDs: `TODO-9` is a legal directory
# name here even though the public ID contract asks for four digits.
_DONE_DIR_NAME_RE = re.compile(r"TODO-(\d+)")


def get_agent_name(config: dict, role: str) -> str:
    """Get the agent_name override for a role, defaulting to the role itself."""
    return cfg_mod.get(config, f"models.{role}.agent_name", role) or role


def argv_agent_flag(config: dict, role: str) -> list[str]:
    """A13: the ``--agent`` argv fragment — only when a name is configured.

    opencode 1.x silently ignored an unknown agent name, 2.x rejects it
    (``Error: Agent not found``). A role without ``models.<role>.agent_name``
    therefore runs on the default agent: no flag at all, as in 1.x.
    """
    name = cfg_mod.get(config, f"models.{role}.agent_name", "")
    if not isinstance(name, str) or not name.strip():
        return []
    return ["--agent", name.strip()]


def get_role_model(config: dict, role: str) -> str | None:
    """BD-24: get the model override for a role from config.yaml.

    Returns the model string (e.g. "vllm/llm") if set, else None.
    """
    val = cfg_mod.get(config, f"models.{role}.model", "")
    return val if val else None


def _snapshot_stage_names(project_dir: Path, todo_id: str) -> list[str] | None:
    """CTX1: effective stage names from the unit's frozen pipeline snapshot.

    The engine executes ``context/PIPELINE-<id>.yaml`` captured at launch;
    the worker prompt must describe that same definition. Returns None
    when the unit has no readable snapshot (old unit, manual launch,
    malformed file) — the caller falls back to the live pipeline, exactly
    like ``_apply_pipeline_snapshot`` degrades.
    """
    if not is_valid_todo_id(todo_id):
        return None
    snap = paths.pipeline_snapshot_path(project_dir, todo_id)
    if not snap.is_file():
        return None

    try:
        stages = load_stages(snap)
    except AwfApiError:
        return None
    names = [s.name for s in stages]
    return names or None


def _live_stage_names(
    project_dir: Path, pipeline_name: str | None
) -> list[str]:
    """Effective stage names from the live pipeline file (the fallback).

    KAUD-2: respect an explicit pipeline_name, then
    config.default_pipeline, then "default". Empty on any resolve/load
    failure — the caller then emits no pipeline context at all.
    """
    import yaml as _yaml

    config_data = cfg_mod.load(project_dir)
    if not pipeline_name:
        pipeline_name = (
            cfg_mod.get(config_data, "default_pipeline", "default") or "default"
        )

    try:
        pipeline_file = resolve_pipeline_file(project_dir, pipeline_name, config_data)
    except FileNotFoundError:
        return []
    except AwfApiError:
        # RUN3 #1: the resolver now rejects an explicit-but-missing name
        # (and invalid names) with AwfApiError. This caller passes the
        # config-derived name explicitly, so a project without a pipeline
        # file (or with an invalid default_pipeline) must degrade to "no
        # pipeline context" — not crash the prompt build.
        return []
    if not pipeline_file.is_file():
        return []

    try:
        pipeline = _yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
    except (OSError, _yaml.YAMLError):
        return []

    stages = pipeline.get("stages", []) if pipeline else []
    return [(s.get("id") or s.get("name")) for s in stages]


def _recent_completed_todos(done_dir: Path, current_todo_id: str) -> list[str]:
    """R1/AC1, K1: the last eight completed units, newest first.

    The order comes from the parsed number, not the name: plain name
    sorting puts ``TODO-9`` before ``TODO-10`` and ``TODO-10000`` before
    ``TODO-9999``. The prompt names the newest first (K1: ``TODO-0100, …
    TODO-0093``), so the worker reads the closest units first. Entries that
    are not TODO directories (retired units, strays) are skipped.
    """
    numbered: list[tuple[int, str]] = []
    for entry in done_dir.iterdir():
        if not entry.is_dir() or entry.name == current_todo_id:
            continue
        match = _DONE_DIR_NAME_RE.fullmatch(entry.name)
        if match is None:
            continue
        numbered.append((int(match.group(1)), entry.name))
    numbered.sort(reverse=True)
    return [name for _, name in numbered[:_PRIOR_TODO_LIMIT]]


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
    parts: list[str] = []

    # 1. Stage list — CTX1: the unit runs the snapshot captured at launch,
    #    so the prompt describes that definition. A live-file edit made
    #    mid-run must not leak new stages into the worker's context.
    stage_names = _snapshot_stage_names(project_dir, todo_id)

    if stage_names is None:
        # No snapshot (old unit / manual launch / malformed) — degrade to
        # the live pipeline, the same fallback the engine takes.
        stage_names = _live_stage_names(project_dir, pipeline_name)
        if not stage_names:
            return ""

    # 2. Find current stage from state file
    from .pipeline_state import read_state

    state = read_state(project_dir)
    current_stage_name = state.get("stage_name", "") if state else ""
    if not current_stage_name:
        return ""

    # 3. Find position in the stage list. ORCH M4.4: a stage addressed by
    # `id` carries no explicit `name` in the YAML (the id becomes the
    # stage name at load, see load_stages) — the names built above are
    # already the effective ones (id or name).
    if current_stage_name not in stage_names:
        return ""
    current_idx = stage_names.index(current_stage_name)

    total = len(stage_names)
    parts.append(f"## Pipeline context (you are stage {current_idx + 1} of {total})")

    # 4. List other stages — effective names, same rule.
    before = stage_names[:current_idx]
    after = stage_names[current_idx + 1:]

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

    # 6. Prior completed work from done/ — R1/AC1: only the last eight
    #    units, never the whole archive. The list must not grow with
    #    history (CTX5: 297 ids were 3 288 of 5 654 prompt characters).
    done_dir = paths.done_dir(project_dir)
    if done_dir.is_dir():
        completed = _recent_completed_todos(done_dir, todo_id)
        if completed:
            parts.append(f"\nPrior completed TODOs: {', '.join(completed)}")
            parts.append("Full list: awf report, .agentic/done/")
            parts.append("Their output files already exist — review them, don't redo.")

    return "\n".join(parts)


## ─── Stage-specific snippets (focused injection) ───────────────────────
# Each snippet is ~15-20 lines of DIRECTLY RELEVANT instructions for one
# stage. Appended to build_prompt() output so Qwen sees focused rules at
# the END of the prompt (recency bias → higher compliance).

_SNIPPET_ALWAYS = """\
## ⚡ Critical rules (always apply)
- DO NOT edit source files, awf tooling, or templates. All changes through pipeline.
- Commit follows the stage policy: `on_approved` in .agentic/pipelines/*.yaml —
  commit_and_next/commit_and_report = the engine commits after approve.
  NEVER run `git add`, `git commit` or `git push`: a refused gate is a REVIEW
  with the reason, never a manual commit.
- You ARE the decision maker. Do NOT ask user "should I approve?" — decide yourself.
- No active run: GO IDLE after start — the engine tracks signals itself, do NOT
  poll awf_wait_for_event/awf_status in a loop. In an active run (забег): keep
  the loop awf_wait_for_event(actionable_only=True) → awf_approve(evidence=...)
  → awf_run_next (see phase-run.md).
- If MCP tool times out → bash fallback: `python3 -m awf status --project-dir <path>`
- Metrics (U8c): `awf_metrics` / `awf metrics` (on owner request and at the end
  of a run) — report to the desktop, copy to `metrics.mirror_dir` if set.
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
1. The verify pack facts are already in this prompt (verdict, red sections,
   first reason) together with the diff and the TODO — do NOT run tests or
   any verify command: the verdict is decided from those facts.
2. TODO-{todo_id}.md is the contract (Goal, Success criteria, Verify).
   For EACH success criterion in the TODO:
   - Mark ✅ met or ❌ not met
   - If ❌ → you MUST write REVIEW with specifics, not approve
3. Check the diff block: changes must be the unit's own source changes,
   not a stray file or an empty tree.
4. Read the actual code changes for correctness where the facts leave doubt.
5. DECIDE YOURSELF (do NOT ask user):
   - ALL criteria met → create .agentic/inbox/ACK-{todo_id}.ready
     (in an ACTIVE run — approve via awf_approve(evidence=...) instead;
     the engine ignores a file-based ACK without RUN-EVIDENCE)
   - ANY criterion not met → write .agentic/outbox/REVIEW-{todo_id}.md
     listing which criteria failed and what to fix
6. DO NOT relay "pipeline waits for your decision" — that's YOUR call.
7. The verdict is yours, the commit is the engine's: NEVER run `git add`,
   `git commit` or `git push`. A refused gate is a REVIEW with the reason.
"""

_SNIPPET_SALVAGE = """\
## Salvage — worker didn't signal
The worker ran but didn't create DONE-{todo_id}.ready. Common with smaller models.
1. Read .agentic/inbox/SALVAGE-{todo_id}.md for details on what happened.
2. Check git diff — did worker produce useful work?
3. If yes → create .agentic/inbox/ACK-{todo_id}.ready (accept)
   (in an ACTIVE run — approve via awf_approve(evidence=...) instead;
   the engine ignores a file-based ACK without RUN-EVIDENCE)
4. If no → call awf_retry_stage(project_dir) to retry the stage
5. If retry also fails → create .agentic/outbox/REVIEW-{todo_id}.md (reject)
6. The verdict is yours, the commit is the engine's — never run `git add`,
   `git commit` or `git push`; a refused gate is a REVIEW with the reason.

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
            f"You are the supervisor. Verify TODO {todo_id}. The facts are already in "
            "this prompt: the verify pack verdict (red sections and the first reason), "
            "the diff against the baseline and short DONE/PROGRESS excerpts. The pack "
            "already ran the checks — do not run them again and do not go looking for "
            "more history on disk. Decide: is the work complete and correct? "
            "If yes, write ACK signal at "
            f".agentic/inbox/ACK-{todo_id}.ready. If no, do NOT ack — leave a note in "
            f".agentic/outbox/REVIEW-{todo_id}.md explaining what's wrong.\n\n"
            "## Signal ends your work\n"
            "Your work ends when you write the signal file. Do NOT poll `awf status` "
            "and do NOT wait for the commit — committing is the engine's gate, "
            "not your step."
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
            f"  ✅ Done?   → touch .agentic/outbox/DONE-{todo_id}.ready AND write a\n"
            f"               1-line summary: .agentic/outbox/DONE-{todo_id}.md\n"
            f"  🚫 Blocked? → write .agentic/outbox/BLOCKED-{todo_id}.md with the reason,\n"
            f"               then touch .agentic/outbox/BLOCKED-{todo_id}.ready (no DONE file)\n\n"
            f"Optional machine facts for the next role: .agentic/outbox/DONE-{todo_id}.json —\n"
            f"a JSON object with keys files_changed, tests_run, gates, notes, e.g.:\n"
            f'  {{"files_changed": ["awf/x.py", "tests/unit/test_x.py"],\n'
            f'   "tests_run": [{{"cmd": "python3 -m pytest tests/unit/test_x.py -q", "result": "12 passed"}}],\n'
            f'   "gates": ["contracts", "ratchet"], "notes": "one line"}}\n'
            f"tests_run items are OBJECTS {{cmd, result}}, not strings — a file that\n"
            f"violates the schema (or is broken JSON) is skipped by the handoff.\n"
            f"If you review or audit another stage's work, your own facts go to\n"
            f"  .agentic/outbox/PROBES-{todo_id}.json — leave DONE-{todo_id}.json to its author.\n"
            f"Full format: docs/unit-contract.md.\n\n"
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
            f"Do not commit — the engine's commit gate commits after verification; "
            f"leave your changes in the working tree."
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


def _run_evidence_ok(project_dir: Path, todo_id: str) -> bool:
    """AUD11-03: approve-signal gate for run (забег) mode.

    Inside an ACTIVE run, an approve (ACK/APPROVE) only counts when the
    independent-verification evidence file exists:
    ``context/RUN-EVIDENCE-{todo_id}.md`` (written by
    ``awf_approve(evidence=...)``). Without it the verify wait and the
    auto-mode fallback ignore the file-based approve — the run protocol's
    audit trail is the reason run mode exists, and the prompts used to
    teach exactly the bypass (manual ACK, no evidence).

    Outside a run, file-based ACK is the normal interactive flow — the
    gate must not change it. REVIEW is never gated (rejection needs no
    evidence).
    """
    from . import run_state

    run = run_state.read_run(project_dir)
    if not (run and run.get("active")):
        return True
    evidence = paths.context_dir(project_dir) / f"RUN-EVIDENCE-{todo_id}.md"
    return evidence.is_file()


class VerifyWaitStopped(RuntimeError):
    """A41: the run's own stop surface ended the verify wait.

    A stop flag on the unit, an exhausted budget or a closed run stops the
    wait with the cause in the message — never a silent ``rc=1`` from the
    supervisor stage. The engine prints this text instead of a generic
    "supervisor stage crashed".
    """


def _verify_wait_stop_reason(project_dir: Path, todo_id: str) -> str:
    """A41: the run-side reason to stop waiting for the verify decision.

    Read-only. No run file (a standalone ``awf start``) is not a stop
    verdict; :func:`run_state.read_run` already degrades a corrupt file to
    "no run", so the wait keeps waiting instead of dying on the check.
    """
    from . import run_state

    # ORCH M5.2: a service run keeps its stop surface in its OWN file
    # (``state/service-run.yaml``), and a CLOSED service run keeps
    # ``queue``/``current`` after ``awf_run_service_finish`` (QA-1). Which run
    # owns the unit NOW is ``run_state.owning_run``'s rule, next to the stop
    # rules themselves — the same answer the verdict surfaces get; this
    # function only applies it. The live owner decides, and only when none
    # exists may a closed owner stop the wait (the stage then has no live run
    # to answer to).
    owner = run_state.owning_run(project_dir, todo_id)
    if owner is None:
        return ""
    return run_state.stop_reason_for_unit(owner[1], todo_id)


def _publish_verify_waiting(
    project_dir: Path,
    todo_id: str,
    started_epoch: float,
    deadline_seconds: int,
    reason: str,
) -> None:
    """A41: the ``verify_waiting`` event in pipeline state (``awf status``).

    The wait is visible, not a frozen stage: the status surface reads this
    key and prints one line. A failed state write is already non-fatal in
    its owner (``pipeline_state._mutate_state_locked`` swallows the write
    failure family), so a broken status file cannot end the wait here.
    """
    from datetime import datetime, timezone

    from .pipeline_state import write_state

    event = {
        "todo_id": todo_id,
        "since": datetime.fromtimestamp(
            started_epoch, tz=timezone.utc
        ).isoformat(),
        "elapsed_seconds": max(0, int(time.time() - started_epoch)),
        "deadline_seconds": int(deadline_seconds),
        "reason": reason,
    }
    write_state(project_dir, verify_waiting=event)


def _clear_verify_waiting(project_dir: Path) -> None:
    """A41: drop the ``verify_waiting`` event once the decision arrived."""
    from .pipeline_state import write_state

    write_state(project_dir, verify_waiting=None)


def _wait_for_verify_decision(
    todo_id: str,
    project_dir: Path,
    logs_dir: Path,
    *,
    wait_timeout: int | None,
    context: str,
    wait_started: float | None = None,
) -> str:
    """A41: the ONE wait for the verify verdict in the auto stage.

    Both entry points land here: the supervisor subprocess never ran (no
    role file, skipped prompt) and it ran but left no verdict (the run-2
    incident). In a run the owner's ``awf_approve`` is the answer either
    way, so neither case may return "" and let the engine exit rc=1.

    ``wait_started`` anchors decision freshness to the stage's spawn — a
    verdict written while the subprocess was still busy counts. ``context``
    names the path in the log line so the two callers stay distinguishable.
    """
    from .pipeline_state import read_state

    prev = read_state(project_dir) or {}
    signal = wait_for_supervisor_signal(
        "verify",
        todo_id,
        project_dir,
        logs_dir,
        timeout=wait_timeout or _safe_supervisor_timeout(),
        wait_started=wait_started,
        publish_waiting=True,
        accepted_decision=prev.get("accepted_decision"),
        accepted_decision_mtime=prev.get("accepted_decision_mtime"),
    )
    _clear_verify_waiting(project_dir)
    _log(
        logs_dir,
        f"A41: verify decision received while waiting ({context}): {signal}",
    )
    return signal


def wait_for_supervisor_signal(
    kind: str,
    todo_id: str,
    project_dir: Path,
    logs_dir: Path,
    poll_interval: int = 3,
    timeout: int = 3600,
    wait_started: float | None = None,
    publish_waiting: bool = False,
    accepted_decision: str | None = None,
    accepted_decision_mtime: float | None = None,
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

    A41: ``wait_started`` anchors the freshness to an earlier moment (the
    auto verify stage's spawn), so a decision written by the supervisor
    subprocess before it exited is still the answer — the engine waits for
    the decision regardless of whether that subprocess ran. With
    ``publish_waiting`` the wait writes the ``verify_waiting`` event to
    pipeline state (``awf status``) and checks the run's own stop surface
    (stop flag, budget, closed run) on every poll; such a stop raises
    :class:`VerifyWaitStopped` with the cause instead of ``rc=1``.
    """
    import time

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    started_epoch = time.time()
    waiting_event = bool(publish_waiting and kind == "verify" and todo_id)
    if waiting_event:
        _publish_verify_waiting(
            project_dir, todo_id, started_epoch, timeout, "waiting"
        )

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
    # A41 anchors the mtime comparison to the caller's spawn, so a decision
    # written before this wait started is not stale.
    wall_start = time.time() if wait_started is None else float(wait_started)
    deadline = start + timeout
    last_log = start
    # AUD04-04: filenames already logged as stale, to keep the poll quiet.
    stale_decision_logged: set[str] = set()
    # AUD11-03: log the evidence-gate rejection once per wait.
    evidence_gate_logged = False
    # REPORTS29: pre-existing APPROVEs whose binding mismatch was already
    # logged with the re-approve instruction (once per wait).
    stale_binding_logged: set[str] = set()
    # U6b: decisions recorded as accepted in a killed cycle, logged once.
    stale_record_logged: set[str] = set()

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
            # AUD11-03: inside an ACTIVE run an approve additionally needs
            # the evidence file (RUN-EVIDENCE-{todo}.md) — a bare ACK/APPROVE
            # is the bypass the run protocol exists to prevent. Recomputed
            # each poll: the run can finish (or the evidence arrive) mid-wait.
            from . import commit_plan

            evidence_ok = _run_evidence_ok(project_dir, todo_id)
            for sig_path in (
                inbox / f"ACK-{todo_id}.ready",
                inbox / f"APPROVE-{todo_id}.ready",
            ):
                if not sig_path.exists():
                    continue
                signal_name = sig_path.stem
                if decision_is_stale(
                    sig_path,
                    signal_name,
                    accepted_decision,
                    accepted_decision_mtime,
                ):
                    # U6b: the same physical file the engine already
                    # accepted in a killed cycle — a leftover, not a new
                    # verdict. Keep waiting for a genuine decision.
                    if sig_path.name not in stale_record_logged:
                        stale_record_logged.add(sig_path.name)
                        _log(
                            logs_dir,
                            f"U6b: {sig_path.name} is the decision a killed "
                            "cycle already accepted — ignored, waiting for "
                            "a fresh verdict",
                        )
                    continue
                if not _decision_signal_fresh(
                    sig_path, wall_start, stale_decision_logged, logs_dir
                ):
                    # REPORTS29 (TODO-0169): the mtime gate rejected this
                    # file (older than this wait — the pipeline died between
                    # the approval and its consumption). For an APPROVE, a
                    # binding that still matches the CURRENT attempt (the
                    # same validator the commit gate enforces, M2.1) is a
                    # live decision: the tree it approved is the tree that
                    # will be committed, and re-approval would republish the
                    # same binding. A mismatched (or absent) binding stays
                    # ignored — U6b/AUD04-04.
                    if sig_path.name.startswith("APPROVE-"):
                        status, reason = commit_plan.preexisting_approval_status(
                            project_dir,
                            todo_id,
                            commit_plan.read_baseline_sha(project_dir, todo_id),
                        )
                        if status == "valid":
                            _log(
                                logs_dir,
                                f"REPORTS29: pre-existing "
                                f"APPROVE-{todo_id}.ready matches the "
                                "current attempt — consuming it without "
                                "re-approval",
                            )
                            return sig_path.stem
                        if sig_path.name not in stale_binding_logged:
                            stale_binding_logged.add(sig_path.name)
                            _log(
                                logs_dir,
                                f"REPORTS29: APPROVE-{todo_id}.ready found, "
                                "but it does not match the current attempt "
                                f"— re-approve (awf_approve): {reason}",
                            )
                    continue
                if not evidence_ok:
                    if not evidence_gate_logged:
                        evidence_gate_logged = True
                        _log(
                            logs_dir,
                            f"AUD11-03: {sig_path.name} ignored — active run "
                            f"requires RUN-EVIDENCE-{todo_id}.md "
                            "(awf_approve with evidence=)",
                        )
                    continue
                _log(logs_dir, f"BD-30: interactive supervisor signal detected: {sig_path.name}")
                return sig_path.stem
            review = outbox / f"REVIEW-{todo_id}.md"
            if (
                review.exists()
                and not decision_is_stale(
                    review,
                    f"REVIEW-{todo_id}",
                    accepted_decision,
                    accepted_decision_mtime,
                )
                and _decision_signal_fresh(
                    review, wall_start, stale_decision_logged, logs_dir
                )
            ):
                _log(logs_dir, f"BD-30: interactive supervisor signal detected: REVIEW-{todo_id}.md")
                return f"REVIEW-{todo_id}"

        if kind == "replan" and inbox.is_dir() and todo_id:
            # Day-2 B2: the replan supervisor answers AFTER the escalation —
            # accept only TODO signals created since the wait started. The old
            # orphan-pickup instantly "completed" the wait with the very TODO
            # being retried (its .ready pre-dated the escalation), and the
            # pipeline stopped as if the supervisor had answered nothing.
            #
            # TODO-0213 (BD-30): freshness alone was still a false positive —
            # it caught ANY new TODO, and a mid-run dispatch (run_revise
            # append) is a normal scenario, so an unrelated unit hijacked the
            # live pipeline. A new TODO is the replan answer ONLY when its
            # front-matter carries the declared marker
            # ``replan_for: <todo_id>`` naming the unit being replaced.
            # Unmarked TODOs do not touch the live pipeline — the escalation
            # keeps waiting for its own answer (or times out).
            from .unit_contract import replan_for_of

            for todo_sig in sorted(inbox.glob("TODO-*.ready")):
                try:
                    if todo_sig.stat().st_mtime <= wall_start:
                        continue
                except OSError:
                    continue
                sig = todo_sig.stem
                if sig == todo_id:
                    continue  # the unit being retried is not its own replan
                md_path = inbox / f"{sig}.md"
                try:
                    md_text = (
                        md_path.read_text(encoding="utf-8", errors="replace")
                        if md_path.is_file()
                        else None
                    )
                except OSError:
                    md_text = None
                target = replan_for_of(md_text)
                if target != todo_id:
                    continue
                _log(
                    logs_dir,
                    f"BD-30: replan signal detected: {sig} "
                    f"(replan_for={target}, replacing {todo_id})",
                )
                return sig

        now = time.monotonic()
        if waiting_event:
            stop_reason = _verify_wait_stop_reason(project_dir, todo_id)
            if stop_reason:
                _publish_verify_waiting(
                    project_dir, todo_id, started_epoch, timeout, stop_reason
                )
                _log(
                    logs_dir,
                    f"A41: verify_waiting — run stopped while waiting: "
                    f"{stop_reason}",
                )
                raise VerifyWaitStopped(
                    f"verify wait on {todo_id} stopped: {stop_reason}"
                )
        if now - last_log >= deadline_log_interval:
            waited = int(now - start)
            _log(
                logs_dir,
                f"BD-30: interactive supervisor still waiting for {kind} signal "
                f"({waited}s elapsed, deadline in {int(deadline - now)}s)",
            )
            if waiting_event:
                _publish_verify_waiting(
                    project_dir, todo_id, started_epoch, timeout, "waiting"
                )
                _log(
                    logs_dir,
                    f"A41: verify_waiting for {todo_id} — no decision yet "
                    f"({waited}s elapsed, deadline in {int(deadline - now)}s)",
                )
            last_log = now

        if now >= deadline:
            waited = int(now - start)
            _log(logs_dir, f"BD-30: supervisor signal timeout after {waited}s (kind={kind})")
            if waiting_event:
                _publish_verify_waiting(
                    project_dir, todo_id, started_epoch, timeout, "deadline"
                )
                _log(
                    logs_dir,
                    f"A41: verify_waiting — no decision within {timeout}s "
                    f"(deadline); the run stops on an explicit event, "
                    f"approve with awf_approve and resume with awf_continue",
                )
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
        if _run_evidence_ok(project_dir, todo_id):
            print(f"SIGNAL TO CREATE: {inbox}/ACK-{todo_id}.ready")
            print(f"  (or write REVIEW to: {outbox}/REVIEW-{todo_id}.md)")
        else:
            # AUD11-03: inside an active run the file-based ACK is ignored
            # by the engine — the approve path is awf_approve(evidence=...).
            print("RUN MODE: an active run is in progress — a hand-made ACK without")
            print(f".agentic/context/RUN-EVIDENCE-{todo_id}.md is IGNORED by the engine.")
            print(f"APPROVE VIA: awf_approve(todo_id=\"{todo_id}\", evidence=...) — evidence")
            print("  = the commands you actually ran + your verdict.")
            print(f"REJECT AS USUAL: write REVIEW-{todo_id}.md to {outbox}/")
    else:
        print(f"STAGE: {kind}")
        print()
        print("This is an internal supervisor path (replan/salvage).")
        print("Read supervisor.md for guidance, then decide:")
        if kind == "replan":
            print("  - replan: create a new refined TODO-NNNN.md + .ready signal,")
            print(f"    marked with `replan_for: {todo_id}` in its front-matter contract")
            print("    block — that marker names the unit being replaced; only a")
            print("    marked TODO is picked up (an unmarked mid-run dispatch is")
            print("    not your answer and does not switch the pipeline)")
        print("  - salvage: review the current state and either ACK or REVIEW")
        print()
        if kind == "salvage":
            print(f"Salvage details: read {inbox}/SALVAGE-{todo_id}.md — it lists what")
            print("happened and, for repeat attempts, what to change before retrying.")
            print()
            if not _run_evidence_ok(project_dir, todo_id):
                print("RUN MODE: an active run is in progress — a hand-made ACK without")
                print(f".agentic/context/RUN-EVIDENCE-{todo_id}.md is IGNORED by the engine.")
                print(f"APPROVE VIA: awf_approve(todo_id=\"{todo_id}\", evidence=...) instead.")
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
        print("  1. Read the pack verdict: .agentic/context/GATES-NNNN.md")
        print("     (facts are already collected — the pack ran the checks)")
        print("  2. Check git diff — changes must be in source files")
        print("  3. Decide: continue / fix / rollback")
        print("  4. If approved: create .agentic/inbox/ACK-NNNN.ready")
    else:
        print(f"What to do ({kind}): see supervisor.md instructions")

    print()
    wait_timeout: int | None = None
    if kind == "verify":
        # A41: the auto verify stage waits for the decision under the SAME
        # stage deadline the rest of the stage obeys (stage key → project
        # key → 3600), instead of returning rc=1 when the subprocess ends
        # without a verdict.
        from . import stage_timeout as _stage_timeout

        try:
            wait_timeout = _stage_timeout.resolve(None, stage.timeout, config).seconds
        except AwfApiError as e:
            # The stage key is validated at pipeline load and a bad project
            # key refuses the launch, so this is a hand-built Stage only.
            # A bounded wait still beats an uncaught AwfApiError (the
            # engine's crash branch catches RuntimeError, not this one) —
            # but never silently: the cause goes to the log.
            _log(
                logs_dir,
                f"A41: {e} — verify wait capped at "
                f"{_stage_timeout.DEFAULT_SECONDS}s",
            )
            wait_timeout = _stage_timeout.DEFAULT_SECONDS
    return run_supervisor_via_subprocess(
        kind, todo_id, project_dir, config, phases_file, logs_dir, pipeline_name,
        wait_timeout=wait_timeout,
    )


def _replan_budget_chars() -> int:
    """A32/A34: the engine's existing cap for text handed to a role.

    The value is ``pipeline_engine.FIX_ROUND_MAX_CHARS`` — the cap A7
    already uses for the same kind of text. ``awf.rejection`` owns the
    accessor (it owns the prompt), this wrapper stays here so the stage can
    substitute a budget in tests without reaching across modules.
    """
    from .rejection import replan_budget_chars

    return replan_budget_chars()


def build_replan_context(
    todo_id: str, project_dir: Path, logs_dir: Path | None = None
) -> tuple[str, list[str]]:
    """A32/A34: the bounded replan prompt (owner: :mod:`awf.rejection`).

    The stage keeps this thin call so the budget stays substitutable here.
    """
    from .rejection import build_replan_context as _build

    return _build(todo_id, project_dir, logs_dir, budget=_replan_budget_chars())


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
        # A42: pack facts inline, handoffs out. The prompt is bounded and
        # carries the verdict; every attachment vanished except the role
        # file and the doctrine, which the spawn adds by itself.
        from .verify_context import build_verify_prompt

        prompt, dropped = build_verify_prompt(
            todo_id,
            project_dir,
            config=config,
            pipeline_name=pipeline_name,
            logs_dir=logs_dir,
        )
        if dropped:
            print(f"  verify context cut: {', '.join(dropped)}")

    elif kind == "replan":
        if not todo_id:
            print("[auto mode] No todo_id for replan — skip.")
            _log(logs_dir, "Supervisor replan auto-skipped (no todo_id)")
            return None
        # A32/A34: the replan prompt is bounded and carries exactly three
        # sources — the reviewer's report, the current TODO, the report of
        # the stage that slid. Handoff history and DONE archives never
        # enter (that is what blew the context window on 01.11). The
        # closing instruction is part of the budgeted text (one builder).
        prompt, dropped = build_replan_context(todo_id, project_dir, logs_dir)
        if dropped:
            print(f"  replan context cut: {', '.join(dropped)}")

    elif kind == "salvage":
        print("[auto mode] salvage not automated — skipping.")
        _log(logs_dir, "Supervisor salvage auto-skipped (not automatable)")
        return None

    else:
        print(f"[auto mode] Unknown kind {kind!r} — skipping.")
        _log(logs_dir, f"Supervisor stage {kind} auto-skipped (unknown kind)")
        return None

    return extra_files, prompt


def _replan_marker_of(inbox: Path, todo_sig: str) -> str | None:
    """TODO-0213 (REVIEW fix): the ``replan_for`` marker of an inbox TODO.

    Reads the TODO's .md (no file / unreadable → None, never raises).
    Shared by the auto-subprocess watcher filter and the fallback signal
    detection — one marker read, one semantics (unit_contract.replan_for_of).
    """
    from .unit_contract import replan_for_of

    md_path = inbox / f"{todo_sig}.md"
    try:
        md_text = (
            md_path.read_text(encoding="utf-8", errors="replace")
            if md_path.is_file()
            else None
        )
    except OSError:
        md_text = None
    return replan_for_of(md_text)


def make_replan_glob_accept(inbox: Path, todo_id: str):
    """TODO-0213 (REVIEW fix): the replan watcher's accept filter.

    A new TODO-*.ready counts as the replan answer only when its
    front-matter carries ``replan_for: <todo_id>`` — an unmarked mid-run
    dispatch (the 12.10 incident) must not end the wait. Returns a
    ``new_glob_accept`` predicate for ``run_subprocess_until_signal``.
    """

    def _accept(watch_dir: Path, name: str) -> bool:
        stem = name[: -len(".ready")] if name.endswith(".ready") else name
        return _replan_marker_of(watch_dir, stem) == todo_id

    return _accept


def _log_replan_pickup(
    logs_dir: Path,
    kind: str,
    signal_name: str,
    todo_id: str,
    inbox: Path,
) -> None:
    """TODO-0213 (REVIEW fix): the auto-subprocess pickup log names the
    marker and the replaced unit — requirement 4, extended to this path
    (the interactive wait already logs the same line)."""
    if kind != "replan" or not signal_name or signal_name == todo_id:
        return
    target = _replan_marker_of(inbox, signal_name) or ""
    _log(
        logs_dir,
        f"BD-30: replan signal detected: {signal_name} "
        f"(replan_for={target}, replacing {todo_id})",
    )


def _verify_grace_seconds(config: dict, logs_dir: Path) -> float:
    """A35: how long the verify subprocess may outlive its own verdict.

    Reads ``wait.verify_grace_seconds`` (default
    ``VERIFY_GRACE_DEFAULT``). A non-number or a negative window falls back
    to the default with a log line: a negative window would mean "give up
    before the signal" — nonsense — and a silent endless wait would restore
    the very deadlock A35 removes.
    """
    raw = cfg_mod.get(config, "wait.verify_grace_seconds", VERIFY_GRACE_DEFAULT)
    try:
        grace = float(raw)
    except (TypeError, ValueError):
        _log(
            logs_dir,
            f"wait.verify_grace_seconds={raw!r} is not a number — "
            f"using {VERIFY_GRACE_DEFAULT:g}s",
        )
        return VERIFY_GRACE_DEFAULT
    if grace < 0:
        _log(
            logs_dir,
            f"wait.verify_grace_seconds={raw!r} is negative — "
            f"using {VERIFY_GRACE_DEFAULT:g}s",
        )
        return VERIFY_GRACE_DEFAULT
    return grace


def _human_decision_line(signal: str, mtime: float, outcome: str) -> str:
    """A29: the one log format for "the owner's verdict was taken"."""
    return f"verify decision: human ({signal}, mtime {int(mtime)}) — {outcome}"


def _prefer_human_verdict(
    kind: str,
    todo_id: str,
    project_dir: Path,
    auto_signal: str,
    wait_start: float,
    logs_dir: Path,
) -> str:
    """A29: the owner's verdict wins over the auto subprocess's.

    Called with the subprocess's answer. A valid human decision for the
    same attempt replaces it and the swap is logged in the required form:
    ``verify decision: human (<signal>, mtime <t>) — auto verdict <s>
    ignored``. No decision, or the same one — the auto answer stands.
    """
    if kind != "verify" or not todo_id:
        return auto_signal
    human = human_verify_decision(project_dir, todo_id, wait_start, logs_dir)
    if human is None:
        return auto_signal
    signal, mtime = human
    if signal == auto_signal:
        # The scan picked the owner's own file up (the fallback reads REVIEW
        # first), so the file to overrule is not ``auto_signal`` — sweep the
        # other decisions by name, or the auto APPROVE stays in the inbox.
        overruled = overruled_auto_verdicts(project_dir, todo_id, signal)
        if overruled:
            line = _human_decision_line(
                signal, mtime, f"auto verdict {' + '.join(overruled)} ignored"
            )
            print(line)
            _log(logs_dir, line)
        discard_overruled(project_dir, todo_id, signal, logs_dir)
        return auto_signal
    line = _human_decision_line(
        signal, mtime, f"auto verdict {auto_signal or 'none'} ignored"
    )
    print(line)
    _log(logs_dir, line)
    discard_overruled(project_dir, todo_id, signal, logs_dir)
    return signal


def run_supervisor_via_subprocess(
    kind: str,
    todo_id: str,
    project_dir: Path,
    config: dict,
    phases_file: str,
    logs_dir: Path,
    pipeline_name: str | None = None,
    wait_timeout: int | None = None,
) -> str:
    """BD-14/29: spawn ``opencode run --auto [--agent <name>] --file supervisor.md``.

    A13: ``--agent`` is present only when ``models.supervisor.agent_name``
    names one — an unnamed role runs on the default agent (2.x rejects a
    made-up name).
    kind is one of: plan / verify / replan / salvage.
    salvage is NOT automatable — needs human judgement.

    Returns the signal name produced by supervisor subprocess (C1 fix):
    - plan/replan: new TODO-NNNN
    - verify: ACK-TODO-NNNN, APPROVE-TODO-NNNN, or REVIEW-TODO-NNNN
    - empty string if skipped.
    """
    # A29: the owner may have decided already. A valid human verdict for
    # this attempt means the subprocess has nothing to arbitrate — do not
    # spawn it, and hand the human answer to the engine. Read BEFORE the
    # role file: a project whose supervisor role is missing used to return
    # "" here, and the verify stage aborts the pipeline on an empty signal
    # — a green owner approval must not die on a missing prompt file.
    wait_start = time.time()
    if kind == "verify" and todo_id:
        human = human_verify_decision(project_dir, todo_id, wait_start, logs_dir)
        if human is not None:
            signal, mtime = human
            line = _human_decision_line(
                signal, mtime, "supervisor subprocess skipped"
            )
            print(line)
            _log(logs_dir, line)
            return signal

    try:
        role_file = resolve_role_file("supervisor", project_dir)
    except RuntimeError as e:
        print(f"[auto mode] supervisor.md not found — skipping. ({e})")
        _log(logs_dir, f"Supervisor stage {kind} auto-skipped (no supervisor.md)")
        if kind == "verify" and todo_id:
            # A41: no subprocess does not mean "no answer" — the owner's
            # awf_approve is still the verdict; wait for it like the spawned
            # path does instead of returning rc=1.
            return _wait_for_verify_decision(
                todo_id, project_dir, logs_dir,
                wait_timeout=wait_timeout,
                wait_started=wait_start,
                context="no subprocess",
            )
        return ""

    prepared = _prepare_supervisor_stage(
        kind, todo_id, project_dir, config, phases_file, logs_dir, pipeline_name
    )
    if prepared is None:
        if kind == "verify" and todo_id:
            return _wait_for_verify_decision(
                todo_id, project_dir, logs_dir,
                wait_timeout=wait_timeout,
                wait_started=wait_start,
                context="no subprocess",
            )
        return ""
    extra_files, prompt = prepared

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    agent_name = get_agent_name(config, "supervisor")
    cmd = [
        "opencode", "run", "--auto",
    ]
    # A13: an unnamed role runs on the default agent (2.x rejects a
    # made-up name), a named one gets exactly one flag.
    cmd += argv_agent_flag(config, "supervisor")
    cmd += [
        "--title", f"awf-supervisor-{kind}",
    ]
    role_model = get_role_model(config, "supervisor")
    if role_model:
        cmd += ["--model", role_model]
    cmd += ["--file", str(role_file)]
    # U9: doctrine section after the role's instructions and before the
    # stage's task context (extra_files) — the same position for every role.
    doctrine_file = materialize_doctrine(project_dir)
    if doctrine_file is not None:
        cmd += ["--file", str(doctrine_file)]
        print(f"  doctrine: {doctrine_file}")
        _log(logs_dir, f"U9: doctrine section injected: {doctrine_file}")
    for f in extra_files:
        cmd += ["--file", f]
    cmd += ["--", prompt]

    # K2: the supervisor spawn reads the SAME helper as the worker, so the
    # two argv builders can never drift (T1).
    from . import opencode_version

    plan = opencode_version.resolve_plan(
        config, opencode_version.probe_version()
    )
    if plan.warning:
        print(f"opencode: {plan.warning}")
        _log(logs_dir, f"opencode: {plan.warning}")
    cmd = opencode_version.apply_standalone(cmd, plan)

    print(f"[auto mode] Spawning supervisor subprocess: agent={agent_name}")
    print(f"  role: {role_file}")
    for f in extra_files:
        print(f"  ctx:  {f}")
    print()

    _log(logs_dir, f"Supervisor {kind} subprocess started (agent={agent_name})")

    watch_paths: list[Path] = []
    watch_new_glob: tuple[Path, str] | None = None
    new_glob_accept: Callable[[Path, str], bool] | None = None
    if kind in ("plan", "replan"):
        watch_new_glob = (inbox, "TODO-*.ready")
        # TODO-0213 (REVIEW fix): the replan watcher fires only on the
        # marked answer — an unmarked mid-run dispatch (the incident)
        # must not end the wait; the wait keeps running until the
        # supervisor writes a replan_for-marked TODO (or times out).
        if kind == "replan" and todo_id:
            new_glob_accept = make_replan_glob_accept(inbox, todo_id)
    elif kind == "verify" and todo_id:
        watch_paths = [
            inbox / f"ACK-{todo_id}.ready",
            inbox / f"APPROVE-{todo_id}.ready",
            outbox / f"REVIEW-{todo_id}.md",
        ]

    from ._env import awf_subprocess_env
    from .signal_watch import run_subprocess_until_signal

    signal_holder: dict[str, str] = {}
    log_holder: dict[str, str] = {}
    files_in_prompt = sum(1 for part in cmd if part == "--file")
    spawn_started = time.time()

    # TODO-0338 (A35): only the verify subprocess can deadlock with the
    # engine (it waits for the commit the engine waits to run). The other
    # kinds keep the old natural-exit wait.
    signal_exit_grace: float | None = None
    if kind == "verify":
        signal_exit_grace = _verify_grace_seconds(config, logs_dir)

    # RUN8 #2: same worker-pid record as the agent stage — the supervisor
    # subprocess is an opencode process in its own group and an
    # awf_kill mid-stage would orphan it the same way.
    # A44 (TODO-0359): the pid/group of this spawn — the reap needs them even
    # when the wait ends with an exception. The token is the env marker the
    # whole tree inherits, the only anchor that survives ``setsid``.
    anchor_token = worker_tree.new_anchor_token()
    anchor = worker_tree.SpawnAnchor(anchor_token)

    def _record_worker_pid(pid: int) -> None:
        anchor(pid)
        worker_tree.record_worker_state(
            project_dir, logs_dir, anchor, role="supervisor", todo_id=todo_id
        )

    # A44: name a previous stage's leftovers before this one adds more.
    worker_tree.log_stage_leftovers(project_dir, logs_dir)

    try:
        result = run_subprocess_until_signal(
            cmd,
            cwd=str(project_dir),
            watch_paths=watch_paths,
            watch_new_glob=watch_new_glob,
            new_glob_accept=new_glob_accept,
            logs_dir=logs_dir,
            # W7: role-aware env (the supervisor can also be listed in
            # automation.readonly_roles — same mechanism as worker roles).
            env=awf_subprocess_env(
                role="supervisor",
                project_dir=project_dir,
                # A44: the tree marker — ``setsid`` children keep it.
                stage_anchor=anchor_token,
            ),
            signal_holder=signal_holder,
            log_holder=log_holder,
            # U6a/U6c are worker-only (TODO-0017): the supervisor subprocess can
            # be legitimately long-silent (verify waits), so no watchdog; no
            # preflight either — keep the supervisor flow exactly as before.
            preflight_timeout=0,
            no_output_timeout=0,
            on_spawn=_record_worker_pid,
            signal_exit_grace=signal_exit_grace,
        )
    finally:
        # A44: plan/verify workers start MCP servers too — reap the tree on
        # both the normal and the exception path.
        worker_tree.reap_stage_tree(
            anchor, logs_dir, label=f"supervisor/{todo_id}"
        )

    _log(logs_dir, f"Supervisor {kind} subprocess finished (exit={result.returncode})")
    if kind == "verify":
        # A42: one narration line — prompt size, attachments, duration and
        # the shell calls the subprocess printed. "Slow" becomes a number.
        from .verify_context import (
            count_logged_commands,
            format_verify_timing,
            log_verify_timing,
        )

        try:
            offset = int(log_holder.get("log_start_offset", 0) or 0)
        except (TypeError, ValueError):
            offset = 0
        commands = count_logged_commands(log_holder.get("log_path"), offset)
        log_verify_timing(
            logs_dir,
            format_verify_timing(
                todo_id,
                prompt_chars=len(prompt),
                files=files_in_prompt,
                duration=max(0.0, time.time() - spawn_started),
                commands=commands,
            ),
        )
    if result.returncode != 0:
        if kind == "verify" and todo_id:
            # A41 (the run-2 incident): the verify subprocess can die after
            # ~100s (rc=1) without a verdict. That is not the run's verdict
            # — the owner's awf_approve still decides. Keep the stage
            # waiting (a verdict the dead subprocess DID write is picked up
            # by the watcher/fallback right below).
            _log(
                logs_dir,
                f"A41: verify subprocess failed (exit={result.returncode}) — "
                "engine keeps waiting for the decision",
            )
            print(
                f"Verify supervisor subprocess failed (exit={result.returncode}) "
                f"for {todo_id} — waiting for APPROVE (awf_approve) or REVIEW."
            )
        else:
            raise RuntimeError(
                f"Supervisor {kind} subprocess exited with code {result.returncode}. "
                f"Cmd: {' '.join(cmd)}"
            )

    fired = signal_holder.get("signal", "")
    if fired:
        signal_name = fired.rsplit(".", 1)[0] if "." in fired else fired
        _log(logs_dir, f"Supervisor {kind} produced signal (from watcher): {signal_name!r}")
        _log_replan_pickup(logs_dir, kind, signal_name, todo_id, inbox)
        return _prefer_human_verdict(
            kind, todo_id, project_dir, signal_name, wait_start, logs_dir
        )

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
        # AUD11-03: same run-mode evidence gate as the interactive wait.
        run_evidence_ok=_run_evidence_ok(project_dir, todo_id),
    )
    _log(logs_dir, f"Supervisor {kind} produced signal (fallback): {signal_name!r}")
    if not signal_name and kind == "verify" and todo_id:
        # A41: the auto supervisor subprocess ended without a verdict (the
        # run's supervisor answers out of band, via awf_approve). The stage
        # used to return "" here and the engine exited rc=1 — the decision
        # that arrived a moment later was orphaned. Wait exactly like the
        # interactive stage does; freshness is anchored to the spawn, so a
        # verdict written during the subprocess still counts (A35 grace
        # stays as it is).
        _log(
            logs_dir,
            f"A41: verify subprocess produced no decision for {todo_id} — "
            "engine keeps waiting (not rc=1)",
        )
        print(
            f"No verify decision yet for {todo_id} — waiting for "
            "APPROVE (awf_approve) or REVIEW."
        )
        signal_name = _wait_for_verify_decision(
            todo_id, project_dir, logs_dir,
            wait_timeout=wait_timeout,
            wait_started=wait_start,
            context="subprocess left no verdict",
        )
    _log_replan_pickup(logs_dir, kind, signal_name, todo_id, inbox)
    return _prefer_human_verdict(
        kind, todo_id, project_dir, signal_name, wait_start, logs_dir
    )


def _detect_supervisor_signal(
    kind: str,
    todo_id: str,
    inbox: Path,
    outbox: Path,
    accepted_decision: str | None = None,
    accepted_decision_mtime: float | None = None,
    run_evidence_ok: bool = True,
) -> str:
    """C1 fix: detect which signal the supervisor actually produced.

    For verify: prefers REVIEW (rejection) over ACK/APPROVE — if supervisor
    wrote REVIEW-{todo_id}.md, that's the most recent decision and should
    override any stale ACK.

    U6b: existence alone is NOT acceptance. ``accepted_decision``/
    ``accepted_decision_mtime`` (from the pipeline state) carry the decision
    the engine already accepted in a previous cycle; a decision file matching
    that record at the same or older mtime is the leftover of a killed cycle
    and is rejected (see ``arbiter.decision_is_stale``). A pre-approval with no record
    (BD-8) and a fresh re-approval (newer mtime) are accepted as before.

    AUD11-03: ``run_evidence_ok`` is the run-mode gate — inside an active
    run, ACK/APPROVE are detected only when the evidence file exists
    (see _run_evidence_ok). REVIEW is never gated.
    """
    if kind == "verify" and todo_id:
        # Check REVIEW first (most recent decision wins)
        review = outbox / f"REVIEW-{todo_id}.md"
        if review.exists() and not decision_is_stale(
            review, f"REVIEW-{todo_id}", accepted_decision, accepted_decision_mtime
        ):
            return f"REVIEW-{todo_id}"
        if not run_evidence_ok:
            return ""
        # Then ACK and APPROVE
        ack = inbox / f"ACK-{todo_id}.ready"
        if ack.exists() and not decision_is_stale(
            ack, f"ACK-{todo_id}", accepted_decision, accepted_decision_mtime
        ):
            return f"ACK-{todo_id}"
        approve = inbox / f"APPROVE-{todo_id}.ready"
        if approve.exists() and not decision_is_stale(
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
        #
        # TODO-0213 (REVIEW fix): for replan, newest-by-mtime was the same
        # false positive the marker removed from the interactive wait — a
        # fresh UNMARKED TODO (a normal mid-run dispatch) outranked the
        # blocked unit's stale .ready and hijacked the pipeline. A replan
        # REPLACES the blocked unit: only a TODO whose front-matter names
        # it (replan_for: <todo_id>) is the answer; the unit's own .ready
        # never is (a replan is never its own answer).
        todos = sorted(inbox.glob("TODO-*.ready"), key=lambda p: p.stat().st_mtime, reverse=True)
        for todo_path in todos:
            sig = todo_path.stem  # "TODO-NNNN.ready" → "TODO-NNNN"
            if kind == "replan" and (
                sig == todo_id or _replan_marker_of(inbox, sig) != todo_id
            ):
                continue  # TODO-0213: not the replan answer
            return sig
    return ""
