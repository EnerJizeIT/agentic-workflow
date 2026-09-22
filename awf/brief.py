"""RUN4 #1 (TODO-0051): ``awf brief`` — карточка погружения и восстановления.

Компактная карточка супервизора, собранная из ЖИВОГО состояния проекта
(не статичный документ, чтобы не протухать): шапка (версия, проект,
фаза, дата), что дальше, состояние (забег, задачи, флаги, последний
сигнал), карта инструментов по ситуациям, ритуалы, рецепты
восстановления, что нового.

Детерминизм: при том же состоянии два вызова дают идентичный текст
(кроме строки даты). Бюджет: ≤900 слов (тест в tests/unit/test_brief.py).

Это лиственный модуль: бизнес-логика (status, run) приходит извне
(``awf.api.brief``), чтобы не тянуть тяжёлый ``awf.api`` на уровне
импорта (AUD14-05).
"""
from __future__ import annotations

import ast
import importlib.util
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from . import paths

MAX_WORDS = 900
_PHASE_PROMPT_MAX_WORDS = 90
_CHANGELOG_MAX_LINES = 10
_NEXT_ACTION_MAX_WORDS = 30

SETUP_HINT = (
    "New project — setup chain: `awf_init` → `awf_set_goal` → "
    "`awf_open_project_setup_form` (owner submits) → `awf_confirm_normalized` "
    "→ first TODO (`awf_dispatch_todo`)."
)
LIVE_LINE = "Live project: setup chain not needed — continue the working cycle."
NEW_LINE = "New project (setup chain applies)."

FEEDBACK_LINE = (
    "Friction with awf → `awf feedback --type bug|feature` (report to the "
    "owner). Do not stay silent."
)

RITUALS: list[str] = [
    "verify: `awf tree-sha` → your own probes (the TODO's verify commands, "
    "`git diff`) → `awf_approve(todo, evidence=..., verified_sha=...)`",
    "run close: `awf_run_finish` — RUN-REPORT to the outbox",
    "incident (net/salvage): infrastructure first "
    "(`opencode run --auto --agent <role> -- 'say hello'`), then "
    "`awf_continue --from-stage <stage>` / `awf_retry_stage`",
    "hygiene: `awf_unblock` (stale closure), `awf_todo_remove` (never "
    "started), `awf_restore` (archived without work)",
]


@dataclass
class BriefResult:
    """Result of :func:`awf.api.brief` — the onboarding/recovery card."""

    version: str
    project: str
    phase: str
    date: str
    is_live_project: bool
    live_line: str
    setup_hint: str
    next_action: str
    phase_summary: str
    phase_clipped: bool
    run: dict[str, Any] | None
    active_todos: list[dict[str, Any]]
    blocked: list[str]
    salvage_stage: str | None
    last_signal: str | None
    pipeline_running: bool
    pipeline_pid: int | None
    current_stage: str | None
    tool_map: list[dict[str, Any]]
    rituals: list[str]
    recovery: str
    doctrine: list[str]
    what_new: str
    text: str = field(default="")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Data files ──────────────────────────────────────────────────────────


def _data_file(name: str) -> Path:
    return Path(__file__).resolve().parent / "data" / name


def load_tool_map() -> list[dict[str, Any]]:
    """Groups from ``awf/data/tool_map.yaml`` (normalized entry shape)."""
    raw = yaml.safe_load(_data_file("tool_map.yaml").read_text(encoding="utf-8")) or {}
    groups: list[dict[str, Any]] = []
    for g in raw.get("groups") or []:
        tools = [
            {
                "mcp": str(t.get("mcp") or ""),
                "cli": str(t.get("cli") or ""),
                "when": str(t.get("when") or ""),
            }
            for t in (g.get("tools") or [])
        ]
        groups.append({"title": str(g.get("title") or ""), "tools": tools})
    return groups


def load_recovery() -> str:
    """``awf/data/recovery.md`` — one recipe per situation."""
    try:
        return _data_file("recovery.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def tool_registry() -> dict[str, list[str]]:
    """All awf tools: MCP tools of agent_workflow_ui + CLI subcommands.

    The MCP list is parsed from the plugin's server.py (``add_tool``
    registrations) — a live registry, not a hand-maintained copy.
    Returns {"mcp": [...], "cli": [...]} (sorted); "mcp" is [] when the
    plugin package is not installed.
    """
    return {"mcp": _mcp_tool_names(), "cli": _cli_subcommands()}


def _cli_subcommands() -> list[str]:
    from . import cli

    _parser, sub = cli._build_parser()
    return sorted(sub.choices.keys())


def _mcp_tool_names() -> list[str]:
    spec = importlib.util.find_spec("agent_workflow_ui")
    if spec is None or not spec.submodule_search_locations:
        return []
    server_py = Path(spec.submodule_search_locations[0]) / "server.py"
    try:
        tree = ast.parse(server_py.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_tool"
        ):
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    names.add(str(kw.value.value))
    return sorted(names)


def map_coverage(registry: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Both directions of the map/registry check.

    ``missing_from_map`` — registry tools absent from every group;
    ``unknown_in_map`` — map entries that do not exist in the registry.
    Both lists empty = the map is in sync.
    """
    reg = registry if registry is not None else tool_registry()
    mcp_reg, cli_reg = set(reg["mcp"]), set(reg["cli"])
    seen_mcp: set[str] = set()
    seen_cli: set[str] = set()
    unknown: set[str] = set()
    for group in load_tool_map():
        for t in group["tools"]:
            if t["mcp"]:
                seen_mcp.add(t["mcp"])
                if t["mcp"] not in mcp_reg:
                    unknown.add(t["mcp"])
            if t["cli"]:
                seen_cli.add(t["cli"])
                if t["cli"] not in cli_reg:
                    unknown.add(t["cli"])
    missing = (mcp_reg - seen_mcp) | (cli_reg - seen_cli)
    return {"missing_from_map": sorted(missing), "unknown_in_map": sorted(unknown)}


# ─── Card sections ───────────────────────────────────────────────────────


def _clip_by_words(text: str, max_words: int) -> tuple[str, bool]:
    """Clip near max_words: whole lines, then a word-level cut on the line
    where the budget runs out. Returns (text, clipped)."""
    text = text.strip()
    if len(text.split()) <= max_words:
        return text, False
    kept: list[str] = []
    count = 0
    for line in text.splitlines():
        w = line.split()
        if not w:
            kept.append(line)
            continue
        if count + len(w) <= max_words:
            kept.append(line)
            count += len(w)
        else:
            kept.append(" ".join(w[: max_words - count]).rstrip())
            break
    return "\n".join(kept).rstrip(), True


def latest_changelog(
    max_lines: int = _CHANGELOG_MAX_LINES, max_words: int = 80
) -> str:
    """Latest section of the awf-repo CHANGELOG, '' if absent.

    Capped by lines (spec) AND words (the card has a 900-word budget —
    a long Unreleased entry would otherwise eat half of it).
    Only the repo layout (CHANGELOG.md next to the package parent) — an
    installed wheel does not ship it, and the card degrades gracefully.
    """
    changelog = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    if not changelog.is_file():
        return ""
    try:
        lines = changelog.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start = next((i for i, ln in enumerate(lines) if ln.startswith("## [")), None)
    if start is None:
        return ""
    out: list[str] = []
    for ln in lines[start:]:
        if out and ln.startswith("## ["):
            break
        out.append(ln)
        if len(out) >= max_lines:
            break
    text = "\n".join(out).rstrip()
    clipped, was = _clip_by_words(text, max_words)
    return clipped + (" …" if was else "")


def doctrine_lines(project_dir: Path) -> list[str]:
    """One line per doctrine file: ``- `file` — H1 title`` (U9 mechanism)."""
    from . import doctrine

    lines: list[str] = []
    for name, content in doctrine.load_doctrine_files(project_dir):
        title = next(
            (ln[2:].strip() for ln in content.splitlines() if ln.startswith("# ")),
            "",
        )
        lines.append(f"- `{name}` — {title}" if title else f"- `{name}`")
    return lines


def _tool_line(t: dict[str, str]) -> str:
    """One map line. Both names are kept (spec) but rendered compact:
    `` `awf_start`/`start` `` — the card has a word budget."""
    mcp, cli, when = t.get("mcp", ""), t.get("cli", ""), t.get("when", "")
    if mcp and cli:
        name = f"`{mcp}`/`{cli}`"
    elif mcp:
        name = f"`{mcp}`"
    else:
        name = f"`awf {cli}`"
    return f"- {name} — {when}"


def _state_lines(r: BriefResult) -> list[str]:
    lines: list[str] = []
    if r.run:
        bits = [f"position {r.run.get('position', '?')}"]
        budget = int(r.run.get("budget_minutes") or 0)
        if budget:
            bits.append(
                f"budget {r.run.get('budget_left_minutes')}/{budget} min "
                "(productive)"
            )
        if r.run.get("no_checkpoints"):
            bits.append("no_checkpoints")
        note = r.run.get("note") or ""
        lines.append(f"- run: active={r.run.get('active')}, " + ", ".join(bits))
        if note:
            lines.append(f"- run note: {note}")
    if r.pipeline_running:
        stage = f" · stage: {r.current_stage}" if r.current_stage else ""
        lines.append(f"- pipeline: RUNNING (PID {r.pipeline_pid}){stage}")
    for t in r.active_todos:
        prog = t.get("progress")
        if prog:
            lines.append(f"- active: {t['todo_id']} ({prog['done']}/{prog['total']} tasks)")
        else:
            lines.append(f"- active: {t['todo_id']} (worker has not started)")
    if r.blocked:
        lines.append(f"- blocked: {', '.join(r.blocked)}")
    if r.salvage_stage:
        lines.append(f"- salvage: stage {r.salvage_stage}")
    if r.last_signal:
        lines.append(f"- last signal: {r.last_signal}")
    return lines


def _demote_headings(text: str) -> str:
    """Shift #/##/### down two levels so the embedded phase prompt does not
    collide with the card's own ``## `` sections."""
    out = []
    for ln in text.splitlines():
        m = re.match(r"^(#{1,3}) ", ln)
        if m:
            ln = "#" * (len(m.group(1)) + 2) + ln[len(m.group(1)) :]
        out.append(ln)
    return "\n".join(out)


def render_brief(r: BriefResult) -> str:
    """Render the card as markdown. Pure function of the result fields."""
    L: list[str] = [f"# awf brief — {r.project}", ""]
    L.append(f"- awf: {r.version} · phase: {r.phase} · date: {r.date}")
    L.append(f"- {r.live_line}")
    L.append("")
    L.append("## What's next")
    L.append("")
    if r.is_live_project:
        if r.next_action:
            L.append(f"- next: {r.next_action}")
        if r.phase_summary:
            L.append("")
            L.append(_demote_headings(r.phase_summary))
            if r.phase_clipped:
                L.append("(… full version: `awf_current_step`)")
    else:
        L.append(r.setup_hint)
    L.append("")
    L.append("## State")
    L.append("")
    L.extend(_state_lines(r) or ["- no active run, tasks, or flags"])
    L.append("")
    L.append("## Tool map")
    L.append("")
    for group in r.tool_map:
        L.append(f"**{group['title']}**")
        for t in group["tools"]:
            L.append(_tool_line(t))
        L.append("")
    L.append("## Rituals")
    L.append("")
    for ritual in r.rituals:
        L.append(f"- {ritual}")
    L.append("")
    L.append("## Recovery")
    L.append("")
    L.append(r.recovery.rstrip())
    if r.doctrine:
        L.append("")
        L.append("**Doctrine** (in every role prompt):")
        L.extend(r.doctrine)
    if r.what_new:
        L.append("")
        L.append("## What's new")
        L.append("")
        L.append(_demote_headings(r.what_new.rstrip()))
    L.append("")
    L.append("## Feedback")
    L.append("")
    L.append(FEEDBACK_LINE)
    return "\n".join(L).rstrip() + "\n"


def build_brief(
    project_dir: Path, *, status: Any | None = None, run: dict[str, Any] | None = None
) -> BriefResult:
    """Assemble the card. ``status``/``run`` come from awf.api (callers)."""
    import awf as _awf

    from . import phase as _phase

    project_dir = Path(project_dir).expanduser().resolve()
    has_agentic = paths.agentic_dir(project_dir).is_dir()

    project = project_dir.name
    if has_agentic:
        from . import config as _config

        try:
            project = _config.get(_config.load(project_dir), "project.name") or project
        except Exception:
            pass

    phase = "init"
    try:
        phase = _phase.detect_phase(project_dir)
    except Exception:
        pass

    is_live = False
    if has_agentic:
        try:
            is_live = _phase._is_live_project(project_dir)
        except Exception:
            is_live = False

    next_action = ""
    phase_summary = ""
    phase_clipped = False
    if is_live and status is not None:
        raw_action = str(status.expected_action or status.suggestion or "")
        next_action, clipped_action = _clip_by_words(raw_action, _NEXT_ACTION_MAX_WORDS)
        if clipped_action:
            next_action += " …"
        try:
            prompt = _phase.get_phase_prompt(phase, project_dir)
            phase_summary, phase_clipped = _clip_by_words(prompt, _PHASE_PROMPT_MAX_WORDS)
        except Exception:
            phase_summary = ""

    active_todos: list[dict[str, Any]] = []
    blocked: list[str] = []
    salvage_stage: str | None = None
    last_signal: str | None = None
    pipeline_running = False
    pipeline_pid: int | None = None
    current_stage: str | None = None
    if status is not None:
        active_todos = [dict(t) for t in status.active_todos]
        blocked = list(status.blocked_ids)
        if status.salvage_needed:
            salvage_stage = status.salvage_stage
        last_signal = status.last_signal
        pipeline_running = status.pipeline_running
        pipeline_pid = status.pipeline_pid
        current_stage = status.current_stage_name

    result = BriefResult(
        version=_awf.__version__,
        project=project,
        phase=phase,
        date=date.today().isoformat(),
        is_live_project=is_live,
        live_line=LIVE_LINE if is_live else NEW_LINE,
        setup_hint="" if is_live else SETUP_HINT,
        next_action=next_action,
        phase_summary=phase_summary,
        phase_clipped=phase_clipped,
        run=dict(run) if run else None,
        active_todos=active_todos,
        blocked=blocked,
        salvage_stage=salvage_stage,
        last_signal=last_signal,
        pipeline_running=pipeline_running,
        pipeline_pid=pipeline_pid,
        current_stage=current_stage,
        tool_map=load_tool_map(),
        rituals=list(RITUALS),
        recovery=load_recovery(),
        doctrine=doctrine_lines(project_dir),
        what_new=latest_changelog(),
    )
    result.text = render_brief(result)
    return result
