"""Pipeline dashboard generator.

Reads structured state (T4.1) + pipeline.yaml + handoffs + log tail →
renders HTML dashboard to ``.agentic/dashboards/current.html``.

Called by orchestrator after each ``write_state()`` — keeps dashboard
in sync with pipeline progress without supervisor polling.

Supervisor opens it via ``awf_open_pipeline_dashboard`` MCP tool.
User sees live progress in browser (auto-refresh 5s via <meta>).
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import paths
from ..pipeline_state import read_state

DASHBOARD_DIR = "dashboards"
DASHBOARD_FILE = "current.html"

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html.j2"
_template_cache: Any = None


def _get_template() -> Any:
    """Load Jinja2 template (cached)."""
    global _template_cache
    if _template_cache is not None:
        return _template_cache
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_PATH.parent)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    _template_cache = env.get_template(_TEMPLATE_PATH.name)
    return _template_cache


def _format_elapsed(started_at: str | None) -> str:
    """Format elapsed time from ISO timestamp → '4m 32s' or '12s'."""
    if not started_at:
        return ""
    try:
        ts_str = started_at.replace("Z", "+00:00")
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds}s"
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        if minutes < 60:
            return f"{minutes}m {seconds}s"
        hours = minutes // 60
        minutes = minutes % 60
        return f"{hours}h {minutes}m"
    except (ValueError, TypeError):
        return ""


def _parse_log_events(log_text: str, max_events: int = 30) -> list[dict[str, str]]:
    """Extract events from orchestrator.log.

    Parses lines like: ``[2026-08-05T15:42:19Z] Stage 1: agent-system-analyst``
    Returns last ``max_events`` events with timestamps converted to local time.
    """
    events: list[dict[str, str]] = []
    # orchestrator.log format: [2026-08-05T15:42:19Z] message
    time_pattern = re.compile(r"\[(\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2}):(\d{2})Z?)\]\s*(.*)")

    for line in log_text.splitlines():
        m = time_pattern.match(line)
        if not m:
            continue
        # Convert UTC HH:MM:SS to local time
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        try:
            utc_dt = _dt.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ")
            utc_dt = utc_dt.replace(tzinfo=_tz.utc)
            local_dt = utc_dt.astimezone()
            ts_short = local_dt.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            ts_short = f"{m.group(2)}:{m.group(3)}:{m.group(4)}"
        msg = m.group(5).strip()

        # Stage transitions
        stage_m = re.match(r"Stage\s+\d+:\s+(\S+)", msg)
        if stage_m:
            events.append({"ts": ts_short, "msg": f"▶ {stage_m.group(1)}", "type": "start"})
            continue

        # Signal detected
        if "signal detected" in msg.lower():
            events.append({"ts": ts_short, "msg": f"📨 {msg[:80]}", "type": "info"})
            continue

        # Checkpoint
        if "BD-36" in msg and "checkpoint" in msg.lower():
            events.append({"ts": ts_short, "msg": "⏸ Checkpoint opened", "type": "info"})
            continue

        # Pipeline complete
        if "Pipeline complete" in msg:
            events.append({"ts": ts_short, "msg": "✅ Pipeline complete", "type": "done"})
            continue

        # Salvage
        if "salvage" in msg.lower() and "waiting" not in msg.lower():
            events.append({"ts": ts_short, "msg": f"🔧 {msg[:80]}", "type": "warn"})
            continue

        # Transition (signal → action)
        if msg.startswith("Transition:"):
            events.append({"ts": ts_short, "msg": f"→ {msg[:80]}", "type": "info"})
            continue

        # Agent stage finished
        if "Agent stage finished" in msg:
            events.append({"ts": ts_short, "msg": "✓ Stage complete", "type": "done"})
            continue

        # Auto-committed
        if "Auto-committed" in msg or "auto-committed" in msg.lower():
            events.append({"ts": ts_short, "msg": "📦 Committed", "type": "done"})
            continue

    return events[-max_events:]


def _read_handoffs(project_dir: Path) -> list[dict[str, str]]:
    """Read handoff files from .agentic/handoff/."""
    handoff_dir = project_dir / ".agentic" / "handoff"
    if not handoff_dir.is_dir():
        return []

    handoffs: list[dict[str, str]] = []
    for f in sorted(handoff_dir.glob("*.md")):
        # Filename format: {role}-{todo_id}.md
        name = f.stem
        parts = name.split("-", 1)
        role = parts[0] if parts else name
        try:
            content = f.read_text(encoding="utf-8")
            preview = content[:200].strip()
        except OSError:
            preview = ""
        handoffs.append({"role": role, "file": f.name, "preview": preview})

    return handoffs


def _extract_stage_timings(project_dir: Path) -> dict[str, str]:
    """Extract per-stage durations from orchestrator.log.

    Returns {stage_name: "Xm Ys"} for completed stages.
    Current stage shows elapsed (ongoing).
    """
    import re as _re
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    log_file = project_dir / ".agentic" / "logs" / "orchestrator.log"
    if not log_file.is_file():
        return {}

    try:
        log_text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    time_pat = _re.compile(r"\[(\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2}):(\d{2})Z)\]")
    stage_pat = _re.compile(r"Stage\s+\d+:\s+(\S+)")

    # Collect (timestamp, stage_name) transitions
    transitions: list[tuple[int, str]] = []
    for line in log_text.splitlines():
        tm = time_pat.search(line)
        sm = stage_pat.search(line)
        if tm and sm:
            try:
                h, mn, s = int(tm.group(2)), int(tm.group(3)), int(tm.group(4))
                epoch = _dt(2026, 8, 5, h, mn, s, tzinfo=_tz.utc).timestamp()  # approximate
                # Better: parse full ISO
                dt = _dt.strptime(tm.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
                epoch = int(dt.timestamp())
                transitions.append((epoch, sm.group(1)))
            except (ValueError, TypeError):
                continue

    if len(transitions) < 2:
        return {}

    timings: dict[str, str] = {}
    for i in range(len(transitions) - 1):
        start_epoch, name = transitions[i]
        end_epoch = transitions[i + 1][0]
        duration = int(end_epoch - start_epoch)
        m, s = divmod(duration, 60)
        timings[name] = f"{m}m {s}s" if m > 0 else f"{s}s"

    # Current (last) stage — elapsed so far
    import time as _time

    last_epoch, last_name = transitions[-1]
    elapsed = int(_time.time()) - last_epoch
    if elapsed > 0:
        m, s = divmod(elapsed, 60)
        timings[last_name] = f"{m}m {s}s" if m > 0 else f"{s}s"

    return timings


def _read_tasks(project_dir: Path, todo_id: str | None) -> list[dict[str, str]]:
    """Read PROGRESS-{todo_id}.md for task checklist."""
    if not todo_id:
        return []
    # Check outbox first, then done/ (DF6-1 archive moves PROGRESS)
    outbox = paths.outbox(project_dir)
    progress_file = outbox / f"PROGRESS-{todo_id}.md"
    if not progress_file.is_file():
        done_progress = paths.done_dir(project_dir) / todo_id / "PROGRESS.md"
        if done_progress.is_file():
            progress_file = done_progress
        else:
            return []

    try:
        content = progress_file.read_text(encoding="utf-8")
    except OSError:
        return []

    tasks: list[dict[str, str]] = []
    for line in content.splitlines():
        if line.startswith("## Task"):
            if "[x]" in line:
                text = line.replace("## Task", "").replace("[x]", "").strip()
                # Remove leading number like "1:"
                text = re.sub(r"^\d+:\s*", "", text)
                tasks.append({"text": text, "status": "done"})
            elif "[!]" in line:
                text = line.replace("## Task", "").replace("[!]", "").strip()
                text = re.sub(r"^\d+:\s*", "", text)
                tasks.append({"text": text, "status": "done"})
            elif "[ ]" in line:
                text = line.replace("## Task", "").replace("[ ]", "").strip()
                text = re.sub(r"^\d+:\s*", "", text)
                # First undone task = active, rest = pending
                status = "active" if not any(t["status"] == "active" for t in tasks) else "pending"
                tasks.append({"text": text, "status": status})

    return tasks


def _determine_status(state: dict[str, Any] | None) -> tuple[str, str, str, str, str]:
    """Determine dashboard status fields from pipeline state.

    Returns (status, status_class, status_text, status_icon, status_label).

    DF6-6: checks pipeline PID liveness. If state has pipeline_pid but
    the process is dead, shows "Pipeline dead" instead of stale "running".
    """
    if not state:
        return ("idle", "done", "Idle", "○", "Status")

    # DF6-6: PID liveness check
    pid = state.get("pipeline_pid")
    if pid:
        try:
            import os as _os
            _os.kill(int(pid), 0)
        except (ProcessLookupError, PermissionError, ValueError, TypeError, OSError):
            return ("dead", "blocked", "⚠️ Pipeline process dead", "✕", "Dead")
        except Exception:
            # AUD-2026-08-09.5: unknown error checking PID liveness → treat
            # as dead rather than falling through to "running" (false green).
            return ("dead", "blocked", "⚠️ Pipeline process dead", "✕", "Dead")

    checkpoint_pending = bool(state.get("checkpoint_pending", False))
    stage_kind = state.get("stage_kind", "")
    last_signal = state.get("last_signal", "")

    if checkpoint_pending:
        return ("checkpoint", "checkpoint", "Checkpoint pending", "⏸", "Paused")
    if last_signal and last_signal.startswith("BLOCKED"):
        return ("blocked", "blocked", "Blocked", "⚠", "Blocked")
    if stage_kind == "verify":
        return ("verify", "running", "Verify stage", "🔍", "Verifying")
    if stage_kind and stage_kind != "verify":
        return ("running", "running", "Pipeline running", "●", "Running")

    return ("running", "running", "Pipeline running", "●", "Running")


def _read_worker_activity(state: dict[str, Any] | None) -> dict[str, Any]:
    """Read worker subprocess activity from /proc (Linux only).

    Shows CPU time, process state, IO stats — so user can tell if
    worker is actively running or hung.

    Returns dict with: active, worker_pid, state (sleeping/running/zombie),
    cpu_seconds, read_mb, write_kb. Empty dict if unavailable.
    """
    if not state:
        return {}
    pid = state.get("pipeline_pid")
    if not pid:
        return {}

    try:
        # Find child processes (worker = child of orchestrator)
        result = subprocess.run(
            ["ps", "--ppid", str(pid), "-o", "pid=", "--noheaders"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        child_pids = [p.strip() for p in result.stdout.split() if p.strip()]
        if not child_pids:
            return {"active": False, "reason": "No child process — worker not running"}

        worker_pid = child_pids[0]

        # Read /proc/<pid>/stat for CPU + state
        stat_path = Path(f"/proc/{worker_pid}/stat")
        if not stat_path.is_file():
            return {"active": False, "reason": f"PID {worker_pid} not in /proc"}

        stat_raw = stat_path.read_text()
        # stat format: pid (comm) state ...
        # Handle comm with spaces/parens
        last_paren = stat_raw.rfind(")")
        state_char = stat_raw[last_paren + 2:].split()[0]
        fields = stat_raw[last_paren + 2:].split()
        utime = int(fields[11])
        stime = int(fields[12])

        ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        cpu_seconds = round((utime + stime) / ticks, 1)

        state_names = {
            "R": "running",
            "S": "sleeping (waiting for model)",
            "Z": "zombie (crashed)",
            "D": "disk wait",
            "T": "stopped",
        }

        # Read /proc/<pid>/io for network/file IO
        io_path = Path(f"/proc/{worker_pid}/io")
        rchar = wchar = 0
        if io_path.is_file():
            for line in io_path.read_text().splitlines():
                key, _, val = line.partition(":")
                if key.strip() == "rchar":
                    rchar = int(val.strip())
                elif key.strip() == "wchar":
                    wchar = int(val.strip())

        return {
            "active": True,
            "worker_pid": int(worker_pid),
            "state": state_names.get(state_char, state_char),
            "cpu_seconds": cpu_seconds,
            "read_mb": round(rchar / 1024 / 1024, 1),
            "write_kb": round(wchar / 1024, 1),
        }
    except (OSError, ValueError, IndexError, FileNotFoundError, subprocess.SubprocessError):
        return {"active": False, "reason": "Could not read worker stats"}


def generate_dashboard(project_dir: Path) -> Path | None:
    """Generate dashboard HTML to ``.agentic/dashboards/current.html``.

    Reads:
    - Pipeline state from ``.agentic/state/current.yaml`` (T4.1)
    - Pipeline stages from ``.agentic/pipelines/default.yaml``
    - Events from ``.agentic/logs/awf-start.out``
    - Handoffs from ``.agentic/handoff/``
    - Task progress from ``.agentic/outbox/PROGRESS-{todo_id}.md``

    Renders Jinja2 template → writes HTML atomically.

    Returns:
        Path to generated HTML file, or None if template rendering failed.
    """
    project_dir = Path(project_dir).resolve()
    state = read_state(project_dir)

    # Read pipeline.yaml for stages (AUD-3: use canonical path)
    stages_raw: list[dict[str, Any]] = []
    try:
        from ..pipeline import resolve_pipeline_file
        pipeline_file = resolve_pipeline_file(project_dir)
        if pipeline_file.is_file():
            import yaml
            data = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
            stages_raw = (data or {}).get("stages", []) if isinstance(data, dict) else []
    except Exception:
        pass

    # Determine current stage from state
    current_stage_idx = state.get("stage_idx") if state else -1

    # Build stage display data
    stages: list[dict[str, str]] = []
    for i, s in enumerate(stages_raw):
        if not isinstance(s, dict):
            continue
        name = s.get("name", f"stage-{i}")
        model = s.get("model", "")
        if i < (current_stage_idx or 0):
            status = "done"
        elif i == current_stage_idx:
            status = "current"
        else:
            status = "pending"
        stages.append({"name": name, "model": model, "status": status})

    stages_done = sum(1 for s in stages if s["status"] == "done")
    stages_total = len(stages)

    # Status
    status, status_class, status_text, status_icon, status_label = _determine_status(state)

    # If pipeline state cleared (pipeline not running) → mark all as done/idle
    if not state and not stages:
        status = "idle"
        status_class = "done"
        status_text = "No active pipeline"
        status_icon = "○"
        status_label = "Status"

    # Events from log
    # Events — read orchestrator.log (has timestamps, unlike awf-start.out)
    log_file = paths.agentic_dir(project_dir) / "logs" / "orchestrator.log"
    events: list[dict[str, str]] = []
    if log_file.is_file():
        try:
            log_text = log_file.read_text(encoding="utf-8", errors="replace")
            events = _parse_log_events(log_text)
        except OSError:
            pass

    # Handoffs
    handoffs = _read_handoffs(project_dir)

    # Tasks
    todo_id = state.get("todo_id") if state else None
    tasks = _read_tasks(project_dir, todo_id)
    tasks_done = sum(1 for t in tasks if t["status"] == "done")

    # DF6-4: completed TODOs from done/ directory
    done_directory = paths.done_dir(project_dir)
    completed_todos = []
    if done_directory.is_dir():
        for d in sorted(done_directory.iterdir()):
            if d.is_dir():
                completed_todos.append(d.name)

    # Per-stage timings from orchestrator.log
    stage_timings = _extract_stage_timings(project_dir)

    # Elapsed time — pass epoch for live JS ticker

    elapsed_epoch = 0
    if state:
        started = state.get("started_at") or state.get("updated_at")
        if started:
            try:
                from datetime import datetime as _dt
                # Parse ISO format: 2026-08-05T15:42:19.123456+00:00
                dt = _dt.fromisoformat(started)
                elapsed_epoch = int(dt.timestamp())
            except (ValueError, TypeError):
                pass
    elapsed = _format_elapsed(state.get("started_at") if state else None)
    if not elapsed and state:
        elapsed = _format_elapsed(state.get("updated_at"))

    # Project name
    import yaml

    config_file = paths.config_file(project_dir)
    project_name = "Project"
    if config_file.is_file():
        try:
            config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
            project_name = (config or {}).get("project", {}).get("name", "Project")
        except (yaml.YAMLError, OSError):
            pass

    # Render template
    try:
        template = _get_template()
        html = template.render(
            project_name=project_name,
            todo_id=todo_id or "",
            todo_summary="",
            elapsed=elapsed,
            elapsed_epoch=elapsed_epoch,
            status=status,
            status_class=status_class,
            status_text=status_text,
            status_icon=status_icon,
            status_label=status_label,
            stages=stages,
            stages_done=stages_done,
            stages_total=stages_total,
            checkpoint_pending=bool(state.get("checkpoint_pending", False)) if state else False,
            checkpoint_form_url=state.get("checkpoint_form_url") if state else None,
            salvage_needed=bool(state.get("salvage_needed", False)) if state else False,
            salvage_stage=state.get("salvage_stage") if state else None,
            events=events,
            handoffs=handoffs,
            tasks=tasks,
            tasks_done=tasks_done,
            completed_todos=completed_todos,
            stage_timings=stage_timings,
            worker_activity=_read_worker_activity(state),
        )
    except Exception as e:
        import traceback as _tb

        from .._log import log as _log
        logs_dir = project_dir / ".agentic" / "logs"
        _log(logs_dir, f"Dashboard generation failed: {e}")
        err_path = project_dir / ".agentic" / DASHBOARD_DIR / "error.txt"
        try:
            err_path.parent.mkdir(parents=True, exist_ok=True)
            err_path.write_text(f"{type(e).__name__}: {e}\n\n{_tb.format_exc()}", encoding="utf-8")
        except OSError:
            pass
        return None

    # Write to .agentic/dashboards/current.html (atomic — concurrent
    # browser meta-refresh reads must not see a half-written file).
    from .._atomic import atomic_write_text

    dashboards_dir = project_dir / ".agentic" / DASHBOARD_DIR
    dashboards_dir.mkdir(parents=True, exist_ok=True)
    output_path = dashboards_dir / DASHBOARD_FILE
    atomic_write_text(output_path, html)
    return output_path


__all__ = ["generate_dashboard"]
