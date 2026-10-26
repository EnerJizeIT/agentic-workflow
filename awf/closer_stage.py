"""The closer stage's ONE decision point per unit (IMP2, R2).

A stage that declares ``skip_if: checklist_green`` is decided at its entry:
the definition-of-done gate runs on the current tree, the verdict is logged
as ``dod: N/M``, and the stage either skips (green), runs one closer worker
(red) or is blocked (a static defect, a bad config key, an unreadable block).

The decision replaces the gate call, not the gate: ``checklist_gate.run``
still executes the probes and owns the cache. This module adds the single
place where the answer becomes an action, the first verdict for the
statistics (``context/CLOSER-<todo>.json``) and the stage-state fields the
engine records for a skip.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import checklist_gate, paths, todos
from . import config as cfg_mod
from ._log import log as _log
from .checklists import ChecklistError
from .infra import managed_fs
from .unit_contract import parse_todo_contract

#: The config key that caps closer runs (IMP4 owns the counter; IMP2 only
#: refuses a value the key can never mean).
CLOSER_ATTEMPTS_KEY = "checklist.closer_attempts"
DEFAULT_CLOSER_ATTEMPTS = 1
MAX_CLOSER_ATTEMPTS = 2

#: The stage-state reason recorded on a green skip.
SKIP_REASON = "checklist_green"


class CloserConfigError(ValueError):
    """``checklist.closer_attempts`` is not an integer in the valid range."""


@dataclass
class Decision:
    """One entry's answer: run, skip or block, with the facts behind it."""

    kind: str
    verdict: dict = field(default_factory=dict)
    log_line: str = ""
    note: str = ""

    @property
    def skip_reason(self) -> str:
        return SKIP_REASON if self.kind == "skip" else ""


def closer_attempts(config: dict) -> int:
    """The configured run cap — a validated integer in 1..2.

    No key means the default (1). Anything else than an integer in range —
    including a bool and an empty string — is a config refusal: a typo must
    not silently become a different budget.
    """
    raw = cfg_mod.get(config or {}, CLOSER_ATTEMPTS_KEY, DEFAULT_CLOSER_ATTEMPTS)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise CloserConfigError(
            f"{CLOSER_ATTEMPTS_KEY} must be an integer 1..{MAX_CLOSER_ATTEMPTS}, "
            f"got {raw!r}"
        )
    if raw < 1 or raw > MAX_CLOSER_ATTEMPTS:
        raise CloserConfigError(
            f"{CLOSER_ATTEMPTS_KEY} must be 1..{MAX_CLOSER_ATTEMPTS}, got {raw}"
        )
    return raw


def state_path(project_dir: str | Path, todo_id: str) -> Path:
    """``.agentic/context/CLOSER-<todo>.json`` — the unit's first verdict."""
    return paths.context_dir(Path(project_dir)) / f"CLOSER-{todo_id}.json"


def read_state(project_dir: str | Path, todo_id: str) -> dict | None:
    """The recorded state, ``None`` when absent.

    A file that exists but cannot be read or parsed raises ``ValueError``:
    the caller blocks instead of treating a corrupt record as a fresh unit
    (a silent overwrite would lose the first verdict and the counter).
    """
    path = state_path(project_dir, todo_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{path.name} is unreadable: {e}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return data


def _verdict_facts(verdict: dict) -> dict:
    """The ``first_verdict`` shape: the answer, without cache bookkeeping."""
    keys = (
        "status",
        "ok",
        "items",
        "missing",
        "manual",
        "tree_sha",
        "block_sha",
        "probe_sha",
    )
    return {key: verdict.get(key) for key in keys}


def _write_record(project_dir: Path, todo_id: str, record: dict) -> None:
    """Write the closer record.

    Named ``_write_record``, not ``_write_state``: the pipeline's own state
    has ONE writer (``pipeline_state.write_state``), and a same-named
    private here reads like a second one at the call sites.
    """
    managed_fs.managed_write_text(
        state_path(project_dir, todo_id),
        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        root=project_dir,
    )


def dod_line(verdict: dict) -> str:
    """``dod: N/M`` with the non-empty ``missing`` and ``manual`` lists.

    ``dod: none`` when the unit has no block at all; an empty block reads
    ``dod: 0/0 (missing: (empty block))``.
    """
    if verdict.get("status") == "none":
        return "dod: none"
    items = verdict.get("items") or []
    total = len(items)
    ok = sum(1 for item in items if item.get("status") == "ok")
    line = f"dod: {ok}/{total}"
    missing = verdict.get("missing") or []
    manual = verdict.get("manual") or []
    if missing:
        line += f" (missing: {', '.join(missing)})"
    if manual:
        line += f" (manual: {', '.join(manual)})"
    return line


def _blocked_note(
    project_dir: Path,
    todo_id: str,
    *,
    verdict_line: str,
    findings: list[str],
    directions: list[str],
) -> str:
    """Write the standard BLOCKED note and return its verdict line."""
    outbox = paths.outbox(project_dir)
    lines = [
        "## ВЕРДИКТ",
        "",
        verdict_line,
        "",
        "## НАХОДКИ",
        "",
        *(f"- {item}" for item in findings),
        "",
        "## НАПРАВЛЕНИЯ-ФИКСА",
        "",
        *(f"- {item}" for item in directions),
        "",
        "## ПЕРЕНОСЫ",
        "",
        "- нет",
        "",
    ]
    try:
        root = managed_fs.agentic_root_for(outbox)
        managed_fs.managed_write_text(
            outbox / f"BLOCKED-{todo_id}.md",
            "\n".join(lines),
            root=root,
        )
        # PA3 (TODO-0269): the pair is the closure contract — ``is_closed``,
        # ``blocked_count`` and the wait-for-event reader key off the .ready
        # marker, not the note. A note without it left the unit "active".
        managed_fs.managed_write_text(
            outbox / f"BLOCKED-{todo_id}.ready",
            "",
            root=root,
        )
    except (OSError, managed_fs.ManagedPathError) as e:
        # The blocked decision still stops the stage; the note is the
        # owner-facing trace, not the gate. The failure is logged, never
        # silent: a blocked state without a reason is worse than a slow one.
        _log(paths.logs_dir(project_dir), f"CLOSER: BLOCKED note not written: {e}")
    return verdict_line


def _blocked(
    project_dir: Path,
    todo_id: str,
    *,
    log_line: str,
    verdict: dict,
    reason: str,
    findings: list[str],
    directions: list[str],
) -> Decision:
    _blocked_note(
        project_dir,
        todo_id,
        verdict_line=reason,
        findings=findings,
        directions=directions,
    )
    return Decision(kind="blocked", verdict=verdict, log_line=log_line, note=reason)


def _static_block_defect(verdict: dict) -> bool:
    """True when the gate produced no runnable item at all.

    ``checklist_gate`` returns a static record — ``status="checked"`` with
    zero ``items`` — for an empty block and for a block the strict parser
    refuses; ``missing`` then carries ``(empty block)`` or the item ids it
    could not read. A red block always has items, so "checked with zero
    items" is the ONE static shape; re-reading the markers here would be a
    second copy of the gate's internal wording.
    """
    return verdict.get("status") == "checked" and not (verdict.get("items") or [])


def decide(
    project_dir: str | Path,
    todo_id: str,
    stage: Any,
    config: dict,
) -> Decision:
    """Decide one entry of a stage that declares ``skip_if``.

    The order is fixed (architecture K2): stage/config defects first (no gate
    run, no spawn), then the gate on the current tree, then the first verdict
    and the one ``dod:`` line, then a static block defect, then green/red.
    """
    project = Path(project_dir).resolve()

    # 1. Static stage defect: the closer must NAME the opt-out from the write
    # check. A ``skip_if`` stage with an implicit allowlist is a config typo,
    # not a stage that should run unbounded.
    if stage.skip_if and stage.write_check != "off":
        reason = (
            f"blocked — стадия `{stage.name}` объявила `skip_if: {stage.skip_if}` "
            f"без `write_check: \"off\"`; спавна и траты бюджета нет"
        )
        return _blocked(
            project,
            todo_id,
            log_line=reason,
            verdict={},
            reason=reason,
            findings=[
                f"стадия `{stage.name}`: `skip_if={stage.skip_if!r}`, "
                f"`write_check={stage.write_check!r}`",
                "условный запуск без явного отказа от write-манифеста — "
                "статический дефект стадии",
            ],
            directions=[
                f"добавь стадии `{stage.name}` ключ `write_check: \"off\"` "
                "или убери `skip_if`",
            ],
        )

    # 2. Config key: validated before any gate run, no default substitution.
    try:
        closer_attempts(config)
    except CloserConfigError as e:
        reason = f"blocked — неверный ключ конфига: {e}"
        return _blocked(
            project,
            todo_id,
            log_line=reason,
            verdict={},
            reason=reason,
            findings=[f"конфиг: `{e}`"],
            directions=[f"исправь `{CLOSER_ATTEMPTS_KEY}` (1 или 2) в `.agentic/config.yaml`"],
        )

    # 3. The gate on the CURRENT tree. Its cache is the only cache.
    try:
        verdict = checklist_gate.run(project, todo_id)
    except (checklist_gate.ChecklistGateError, ChecklistError) as e:
        reason = f"blocked — чек-лист не выполнен: {e}"
        return _blocked(
            project,
            todo_id,
            log_line=reason,
            verdict={},
            reason=reason,
            findings=[f"гейт: {e}"],
            directions=["проверь TODO юнита и блок `## Чек-лист завершения`"],
        )

    # 4. The contract supplies the frozen wave for the statistics. A broken
    # contract fails closed: the verdict record would carry a wrong wave.
    text = todos.read_todo_text(project, todo_id)
    try:
        contract, _unknown = parse_todo_contract(text)
    except ValueError as e:
        reason = f"blocked — контракт TODO нечитаем: {e}"
        return _blocked(
            project,
            todo_id,
            log_line=f"{dod_line(verdict)} | {reason}",
            verdict=verdict,
            reason=reason,
            findings=[f"контракт TODO: {e}"],
            directions=["почини YAML-блок контракта в TODO и повтори стадию"],
        )
    wave = ""
    if isinstance(contract, dict):
        raw = contract.get("wave")
        if isinstance(raw, str):
            wave = raw.strip()

    # 5. First verdict — written once per unit, on the first entry (a green
    # skip included). A corrupt record blocks instead of being overwritten.
    try:
        record = read_state(project, todo_id)
    except ValueError as e:
        reason = f"blocked — состояние closer нечитаемо: {e}"
        return _blocked(
            project,
            todo_id,
            log_line=f"{dod_line(verdict)} | {reason}",
            verdict=verdict,
            reason=reason,
            findings=[str(e)],
            directions=[
                "разберись с `context/CLOSER-<todo>.json` вручную: "
                "молча перезаписывать историю запрещено",
            ],
        )
    if record is None:
        record = {
            "todo_id": todo_id,
            "wave": wave,
            "first_verdict": _verdict_facts(verdict),
            "attempts": 0,
        }
        try:
            _write_record(project, todo_id, record)
        except (OSError, managed_fs.ManagedPathError) as e:
            reason = f"blocked — не удалось записать CLOSER-{todo_id}.json: {e}"
            return _blocked(
                project,
                todo_id,
                log_line=f"{dod_line(verdict)} | {reason}",
                verdict=verdict,
                reason=reason,
                findings=[str(e)],
                directions=["проверь права на `.agentic/context/`"],
            )

    line = dod_line(verdict)

    # 6. A block that cannot be executed is the owner's fix, not a closer
    # run: no spawn, no budget.
    if _static_block_defect(verdict):
        reason = "blocked — блок чек-листа неисполним: починить блок"
        return _blocked(
            project,
            todo_id,
            log_line=line,
            verdict=verdict,
            reason=reason,
            findings=[
                f"`missing`: {', '.join(verdict.get('missing') or [])}",
                "пустой или нечитаемый блок не даёт ни одной пробы — "
                "closer закрывать нечего",
            ],
            directions=["починить блок `## Чек-лист завершения` в TODO"],
        )

    # 7. Green (including "no block" and manual-only) skips without a worker.
    # ``manual`` items never launch the closer; ``ok`` ignores them already.
    if verdict.get("status") == "none" or verdict.get("ok"):
        return Decision(kind="skip", verdict=verdict, log_line=line)

    # 8. Red — the closer runs. The attempt counter and its cap belong to
    # IMP4; this entry only names the decision.
    return Decision(kind="run", verdict=verdict, log_line=line)


__all__ = [
    "CLOSER_ATTEMPTS_KEY",
    "CloserConfigError",
    "Decision",
    "SKIP_REASON",
    "closer_attempts",
    "decide",
    "dod_line",
    "read_state",
    "state_path",
]
