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
        autoescape=False,
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
    """Extract events from awf-start.out log.

    Looks for lines with timestamps + known patterns:
    - 'Stage N/M: <name>' → start event
    - 'DONE' → done event
    - 'BLOCKED' → blocked event
    - 'BD-36: checkpoint' → checkpoint event
    - 'signal detected' → signal event
    """
    events: list[dict[str, str]] = []
    time_pattern = re.compile(r"(\d{2}:\d{2}:\d{2})")
    stage_pattern = re.compile(r"Stage\s+\d+/\d+:\s+(\S+)")

    for line in log_text.splitlines():
        ts_match = time_pattern.search(line)
        ts = ts_match.group(1) if ts_match else "--:--:--"

        stage_match = stage_pattern.search(line)
        if stage_match:
            events.append({"ts": ts, "msg": f"▶ {stage_match.group(1)} started", "type": "start"})
            continue

        line_lower = line.lower()
        if "done" in line_lower and "todo" in line_lower:
            # Extract artifact info if present
            events.append({"ts": ts, "msg": f"✓ {line.strip()[:100]}", "type": "done"})
        elif "blocked" in line_lower:
            events.append({"ts": ts, "msg": f"⚠ {line.strip()[:100]}", "type": "warn"})
        elif "bd-36" in line_lower and "checkpoint" in line_lower:
            events.append({"ts": ts, "msg": f"⏸ {line.strip()[:100]}", "type": "info"})
        elif "signal detected" in line_lower or "signal received" in line_lower:
            events.append({"ts": ts, "msg": f"📨 {line.strip()[:100]}", "type": "info"})
        elif "created:" in line_lower or "wrote:" in line_lower:
            events.append({"ts": ts, "msg": f"  {line.strip()[:100]}", "type": "info"})

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


def _read_tasks(project_dir: Path, todo_id: str | None) -> list[dict[str, str]]:
    """Read PROGRESS-{todo_id}.md for task checklist."""
    if not todo_id:
        return []
    outbox = paths.outbox(project_dir)
    progress_file = outbox / f"PROGRESS-{todo_id}.md"
    if not progress_file.is_file():
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
    """
    if not state:
        return ("idle", "done", "Idle", "○", "Status")

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

    # Read pipeline.yaml for stages
    pipeline_file = project_dir / ".agentic" / "pipelines" / "default.yaml"
    stages_raw: list[dict[str, Any]] = []
    if pipeline_file.is_file():
        try:
            import yaml

            data = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
            stages_raw = (data or {}).get("stages", []) if isinstance(data, dict) else []
        except (yaml.YAMLError, OSError):
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
    log_file = paths.agentic_dir(project_dir) / "logs" / "awf-start.out"
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

    # Elapsed time
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
            events=events,
            handoffs=handoffs,
            tasks=tasks,
            tasks_done=tasks_done,
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
