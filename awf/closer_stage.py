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

IMP4 adds the attempt budget around the same record: :func:`settle_run`
recomputes the gate after a signal and answers green / retry / blocked. A
completed run with a verdict charges one attempt; a salvaged signal is
re-checked with ``charge=False`` — green advances, red blocks, no attempt
is spent (a crash can never consume the budget).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import checklist_gate, paths, todos
from . import config as cfg_mod
from ._log import log as _log
from .checklists import CHECKLIST_HEADING, ChecklistError, block_hash
from .infra import managed_fs
from .unit_contract import parse_todo_contract

#: The config key that caps COMPLETED closer runs per unit (IMP4).
CLOSER_ATTEMPTS_KEY = "checklist.closer_attempts"
DEFAULT_CLOSER_ATTEMPTS = 1
MAX_CLOSER_ATTEMPTS = 2

#: Allowance keys whose default is 0: an absent key and an explicit 0 mean
#: the same check, so they hash the same (TODO-0298). The one place the pair
#: is spelled — ``FROZEN_CONTRACT_KEYS`` is built from it, so a new allowance
#: cannot land in the hash without also landing in the zero-normalisation.
_ALLOWANCE_KEYS: tuple[str, ...] = ("xfail_allow", "skip_allow")

#: The contract keys frozen before the first closer (IMP5, R5; IMP4,
#: TODO-0298): the closer may not rewrite the check semantics; ``files`` is
#: a report, not a norm. ``xfail_allow``/``skip_allow`` are part of the norm
#: too: a unit that adds an allowance widens what counts as green, so the
#: hash must move.
FROZEN_CONTRACT_KEYS: tuple[str, ...] = (
    "verify", "gates", "prove_red",
) + _ALLOWANCE_KEYS

#: The one wording AC12/AC13 require on a norm edit.
NORM_EDIT_REASON = "closer edited the checklist/contract"

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


@dataclass
class RunOutcome:
    """The answer after a completed closer run: green, retry or blocked.

    The verdict itself is not carried back: the engine only needs the colour,
    the counter (for the log line) and the note it may print. A red verdict
    already reached the owner through the BLOCKED note written here.
    """

    kind: str
    attempts: int = 0
    log_line: str = ""


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


#: The record's filename prefix — the one place the name is spelled.
STATE_PREFIX = "CLOSER-"


def state_path(project_dir: str | Path, todo_id: str) -> Path:
    """``.agentic/context/CLOSER-<todo>.json`` — the unit's first verdict."""
    return paths.context_dir(Path(project_dir)) / f"{STATE_PREFIX}{todo_id}.json"


def state_paths(project_dir: str | Path) -> list[Path]:
    """Every recorded unit state, in sorted path order (R6 statistics).

    The listing lives next to the writer: a reader that re-spells
    ``CLOSER-*.json`` silently misses the file the moment this module
    renames it — the same rule :mod:`awf.unit_history` follows.
    """
    directory = paths.context_dir(Path(project_dir))
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{STATE_PREFIX}*.json"))


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
    attempts = data.get("attempts", 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError(
            f"{path.name} has an invalid attempts counter: {attempts!r}"
        )
    before = data.get("before")
    if before is not None and not isinstance(before, dict):
        raise ValueError(f"{path.name} has an invalid before snapshot: {before!r}")
    return data


def _attempts_of(record: dict) -> int:
    """The counter of a record already validated by :func:`read_state`."""
    return int(record.get("attempts", 0))


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


def contract_fingerprint(contract: dict | None) -> str:
    """sha256 of the frozen contract keys (IMP5, R5; TODO-0298).

    ``verify``, ``gates``, ``prove_red`` and the allowances
    ``xfail_allow``/``skip_allow`` decide what the unit is checked against;
    ``files`` and the other keys are a report, not a norm, and are
    deliberately outside the hash (AC13). An allowance equal to its default
    (0) is dropped, so an explicit ``xfail_allow: 0`` hashes like an absent
    key — the norm did not change. Key order never matters (``sort_keys``).
    No contract at all and an empty contract hash the same — an absent norm
    is a fixed value, not a crash.
    """
    frozen: dict[str, Any] = {}
    if isinstance(contract, dict):
        frozen = {k: contract[k] for k in FROZEN_CONTRACT_KEYS if k in contract}
        for key in _ALLOWANCE_KEYS:
            value = frozen.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value == 0:
                del frozen[key]
    blob = json.dumps(
        frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def norm_fingerprints(todo_text: str, contract: dict | None) -> dict:
    """The norm snapshot: the checklist block hash plus the contract hash."""
    return {
        "block_sha": block_hash(todo_text),
        "contract_sha": contract_fingerprint(contract),
    }


def _norm_surfaces(before: dict, current: dict) -> list[str]:
    """Names of the frozen surfaces whose fingerprint changed."""
    changed: list[str] = []
    if before.get("block_sha") != current.get("block_sha"):
        changed.append(f"блок `{CHECKLIST_HEADING}`")
    if before.get("contract_sha") != current.get("contract_sha"):
        changed.append(
            "контракт TODO (`" + "`, `".join(FROZEN_CONTRACT_KEYS) + "`)"
        )
    return changed


def _norm_refusal(
    before: dict, current: dict, changed: list[str]
) -> tuple[str, list[str], list[str]]:
    """The shared norm-edit verdict: reason, findings, directions (AC12)."""
    reason = f"blocked — {NORM_EDIT_REASON}: {', '.join(changed)}"
    findings = [
        f"изменено: {', '.join(changed)}",
        f"`block_sha`: {before.get('block_sha') or '—'} → "
        f"{current.get('block_sha') or '—'}",
        f"`contract_sha`: {before.get('contract_sha') or '—'} → "
        f"{current.get('contract_sha') or '—'}",
    ]
    directions = [
        "решение супервизора: верни блок/контракт в исходное состояние "
        "вручную (файл не откатывается автоматически) или выдай юнит заново",
    ]
    return reason, findings, directions


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


def _config_refusal(e: CloserConfigError) -> tuple[str, list[str], list[str]]:
    """The shared "bad config key" verdict: reason, findings, directions."""
    reason = f"blocked — неверный ключ конфига: {e}"
    return reason, [f"конфиг: `{e}`"], [
        f"исправь `{CLOSER_ATTEMPTS_KEY}` (1 или 2) в `.agentic/config.yaml`"
    ]


def _state_refusal(e: ValueError) -> tuple[str, list[str], list[str]]:
    """The shared corrupt-record verdict — fail closed, never overwrite."""
    reason = f"blocked — состояние closer нечитаемо: {e}"
    return reason, [str(e)], [
        "разберись с `context/CLOSER-<todo>.json` вручную: "
        "молча перезаписывать историю и счётчик запрещено",
    ]


def _record_refusal(
    todo_id: str, e: Exception
) -> tuple[str, list[str], list[str]]:
    """The shared "record not written" verdict — three writers, one wording.

    The first verdict, the ``before`` snapshot and the attempt counter all
    land in the same file through :func:`_write_record`; a failure of any of
    them is the same owner-facing refusal, so the text must not drift
    between the entry and :func:`settle_run`.
    """
    reason = f"blocked — не удалось записать CLOSER-{todo_id}.json: {e}"
    return reason, [str(e)], ["проверь права на `.agentic/context/`"]


def _cap_refusal(
    verdict: dict, attempts: int, limit: int
) -> tuple[str, list[str], list[str]]:
    """The shared budget-exhausted verdict: red at the cap is the owner's call.

    Both the entry (a restart at the cap) and :func:`settle_run` (a red run
    landing on the cap) must produce the same note — one wording, one place.
    """
    reason = (
        f"blocked — бюджет closer исчерпан: {attempts}/{limit}; "
        "решение супервизора"
    )
    return reason, [
        f"`missing`: {', '.join(verdict.get('missing') or [])}",
        f"потрачено попыток closer: {attempts} из {limit}",
    ], [
        "решение супервизора: закрыть пункты вручную, переписать TODO "
        "или перенести остаток в отдельный юнит",
    ]


def _salvage_refusal(verdict: dict) -> tuple[str, list[str], list[str]]:
    """The red-after-salvage verdict: no new spawn, no attempt spent.

    A salvaged signal is not a completed closer run (AC11), so the separate
    wording names the crash instead of a spent budget — the shared
    ``_cap_refusal`` text would claim a counter that never moved. The note
    keeps the same ``missing`` / «решение супервизора» contract.
    """
    reason = (
        "blocked — salvage после падения closer: гейт красный, "
        "нового спавна нет; решение супервизора"
    )
    return reason, [
        f"`missing`: {', '.join(verdict.get('missing') or [])}",
        "сигнал дал salvage: попытка closer не потрачена",
    ], [
        "решение супервизора: разобрать причину падения closer и закрыть "
        "пункты вручную или переписать TODO",
    ]


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


def _is_green(verdict: dict) -> bool:
    """Green means: no block at all, or every checkable item is closed.

    ``manual`` items never block — the gate owns that rule already. ONE
    spelling: the entry's skip and the post-run result check must not drift
    apart, or a closer run would be routed by a different definition of
    "done" than the one that spawned it.
    """
    return verdict.get("status") == "none" or bool(verdict.get("ok"))


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
        limit = closer_attempts(config)
    except CloserConfigError as e:
        reason, findings, directions = _config_refusal(e)
        return _blocked(
            project,
            todo_id,
            log_line=reason,
            verdict={},
            reason=reason,
            findings=findings,
            directions=directions,
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
        reason, findings, directions = _state_refusal(e)
        return _blocked(
            project,
            todo_id,
            log_line=f"{dod_line(verdict)} | {reason}",
            verdict=verdict,
            reason=reason,
            findings=findings,
            directions=directions,
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
            reason, findings, directions = _record_refusal(todo_id, e)
            return _blocked(
                project,
                todo_id,
                log_line=f"{dod_line(verdict)} | {reason}",
                verdict=verdict,
                reason=reason,
                findings=findings,
                directions=directions,
            )

    line = dod_line(verdict)

    # 5a. Norm snapshot (IMP5, K5): the CURRENT block and frozen contract are
    # compared with the snapshot taken before the first spawn. The check runs
    # on every entry — a restart may not launder an edit, and closed items
    # never cancel the refusal (AC12–AC14). The file is not rolled back.
    current_norm = norm_fingerprints(text, contract)
    stored_before = record.get("before")
    if isinstance(stored_before, dict) and stored_before:
        changed = _norm_surfaces(stored_before, current_norm)
        if changed:
            reason, findings, directions = _norm_refusal(
                stored_before, current_norm, changed
            )
            return _blocked(
                project,
                todo_id,
                log_line=f"{line} | {reason}",
                verdict=verdict,
                reason=reason,
                findings=findings,
                directions=directions,
            )

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
    if _is_green(verdict):
        return Decision(kind="skip", verdict=verdict, log_line=line)

    # 8. Red — the closer may run while the unit still has budget. The
    # counter is spent by ``settle_run`` AFTER a completed run, never by the
    # entry: a crash, a salvaged signal or a retry of a crashed spawn must
    # not consume an attempt. A unit at the cap is the owner's decision.
    #
    # The first RED decision freezes the norm BEFORE the first spawn; later
    # entries compare against this original snapshot, never the previous
    # attempt. The write stays on this path: a green unit never had a closer
    # that could edit the norm.
    if not isinstance(stored_before, dict) or not stored_before:
        record["before"] = current_norm
        try:
            _write_record(project, todo_id, record)
        except (OSError, managed_fs.ManagedPathError) as e:
            reason, findings, directions = _record_refusal(todo_id, e)
            return _blocked(
                project,
                todo_id,
                log_line=f"{line} | {reason}",
                verdict=verdict,
                reason=reason,
                findings=findings,
                directions=directions,
            )

    attempts = _attempts_of(record)
    if attempts >= limit:
        reason, findings, directions = _cap_refusal(verdict, attempts, limit)
        return _blocked(
            project,
            todo_id,
            log_line=f"{line} | {reason}",
            verdict=verdict,
            reason=reason,
            findings=findings,
            directions=directions,
        )
    return Decision(kind="run", verdict=verdict, log_line=line)


def settle_run(
    project_dir: str | Path,
    todo_id: str,
    config: dict,
    *,
    charge: bool = True,
) -> RunOutcome:
    """Re-check the gate after a signal and decide the colour — ONE path.

    The order is fixed by the architecture (K4): the gate is recomputed on
    the CURRENT tree first, then (for a completed run) one attempt is
    charged, then the verdict decides where the unit goes.

    ``charge=True`` — the worker left a signal (``DONE``, ``APPROVED`` or
    ``BLOCKED``): a completed run with a verdict spends one attempt; red
    below the cap returns to the same stage, red at the cap blocks.

    ``charge=False`` — the signal came from salvage after the worker died:
    the budget is NOT spent and no new spawn is made (AC11). Green advances
    normally; red blocks through the same ``_blocked_note`` writer with
    ``missing`` and the «решение супервизора» line.

    A green verdict wins over the signal type: the verdict, not the signal,
    decides the outcome.
    """
    project = Path(project_dir).resolve()

    try:
        limit = closer_attempts(config)
    except CloserConfigError as e:
        reason, findings, directions = _config_refusal(e)
        return _settle_blocked(
            project,
            todo_id,
            log_line=reason,
            reason=reason,
            findings=findings,
            directions=directions,
        )

    # 1. The gate on the tree the closer just produced (the result check —
    # NOT a second decision point: no new first verdict, no new ``dod:``).
    # The order matches the entry's K2: defects and the gate first, the
    # stored counter after; the two paths must not answer the same pair of
    # failures with different attribution.
    try:
        verdict = checklist_gate.run(project, todo_id)
    except (checklist_gate.ChecklistGateError, ChecklistError) as e:
        reason = f"blocked — чек-лист не выполнен после closer: {e}"
        return _settle_blocked(
            project,
            todo_id,
            log_line=reason,
            reason=reason,
            findings=[f"гейт: {e}"],
            directions=["проверь TODO юнита и блок `## Чек-лист завершения`"],
        )

    line = dod_line(verdict)

    try:
        record = read_state(project, todo_id)
    except ValueError as e:
        reason, findings, directions = _state_refusal(e)
        return _settle_blocked(
            project,
            todo_id,
            log_line=f"{line} | {reason}",
            reason=reason,
            findings=findings,
            directions=directions,
        )
    if record is None:
        # The entry wrote the record before the spawn; a missing file means
        # it was removed mid-run. Fail closed — a fresh counter would hand
        # the unit a budget it already spent.
        reason = (
            f"blocked — нет `context/CLOSER-{todo_id}.json`: счётчик попыток "
            "потерян, ближе не списываем"
        )
        return _settle_blocked(
            project,
            todo_id,
            log_line=f"{line} | {reason}",
            reason=reason,
            findings=["файл состояния юнита исчез во время прогона"],
            directions=[
                "решение супервизора: восстановить запись или выдать юнит "
                "заново",
            ],
        )

    # 2. A COMPLETED run with a verdict spends exactly one attempt. A
    # salvaged signal (``charge=False``) never writes the counter — it is
    # not a run the closer finished (AC11).
    attempts = _attempts_of(record)
    if charge:
        attempts += 1
        record["attempts"] = attempts
        try:
            _write_record(project, todo_id, record)
        except (OSError, managed_fs.ManagedPathError) as e:
            reason, findings, directions = _record_refusal(todo_id, e)
            return _settle_blocked(
                project,
                todo_id,
                log_line=reason,
                reason=reason,
                findings=findings,
                directions=directions,
            )

    # 2b. Norm snapshot (IMP5, K5): the snapshot taken before the first
    # spawn is compared with the tree the closer produced — BEFORE the colour
    # decision, so closed items cannot launder an edited block or contract
    # (AC12/AC13). A contract that no longer parses after the run is the same
    # refusal. The comparison is against the ORIGINAL snapshot, never the
    # previous attempt (AC14).
    try:
        rule_text = todos.read_todo_text(project, todo_id)
        current_contract, _unknown = parse_todo_contract(rule_text)
    except ValueError as e:
        reason = (
            f"blocked — {NORM_EDIT_REASON}: контракт TODO (`"
            + "`, `".join(FROZEN_CONTRACT_KEYS)
            + f"`) нечитаем: {e}"
        )
        return _settle_blocked(
            project,
            todo_id,
            log_line=f"{line} | {reason}",
            reason=reason,
            attempts=attempts,
            findings=[f"контракт TODO после closer: {e}"],
            directions=[
                "решение супервизора: верни контракт в исходное состояние "
                "вручную (файл не откатывается автоматически) или выдай юнит "
                "заново",
            ],
        )
    stored_before = record.get("before")
    if isinstance(stored_before, dict) and stored_before:
        current_norm = norm_fingerprints(rule_text, current_contract)
        changed = _norm_surfaces(stored_before, current_norm)
        if changed:
            reason, findings, directions = _norm_refusal(
                stored_before, current_norm, changed
            )
            return _settle_blocked(
                project,
                todo_id,
                log_line=f"{line} | {reason}",
                reason=reason,
                attempts=attempts,
                findings=findings,
                directions=directions,
            )

    # 3. The colour decides, by the same definition the entry used.
    if _is_green(verdict):
        return RunOutcome(kind="green", attempts=attempts, log_line=line)

    # 4. Red after salvage: blocked without a new spawn and without a spent
    # attempt (AC11, K4).
    if not charge:
        reason, findings, directions = _salvage_refusal(verdict)
        return _settle_blocked(
            project,
            todo_id,
            log_line=f"{line} | {reason}",
            reason=reason,
            attempts=attempts,
            findings=findings,
            directions=directions,
        )

    if attempts >= limit:
        reason, findings, directions = _cap_refusal(verdict, attempts, limit)
        return _settle_blocked(
            project,
            todo_id,
            log_line=f"{line} | {reason}",
            reason=reason,
            attempts=attempts,
            findings=findings,
            directions=directions,
        )

    return RunOutcome(
        kind="retry",
        attempts=attempts,
        log_line=f"{line} | closer: попытка {attempts}/{limit}, ещё красный",
    )


def _settle_blocked(
    project_dir: Path,
    todo_id: str,
    *,
    log_line: str,
    reason: str,
    findings: list[str],
    directions: list[str],
    attempts: int = 0,
) -> RunOutcome:
    """The blocked answer of :func:`settle_run` — the entry's note, re-typed.

    Writes through :func:`_blocked_note` so the note shape and wording have
    ONE writer; the engine gets only the attempt count and the log line.
    """
    _blocked_note(
        project_dir,
        todo_id,
        verdict_line=reason,
        findings=findings,
        directions=directions,
    )
    return RunOutcome(kind="blocked", attempts=attempts, log_line=log_line)


__all__ = [
    "CLOSER_ATTEMPTS_KEY",
    "CloserConfigError",
    "Decision",
    "FROZEN_CONTRACT_KEYS",
    "NORM_EDIT_REASON",
    "RunOutcome",
    "SKIP_REASON",
    "closer_attempts",
    "contract_fingerprint",
    "decide",
    "dod_line",
    "norm_fingerprints",
    "read_state",
    "settle_run",
    "state_path",
    "state_paths",
]
