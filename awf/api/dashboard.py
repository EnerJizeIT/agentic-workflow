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

# Role visual identity — icons + colors for dashboard
ROLE_VISUALS: dict[str, dict[str, str]] = {
    "supervisor": {"icon": "📋", "color": "#569cd6", "label": "Supervisor"},
    "agent-system-analyst": {"icon": "🔍", "color": "#4fc1ff", "label": "Analyst"},
    "agent-architector": {"icon": "📐", "color": "#c586c0", "label": "Architector"},
    "agent-implementer": {"icon": "🔧", "color": "#ce9178", "label": "Implementer"},
    "agent-qa-review": {"icon": "✅", "color": "#4ec9b0", "label": "QA Review"},
    "agent-project-auditor": {"icon": "🔬", "color": "#dcdcaa", "label": "Auditor"},
    "agent-debugger": {"icon": "🐛", "color": "#f14c4c", "label": "Debugger"},
    "agent-test-automator": {"icon": "🧪", "color": "#4ec9b0", "label": "Test Auto"},
    "agent-security-auditor": {"icon": "🛡️", "color": "#c586c0", "label": "Security"},
    "agent-refactoring-specialist": {"icon": "♻️", "color": "#4fc1ff", "label": "Refactor"},
    "agent-code-reviewer": {"icon": "👁️", "color": "#dcdcaa", "label": "Reviewer"},
    "agent-performance-engineer": {"icon": "⚡", "color": "#ce9178", "label": "Perf"},
    "agent-dependency-manager": {"icon": "📦", "color": "#569cd6", "label": "Deps"},
    "agent-sql-pro": {"icon": "🗃️", "color": "#4ec9b0", "label": "SQL"},
}


def _role_visual(role_name: str) -> dict[str, str]:
    """Get icon+color+label for a role name."""
    if role_name in ROLE_VISUALS:
        return ROLE_VISUALS[role_name]
    # Fallback: derive icon from role keywords
    name_lower = (role_name or "").lower()
    _KEYWORD_ICONS = [
        ("test", "🧪", "#4ec9b0"), ("security", "🛡️", "#c586c0"),
        ("review", "👁️", "#dcdcaa"), ("debug", "🐛", "#f14c4c"),
        ("implement", "🔧", "#ce9178"), ("develop", "🔧", "#ce9178"),
        ("analy", "🔍", "#4fc1ff"), ("architect", "📐", "#c586c0"),
        ("audit", "🔬", "#dcdcaa"), ("refactor", "♻️", "#4fc1ff"),
        ("sql", "🗃️", "#4ec9b0"), ("perf", "⚡", "#ce9178"),
        ("dep", "📦", "#569cd6"), ("doc", "📄", "#858585"),
    ]
    for keyword, icon, color in _KEYWORD_ICONS:
        if keyword in name_lower:
            label = role_name.replace("agent-", "").replace("-", " ").title()
            return {"icon": icon, "color": color, "label": label}
    # Final fallback: letter avatar
    letter = (role_name or "?")[0].upper()
    return {"icon": letter, "color": "#858585", "label": role_name.replace("agent-", "").replace("-", " ").title()}


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
        return _format_elapsed_from_seconds(int(delta.total_seconds()))
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

        # Checkpoint (Day-4: an auto-approved checkpoint is NOT "opened")
        if "BD-36" in msg and "checkpoint" in msg.lower():
            if "decision=approve" in msg or "auto-approve" in msg:
                events.append({"ts": ts_short, "msg": "⏸ Checkpoint auto-approved", "type": "info"})
            elif "still waiting" in msg.lower():
                pass  # polling noise — invisible
            elif "decision" in msg:
                events.append({"ts": ts_short, "msg": "⏸ Checkpoint decision recorded", "type": "info"})
            else:
                events.append({"ts": ts_short, "msg": "⏸ Checkpoint opened — needs user", "type": "warn"})
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


def _read_handoffs(
    project_dir: Path,
    stage_order: list[str] | None = None,
    todo_id: str | None = None,
) -> list[dict[str, str]]:
    """Read the CURRENT TODO's handoffs from .agentic/handoff/.

    Day-3 (dashboard review): the directory used to be read wholesale — old
    TODOs' files and agent-written legacy names leaked into the chat as if
    they were current. Now:
    - only files matching ``*-{todo_id}.md`` / ``*-{todo_id}-*.md`` count
      (no todo_id → nothing);
    - one entry per role: the canonical ``{role}-{todo}.md`` wins, otherwise
      the newest file by mtime;
    - ``rev`` (mtime + size) tells the client when to re-render.
    """
    handoff_dir = project_dir / ".agentic" / "handoff"
    if not handoff_dir.is_dir() or not todo_id:
        return []

    # Build role → sort index from pipeline stages
    role_order: dict[str, int] = {}
    if stage_order:
        for i, name in enumerate(stage_order):
            role_order[name] = i

    # Two exact globs on purpose: a single '*-{todo}*.md' would swallow
    # TODO-00010 when the current TODO is TODO-0001.
    candidates = set(handoff_dir.glob(f"*-{todo_id}.md")) | set(
        handoff_dir.glob(f"*-{todo_id}-*.md")
    )

    def _role_of(name: str) -> str:
        stem = Path(name).stem
        if f"-{todo_id}" in stem:
            return stem.split(f"-{todo_id}")[0]
        if "-TODO-" in stem:
            return stem.split("-TODO-")[0]
        return stem.rsplit("-", 1)[0] if "-" in stem else stem

    # One entry per role: canonical name wins, otherwise newest mtime.
    best: dict[str, Path] = {}
    for p in sorted(candidates):
        role = _role_of(p.name)
        current = best.get(role)
        if current is None:
            best[role] = p
            continue
        cur_exact = current.name == f"{role}-{todo_id}.md"
        new_exact = p.name == f"{role}-{todo_id}.md"
        if new_exact and not cur_exact:
            best[role] = p
        elif new_exact == cur_exact:
            try:
                if p.stat().st_mtime > current.stat().st_mtime:
                    best[role] = p
            except OSError:
                pass

    handoffs: list[dict[str, str]] = []
    for role in best:
        f = best[role]
        try:
            content = f.read_text(encoding="utf-8")
            mtime = f.stat().st_mtime_ns
        except OSError:
            content = ""
            mtime = 0
        # Day-3: escape raw HTML before markdown — handoffs are written by
        # LLM workers; markup must not survive into the dashboard.
        import html as _html

        try:
            import markdown as _md

            content_html = _md.markdown(_html.escape(content), extensions=["fenced_code"])
        except Exception:
            content_html = f"<pre>{_html.escape(content)}</pre>"

        handoffs.append({
            "role": role,
            "file": f.name,
            "preview": content[:200].strip(),
            "content_html": content_html,
            "rev": f"{mtime}-{len(content)}",
        })

    # Sort by pipeline order (roles not in pipeline go last, alphabetically)
    handoffs.sort(key=lambda h: (role_order.get(h["role"], 999), h["role"]))
    return handoffs


def _extract_stage_timings(project_dir: Path) -> tuple[dict[str, str], int]:
    """Extract per-stage durations from orchestrator.log.

    Returns (timings_dict, current_stage_epoch).
    - timings_dict: {stage_name: "Xm Ys"} for completed + current stages.
    - current_stage_epoch: Unix epoch of current stage start (for live JS ticker).
    """
    import re as _re
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    log_file = project_dir / ".agentic" / "logs" / "orchestrator.log"
    if not log_file.is_file():
        return {}, 0

    try:
        log_text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, 0

    time_pat = _re.compile(r"\[(\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2}):(\d{2})Z)\]")
    stage_pat = _re.compile(r"Stage\s+\d+:\s+(\S+)")

    # Collect (timestamp, stage_name) transitions
    transitions: list[tuple[int, str]] = []
    for line in log_text.splitlines():
        tm = time_pat.search(line)
        sm = stage_pat.search(line)
        if tm and sm:
            try:
                dt = _dt.strptime(tm.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc)
                epoch = int(dt.timestamp())
                transitions.append((epoch, sm.group(1)))
            except (ValueError, TypeError):
                continue

    if not transitions:
        return {}, 0

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

    return timings, last_epoch


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
    salvage_needed = bool(state.get("salvage_needed", False))
    stage_kind = state.get("stage_kind", "")
    last_signal = state.get("last_signal", "")

    if checkpoint_pending:
        return ("checkpoint", "checkpoint", "Checkpoint pending", "⏸", "Paused")
    if salvage_needed:
        # dogfood-11: worker exited without a signal — orchestrator waits for
        # the supervisor's ACK/retry. Previously this showed "Pipeline running",
        # so users staring at the dashboard had no idea the pipeline was stuck.
        stage = state.get("salvage_stage") or "?"
        return (
            "salvage", "blocked",
            f"Salvage: {stage} didn't signal — decision needed",
            "🔧", "Salvage",
        )
    if last_signal and last_signal.startswith("BLOCKED"):
        return ("blocked", "blocked", "Blocked", "⚠", "Blocked")
    if stage_kind == "verify":
        return ("verify", "running", "Verify stage", "🔍", "Verifying")
    if stage_kind and stage_kind != "verify":
        return ("running", "running", "Pipeline running", "●", "Running")

    # AUD10-01: no live stage and no pid — the engine is not running.
    # Post-completion residue ({phase: done}) → "done" (first server-side
    # consumer of the .status-badge.done CSS); pre-start ({phase: run}) →
    # "idle". Both used to render as "Pipeline running" with a forever-
    # ticking timer.
    if state.get("phase") == "done":
        return ("done", "done", "Iteration complete", "✓", "Done")
    return ("idle", "done", "Idle", "○", "Idle")


def _pick_worker_pid(pids: list[str], read_cmdline: Any = None) -> str:
    """Prefer the real worker (cmdline contains 'opencode') over helpers.

    Day-3 (dashboard review): ``child_pids[0]`` could be a short-lived helper
    spawned by the orchestrator itself (git/ps) — the panel then showed a
    dead or foreign PID. Falls back to the first child.
    """
    if read_cmdline is None:

        def read_cmdline(pid: str) -> bytes:  # type: ignore[misc]
            try:
                return Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                return b""

    for pid in pids:
        try:
            if b"opencode" in (read_cmdline(pid) or b""):
                return pid
        except OSError:
            continue
    return pids[0]


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

        worker_pid = _pick_worker_pid(child_pids)

        # Read /proc/<pid>/stat for CPU + state
        stat_path = Path(f"/proc/{worker_pid}/stat")
        if not stat_path.is_file():
            # Day-4: the "child" was a transient helper (our own ps/git call) —
            # "PID X not in /proc" confused more than helped.
            return {"active": False, "reason": "No worker running"}

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

    Day-3 (dashboard review): the Jinja context is derived from a SINGLE
    source — :func:`generate_state_dict` — instead of independently
    recomputing status/elapsed/handoffs (the two used to diverge). The
    template needs only a few primitives for the first paint; the live view
    is driven by ``/api/state``.

    Returns:
        Path to generated HTML file, or None if template rendering failed.
    """
    import json as _json
    import sys

    project_dir = Path(project_dir).resolve()

    try:
        initial_state = generate_state_dict(project_dir)
        # Day-3 (XSS): json.dumps does not escape '<' — a '</script>' inside
        # any string (e.g. a handoff) would break out of the script tag.
        initial_state_json = _json.dumps(initial_state, ensure_ascii=False).replace(
            "<", "\\u003c"
        )
    except Exception as e:
        print(f"dashboard: state generation failed: {e}", file=sys.stderr)
        initial_state = {}
        initial_state_json = "{}"

    try:
        template = _get_template()
        html = template.render(
            project_name=initial_state.get("project_name") or "Project",
            todo_id=initial_state.get("todo_id") or "",
            status=initial_state.get("status") or "idle",
            status_text=initial_state.get("status_text") or "",
            elapsed=initial_state.get("elapsed_str") or "",
            elapsed_epoch=initial_state.get("elapsed_epoch") or 0,
            elapsed_frozen=bool(initial_state.get("elapsed_frozen", False)),
            initial_state_json=initial_state_json,
        )
    except Exception as e:
        import traceback as _tb

        from .._log import log as _log

        logs_dir = project_dir / ".agentic" / "logs"
        _log(logs_dir, f"Dashboard generation failed: {e}")
        err_path = project_dir / ".agentic" / DASHBOARD_DIR / "error.txt"
        try:
            err_path.parent.mkdir(parents=True, exist_ok=True)
            err_path.write_text(
                f"{type(e).__name__}: {e}\n\n{_tb.format_exc()}", encoding="utf-8"
            )
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


__all__ = ["generate_dashboard", "generate_state_dict", "ROLE_VISUALS"]


def _read_todo_content(project_dir: Path, todo_id: str | None) -> str:
    """Read current TODO content as rendered HTML."""
    if not todo_id:
        return ""
    inbox = paths.inbox(project_dir)
    for fname in [f"{todo_id}.md", "TODO-content.md"]:
        f = inbox / fname
        if f.is_file():
            try:
                import html as _html

                import markdown as _md

                raw = f.read_text(encoding="utf-8")
                # Day-3: escape raw HTML before markdown — TODO bodies are
                # LLM-written and must not inject markup into the dashboard.
                return _md.markdown(
                    _html.escape(raw), extensions=["fenced_code"]
                )
            except Exception:
                return ""
    return ""


def _run_brief_for_dashboard(project_dir: Path) -> dict | None:
    """SPEC A-run: run state for the topbar chip (None when no run)."""
    try:
        from .run import run_brief

        return run_brief(project_dir)
    except Exception:
        return None


def _read_todo_diff_stat(project_dir: Path, todo_id: str | None) -> str:
    """Day-2 spec: `git diff --stat` against the TODO baseline.

    One place for the supervisor instead of a manual diff on verify.
    Returns ``""`` when there is no baseline / not a git repo / git fails.
    """
    if not todo_id:
        return ""
    from ..verify import diff_stat_for_todo

    return diff_stat_for_todo(project_dir, todo_id)


def _scoped_log_text(project_dir: Path) -> tuple[datetime | None, str]:
    """Log text of the CURRENT run only (after the last 'Pipeline started').

    Day-4 (live review): orchestrator.log accumulates across restarts and
    TODOs — the elapsed timer picked the first agent line of the WHOLE log
    and showed 47h on a one-minute-old run.
    """
    log_file = paths.agentic_dir(project_dir) / "logs" / "orchestrator.log"
    if not log_file.is_file():
        return None, ""
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, ""

    marker = None
    for m in re.finditer(
        r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]\s*Pipeline started", text,
    ):
        marker = m
    if marker is None:
        return None, text
    try:
        start = datetime.strptime(marker.group(1), "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        start = None
    return start, text[marker.start():]


def _run_elapsed(
    project_dir: Path, status: str,
) -> tuple[int, bool, str]:
    """(elapsed_epoch, elapsed_frozen, elapsed_str) for the CURRENT run.

    Running → epoch is the run's first agent stage (fallback: run start) and
    the string grows with wall time. Frozen statuses → span between the first
    agent line and the verify line of the scoped run.
    """
    scoped_start, text = _scoped_log_text(project_dir)

    agent_pat = re.compile(r"Stage\s+\d+:\s+\S+\s+\([^)]*::\s*execute")
    verify_pat = re.compile(r"Stage\s+\d+:\s+\S+\s+\([^)]*::\s*verify")
    time_pat = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]")

    first_agent = None
    verify_ts = None
    for line in text.splitlines():
        tm = time_pat.search(line)
        if not tm:
            continue
        try:
            dt = datetime.strptime(tm.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if agent_pat.search(line) and first_agent is None:
            first_agent = dt
        if verify_pat.search(line):
            verify_ts = dt

    if status == "running":
        epoch = int(first_agent.timestamp()) if first_agent else (
            int(scoped_start.timestamp()) if scoped_start else 0
        )
        return epoch, False, ""

    # Frozen statuses
    if first_agent and verify_ts:
        epoch = int(first_agent.timestamp())
        return epoch, True, _format_elapsed_from_seconds(
            int(verify_ts.timestamp() - epoch)
        )
    if first_agent:
        return int(first_agent.timestamp()), True, ""
    return 0, True, ""


def _fmt_clock(epoch: int | None) -> str:
    """Epoch → local 'HH:MM:SS' for the chat entry times."""
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(int(epoch)).astimezone().strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _stage_spans(project_dir: Path) -> dict[str, dict[str, int]]:
    """Run-scoped {stage_name: {"start": epoch, "end": epoch|None}}.

    Day-4: built from the CURRENT run only (see ``_scoped_log_text``);
    ``end=None`` means the stage is still running.
    """
    _, text = _scoped_log_text(project_dir)
    time_pat = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]")
    stage_pat = re.compile(r"Stage\s+\d+:\s+(\S+)")
    transitions: list[tuple[int, str]] = []
    for line in text.splitlines():
        tm = time_pat.search(line)
        sm = stage_pat.search(line)
        if not (tm and sm):
            continue
        try:
            dt = datetime.strptime(tm.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        transitions.append((int(dt.timestamp()), sm.group(1)))
    spans: dict[str, dict[str, int]] = {}
    for i, (ts, name) in enumerate(transitions):
        end = transitions[i + 1][0] if i + 1 < len(transitions) else None
        spans[name] = {"start": ts, "end": end}
    return spans


def _total_elapsed(project_dir: Path) -> tuple[int, bool, int]:
    """(closed_seconds, running, open_run_start_epoch) across ALL runs.

    Day-5 (owner): the board needs BOTH the current iteration timer and the
    total across iterations. Runs are delimited by 'Pipeline started' /
    'Pipeline complete' lines in the (multi-run) orchestrator log.
    """
    log = paths.agentic_dir(project_dir) / "logs" / "orchestrator.log"
    if not log.is_file():
        return 0, False, 0
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, False, 0

    time_pat = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]")
    closed_seconds = 0
    open_start: datetime | None = None
    for line in text.splitlines():
        is_start = "Pipeline started" in line
        is_complete = "Pipeline complete" in line
        if not (is_start or is_complete):
            continue
        tm = time_pat.search(line)
        if not tm:
            continue
        try:
            dt = datetime.strptime(tm.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if is_start:
            if open_start is not None:  # previous run never completed — close it here
                closed_seconds += int((dt - open_start).total_seconds())
            open_start = dt
        elif is_complete and open_start is not None:
            closed_seconds += int((dt - open_start).total_seconds())
            open_start = None
    return closed_seconds, open_start is not None, (
        int(open_start.timestamp()) if open_start else 0
    )


def _read_todo_summary(project_dir: Path, todo_id: str | None, limit: int = 400) -> str:
    """Day-4 UX: one-paragraph human summary of the TODO.

    The TODO file is written for the AGENT; the user needs the gist. Takes
    the first non-heading paragraph after the title, strips markdown marks.
    """
    if not todo_id:
        return ""
    f = paths.inbox(project_dir) / f"{todo_id}.md"
    if not f.is_file():
        return ""
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    lines = text.splitlines()
    i = 0
    if i < len(lines) and lines[i].strip() == "---":  # skip front-matter
        i += 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        i += 1

    para: list[str] = []
    for line in lines[i:]:
        s = line.strip()
        if not s:
            if para:
                break
            continue
        if s.startswith(("#", "<!--", "-->", "```", "|", "> ", "**TODO", "- [")):
            if para:
                break
            continue
        para.append(s)
    if not para:
        return ""
    joined = re.sub(r"[*_`]+", "", " ".join(para)).strip()
    if len(joined) > limit:
        joined = joined[: limit - 1].rstrip() + "…"
    return joined


def _read_worker_last_line(project_dir: Path, todo_id: str | None = None) -> str:
    """Last meaningful line from the CURRENT worker's log.

    Day-4 (live review): the glob used 'agent-*.out' while awf writes
    'awf-agent-*.out' — the worker panel's last line was always empty.
    """
    import re as _re

    logs_dir = project_dir / ".agentic" / "logs"
    candidates: list[Path] = []
    if todo_id:
        candidates = sorted(
            logs_dir.glob(f"awf-agent-*-{todo_id}.out"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    if not candidates:
        try:
            candidates = sorted(
                logs_dir.glob("awf-agent-*.out"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
        except OSError:
            candidates = []

    ansi = _re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
    for log_name in candidates[:1]:
        try:
            lines = log_name.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            clean = ansi.sub("", line).strip()
            if not clean or len(clean) < 3:
                continue
            if clean.startswith("timestamp="):
                # opencode structured log — surface the file being touched
                m = _re.search(r'message="touching file" file="([^"]+)"', clean)
                if m:
                    return f"touching {Path(m.group(1)).name}"
                continue
            return clean[:200]
    return ""


def _verify_commit_shas(project_dir: Path) -> dict[str, str]:
    """Map TODO id → short sha of its ``awf(verify): TODO-NNNN`` commit.

    Day-3 (dashboard review): the old code looked for
    ``done/{id}/BASELINE.sha`` which archive_todo never creates — the pill
    tooltips were silently empty. One ``git log`` call per poll, not N.
    """
    from .. import git_utils

    try:
        out = git_utils.git_stdout(
            project_dir, "log", "--format=%h %s", "-n", "300", check=False
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return {}
    mapping: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, subject = line.partition(" ")
        m = re.search(r"awf\(verify\):\s*(TODO-\d+)", subject)
        if m and m.group(1) not in mapping:
            mapping[m.group(1)] = sha
    return mapping


def _build_todo_timeline(project_dir: Path, current_todo: str | None) -> list[dict]:
    """Build TODO timeline from done/ directory + current state."""
    timeline = []
    commit_shas = _verify_commit_shas(project_dir)
    done_dir = paths.done_dir(project_dir)
    if done_dir.is_dir():
        for d in sorted(done_dir.iterdir()):
            if d.is_dir():
                timeline.append({
                    "id": d.name,
                    "status": "approved",
                    "commit_sha": commit_shas.get(d.name, ""),
                })
    if current_todo and current_todo not in [t["id"] for t in timeline]:
        timeline.append({"id": current_todo, "status": "running", "commit_sha": ""})
    return timeline


def generate_state_dict(project_dir: Path) -> dict[str, Any]:
    """Generate full dashboard state as dict (for /api/state JSON endpoint).

    This is the data backbone — the HTML template and JS both consume it.
    Called by dashboard HTTP server every 3 seconds for live updates.
    """
    project_dir = Path(project_dir).resolve()
    state = read_state(project_dir)

    # Pipeline stages
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

    current_stage_idx = state.get("stage_idx", -1) if state else -1
    stage_names = []
    stages = []
    for i, s in enumerate(stages_raw):
        if not isinstance(s, dict):
            continue
        name = s.get("name", f"stage-{i}")
        role = s.get("role", name)
        kind = s.get("kind", "")
        stage_names.append(name)
        rv = _role_visual(role) if role != "supervisor" else _role_visual("supervisor")
        if i < (current_stage_idx or 0):
            st = "done"
        elif i == current_stage_idx:
            st = "current"
        else:
            st = "pending"
        stages.append({
            "name": name, "role": role, "kind": kind,
            "icon": rv["icon"], "color": rv["color"], "label": rv["label"],
            "status": st,
        })

    # Status
    status, status_class, status_text, status_icon, status_label = _determine_status(state)

    # Elapsed — scoped to the CURRENT run (Day-4 live fix: the timer used to
    # pick the first agent line of the whole multi-run log — 47h on a fresh run)
    if state and status == "running":
        elapsed_epoch, elapsed_frozen, elapsed_str = _run_elapsed(project_dir, "running")
    elif state and status in ("verify", "done", "dead", "salvage"):
        # AUD10-01: "done" → frozen span of the finished run (no growth).
        # "idle" is NOT here: pre-start state must not show a timer computed
        # from a previous run's log — it goes to the else branch (no timer).
        elapsed_epoch, elapsed_frozen, elapsed_str = _run_elapsed(project_dir, status)
    else:
        elapsed_epoch, elapsed_frozen, elapsed_str = 0, False, ""

    # Total across iterations (closed runs + the open one, if any)
    total_closed_sec, total_running, total_open_start = _total_elapsed(project_dir)

    if not elapsed_str and elapsed_epoch and not elapsed_frozen:
        elapsed_str = _format_elapsed(
            datetime.fromtimestamp(elapsed_epoch, tz=timezone.utc).isoformat()
        )

    # TODO id + worker status (needed by the chat and the todo pane)
    todo_id = state.get("todo_id") if state else None
    worker = _read_worker_activity(state)
    if worker and worker.get("active"):
        worker["last_line"] = _read_worker_last_line(project_dir, todo_id)

    # Stage spans of the CURRENT run — start/end times for the chat entries
    spans = _stage_spans(project_dir)
    stage_kind_now = str(state.get("stage_kind", "") or "") if state else ""
    current_stage = str(state.get("stage_name", "") or "") if state else ""

    # Handoffs (chat-style, NEWEST FIRST) + the active stage entry on top
    handoffs = _read_handoffs(project_dir, stage_order=stage_names, todo_id=todo_id)
    handoff_chat: list[dict[str, Any]] = []

    if current_stage and status in ("running", "verify"):
        rv = _role_visual(current_stage)
        span = spans.get(current_stage) or {}
        handoff_chat.append({
            "role": current_stage,
            "icon": rv["icon"],
            "color": rv["color"],
            "label": rv["label"],
            "content_html": "",
            "duration": "",
            "rev": f"active|{current_stage}|{span.get('start', 0)}",
            "started_at": _fmt_clock(span.get("start")),
            "ended_at": "",
            "started_epoch": int(span.get("start") or 0),
            "active": True,
            "awaiting": stage_kind_now == "verify",
            "is_verify": stage_kind_now == "verify",
            "line": (worker or {}).get("last_line", "") if stage_kind_now != "verify" else "",
        })

    for h in reversed(handoffs):  # newest first for display
        rv = _role_visual(h["role"])
        span = spans.get(h["role"]) or {}
        started, ended = span.get("start"), span.get("end")
        duration = ""
        if started and ended:
            duration = _format_elapsed_from_seconds(int(ended - started))
        handoff_chat.append({
            "role": h["role"],
            "icon": rv["icon"],
            "color": rv["color"],
            "label": rv["label"],
            "content_html": h.get("content_html", ""),
            "duration": duration,
            "rev": h.get("rev", ""),
            "started_at": _fmt_clock(started),
            "ended_at": _fmt_clock(ended),
            "started_epoch": int(started or 0),
            "active": False,
            "awaiting": False,
            "is_verify": False,
            "line": "",
        })

    # TODO content
    todo_content_html = _read_todo_content(project_dir, todo_id)
    todo_summary = _read_todo_summary(project_dir, todo_id)
    todo_diff_stat = _read_todo_diff_stat(project_dir, todo_id)

    # TODO timeline
    todo_timeline = _build_todo_timeline(project_dir, todo_id)

    # Events
    log_file = paths.agentic_dir(project_dir) / "logs" / "orchestrator.log"
    events = []
    if log_file.is_file():
        try:
            events = _parse_log_events(log_file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass

    # Project name
    import yaml as _yaml
    config_file = paths.config_file(project_dir)
    project_name = "Project"
    if config_file.is_file():
        try:
            config = _yaml.safe_load(config_file.read_text(encoding="utf-8"))
            project_name = (config or {}).get("project", {}).get("name", "Project")
        except Exception:
            pass

    # Next stage preview
    next_stage = None
    if current_stage_idx is not None and 0 <= current_stage_idx < len(stages) - 1:
        ns = stages[current_stage_idx + 1]
        next_stage = {"icon": ns["icon"], "label": ns["label"], "color": ns["color"]}

    return {
        "project_name": project_name,
        "todo_id": todo_id or "",
        "todo_content_html": todo_content_html,
        "todo_summary": todo_summary,
        "todo_diff_stat": todo_diff_stat,
        "run": _run_brief_for_dashboard(project_dir),
        "status": status,
        "status_text": status_text,
        "status_icon": status_icon,
        "salvage_needed": bool(state.get("salvage_needed", False)) if state else False,
        "salvage_stage": state.get("salvage_stage") if state else None,
        "stages": stages,
        "stages_done": sum(1 for s in stages if s["status"] == "done"),
        "stages_total": len(stages),
        "next_stage": next_stage,
        "elapsed_epoch": elapsed_epoch,
        "elapsed_frozen": elapsed_frozen,
        "elapsed_str": elapsed_str,
        "total_elapsed_sec": total_closed_sec,
        "total_running": total_running,
        "total_run_started_epoch": total_open_start,
        "handoffs": handoff_chat,
        "todo_timeline": todo_timeline,
        "worker": worker,
        "events": events[-30:] if events else [],
        "pipeline_running": status in ("running", "verify"),
    }


def _format_elapsed_from_seconds(seconds: int) -> str:
    """Format seconds → unified 'Xd Xh Xm' / 'Xh Xm' / 'Xm Ys' / 'Ys'.

    Day-4: one formatter for every elapsed display (the JS ticker used to
    show '2865m' while the server showed '47h' — now both say '1d 23h 45m').
    """
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h {m}m"
