"""The brief card's situational half (TODO-0348 IMP5).

One table owns every situation code — the words the card prints (AC14)
and the first tool of its row (AC19) — plus the three readers that turn
live state into the card's situational fields: the classifier (D5), the
current pass's verify/commit facts (K4/D7) and the waiting rule (K3/D11).

This module is a leaf: it imports neither ``awf.brief`` nor the ``awf.api``
package (the wait cap comes from ``awf.wait_policy``), so ``awf.brief``
grows by assembly only — the supervisor's review-fix asked for the new
blocks here instead of pushing the 1689-line module further.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

# R6/AC22–AC25: ONE waiting rule for a live pipeline — the same phrase on
# every surface (``awf_status``, ``awf_brief``, the wait tool's docstring).
# The literal is the requirement's key phrase; do not paraphrase it.
WAIT_RULE_RUN = "wait only inside an active run"
# TODO-0348 (IMP5)/D11: the reactive half of the same rule. The card prints
# one of the two by ``mode``; the literal is the requirement's key phrase.
WAIT_RULE_REACTIVE = "react to the user, no loop"

# TODO-0348 (IMP5)/D3: the card's own pointer to the full phase prompt.
# The full text is the ``phase_prompt`` FIELD (one call, no truncation);
# the card only names it, exactly like the tool inventory.
ENTRY_RECALL_WHEN = ["unclear moment", "lost context", "stage changed"]
ENTRY_FIELD = {
    "tool": "awf_brief",
    "single": True,
    "recall_when": list(ENTRY_RECALL_WHEN),
}


def entry_field() -> dict[str, Any]:
    """A fresh entry block — a consumer may extend ``recall_when``.

    ``dict(ENTRY_FIELD)`` copies one level: the list stayed shared with the
    module constant, so one mutation rewrote every later card (AUD6).
    """
    return {**ENTRY_FIELD, "recall_when": list(ENTRY_RECALL_WHEN)}


# TODO-0347 (IMP4): the coarse names the advice uses, resolved to the fine
# situation codes of D5. The fine codes stay the data keys; the aliases let
# the card ask by the advice's own vocabulary (verify / run / blocked /
# salvage / hygiene / setup).
SITUATION_ALIASES = {
    "setup": "S0",
    "salvage": "S1",
    "blocked": "S2",
    "run": "S7",
    "verify": "S8",
    "hygiene": "S10",
}

# TODO-0348 (IMP5)/D5: ONE table per situation — the words the card prints
# (AC14: the state is named in words, never as a bare ``S8``) and the first
# tool of its row (AC19, the tool the advice names). The two views below are
# derived, so a code cannot appear in one and be missing in the other. The
# order is canonical: ``S1a`` right after ``S1``, ``S11a``/``S11b`` after
# ``S11``.
SITUATIONS: tuple[tuple[str, str, str], ...] = (
    ("S0", "no .agentic/ yet — the project is not set up", "awf_init"),
    ("S1", "salvage — a stage did not signal", "awf_retry_stage"),
    ("S1a", "commit outside the gate stopped the run", "awf_rollback"),
    ("S2", "blocked — a unit waits for an answer", "awf_continue"),
    ("S3", "the unit reached its review-pass budget", "awf_todo_retire"),
    ("S4", "engine dead — the stage state is frozen", "awf_continue"),
    ("S5", "stale stage — the heartbeat is quiet", "awf_kill"),
    ("S6", "zombie stage state — the stage was never closed", "awf_continue"),
    ("S7", "pipeline running", "awf_wait_for_event"),
    ("S8", "verify waits for your decision", "awf_approve"),
    ("S9", "a unit is ready to start", "awf_start"),
    ("S10", "no ready units — plan the next one", "awf_dispatch_todo"),
    ("S11", "a reject decision is recorded", "awf_review_append"),
    ("S11a", "reject recorded in a live run", "awf_run_revise"),
    ("S11b", "reject recorded — dispatch the retry", "awf_dispatch_todo"),
    ("S12", "a unit is approved — the queue has the next position", "awf_run_next"),
    ("S13", "a form waits for the user's confirmation", "read_submit"),
)

SITUATION_LINES = {code: line for code, line, _tool in SITUATIONS}
SITUATION_ADVICE = {code: tool for code, _line, tool in SITUATIONS}
SITUATION_ORDER = tuple(SITUATION_ADVICE)

# AUD3: S0 answers two filesystem states — the setup chain is the advice in
# both, but the card must not claim ".agentic/ is absent" when it exists.
SETUP_LINE_NO_AGENTIC = SITUATION_LINES["S0"]
SETUP_LINE_NO_HISTORY = "no work history yet — run the setup chain"


def status_field(s: Any, name: str, default: Any = None) -> Any:
    """Read one field from a status (dataclass or its ``as_dict`` form).

    The advice owner and the card used to carry a private copy of this
    lookup next to each other; one reader keeps their view of a status
    identical by construction.
    """
    if isinstance(s, dict):
        return s.get(name, default)
    return getattr(s, name, default)


def classify_status(status: Any) -> str:
    """Situation code ``S0``–``S13``/``S1a`` from status alone (D5/T6).

    The order mirrors the advice owner (``next_action_from_status``) so the
    card names the same state the advice answers. Run-only branches
    (reject/approve) live in :func:`refine_with_run` — a status does not
    carry the run's last decision.

    AC46: ``checkpoint`` is weaker than salvage; a stale stage while the
    pipeline runs is S5, not S7; ``expected_action`` outranks blocked/budget
    whenever the pipeline is live (it exists only then).
    """
    if status is None:
        return "S0"
    # A31: a commit outside the gate is its own branch — the salvage text
    # would bury the cause. Foreign beats salvage (T6).
    if status_field(status, "foreign_commit"):
        return "S1a"
    if status_field(status, "salvage_needed"):
        return "S1"
    # BD-36: the form waits for the user. Stronger than the running-stage
    # tail, weaker than salvage (AC46).
    if status_field(status, "checkpoint_pending"):
        return "S13"
    expected = status_field(status, "expected_action")
    if expected and not (
        status_field(status, "stale_stage")
        and status_field(status, "pipeline_running")
    ):
        # ``_compute_expected_action`` prefixes the verify instruction with
        # "VERIFY STAGE"; every other prefix is a running-stage wait.
        return "S8" if str(expected).lstrip().upper().startswith("VERIFY STAGE") else "S7"
    if list(status_field(status, "blocked_ids") or []):
        return "S2"
    if status_field(status, "budget_exhausted_todo"):
        return "S3"
    if status_field(status, "engine_dead"):
        return "S4"
    if status_field(status, "stale_stage"):
        return "S5"
    if status_field(status, "zombie_state"):
        return "S6"
    if status_field(status, "pipeline_running"):
        return "S7"
    if list(status_field(status, "active_todos") or []):
        return "S9"
    # S10 covers both "a suggestion exists" and "nothing ready".
    return "S10"


def refine_with_run(
    situation: str, status: Any | None = None, run_record: Any | None = None
) -> str:
    """Run-aware branches S11a/S11b/S12 (AC41/T6).

    The run record owns its last decision: a reject in a live run closes
    through ``awf_run_revise`` (S11a); without a run the retry is a new
    dispatch (S11b); an approve moves the queue position (S12).

    QA (TODO-0348): the branch must survive only while the advice layer
    reaches it. An APPROVE is S12 only for the run's CURRENT unit — after
    ``awf_run_next`` the same decision still sits on disk while the next
    unit runs, and the card would tell the supervisor to move a queue
    position that already moved. A REJECT already closed by
    ``awf_run_revise`` is no longer in the queue: ``next_action_for_record``
    falls back there, so the situation falls back too. Both guards mirror
    the advice owner; a closed run is never S12 (its step is history).
    ``state`` is ``None`` for a hand-built record (tests only) — the old
    branch is kept then.
    """
    decision = getattr(run_record, "last_decision", None)
    if not isinstance(decision, dict):
        return situation
    kind = str(decision.get("kind") or "")
    active = bool(getattr(run_record, "active", False))
    state = getattr(run_record, "state", None)
    raw = state if isinstance(state, dict) else None
    todo = str(decision.get("todo_id") or "")
    if kind == "reject":
        if not active:
            return "S11b"
        if raw is not None:
            from . import run_state as _run_state

            queue_ids = {
                item["todo_id"]
                for item in _run_state.normalize_queue(raw.get("queue"))
            }
            if queue_ids and todo not in queue_ids:
                return situation
        return "S11a"
    if kind == "approve":
        if not active:
            return situation
        if raw is not None:
            current = str(raw.get("current") or "")
            if not current or current != todo:
                return situation
        return "S12"
    return situation


def situation_for(status: Any | None, run_record: Any | None = None) -> str:
    """The card's situation id: the classifier plus the run-aware overrides."""
    return refine_with_run(classify_status(status), status, run_record)


def verify_facts(
    project_dir: Path,
    status: Any | None,
    situation: str,
    advice_tool: str,
) -> dict[str, Any]:
    """K1/K4/D7: the current pass's verify and commit verdicts.

    Read by NAME (``GATES-<todo>.md`` / ``COMMIT-AUDIT-<todo>.json``), never
    a glob. The section is live while the verify stage runs — the state fact
    ``current_stage_kind``, not the card's situation id, so a checkpoint or
    a stale heartbeat cannot hide a pack that was just written — or while
    the artifact is newer than ``state.updated_at``; an old artifact is
    hidden (AC28) and the card says nothing about it. No todo in state =
    outside the live cycle = ``{}``.

    ``advice_tool`` is the caller's situation tool: the card already picked
    it, so re-deriving it here would be a second owner of the same answer.
    """
    if status is None:
        return {}
    todo = str(status_field(status, "current_todo") or "")
    if not todo:
        return {}

    from .pipeline_state import parse_updated_at

    state_ts = parse_updated_at(status_field(status, "state_updated_at"))
    verify_stage = str(status_field(status, "current_stage_kind") or "") == "verify"
    context = paths.context_dir(Path(project_dir))
    gates_path = context / f"GATES-{todo}.md"

    def fresh(path: Path) -> bool:
        if verify_stage:
            return True
        if not path.is_file():
            return True  # "missing" is a fact of this pass
        if state_ts is None:
            return False  # unknown clock counts as old (D7)
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            return False
        return mtime > state_ts

    from .verify_pack import read_gates_facts

    gates = read_gates_facts(gates_path)
    if gates is not None and not fresh(gates_path):
        return {}

    from .commit_gate import read_commit_audit

    audit = read_commit_audit(Path(project_dir), todo)
    if audit is not None and not fresh(context / f"COMMIT-AUDIT-{todo}.json"):
        audit = None

    if gates is None:
        verify_status = "missing"
        failed_sections: list[str] = []
        first_failure = ""
        # TODO-0348 QA (AC29/T8): the engine's crash note is the reason a
        # pack that produced no JSON could not run. IMP3 reads it; the card
        # must not hide it behind the bare "did not run".
        note = ""
    else:
        verify_status = str(gates.get("verdict") or "unreadable")
        failed_sections = list(gates.get("failed_sections") or [])
        first_failure = str(gates.get("first_failure") or "")
        note = str(gates.get("note") or "")

    commit: dict[str, Any]
    if audit is None:
        commit = {"status": "missing", "reason": ""}
    else:
        commit = {
            "status": str(audit.get("status") or "missing"),
            "reason": str(audit.get("reason") or ""),
        }

    if situation == "S8":
        if verify_status == "missing":
            waiting_on, close_with = "supervisor", "awf_verify_pack"
        elif verify_status == "ok":
            waiting_on, close_with = "commit", ""
        else:
            waiting_on, close_with = "supervisor", advice_tool
    elif situation in {"S7", "S12"} and commit["status"] != "missing":
        waiting_on, close_with = "commit", ""
    elif situation == "S7":
        waiting_on, close_with = "engine", ""
    else:
        waiting_on, close_with = ("supervisor", advice_tool) if advice_tool else ("none", "")

    return {
        "unit": todo,
        "status": verify_status,
        "report": str(gates_path),
        "failed_sections": failed_sections,
        "first_failure": first_failure,
        "note": note,
        "commit": commit,
        "waiting_on": waiting_on,
        "close_with": close_with,
    }


def wait_block(run_active: bool, project_dir: Path) -> dict[str, Any]:
    """K3/D11: one waiting rule, printed by mode (IMP6's fact + IMP5 card)."""
    if not run_active:
        return {
            "rule": WAIT_RULE_REACTIVE,
            "loop": False,
            "tool": "",
            "timeout": None,
        }
    from .wait_policy import wait_cap

    try:
        timeout: int | None = wait_cap(project_dir)
    except Exception:
        timeout = None
    return {
        "rule": WAIT_RULE_RUN,
        "loop": True,
        "tool": "awf_wait_for_event",
        "timeout": timeout,
    }
