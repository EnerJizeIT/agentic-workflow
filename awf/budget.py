"""The unit's review-pass budget (TODO-0262).

A ``## REVIEW-фикс`` section is the supervisor's decision that the unit did
not pass and needs another round. The number of those sections is the unit's
price: the counter reads the TODO text only (the same ``awf.todos`` parser the
appender and the RUN-REPORT use), so a silent retry — a repeated stage, a
salvage, an extra attempt — never grows it.

``limits.max_review_passes`` in ``.agentic/config.yaml`` (default 3, ``0``
disables the limit, the valid range is 0..20) caps the rounds. Once the count
reaches the cap the unit does not continue: the verify entry, ``awf continue``
and ``awf run_next`` stop it and leave ``outbox/BUDGET-<id>.md`` for the owner.
No role switch, no retry — the supervisor decides what happens next.

The module also owns the run economics: per-unit stage times parsed from the
pipeline log (``orchestrator.log``, the stage log) and the gate failure counts
from ``context/GATES-<id>.md``. Missing data is ``n/a``, never a measured zero.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths, todos
from ._log import archive_files
from ._markdown import section_body
from .infra import managed_fs

#: The number of review rounds a unit may take without an explicit config key.
DEFAULT_MAX_REVIEW_PASSES = 3
#: The valid upper bound of ``limits.max_review_passes`` — a typo like 300 is
#: a config refusal, not a silent "no limit".
MAX_REVIEW_PASSES_CEILING = 20

#: The report order of the pipeline stages; an unknown stage name is appended
#: after them with its own label.
_STAGE_ORDER = ("plan", "impl", "arch", "qa", "audit", "verify")
_STAGE_LABELS = {
    "plan": "plan",
    "agent-implementer": "impl",
    "implementer": "impl",
    "implement": "impl",
    "agent-architect-review": "arch",
    "architect-review": "arch",
    "architector": "arch",
    "agent-qa-review": "qa",
    "qa-review": "qa",
    "qa": "qa",
    "agent-project-auditor": "audit",
    "project-auditor": "audit",
    "auditor": "audit",
    "verify": "verify",
}

_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]\s*(.*)$")
_STAGE_RE = re.compile(r"Stage\s+\d+(?:/\d+)?:\s+(\S+)")
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_QA_TEST_RE = re.compile(r"^def\s+(test_\w+)", re.MULTILINE)


class BudgetConfigError(ValueError):
    """``limits.max_review_passes`` is not an integer in the valid range."""


def _parse_ts(raw: str):
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def max_review_passes(config: dict) -> int:
    """The configured cap — a validated integer in 0..20.

    ``0`` disables the limit. Anything else than an integer in range is a
    config refusal (``BudgetConfigError``): a typo must not silently disable
    the gate and let a unit polish forever.
    """
    raw = cfg_mod.get(config or {}, "limits.max_review_passes", DEFAULT_MAX_REVIEW_PASSES)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BudgetConfigError(
            "limits.max_review_passes must be an integer 0.."
            f"{MAX_REVIEW_PASSES_CEILING}, got {raw!r}"
        )
    if raw < 0 or raw > MAX_REVIEW_PASSES_CEILING:
        raise BudgetConfigError(
            "limits.max_review_passes must be 0.."
            f"{MAX_REVIEW_PASSES_CEILING} (0 disables the limit), got {raw}"
        )
    return raw


def review_headings(project_dir: str | Path, todo_id: str) -> list[str]:
    """The unit's ``## REVIEW-фикс`` heading lines, in file order."""
    return todos.review_headings(todos.read_todo_text(project_dir, todo_id))


def review_passes(project_dir: str | Path, todo_id: str) -> int:
    """How many review rounds the unit already took — sections, nothing else.

    One ritual round writes two headings: the supervisor's section and the
    audit's. Counting headings doubled the price and exhausted the cap after
    half the intended rounds (audit AU1). The audit heading is evidence of the
    same round, not a new pass; the supervisor's sections are the passes. The
    headings come from the ONE parser (TODO-0231) so the readers cannot drift.
    """
    headings = todos.review_headings(
        todos.read_todo_text(project_dir, todo_id)
    )
    return sum(
        1
        for heading in headings
        if "аудит" not in heading.casefold()
        and "spec" not in heading.casefold()
    )


def _at_cap(passes: int, cap: int) -> bool:
    """The one exhaustion predicate: ``0`` disables the cap."""
    return cap > 0 and passes >= cap


def is_exhausted(project_dir: str | Path, todo_id: str, config: dict) -> bool:
    """True when the unit reached its cap (and the cap is not disabled)."""
    return _at_cap(review_passes(project_dir, todo_id), max_review_passes(config))


def note_path(project_dir: str | Path, todo_id: str) -> Path:
    """The owner-facing ``outbox/BUDGET-<id>.md`` path of a unit."""
    return paths.outbox(Path(project_dir)) / f"BUDGET-{todo_id}.md"


def note_exists(project_dir: str | Path, todo_id: str) -> bool:
    """True when a BUDGET note is on disk — the unit was stopped for budget.

    Read-only presence check: the surfaces (status, wait, watchdog) use it to
    answer with the owner's decision instead of ``awf_continue``. The note is
    the durable fact; the stage state may already be gone or stale.
    """
    return bool(todo_id) and note_path(project_dir, todo_id).is_file()


def stop_active(
    project_dir: str | Path, todo_id: str, config: dict | None = None
) -> bool:
    """True when a recorded BUDGET stop still holds under the CURRENT config.

    A note on disk proves a stop happened; it is not a permanent lock. An
    owner who raised ``limits.max_review_passes`` (or set it to 0) must not
    keep hearing "exhausted" from the status/wait surfaces — only the units
    still at their cap are actionable. A broken limit keeps the stop (the
    gate cannot be verified, same direction as the launch gates).
    """
    if not note_exists(project_dir, todo_id):
        return False
    try:
        cap = max_review_passes(
            config if config is not None else cfg_mod.load(Path(project_dir))
        )
    except BudgetConfigError:
        return True
    if cap <= 0:
        return False
    return review_passes(project_dir, todo_id) >= cap


def budget_hint(todo_id: str) -> str:
    """The owner-facing next step for an exhausted unit — never ``continue``."""
    return (
        f"Budget exhausted for {todo_id}: the review-pass limit is reached and "
        f"the unit belongs to the owner — raise `limits.max_review_passes`, "
        f"close the unit, or move the open items to a new TODO. Do not resume "
        f"it (see outbox/BUDGET-{todo_id}.md)."
    )


def check_and_note(
    project_dir: str | Path,
    todo_id: str,
    config: dict,
    *,
    logs_dir: str | Path | None = None,
) -> str:
    """Stop reason for an exhausted unit, or ``""`` when it may continue.

    On exhaustion writes ``outbox/BUDGET-<id>.md`` (managed write) and returns
    a one-line reason. The cap is validated here, so a bad config is a loud
    ``BudgetConfigError`` instead of an exhausted/not-exhausted guess.
    """
    cap = max_review_passes(config)
    if cap <= 0:
        return ""
    passes = review_passes(project_dir, todo_id)
    if passes < cap:
        return ""
    write_budget_note(
        project_dir,
        todo_id,
        passes=passes,
        cap=cap,
        logs_dir=logs_dir,
    )
    return f"unit {todo_id}: review budget exhausted ({passes}/{cap})"


def write_budget_note(
    project_dir: str | Path,
    todo_id: str,
    *,
    passes: int,
    cap: int,
    logs_dir: str | Path | None = None,
) -> Path:
    """Write ``outbox/BUDGET-<id>.md`` through the managed no-follow barrier."""
    project_dir = Path(project_dir)
    outbox = paths.outbox(project_dir)
    note = outbox / f"BUDGET-{todo_id}.md"
    text = budget_note_text(
        project_dir, todo_id, passes=passes, cap=cap, logs_dir=logs_dir
    )
    try:
        managed_fs.managed_write_text(
            note,
            text,
            root=managed_fs.agentic_root_for(note),
            mode=0o600,
        )
    except managed_fs.ManagedPathError as e:
        raise OSError(f"budget note {todo_id} refused: {e}") from e
    return note


def budget_note_text(
    project_dir: str | Path,
    todo_id: str,
    *,
    passes: int,
    cap: int,
    logs_dir: str | Path | None = None,
) -> str:
    """The owner-facing note: the price paid, the open items, the rounds."""
    stages = stage_durations(project_dir, todo_id)
    lines = [
        f"# BUDGET — {todo_id}: лимит витков исчерпан",
        "",
        f"passes: {passes}/{cap}",
        "",
        "## Открытые пункты",
        "",
        _last_open_items(project_dir, todo_id),
        "",
        "## Время по стадиям",
        "",
    ]
    if stages is None:
        lines.append("n/a — в stage-log нет данных по этому юниту")
    else:
        for label in _ordered_labels(stages["stages"]):
            minutes = stages["stages"].get(label)
            lines.append(f"- {label}: {minutes if minutes is not None else 'n/a'} мин")
    lines += ["", "## Заголовки витков", ""]
    headings = review_headings(project_dir, todo_id)
    lines.extend(f"- {h}" for h in headings)
    lines += [
        "",
        "Автоматической смены роли и ретрая нет: решение за владельцем — "
        "закрыть юнит, перенести пункты в новый или поднять "
        "`limits.max_review_passes`.",
        "",
    ]
    return "\n".join(lines)


def _last_open_items(project_dir: str | Path, todo_id: str) -> str:
    """The newest BLOCKED findings, else the QA findings test, else a marker."""
    project_dir = Path(project_dir)
    candidates: list[Path] = []
    live = paths.outbox(project_dir) / f"BLOCKED-{todo_id}.md"
    if live.is_file():
        candidates.append(live)
    archived = paths.done_dir(project_dir) / todo_id
    if archived.is_dir():
        # Newest first: the live note (if any) is the current blocker, the
        # archive copies follow from the latest to the oldest.
        candidates.extend(sorted(archived.glob("BLOCKED-*.md"), reverse=True))
    for note in candidates:
        try:
            text = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        body = section_body(text, "НАХОДКИ") or section_body(text, "НАПРАВЛЕНИЯ-ФИКСА")
        if body:
            return body

    short = todo_id.removeprefix("TODO-")
    context = paths.context_dir(project_dir)
    for name in (
        f"QA-{short}-findings-test.py",
        f"QA-{todo_id}-findings-test.py",
    ):
        qa_file = context / name
        if not qa_file.is_file():
            continue
        try:
            text = qa_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        names = _QA_TEST_RE.findall(text)
        detail = ", ".join(names) if names else "тесты находок"
        return f"QA-файл {name}: {detail}"
    return "нет данных"


# ─── stage economics ────────────────────────────────────────────────────


def _log_text(project_dir: Path) -> str:
    """The whole pipeline log: rotated archives first, then the live file."""
    log = paths.logs_dir(project_dir) / "orchestrator.log"
    chunks: list[str] = []
    for path in [*archive_files(log), log]:
        if not path.is_file():
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def _unit_segments(project_dir: Path, todo_id: str) -> list[dict]:
    """The pipeline segments of one unit: stage starts and the complete stamp."""
    marker = f"PIPELINE-{todo_id}.yaml"
    segments: list[dict] = []
    current: dict | None = None
    for line in _log_text(project_dir).splitlines():
        m = _TS_RE.match(line)
        if not m:
            continue
        ts = _parse_ts(m.group(1))
        if ts is None:
            continue
        msg = m.group(2)
        if marker in msg and ("snapshot captured" in msg or "resumed" in msg):
            if current is not None:
                segments.append(current)
            current = {"start": ts, "stages": [], "end": None}
            continue
        if current is None:
            continue
        if "Pipeline complete" in msg:
            current["end"] = ts
            segments.append(current)
            current = None
            continue
        stage_m = _STAGE_RE.search(msg)
        if stage_m:
            current["stages"].append((ts, stage_m.group(1)))
    if current is not None:
        segments.append(current)
    return segments


def _stage_label(name: str) -> str:
    return _STAGE_LABELS.get(name, name)


def _ordered_labels(stages: dict[str, int]) -> list[str]:
    extra = sorted(k for k in stages if k not in _STAGE_ORDER)
    return [*_STAGE_ORDER, *extra]


def stage_durations(
    project_dir: str | Path, todo_id: str
) -> dict | None:
    """Per-stage minutes and the unit's wall clock — ``None`` without a log.

    A stage's time runs from its start to the next stage start; the last stage
    ends at ``Pipeline complete``. A segment without the complete stamp leaves
    its last stage and the wall clock unknown (omitted / ``None``) — the
    report shows ``n/a`` instead of inventing a zero.
    """
    project_dir = Path(project_dir)
    segments = _unit_segments(project_dir, todo_id)
    stages: dict[str, int] = {}
    wall_seconds = 0.0
    wall_known = bool(segments)
    saw_stages = False
    for segment in segments:
        starts = segment["stages"]
        if not starts:
            wall_known = False
            continue
        saw_stages = True
        for index, (ts, name) in enumerate(starts):
            label = _stage_label(name)
            if index + 1 < len(starts):
                seconds = (starts[index + 1][0] - ts).total_seconds()
            elif segment["end"] is not None:
                seconds = (segment["end"] - ts).total_seconds()
            else:
                continue
            stages[label] = stages.get(label, 0) + int(round(seconds / 60))
        if segment["end"] is not None:
            wall_seconds += (segment["end"] - starts[0][0]).total_seconds()
        else:
            # An open segment makes the unit's wall clock partial — say
            # ``n/a`` instead of undercounting it.
            wall_known = False
    if not saw_stages:
        return None
    return {
        "stages": stages,
        "wall": int(round(wall_seconds / 60)) if wall_known else None,
    }


def _gate_statuses(project_dir: str | Path, todo_id: str) -> dict[str, str] | None:
    """The per-gate statuses of the unit's GATES report, or ``None``.

    ``None`` means "no report at all" — the report line says ``n/a``. A gate
    that IS in the report but was not measured reads ``skipped`` (or any
    other non-``pass``/``fail`` value) and stays distinguishable from a
    measured pass: requirement 3 of the unit — no data is ``n/a``, never a
    zero.
    """
    project_dir = Path(project_dir)
    report = paths.context_dir(project_dir) / f"GATES-{todo_id}.md"
    if not report.is_file():
        return None
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    sections: dict = {}
    match = _JSON_BLOCK_RE.search(text)
    if match:
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("sections"), dict):
            sections = payload["sections"]
    if sections:
        return {str(k): str(v).lower() for k, v in sections.items()}
    found: dict[str, str] = {}
    for gate in ("qa_findings", "contract_tests"):
        m = re.search(
            rf"^##\s+\d+\.\s+{gate}:\s+(PASS|FAIL)",
            text,
            re.MULTILINE,
        )
        if m:
            found[gate] = m.group(1).lower()
    return found


def gate_failures(project_dir: str | Path, todo_id: str) -> tuple[int, int] | None:
    """``(qa_fail, contract_fail)`` counts from the unit's GATES report.

    The report is written once per verify attempt (the last attempt wins), so
    a count is 0 or 1 per gate. ``None`` when the unit has no report at all —
    the report line then says ``n/a``. ``skipped`` is a zero here (it is not
    a failure); :func:`economics_line` keeps the ``n/a`` distinction.
    """
    statuses = _gate_statuses(project_dir, todo_id)
    if statuses is None:
        return None
    return (
        1 if statuses.get("qa_findings") == "fail" else 0,
        1 if statuses.get("contract_tests") == "fail" else 0,
    )


def _gate_label(status: str | None) -> str:
    """One gate's count for the RUN-REPORT: ``1 раз`` / ``0`` / ``n/a``."""
    if status == "fail":
        return "1 раз"
    if status == "pass":
        return "0"
    return "n/a"


def economics_line(project_dir: str | Path, todo_id: str) -> str:
    """One RUN-REPORT line: passes, wall, stages, gate failures."""
    project_dir = Path(project_dir)
    passes = review_passes(project_dir, todo_id)
    stages = stage_durations(project_dir, todo_id)
    if stages is None:
        wall = "n/a"
        stages_part = "n/a"
    else:
        wall = f"{stages['wall']} мин" if stages["wall"] is not None else "n/a"
        stages_part = "/".join(
            f"{label} {stages['stages'].get(label, 'n/a')}"
            for label in _ordered_labels(stages["stages"])
        )
    statuses = _gate_statuses(project_dir, todo_id)
    if statuses is None:
        gates = "n/a"
    else:
        gates = (
            f"qa_fail {_gate_label(statuses.get('qa_findings'))}, "
            f"contract_fail {_gate_label(statuses.get('contract_tests'))}"
        )
    return (
        f"- {todo_id}: passes: {passes}, wall: {wall}, "
        f"stages: {stages_part}, gates: {gates}"
    )


def status_fields(
    project_dir: str | Path, todo_id: str, config: dict
) -> dict[str, Any]:
    """The active-unit fields of ``awf_status``/``awf_brief``.

    A broken config degrades to the default here: a status read must never
    fall over (the config refusal belongs to the launch/verify gates).
    """
    try:
        cap = max_review_passes(config)
    except BudgetConfigError:
        cap = DEFAULT_MAX_REVIEW_PASSES
    passes = review_passes(project_dir, todo_id)
    return {
        "review_passes": passes,
        "review_passes_max": cap,
        "budget_exhausted": _at_cap(passes, cap),
    }


__all__ = [
    "BudgetConfigError",
    "DEFAULT_MAX_REVIEW_PASSES",
    "MAX_REVIEW_PASSES_CEILING",
    "budget_hint",
    "budget_note_text",
    "check_and_note",
    "economics_line",
    "gate_failures",
    "is_exhausted",
    "max_review_passes",
    "note_exists",
    "note_path",
    "review_headings",
    "review_passes",
    "stage_durations",
    "status_fields",
    "stop_active",
    "write_budget_note",
]
