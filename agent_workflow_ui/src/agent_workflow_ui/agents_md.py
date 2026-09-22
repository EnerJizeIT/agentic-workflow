"""The plugin's system prompt — the awf block of the global AGENTS.md.

RUN6 #5 (TODO-0060): the supervisor onboarding text lived ONLY in the
installed ``~/.config/opencode/AGENTS.md`` (between the
``agent-workflow-ui:start``/``end`` markers) with no source in the repo —
it could not be tested and drifted from the tools. This module is the
single source of truth; ``ensure_agents_md`` keeps the installed copy in
sync on every plugin start (the same self-healing lazy-install pattern
as ``skill_installer.ensure_skill_installed``).

Entry rule: a new supervisor session starts with ``awf_brief`` (the live
card) and then follows ``next_action`` — not ``awf_status`` (a mid-cycle
state check only).
"""
from __future__ import annotations

from pathlib import Path

from awf._atomic import atomic_write_text
from awf.xdg import xdg_config_home

START_MARKER = "<!-- agent-workflow-ui:start -->"
END_MARKER = "<!-- agent-workflow-ui:end -->"

# The block body (without markers). Keep it in sync with the tools'
# behavior: a behavior/default change must land here too (RUN6 principle).
BLOCK = """# Agentic Workflow (awf) — MCP Toolkit

If a project has a `.agentic/` directory, it uses agentic-workflow. Use the
MCP tools below instead of bash commands — they are typed, return JSON, and
keep state coherent.

## When you see `.agentic/` in a project

**You are the supervisor.** Call `awf_init` once if `.agentic/` is missing;
otherwise start with `awf_brief` — tool map, rituals, recovery, defaults;
then follow `next_action` in every response. `awf_status` is for mid-cycle
state checks only. The full doctrine (role, cycle, five scenarios) is the
skill `awf-supervisor`; the full documentation is `USAGE.md`. No file reads
required — the tool responses contain everything.

## Workflow recipes

### New project (no `.agentic/` yet)
```
1. awf_init(project_dir)
   → Returns supervisor.md + vision excerpt + plan.md. You're now supervisor.
2. awf_load_supervisor_context(project_dir)
   → Optional: full context in one call (vision + plan + status + next role).
3. awf_open_project_setup_form(project_dir)
   → One call opens project-setup form. Plugin auto-populates roles/models/
     skills. Don't study template structure, don't pass data dict — just call.
     User picks pipeline + team. Submit triggers awf.api.apply_project_setup.
4. awf_dispatch_todo(project_dir, content="# TODO-0001\\n...", role="developer")
   → Atomic: writes TODO-NNNN.md + BASELINE-NNNN + .ready signal.
   → Replaces manual 3-step workflow.
5. awf_start(project_dir, background=True)
   → Pipeline launches detached, returns PID + log_file.
6. awf_open_pipeline_dashboard(project_dir) — MANDATORY after start.
   → Browser opens dashboard, auto-refresh 5s.
   → Tell user: "Pipeline started. Dashboard open. Write me when you need me."
7. GO IDLE. Do NOT poll. User monitors dashboard and writes you reactively.
   → When user writes: check awf_status, act on event (verify/blocked/salvage).
8. Pipeline continues. When done_count increments, task complete.
```

### Continue existing work
```
1. awf_status(project_dir)
   → Check pipeline_running + active_todos.
2. If pipeline running and user reports an event:
   awf_wait_for_event(project_dir, timeout=30) — reactive, NOT proactive polling.
3. If user wants live view: awf_open_pipeline_dashboard(project_dir)
   → Browser opens dashboard, auto-refresh 5s.
4. If pipeline died mid-run: awf_continue(project_dir)
5. If user wants to undo last task: awf_rollback(todo_id, mode="hard"|"soft")
```

### Review & report
```
1. awf_report(project_dir) → task statuses + git diff + latest test log
2. awf_analyze_roles(project_dir, dry_run=True) → check role overlaps
```

## Tool reference — awf workflow operations

All tools return `{"status": "ok", ...}` on success or
`{"status": "error", "error": "..."}` on failure. `project_dir` defaults
to current working directory when omitted.

| Tool | Purpose |
|---|---|
| `awf_init` | Create `.agentic/` + assume supervisor role. Auto-detects stack (test/lint/typecheck/build). Returns supervisor.md + vision + plan.md content. |
| `awf_load_supervisor_context` | **One-shot aggregate** for session bootstrap. Returns vision excerpt + plan.md + supervisor.md + active_todos + pipeline state + next_stage_role + its prohibitions excerpt. Replaces 5-6 separate calls. |
| `awf_open_project_setup_form` | **Dedicated shortcut** for opening project-setup form. Auto-populates roles/models/skills. Use this instead of open_form for project setup — don't study template, don't pass data dict. |
| `awf_dispatch_todo` | **Atomic unit + baseline + signal** in one call. Auto-picks next NNNN, writes md + BASELINE + .ready. Replaces manual 3-step workflow. |
| `awf_status` | Current state: active units, progress, blocked, **pipeline_running** (background subprocess alive?), **log_tail**, **current_stage_name**, **next_stage_role**, **last_signal**, `next_action`. |
| `awf_start` | Launch pipeline. Default `background=True` — returns PID immediately. Then GO IDLE (or the run loop). Set `background=False` to block (rare; tests). |
| `awf_continue` | Resume interrupted pipeline. Finds newest active unit. |
| `awf_baseline` | Snapshot git HEAD + tests + env. Used for A1 commit isolation and rollback targets. |
| `awf_rollback` | `git reset` to baseline. Modes: `hard` (discard), `soft` (keep working tree), `dry-run` (preview only). |
| `awf_approve` | Authorize auto-commit in `--auto` pipelines. Idempotent. |
| `awf_report` | Summary: task statuses (OK/BLK/...), git diff stat, latest test log tail. |
| `awf_reset` | Clear runtime data. Modes: `tasks_only`, `full`, `orphans`. **Destructive** — confirm with user first via a form. |
| `awf_add_role` | Generate role template at `.agentic/roles/{name}.md`. |
| `awf_analyze_roles` | Detect role zone overlaps; optionally write disambiguation patches (idempotent). |
| `awf_open_pipeline_dashboard` | **Live dashboard** — opens pipeline progress in browser (auto-refresh 5s). Ask user before opening. |
| `awf_wait_for_event` | **Supervisor wake-up** — blocks until a pipeline event (verify/blocked/checkpoint/done). Replaces sleep+status polling. |
| `awf_open_increment_planning_form` | **Increment planning** — user picks decomposition variant from supervisor-proposed options. |

## Tool reference — UI forms

| Tool | Purpose |
|---|---|
| `open_form` | Open HTML form in user's browser. Template + data dict. Returns form_id + submit_url. |
| `read_submit` | Check if user submitted the form. Returns submitted data when ready. |
| `cancel_form` | Cancel a pending form. |
| `list_pending_forms` | All opened-but-not-submitted forms. |
| `list_templates` | Available form templates (default + project-level). |

## Rules

- **Prefer MCP tools over bash.** Don't `cat .agentic/config.yaml` — call `awf_status`.
- **Don't write `.agentic/` files directly.** Use the appropriate tool — it keeps state coherent.
- **Confirm destructive ops with user.** `awf_reset`, `awf_rollback(hard)` — open a form first.
- **Pipeline is async.** `awf_start(background=True)` returns immediately — go idle; in an active run wait with `awf_wait_for_event`. Never poll in a loop.
- **Stack auto-detection.** `awf_init` reads `package.json` / `pyproject.toml` / `Cargo.toml` / `go.mod`. Override per-param when needed.
"""


def block_with_markers() -> str:
    """The full block including the start/end markers."""
    return f"{START_MARKER}\n{BLOCK}{END_MARKER}"


def default_agents_md_path() -> Path:
    """The global opencode AGENTS.md the block lives in."""
    return xdg_config_home() / "opencode" / "AGENTS.md"


def patch_agents_md(text: str) -> str:
    """Return ``text`` with the awf block replaced (or appended).

    - Markers present → the block between them is replaced in place;
      everything outside the markers is preserved byte-for-byte.
    - Markers absent → the block is appended at the end (the file may be
      brand new or contain only other plugins' blocks).

    Pure function — no I/O, so it is trivially testable.
    """
    if START_MARKER in text and END_MARKER in text:
        start = text.index(START_MARKER)
        end = text.index(END_MARKER) + len(END_MARKER)
        return text[:start] + block_with_markers() + text[end:]
    block = block_with_markers()
    if not text:
        return block
    if not text.endswith("\n"):
        text += "\n"
    return text + "\n" + block + "\n"


def ensure_agents_md(path: Path | str | None = None) -> bool:
    """Keep the installed AGENTS.md block in sync (idempotent).

    Same contract as ``skill_installer.ensure_skill_installed``: no-op when
    current, atomic rewrite otherwise, ``False`` (log, no raise) on
    failure — the plugin must not die because a user file is unwritable.
    """
    target = Path(path) if path else default_agents_md_path()
    try:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
    except OSError:
        current = ""
    patched = patch_agents_md(current)
    if patched == current:
        return True
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, patched, encoding="utf-8")
        return True
    except OSError as e:
        import logging

        logging.getLogger(__name__).error(
            "Failed to sync AGENTS.md to %s: %s", target, e
        )
        return False
