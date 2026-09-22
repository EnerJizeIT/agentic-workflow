"""Lifecycle API: init, status, report, reset, orphans.

All functions return a Result dataclass (see :mod:`awf.api._results`)
or raise :class:`awf.api.AwfApiError` with a human-readable message.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config as cfg_mod
from .. import paths, run_state, todos
from .._atomic import atomic_write_text
from ._background import check_pipeline_running
from ._errors import AwfApiError
from ._helpers import read_file_text, require_agentic, require_git_repo
from ._results import (
    InitResult,
    ReportResult,
    ResetResult,
    RestoreResult,
    StatusResult,
)
from ._stack import derive_project_name, detect_stack
from ._templates import _CONFIG_TEMPLATE, update_gitignore

# ─── init_project ───────────────────────────────────────────────────────

_RUNTIME_DIRS = [
    "inbox", "outbox", "handoff", "done", "state",
    "logs", "context", "dashboards", "inputs",
]


def _clean_runtime(project_dir: Path) -> list[str]:
    """R1: Remove runtime directories, preserve config. Returns cleaned list."""
    agentic = project_dir / ".agentic"
    cleaned: list[str] = []
    for name in _RUNTIME_DIRS:
        d = agentic / name
        if d.exists():
            shutil.rmtree(d)
            cleaned.append(name)
    return cleaned


def init_project(
    project_dir: Path,
    *,
    force: bool = False,
    project_name: str | None = None,
    test_cmd: str | None = None,
    lint_cmd: str | None = None,
    typecheck_cmd: str | None = None,
    build_cmd: str | None = None,
    dry_run: bool = False,
) -> InitResult:
    """Initialize ``.agentic/`` skeleton in project_dir.

    All command parameters are auto-detected via :func:`detect_stack` when
    not provided explicitly. Project name is derived from directory when
    not provided.

    **R1 (AUD05-04):** when ``.agentic/`` already exists and
    ``force=False``, the runtime directories (inbox/outbox/handoff/done/
    state/logs/context/dashboards/inputs) are deleted and config.yaml is
    preserved — calling this on a live project drops active TODOs and
    state. ``dry_run=True`` is a pure read and changes nothing.

    Returns :class:`InitResult` with supervisor.md, vision excerpt, plan.md
    content — caller (CLI/MCP) has everything needed to assume the supervisor
    role without further file reads.
    """
    project_dir = Path(project_dir).resolve()
    require_git_repo(project_dir)

    agentic = project_dir / ".agentic"
    if agentic.exists() and not force:
        if dry_run:
            # AUD05-01: dry-run is a pure read — return BEFORE _clean_runtime.
            config = cfg_mod.load(project_dir)
            project_name_val = config.get("project", {}).get("name", project_dir.name)
            vision_path = paths.find_vision_file(project_dir)
            return InitResult(
                project_name=project_name_val,
                project_dir=str(project_dir),
                stack="(preserved)",
                vision_path=str(vision_path) if vision_path else None,
                vision_excerpt="",
                supervisor_md="",
                plan_md="",
                pipeline_configured=bool(config.get("default_pipeline")),
                next_action="[dry-run] .agentic/ already exists — nothing written, runtime untouched.",
                warnings=[],
            )
        # R1: Clean runtime dirs, preserve config.
        cleaned = _clean_runtime(project_dir)
        config = cfg_mod.load(project_dir)
        project_name_val = config.get("project", {}).get("name", project_dir.name)
        vision_path = paths.find_vision_file(project_dir)
        phases_file = cfg_mod.get(config, "phases.current", ".agentic/phases/plan.md")
        supervisor_md_path = project_dir / ".agentic" / "roles" / "supervisor.md"
        plan_md = ""
        plan_path = project_dir / phases_file if not Path(phases_file).is_absolute() else Path(phases_file)
        if plan_path.is_file():
            plan_md = plan_path.read_text(encoding="utf-8")
        # SMO: detect phase for compact prompt + next_action
        try:
            from ..phase import detect_phase, get_phase_prompt
            phase_r1 = detect_phase(project_dir)
            supervisor_md = get_phase_prompt(phase_r1, project_dir)
        except Exception:
            phase_r1 = "goal"
            supervisor_md = supervisor_md_path.read_text(encoding="utf-8") if supervisor_md_path.is_file() else ""

        _NEXT_ACTIONS_R1 = {
            "goal": "Спроси пользователя о цели сессии. После ответа — awf_set_goal.",
            "form": "Открой project-setup форму через awf_open_project_setup_form.",
            "normalize": "Выполни normalize checklist, затем awf_confirm_normalized.",
            "brief": "Изучи vision и план, напиши TODO-NNNN.md (awf_dispatch_todo) и запусти пайплайн.",
            "run": "Проверь awf_status, при необходимости dispatch_todo + awf_start.",
            "verify": "Проверь handoffs + git diff, реши ACK или REVIEW.",
            "done": "Pipeline завершён. Спроси пользователя о следующем шаге.",
        }
        next_action_r1 = _NEXT_ACTIONS_R1.get(
            phase_r1,
            f"Runtime cleaned ({', '.join(cleaned)}). Config preserved. "
            "Pipeline ready — use awf_dispatch_todo to start next iteration.",
        )
        return InitResult(
            project_name=project_name_val,
            project_dir=str(project_dir),
            stack="(preserved)",
            vision_path=str(vision_path) if vision_path else None,
            vision_excerpt="",
            supervisor_md=supervisor_md,
            plan_md=plan_md,
            pipeline_configured=bool(config.get("default_pipeline")),
            next_action=next_action_r1,
            warnings=[],
        )

    if project_name is None:
        project_name = derive_project_name(project_dir)

    stack_info = detect_stack(project_dir)
    if test_cmd is None:
        test_cmd = stack_info["test_cmd"]
    if lint_cmd is None:
        lint_cmd = stack_info["lint_cmd"]
    if typecheck_cmd is None:
        typecheck_cmd = stack_info["typecheck_cmd"]
    if build_cmd is None:
        build_cmd = stack_info["build_cmd"]

    vision_path = paths.find_vision_file(project_dir)

    warnings: list[str] = []
    if vision_path is None:
        warnings.append("Vision/README not found in project root")

    if dry_run:
        return InitResult(
            project_name=project_name,
            project_dir=str(project_dir),
            stack=stack_info["stack"],
            vision_path=str(vision_path) if vision_path else None,
            vision_excerpt="",
            supervisor_md="",
            plan_md="",
            pipeline_configured=False,
            next_action="[dry-run] no files written",
            warnings=warnings,
            created_files=[],
        )

    created_files: list[str] = []
    for d in [
        "roles",
        "pipelines",
        "phases",
        "inbox",
        "outbox",
        "context",
        "logs",
    ]:
        (agentic / d).mkdir(parents=True, exist_ok=True)
        created_files.append(f".agentic/{d}/")

    config_content = _CONFIG_TEMPLATE.format(
        project_name=project_name,
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        typecheck_cmd=typecheck_cmd,
        build_cmd=build_cmd,
    )
    atomic_write_text(agentic / "config.yaml", config_content)
    created_files.append(".agentic/config.yaml")

    # Copy supervisor.md template from the awf package
    framework_dir = Path(__file__).resolve().parent.parent.parent
    templates_dir = framework_dir / "templates"
    supervisor_template = templates_dir / "roles" / "supervisor.md"
    supervisor_dest = agentic / "roles" / "supervisor.md"
    if supervisor_template.is_file():
        shutil.copy2(supervisor_template, supervisor_dest)
        created_files.append(".agentic/roles/supervisor.md")
    else:
        warnings.append(f"supervisor.md template not found at {supervisor_template}")

    # plan.md stub — point to vision if found (atomic per H5 invariant)
    if vision_path is not None:
        rel = Path("..") / ".." / vision_path.name
        plan_body = (
            f"# {project_name} — Plan\n\n"
            f"> Контекст проекта: прочитай `{rel}` перед планированием.\n\n"
            f"Steps:\n"
            f"- [ ] (supervisor заполнит после изучения vision)\n"
        )
    else:
        plan_body = (
            f"# {project_name} — Plan\n\n"
            f"> Vision/README не найден в корне проекта. Спроси пользователя "
            f"о контексте перед планированием.\n\n"
            f"Steps:\n"
            f"- [ ] (supervisor заполнит)\n"
        )
    plan_path = agentic / "phases" / "plan.md"
    atomic_write_text(plan_path, plan_body)
    created_files.append(".agentic/phases/plan.md")

    update_gitignore(project_dir)

    supervisor_md_full = read_file_text(supervisor_dest) if supervisor_dest.is_file() else ""
    plan_md = read_file_text(plan_path)
    vision_excerpt = read_file_text(vision_path, max_chars=4000) if vision_path else ""

    # SMO: return compact phase prompt instead of full 639-line supervisor.md.
    # Dogfood finding: supervisor reads full supervisor.md and ignores phase system.
    # Compact prompt tells supervisor to call awf_current_step for phase guidance.
    try:
        from ..phase import detect_phase, get_phase_prompt
        phase = detect_phase(project_dir)
        supervisor_md = get_phase_prompt(phase, project_dir)
    except Exception:
        phase = "goal"
        supervisor_md = supervisor_md_full  # fallback to full on any error

    # SMO: next_action must match the detected phase — not hardcoded.
    # Dogfood #2: weak model followed next_action ("open form") instead of
    # phase prompt ("ask for goal"). Now both fields say the same thing.
    _NEXT_ACTIONS = {
        "goal": (
            "Спроси пользователя: «Какая цель на эту сессию?» "
            "(фича, багфик, аудит, рефакторинг). После ответа — awf_set_goal."
        ),
        "form": "Открой project-setup форму через awf_open_project_setup_form.",
        "normalize": "Выполни normalize checklist, затем awf_confirm_normalized.",
        "brief": "Изучи vision и план, напиши TODO-NNNN.md (awf_dispatch_todo) и запусти пайплайн.",
        "run": "Проверь awf_status, при необходимости dispatch_todo + awf_start.",
        "verify": "Проверь handoffs + git diff, реши ACK или REVIEW.",
        "done": "Pipeline завершён. Спроси пользователя о следующем шаге.",
        "init": (
            "Спроси пользователя: «Какая цель на эту сессию?» "
            "(фича, багфик, аудит, рефакторинг). После ответа — awf_set_goal."
        ),
    }
    next_action = _NEXT_ACTIONS.get(phase, _NEXT_ACTIONS["goal"])

    return InitResult(
        project_name=project_name,
        project_dir=str(project_dir),
        stack=stack_info["stack"],
        vision_path=str(vision_path) if vision_path else None,
        vision_excerpt=vision_excerpt,
        supervisor_md=supervisor_md,
        plan_md=plan_md,
        pipeline_configured=False,
        next_action=next_action,
        warnings=warnings,
        created_files=created_files,
    )


# ─── get_status ─────────────────────────────────────────────────────────


def _read_ack(inbox: Path, todo_id: str) -> str | None:
    """Read ACK decision from inbox, or None if no decision found.

    Returns the decision string (e.g. ``"decision: rollback"``) if found.
    Returns None if no ACK file exists, or if file lacks ``decision`` line.
    """
    ack_file = inbox / f"ACK-{todo_id}.ready"
    if not ack_file.exists():
        return None
    text = ack_file.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "decision" in line:
            return line.strip()
    return None


def _read_progress(outbox: Path, todo_id: str) -> dict[str, Any]:
    """Parse PROGRESS-{todo_id}.md for task counts and last entry."""
    progress_file = outbox / f"PROGRESS-{todo_id}.md"
    if not progress_file.exists():
        return {}
    text = progress_file.read_text(encoding="utf-8")
    task_lines = [line for line in text.splitlines() if line.startswith("## Task")]
    total = len(task_lines)
    done = sum(1 for ln in task_lines if "[x]" in ln)
    failed = sum(1 for ln in task_lines if "[!]" in ln)
    last = task_lines[-1] if task_lines else ""
    return {"total": total, "done": done, "failed": failed, "last": last}


def _count_done_blocked(inbox: Path, outbox: Path, done: Path | None = None) -> tuple[int, int, list[str]]:
    """Count DONE/BLOCKED TODOs. Returns (done_count, blocked_count, blocked_ids).

    DF6-4: also counts archived TODOs in done/ directory.
    """
    from ..signals import find_signal_file

    done_count = 0
    blocked_count = 0
    blocked_ids: list[str] = []

    if not inbox.exists():
        # DF6-4: even if inbox is empty, count done/ archives
        if done and done.is_dir():
            done_count = sum(1 for d in done.iterdir() if d.is_dir())
        return done_count, blocked_count, blocked_ids

    for ready_file in sorted(inbox.glob("TODO-*.ready")):
        if not ready_file.is_file():
            continue
        todo_id = ready_file.stem
        done_sig = find_signal_file(outbox, "DONE", todo_id, ".ready")
        blocked = find_signal_file(outbox, "BLOCKED", todo_id, ".ready")
        if done_sig:
            done_count += 1
        elif blocked:
            blocked_count += 1
            blocked_ids.append(todo_id)

    # DF6-4: count archived TODOs in done/ directory
    if done and done.is_dir():
        for d in done.iterdir():
            if d.is_dir():
                todo_id = d.name  # e.g. "TODO-0001"
                # Only count if not already counted from outbox (back-compat)
                done_sig = find_signal_file(outbox, "DONE", todo_id, ".ready")
                if not done_sig:
                    done_count += 1

    return done_count, blocked_count, blocked_ids


def _run_brief(project_dir: Path) -> dict | None:
    """SPEC A-run: compact run state for the status payload (None when none)."""
    from .run import run_brief

    return run_brief(project_dir)


def restore_todo(project_dir: Path, todo_id: str) -> RestoreResult:
    """Bring an archived TODO back from done/{id}/ to the inbox (active again).

    NEG-2026-09-19 R2a safety net: the manual recovery path for TODOs
    archived without work. Restores TODO.md and re-creates .ready; handoff
    files move back too. PROGRESS/DONE reports stay in done/ as history.
    """
    import re as _re
    import shutil

    if not _re.match(r"^TODO-\d{4,}$", todo_id or ""):
        raise AwfApiError(f"invalid todo_id {todo_id!r}, expected TODO-NNNN")
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    done_dir = paths.done_dir(project_dir) / todo_id
    if not done_dir.is_dir():
        raise AwfApiError(f"No archived TODO at done/{todo_id} — nothing to restore.")
    md = done_dir / "TODO.md"
    if not md.is_file():
        raise AwfApiError(f"done/{todo_id}/ has no TODO.md — nothing to restore.")

    inbox = paths.inbox(project_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    shutil.move(str(md), str(inbox / f"{todo_id}.md"))
    (inbox / f"{todo_id}.ready").touch()

    restored_handoffs = 0
    handoff_src = done_dir / "handoff"
    if handoff_src.is_dir():
        handoff_dst = paths.agentic_dir(project_dir) / "handoff"
        handoff_dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(handoff_src.iterdir()):
            if f.is_file():
                shutil.move(str(f), str(handoff_dst / f.name))
                restored_handoffs += 1
        try:
            handoff_src.rmdir()
        except OSError:
            pass

    remaining = sorted(p.name for p in done_dir.iterdir())
    if not remaining:
        try:
            done_dir.rmdir()
        except OSError:
            pass

    return RestoreResult(
        todo_id=todo_id,
        message=(
            f"{todo_id} restored to inbox (active)."
            + (f" Handoffs restored: {restored_handoffs}." if restored_handoffs else "")
            + (f" History kept in done/{todo_id}/: {', '.join(remaining)}." if remaining else "")
        ),
    )


def get_status(project_dir: Path) -> StatusResult:
    """Get current workflow state — active TODOs, progress, blocked, conflicts."""
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    config_data = cfg_mod.load(project_dir)
    project_name = (
        cfg_mod.get(config_data, "project.name", "Project") or "Project"
    )

    active_ids = todos.list_active_todos(inbox, outbox)
    done_count, blocked_count, blocked_ids = _count_done_blocked(
        inbox, outbox, paths.done_dir(project_dir)
    )

    active_todos_list: list[dict[str, Any]] = []
    for todo_id in active_ids:
        entry: dict[str, Any] = {"todo_id": todo_id}
        ack = _read_ack(inbox, todo_id)
        if ack is not None:
            entry["ack"] = ack
        progress = _read_progress(outbox, todo_id)
        entry["progress"] = progress if progress else None
        active_todos_list.append(entry)

    conflict_warning: str | None = None
    if len(active_ids) > 1:
        # RUN6 #3: inside an active run the queue is NORMAL — the items wait
        # for their turn and each todo_id is pinned by awf_run_next. The old
        # "stale — rollback or reset" text was a false alarm on every run
        # status call. Outside a run the old warning stands.
        from ..run_state import read_run

        _run = read_run(project_dir)
        if _run and _run.get("active"):
            conflict_warning = (
                f"Run queue: {len(active_ids)} items wait for a turn — "
                "normal in an active run (each TODO is pinned by "
                "awf_run_next)."
            )
        else:
            newest = active_ids[0]
            conflict_warning = (
                f"{len(active_ids)} active TODOs detected. "
                f"Orchestrator will run: {newest} (highest NNNN). "
                f"Others are stale — rollback or 'awf reset --orphans'."
            )

    suggestion: str | None = None
    if not active_ids and blocked_count == 0:
        # SMO: suggestion must be phase-aware — don't suggest "create TODO"
        # during early phases (goal/form/normalize/brief).
        try:
            from ..phase import detect_phase
            current_phase = detect_phase(project_dir)
        except Exception:
            current_phase = "unknown"

        _PHASE_SUGGESTIONS = {
            "goal": "Спроси пользователя о цели сессии → awf_set_goal.",
            "form": "Открой project-setup форму → awf_open_project_setup_form.",
            "normalize": "Выполни normalize checklist → awf_confirm_normalized.",
            "brief": "Напиши TODO-NNNN.md (awf_dispatch_todo) → .ready сигнал.",
            "init": "Спроси пользователя о цели сессии → awf_set_goal.",
        }
        if current_phase in _PHASE_SUGGESTIONS:
            suggestion = _PHASE_SUGGESTIONS[current_phase]
        elif current_phase in ("run", "verify", "done"):
            suggestion = (
                "No active tasks. Supervisor should create the next TODO, "
                "then run: awf start"
            )
        else:
            suggestion = "No active tasks. Check awf_current_step for guidance."

    # RUN3 #1: named pipelines — the active one (config) + how many exist.
    # Same rules as resolve_pipeline_file, so status never disagrees with
    # the file the engine would load.
    from ..pipeline import active_pipeline_name as _active_name
    from ..pipeline import list_pipeline_names as _list_names

    active_pipeline = _active_name(project_dir, config_data)
    pipeline_count = len(_list_names(project_dir))

    pipeline_running, pipeline_pid, log_tail = check_pipeline_running(project_dir)

    # Dogfood-2/6: stage visibility + expected action (only when pipeline running)
    current_stage_name: str | None = None
    next_stage_role: str | None = None
    last_signal: str | None = None
    current_stage_kind: str | None = None
    expected_action: str | None = None
    checkpoint_pending = False
    checkpoint_port: int | None = None
    checkpoint_form_url: str | None = None
    if pipeline_running:
        from .context import (
            _compute_expected_action,
            _compute_stage_kind,
            _extract_stage_info,
        )

        (
            current_stage_name,
            next_stage_role,
            last_signal,
            _log_tail_alt,
            checkpoint_pending,
            checkpoint_port,
            checkpoint_form_url,
        ) = _extract_stage_info(project_dir)
        log_tail = log_tail or _log_tail_alt
        current_stage_kind = _compute_stage_kind(project_dir, current_stage_name)
        expected_action = _compute_expected_action(
            pipeline_running=pipeline_running,
            current_stage_kind=current_stage_kind,
            checkpoint_pending=checkpoint_pending,
            has_active_todos=bool(active_ids),
            done_count=done_count,
            last_signal=last_signal,
        )

    # DF5-4: salvage state — read from state file even if pipeline not running
    # (orchestrator may have crashed after setting salvage_needed)
    salvage_needed = False
    salvage_stage: str | None = None
    from ..pipeline_state import read_state as _read_state
    _state = _read_state(project_dir)
    if _state:
        salvage_needed = bool(_state.get("salvage_needed", False))
        salvage_stage = _state.get("salvage_stage")
        salvage_attempt = _state.get("salvage_count") or 1
        # If state exists but pipeline not detected as running, enrich
        # stage info from state (crash recovery)
        if not pipeline_running:
            from .context import _compute_expected_action, _compute_stage_kind
            if not current_stage_name:
                current_stage_name = _state.get("stage_name")
            current_stage_kind = _compute_stage_kind(project_dir, current_stage_name)
        if salvage_needed:
            # B5 (dogfood-11): salvage overrides the computed action even when
            # the orchestrator process is alive — it waits for the supervisor,
            # it is NOT running a worker. The old text ("worker stage running —
            # auto-transition on worker DONE. Do NOT intervene") contradicted
            # salvage_needed: true.
            expected_action = (
                f"Salvage (attempt {salvage_attempt}): worker didn't signal. "
                f"Review git diff, then ACK, retry, or split the task."
            )

    return StatusResult(
        project_name=project_name,
        active_todos=active_todos_list,
        done_count=done_count,
        blocked_count=blocked_count,
        blocked_ids=blocked_ids,
        conflict_warning=conflict_warning,
        suggestion=suggestion,
        pipeline_running=pipeline_running,
        pipeline_pid=pipeline_pid,
        log_tail=log_tail,
        current_stage_name=current_stage_name,
        next_stage_role=next_stage_role,
        last_signal=last_signal,
        current_stage_kind=current_stage_kind,
        expected_action=expected_action,
        checkpoint_pending=checkpoint_pending,
        checkpoint_port=checkpoint_port,
        checkpoint_form_url=checkpoint_form_url,
        salvage_needed=salvage_needed,
        salvage_stage=salvage_stage,
        active_pipeline=active_pipeline,
        pipeline_count=pipeline_count,
        run_state=_run_brief(project_dir),
    )


# ─── get_report ─────────────────────────────────────────────────────────


def get_report(project_dir: Path) -> ReportResult:
    """Generate workflow report — task statuses, git diff, latest test log."""
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    done_d = paths.done_dir(project_dir)

    config_data = cfg_mod.load(project_dir)
    project_name = (
        cfg_mod.get(config_data, "project.name", "Project") or "Project"
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    items: list[dict[str, str]] = []
    # KAUD-1: reuse _count_done_blocked for consistent counting with get_status
    done_count, blocked_count, _ = _count_done_blocked(inbox, outbox, done_d)

    # Build items list from active TODOs + archived TODOs
    if inbox.exists():
        from ..signals import find_signal_file

        for ready_file in sorted(inbox.glob("TODO-*.ready")):
            if not ready_file.is_file():
                continue
            todo_id = ready_file.stem
            done = find_signal_file(outbox, "DONE", todo_id, ".ready")
            blocked = find_signal_file(outbox, "BLOCKED", todo_id, ".ready")
            if done:
                items.append({"todo_id": todo_id, "status": "OK"})
            elif blocked:
                items.append({"todo_id": todo_id, "status": "BLK"})
            else:
                items.append({"todo_id": todo_id, "status": "..."})

    # KAUD-1: include archived TODOs in items
    if done_d.is_dir():
        for d in sorted(done_d.iterdir()):
            if d.is_dir():
                items.append({"todo_id": d.name, "status": "OK"})

    diff_result = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    git_diff = diff_result.stdout if diff_result.returncode == 0 else ""

    # AUD15-07: an archived task carries its TEST-RESULTS log in done/{id}/ —
    # look there too, and read only the tail (the file can be many MB).
    latest_test_log_tail: str | None = None
    candidates: list[Path] = []
    if outbox.exists():
        candidates.extend(p for p in outbox.glob("TEST-RESULTS-*.log") if p.is_file())
    if done_d.is_dir():
        for d in sorted(done_d.iterdir()):
            if d.is_dir():
                candidates.extend(
                    p for p in d.glob("TEST-RESULTS-*.log") if p.is_file()
                )
    if candidates:
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        try:
            from .._log_reader import read_tail_lines

            lines = read_tail_lines(newest, max_lines=5)
            if lines:
                latest_test_log_tail = "\n".join(lines)
        except OSError:
            pass

    return ReportResult(
        project_name=project_name,
        generated_at=now,
        items=items,
        done_count=done_count,
        blocked_count=blocked_count,
        git_diff=git_diff,
        latest_test_log_tail=latest_test_log_tail,
    )


# ─── reset_runtime ──────────────────────────────────────────────────────


def reset_runtime(
    project_dir: Path,
    *,
    tasks_only: bool = False,
    full: bool = False,
    orphans: bool = False,
) -> ResetResult:
    """Clean runtime data.

    Modes (mutually exclusive). Every mode also clears ``state/current.yaml``
    and (when present) ``state/run.yaml`` — a leftover state file is a stale
    "running" banner / a ghost run (AUD05-02):

    - ``tasks_only``: clean inbox + outbox.
    - ``full``: clean inbox/outbox/context/logs + handoff/inputs/dashboards
      (iteration artifacts).
    - ``orphans``: convenience mode — list + remove orphans in one call.
      Prefer :func:`list_orphans` + :func:`remove_orphans` two-step protocol
      when confirmation is needed.
    - default: clean inbox/outbox/context/logs (keep phases, handoff,
      inputs, dashboards).
    """
    project_dir = Path(project_dir).resolve()
    agentic = project_dir / ".agentic"
    if not agentic.is_dir():
        return ResetResult(cleaned_dirs=[], orphan_ids=[], mode="noop")

    if orphans:
        # T1.6: use two-step protocol (list + remove) instead of legacy
        # _reset_orphans one-shot. Single computation path, no duplication.
        ids = list_orphans(project_dir)
        if not ids:
            return ResetResult(cleaned_dirs=[], orphan_ids=[], mode="orphans")
        return remove_orphans(project_dir, ids)

    if full or not tasks_only:
        dirs_to_clean = ["inbox", "outbox", "context", "logs"]
        if full:
            # AUD07-03: --full was functionally identical to default (only
            # the mode label differed — a decoration flag). It now also
            # cleans iteration artifacts. Default behavior is unchanged —
            # the AUD05-02 ghost-run regression test locks it.
            dirs_to_clean += ["handoff", "inputs", "dashboards"]
        mode = "full" if full else "default"
    else:
        dirs_to_clean = ["inbox", "outbox"]
        mode = "tasks_only"

    cleaned: list[str] = []
    for d in dirs_to_clean:
        dirpath = agentic / d
        if dirpath.is_dir():
            for f in dirpath.iterdir():
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    shutil.rmtree(f)
            cleaned.append(d)

    # Clear pipeline state file so stale "running" doesn't persist
    state_file = agentic / "state" / "current.yaml"
    state_cleared = False
    if state_file.is_file():
        state_file.unlink()
        state_cleared = True
    # AUD05-02: a leftover state/run.yaml is a ghost run — it blocks the
    # next run_start with "Run already active". Clear it alongside.
    if run_state.read_run(project_dir) is not None:
        run_state.clear_run(project_dir)
        state_cleared = True
    if state_cleared:
        cleaned.append("state")

    # Regenerate dashboard so user sees clean state
    try:
        from .dashboard import generate_dashboard as _gen_dash
        _gen_dash(project_dir)
    except Exception:
        pass

    return ResetResult(cleaned_dirs=cleaned, orphan_ids=[], mode=mode)


# ─── list_orphans / remove_orphans (two-step protocol) ──────────────────


def list_orphans(project_dir: Path) -> list[str]:
    """Return orphan TODO ids — active without progress.

    Pure read-only function. Use :func:`remove_orphans` to delete them,
    or :func:`reset_runtime` with ``orphans=True`` for the one-step legacy mode.
    """
    project_dir = Path(project_dir).resolve()
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    active_ids = todos.list_active_todos(inbox, outbox)
    return [tid for tid in active_ids if not todos.has_progress(outbox, tid)]


def remove_orphans(project_dir: Path, orphan_ids: list[str]) -> ResetResult:
    """Remove specific orphan TODOs (pre-computed via :func:`list_orphans`).

    Use this two-step protocol when caller needs to confirm with user
    before deletion:

        ids = api.list_orphans(project_dir)
        if user_confirms(ids):
            api.remove_orphans(project_dir, ids)
    """
    project_dir = Path(project_dir).resolve()
    inbox = paths.inbox(project_dir)
    # AUD05-06: a fresh worker may not have written PROGRESS yet, so its
    # TODO looks like an orphan. Refuse to delete while the pipeline is
    # alive — wait for verify/salvage, or kill first (no force flag).
    running, _pid, _tail = check_pipeline_running(project_dir)
    if running:
        raise AwfApiError(
            "pipeline is running — wait for verify/salvage, or awf kill first"
        )
    removed: list[str] = []
    for tid in orphan_ids:
        ready = inbox / f"{tid}.ready"
        md = inbox / f"{tid}.md"
        deleted_any = False
        if ready.exists():
            ready.unlink()
            deleted_any = True
        if md.exists():
            md.unlink()
            deleted_any = True
        if deleted_any:
            removed.append(tid)
    return ResetResult(cleaned_dirs=[], orphan_ids=removed, mode="orphans")


__all__ = [
    "init_project",
    "get_status",
    "get_report",
    "reset_runtime",
    "list_orphans",
    "remove_orphans",
    # AUD05-09: restore_todo was exported by awf.api but missing from the
    # module's own __all__ — the "star import of the declaring module"
    # contract was broken.
    "restore_todo",
]
