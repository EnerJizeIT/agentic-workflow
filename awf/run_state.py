"""Autonomous run (забег) state — SPEC A-run v1.

A run is a queue of TODO ids with gates. The supervisor chat drives the
loop; awf keeps the state and enforces the mechanical stop-list:

- queue exhausted or budget exceeded
- stop flags on the next TODO (phase boundary, external audit, owner decision)
- the next TODO was rejected twice already
- the previous TODO is not finished (not archived)

Stored in ``.agentic/state/run.yaml`` — a SEPARATE file from current.yaml so
stage-state writes never clobber the run (same reason as dashboard_port).
Crash-safe: the file survives process death; the supervisor re-reads it via
``awf_run_status`` and continues with ``awf_run_next``.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import paths
from ._atomic import atomic_write_text
from ._log import log as _log

# Same id format the run API validates (awf/api/run.py::_TODO_RE).
_TODO_ID_RE = re.compile(r"^TODO-\d{4,}$")


#: ORCH M5.2: the run state slots — file per logical run. "main" is the
#: main run (run.yaml, the original behavior); "service" is the service
#: run (service-run.yaml, role creation DURING the main run). The file is
#: the unit of atomicity: two logical states in one file would break the
#: byte-for-byte survival of the main run across a service run.
_RUN_SLOT_FILES = {"main": "run.yaml", "service": "service-run.yaml"}


def run_file(project_dir: Path, slot: str = "main") -> Path:
    """Path of the run state file for the slot (ORCH M5.2).

    ``slot="main"`` (default) — the main run, ``state/run.yaml`` — every
    pre-M5.2 caller is unchanged. ``slot="service"`` — the service run in
    its OWN file, ``state/service-run.yaml``: the service flow never opens
    the main file for writing (see :func:`awf.api.run_service_start`).
    """
    try:
        name = _RUN_SLOT_FILES[slot]
    except KeyError:
        raise ValueError(
            f"unknown run slot {slot!r} — expected 'main' or 'service'"
        ) from None
    return paths.agentic_dir(project_dir) / "state" / name


def _queue_item_ok(q: object) -> bool:
    """RUN3 #2: a queue item is a legacy ``TODO-NNNN`` string OR an object
    ``{"todo_id": "TODO-NNNN", "pipeline": "..."}`` (object = per-item
    pipeline). Anything else is a corrupt item."""
    if isinstance(q, str):
        return bool(_TODO_ID_RE.match(q))
    if isinstance(q, dict):
        return bool(_TODO_ID_RE.match(str(q.get("todo_id", ""))))
    return False


def normalize_queue(queue: object) -> list[dict]:
    """RUN3 #2: queue items in the canonical form ``{"todo_id", "pipeline"}``.

    Legacy state files store plain ``TODO-NNNN`` strings — those upgrade to
    ``{"todo_id": <id>, "pipeline": ""}`` (empty pipeline = the pipeline from
    config, i.e. unchanged behavior). The reader normalizes in place, so old
    run.yaml files keep working and self-upgrade on the next write.
    """
    items: list[dict] = []
    for q in queue or []:
        if isinstance(q, dict):
            items.append(
                {
                    "todo_id": str(q.get("todo_id", "")),
                    "pipeline": str(q.get("pipeline", "") or ""),
                }
            )
        else:
            items.append({"todo_id": str(q), "pipeline": ""})
    return items


def _run_shape_ok(state: dict) -> bool:
    """AUD02-06: a truncated run.yaml can parse as a valid partial dict
    (``{'queue': ['TODO-00']}``) — without shape validation the reader
    returns it as normal state: ``active`` lost, queue replaced by a
    garbage id, the rest of the queue silently gone.

    Valid shape: queue is a non-empty list of ``TODO-NNNN`` ids (RUN3 #2:
    or ``{todo_id, pipeline}`` objects), index (when present) is an int in
    ``[0, len(queue)]`` (``len`` = exhausted), active (when present) is a
    bool. Absent index/active is tolerated — a crash-truncated file must
    degrade to "no run", not to a half-run.
    """
    queue = state.get("queue")
    if not isinstance(queue, list) or not queue:
        return False
    for q in queue:
        if not _queue_item_ok(q):
            return False
    idx = state.get("index")
    if idx is not None and (
        isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx <= len(queue)
    ):
        return False
    active = state.get("active")
    if active is not None and not isinstance(active, bool):
        return False
    # A-13: generation — additive run-identity field (absent = 0). A
    # present-but-garbage value is corrupt the same way a garbage index is.
    gen = state.get("generation")
    if gen is not None and (
        isinstance(gen, bool) or not isinstance(gen, int) or gen < 0
    ):
        return False
    return True


def _decision_entry_ok(d: object) -> bool:
    """ORCH M1.1: a decisions entry is a dict with non-empty string
    ``kind`` and ``todo_id`` — everything else is a corrupt entry."""
    if not isinstance(d, dict):
        return False
    return bool(str(d.get("kind") or "").strip()) and bool(
        str(d.get("todo_id") or "").strip()
    )


def _normalize_decision(d: dict) -> dict:
    """ORCH M1.1: the canonical decision shape {ts, kind, todo_id, reason};
    absent fields become empty strings (a partial entry survives as far as
    its kind/todo_id allow)."""
    return {
        "ts": str(d.get("ts") or ""),
        "kind": str(d.get("kind") or "").strip(),
        "todo_id": str(d.get("todo_id") or "").strip(),
        "reason": str(d.get("reason") or ""),
    }


def _evidence_plan_entry_ok(e: object) -> bool:
    """ORCH M2.3: an evidence plan entry is a dict with a non-empty string
    ``todo_id`` — everything else is a corrupt entry (same rule class as
    :func:`_decision_entry_ok`)."""
    return isinstance(e, dict) and bool(str(e.get("todo_id") or "").strip())


def _normalize_evidence_plan(e: dict) -> dict:
    """ORCH M2.3: the canonical evidence plan shape
    ``{ts, todo_id, todo_sha, verify, gates, prove_red, note}``; absent or
    mistyped fields degrade to empty (a partial entry survives as far as
    its todo_id allows)."""

    def _str_list(v: object) -> list[str]:
        # strict: only real strings survive — a coerced None would read as
        # the id "None" in a prove_red list (machine identifiers, not text)
        if not isinstance(v, list):
            return []
        return [x for x in v if isinstance(x, str) and x.strip()]

    return {
        "ts": str(e.get("ts") or ""),
        "todo_id": str(e.get("todo_id") or "").strip(),
        "todo_sha": str(e.get("todo_sha") or ""),
        "verify": _str_list(e.get("verify")),
        "gates": _str_list(e.get("gates")),
        "prove_red": _str_list(e.get("prove_red")),
        "note": str(e.get("note") or ""),
    }


def _revision_entry_ok(e: object) -> bool:
    """ORCH M3.4: a revisions entry is a dict with a non-empty string
    ``key`` — everything else is a corrupt entry (same rule class as
    :func:`_decision_entry_ok`)."""
    return isinstance(e, dict) and bool(str(e.get("key") or "").strip())


def _normalize_revision(e: dict) -> dict:
    """ORCH M3.4/M4.1: the canonical revision shape
    ``{ts, key, kind, reason, changes, generation, resume_from}``; absent
    or mistyped fields degrade to empty (a partial entry survives as far
    as its key allows). ``resume_from`` (ORCH M4.1) — the stage the
    stopped unit resumes from; absent in v1 (M3.4) records → ``""``."""

    def _norm_change(c: dict) -> dict:
        return {
            "todo_id": str(c.get("todo_id") or "").strip(),
            "from": str(c.get("from") or ""),
            "to": str(c.get("to") or ""),
        }

    changes = e.get("changes")
    norm_changes = [
        _norm_change(c)
        for c in (changes if isinstance(changes, list) else [])
        if isinstance(c, dict) and str(c.get("todo_id") or "").strip()
    ]
    try:
        generation = int(e.get("generation", 0) or 0)
    except (TypeError, ValueError):
        generation = 0
    return {
        "ts": str(e.get("ts") or ""),
        "key": str(e.get("key") or "").strip(),
        "kind": "revision",
        "reason": str(e.get("reason") or ""),
        "changes": norm_changes,
        "generation": generation,
        "resume_from": str(e.get("resume_from") or "").strip(),
    }


def _sanitize_plan_fields(state: dict, logs_dir: Path | None) -> None:
    """ORCH M1.1 (invariant 4): broken/partial ``goal``/``criteria``/
    ``decisions``/``evidence_plans``/``revisions`` in run.yaml must not
    crash the read — the broken value is dropped with a warning (the file
    itself is kept, like AUD02-06), the rest of the run stays usable.

    After this call the state ALWAYS carries ``goal`` (str), ``criteria``
    (list of non-empty str), ``decisions`` (list of normalized entries),
    ``evidence_plans`` (list of normalized entries) and ``revisions``
    (list of normalized entries), so every consumer indexes them without a
    defensive isinstance.
    """
    goal = state.get("goal")
    if goal is None:
        state["goal"] = ""
    elif not isinstance(goal, str):
        if logs_dir is not None:
            _log(
                logs_dir,
                "ORCH M1.1: run.yaml 'goal' is not a string — dropped "
                "(corrupt file kept for inspection)",
            )
        state["goal"] = ""

    criteria = state.get("criteria")
    if criteria is None:
        state["criteria"] = []
    elif not isinstance(criteria, list):
        if logs_dir is not None:
            _log(
                logs_dir,
                "ORCH M1.1: run.yaml 'criteria' is not a list — dropped "
                "(corrupt file kept for inspection)",
            )
        state["criteria"] = []
    else:
        state["criteria"] = [str(c).strip() for c in criteria if str(c).strip()]

    decisions = state.get("decisions")
    if decisions is None:
        state["decisions"] = []
    elif not isinstance(decisions, list):
        if logs_dir is not None:
            _log(
                logs_dir,
                "ORCH M1.1: run.yaml 'decisions' is not a list — dropped "
                "(corrupt file kept for inspection)",
            )
        state["decisions"] = []
    else:
        kept: list[dict] = []
        skipped = 0
        for d in decisions:
            if _decision_entry_ok(d):
                kept.append(_normalize_decision(d))
            else:
                skipped += 1
        if skipped and logs_dir is not None:
            _log(
                logs_dir,
                f"ORCH M1.1: {skipped} corrupt run.yaml decision entr(y/ies) "
                "skipped (file kept for inspection)",
            )
        state["decisions"] = kept

    # ORCH M2.3: the per-item evidence plans — absent (legacy state) is not
    # corruption (no migration); a broken value degrades the same way
    # decisions do (the file is kept, the rest of the run stays usable).
    plans = state.get("evidence_plans")
    if plans is None:
        state["evidence_plans"] = []
    elif not isinstance(plans, list):
        if logs_dir is not None:
            _log(
                logs_dir,
                "ORCH M2.3: run.yaml 'evidence_plans' is not a list — "
                "dropped (corrupt file kept for inspection)",
            )
        state["evidence_plans"] = []
    else:
        plans_kept: list[dict] = []
        plans_skipped = 0
        for e in plans:
            if _evidence_plan_entry_ok(e):
                plans_kept.append(_normalize_evidence_plan(e))
            else:
                plans_skipped += 1
        if plans_skipped and logs_dir is not None:
            _log(
                logs_dir,
                f"ORCH M2.3: {plans_skipped} corrupt run.yaml evidence-plan "
                "entr(y/ies) skipped (file kept for inspection)",
            )
        state["evidence_plans"] = plans_kept

    # ORCH M3.4: the applied queue revisions — absent (legacy state) is not
    # corruption (no migration); a broken value degrades the same way
    # decisions do (the file is kept, the rest of the run stays usable).
    revisions = state.get("revisions")
    if revisions is None:
        state["revisions"] = []
    elif not isinstance(revisions, list):
        if logs_dir is not None:
            _log(
                logs_dir,
                "ORCH M3.4: run.yaml 'revisions' is not a list — "
                "dropped (corrupt file kept for inspection)",
            )
        state["revisions"] = []
    else:
        rev_kept: list[dict] = []
        rev_skipped = 0
        for e in revisions:
            if _revision_entry_ok(e):
                rev_kept.append(_normalize_revision(e))
            else:
                rev_skipped += 1
        if rev_skipped and logs_dir is not None:
            _log(
                logs_dir,
                f"ORCH M3.4: {rev_skipped} corrupt run.yaml revision "
                "entr(y/ies) skipped (file kept for inspection)",
            )
        state["revisions"] = rev_kept


def read_run(
    project_dir: Path,
    *,
    logs_dir: Path | None = None,
    slot: str = "main",
) -> dict | None:
    """Read run state. Returns None when no run was ever started or the
    file is corrupt (non-UTF-8, broken YAML, non-dict root, bad shape).

    ``slot`` (ORCH M5.2) selects the state file — "main" (default, the
    original behavior) or "service" (the service run's own file). A corrupt
    service file degrades to "no service run" exactly like a corrupt main
    file degrades to "no run" (AUD02-06) — the other slot is unaffected."""
    f = run_file(project_dir, slot=slot)
    if not f.is_file():
        return None
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        # AUD12-11: non-UTF-8 bytes in run.yaml are a corrupt file —
        # degrade to "no run", not traceback (same as broken YAML).
        return None
    if not isinstance(data, dict):
        return None
    if not _run_shape_ok(data):
        # The run is lost — make it visible (the file itself is kept).
        if logs_dir is not None:
            _log(
                logs_dir,
                "AUD02-06: run.yaml shape invalid "
                "(queue/index/active) — treating as no run "
                "(corrupt file kept for inspection)",
            )
        return None
    # RUN3 #2: upgrade legacy string queue items to the canonical
    # {"todo_id", "pipeline"} form on read (old state files stay readable).
    data["queue"] = normalize_queue(data["queue"])
    # ORCH M1.1: the RunPlan fields always exist in the returned state
    # (""/[]/[]); broken values degrade with a warning, not a traceback.
    _sanitize_plan_fields(data, logs_dir)
    return data


def _ensure_lock_file(project_dir: Path) -> None:
    """AUD05-05 (QA): the .lock must hold at least one byte before first use.

    ``locked()`` creates the lock file empty via ``open(..., "a+b")`` on
    first use; msvcrt.locking (Windows) locks by byte offset, and locking
    byte 0 of an empty file is unstable. Writing one byte up front — only
    when the file is absent — makes the Windows path stable. The Linux path
    is unchanged: flock does not depend on file contents.
    """
    state_dir = paths.agentic_dir(project_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_file = state_dir / ".lock"
    if lock_file.is_file():
        return
    try:
        with open(lock_file, "xb") as f:
            f.write(b"\0")
    except FileExistsError:
        pass  # a concurrent caller created it first


def write_run(project_dir: Path, *, slot: str = "main", **fields) -> dict:
    """Merge fields into run state and return the merged dict.

    AUD05-05: the read → merge → write runs under an advisory lock so
    concurrent write_run calls (parallel approve/reject, run_next +
    run_finish) cannot lose each other's updates.

    ``slot`` (ORCH M5.2): which state file to merge into — "main" (default)
    or "service". Both slots share the same advisory lock (one project,
    one writer at a time), so the two runs cannot interleave mid-write.
    """
    from ._lock import locked

    _ensure_lock_file(project_dir)
    with locked(project_dir):
        state = read_run(project_dir, slot=slot) or {}
        state.update(fields)
        atomic_write_text(
            run_file(project_dir, slot=slot),
            yaml.safe_dump(state, allow_unicode=True, sort_keys=False),
        )
    return state


def update_run(project_dir: Path, mutator, *, slot: str = "main") -> dict:
    """Atomic read → mutate → write in ONE lock hold (AUD05-05, rest).

    ``write_run`` serializes its own RMW, but callers like
    ``approve_commit`` / ``reject_commit`` / ``run_next`` used to read the
    state BEFORE the lock and pass whole keys (``outcomes``, ``rejects``,
    ``index``/``current``) computed from that stale snapshot — the shallow
    merge then clobbered a concurrent writer's update (supervisor repro:
    verdict='approved' while rejects>=1). ``update_run`` moves the
    computation INSIDE the lock: ``mutator`` receives the state exactly as
    it is on disk at that moment and returns the state to write (mutating
    the dict in place is enough; returning ``None`` keeps it). A mutator
    that finds a conflict (e.g. an approve that sees the TODO already
    rejected) returns the input unchanged — the audit invariant
    ``'approved' ⇒ rejects == 0`` is enforced by the caller, not by hope.

    Returns the written state.
    """
    from ._lock import locked

    _ensure_lock_file(project_dir)
    with locked(project_dir):
        state = read_run(project_dir, slot=slot) or {}
        new_state = mutator(state)
        if new_state is None:
            new_state = state
        if not isinstance(new_state, dict):
            raise TypeError(
                "update_run mutator must return a dict (or None) — "
                f"got {type(new_state).__name__}"
            )
        atomic_write_text(
            run_file(project_dir, slot=slot),
            yaml.safe_dump(new_state, allow_unicode=True, sort_keys=False),
        )
    return new_state


def generation_of(state: dict) -> int:
    """A-13: the run generation — the identity of a run (забег) for
    conditional queue transitions. Additive run.yaml field: absent (old
    state files) = 0; a garbage value degrades to 0 (a mismatch with the
    expected int is a safe refusal, not a crash)."""
    try:
        return int(state.get("generation", 0) or 0)
    except (TypeError, ValueError):
        return 0


def cas_match(
    state: dict,
    *,
    generation: int | None = None,
    index: int | None = None,
    current: str | None = None,
    reserved_by: str | None = None,
) -> bool:
    """A-13: does the state match the expected (generation, index, current,
    reserved_by) tuple — the compare of compare-and-swap? An absent (None)
    component is not checked. The swap (``update_run_cas``) happens only
    when every given component matches the state exactly.

    ``reserved_by`` (TODO-0189, A-13 residual angle): the owner stamp of
    the queue-position reservation — set by the reserving caller, and
    re-stamped by the takeover path. A release that carries a stamp only
    matches a state it still owns; a captured (or legacy, unstamped)
    reservation never matches, so a refused launch cannot clobber
    someone else's reservation."""
    if generation is not None and generation_of(state) != int(generation):
        return False
    if index is not None:
        try:
            disk_index = int(state.get("index", 0) or 0)
        except (TypeError, ValueError):
            return False
        if disk_index != int(index):
            return False
    if current is not None and str(state.get("current", "") or "") != str(current):
        return False
    if reserved_by is not None and str(
        state.get("reserved_by", "") or ""
    ) != str(reserved_by):
        return False
    return True


def update_run_cas(
    project_dir: Path,
    mutator,
    *,
    slot: str = "main",
    generation: int | None = None,
    index: int | None = None,
    current: str | None = None,
    reserved_by: str | None = None,
) -> tuple[dict | None, bool]:
    """A-13: conditional read → mutate → write in ONE lock hold.

    The mutator runs only when the on-disk state matches the expected
    tuple (:func:`cas_match`). Match: returns ``(state_written, True)``.
    Mismatch (or a missing/corrupt run.yaml, which never matches): nothing
    is written, returns ``(None, False)`` — the caller treats this as
    "another caller owns this run position" and refuses, not retries.

    ``reserved_by`` (TODO-0189): the owner-stamp component — see
    :func:`cas_match`.

    ``update_run`` stays for unconditional RMW (approve/reject diary); the
    run queue transitions (reserve → launch → commit/release in
    ``awf/api/run.py``) need the condition, because their check used to
    run BEFORE the lock and the write clobbered a concurrent winner.
    """
    from ._lock import locked

    _ensure_lock_file(project_dir)
    with locked(project_dir):
        state = read_run(project_dir, slot=slot)
        if state is None or not cas_match(
            state,
            generation=generation,
            index=index,
            current=current,
            reserved_by=reserved_by,
        ):
            return None, False
        new_state = mutator(state)
        if new_state is None:
            new_state = state
        if not isinstance(new_state, dict):
            raise TypeError(
                "update_run_cas mutator must return a dict (or None) — "
                f"got {type(new_state).__name__}"
            )
        atomic_write_text(
            run_file(project_dir, slot=slot),
            yaml.safe_dump(new_state, allow_unicode=True, sort_keys=False),
        )
        return new_state, True


def append_decision(state: dict, kind: str, todo_id: str, reason: str) -> bool:
    """ORCH M1.1: append a causal decision to the state dict IN PLACE.

    A mutator fragment for :func:`update_run` / :func:`update_run_cas` —
    the caller runs it inside the lock so the decision lands in the SAME
    atomic write as the outcomes/rejects update that triggered it. The
    entry is ``{ts, kind, todo_id, reason}``.

    Idempotent by (kind, todo_id, reason): a repeat of the same decision
    adds no duplicate. The check and the append are one step inside one
    lock hold, so two serialized writers cannot race into a double entry —
    that is the mechanism the TODO-0128 invariant 2 asks for.

    Returns True when the entry was added, False when it already existed.
    """
    kind = str(kind or "").strip()
    todo_id = str(todo_id or "").strip()
    reason = str(reason or "").strip()
    decisions = state.get("decisions")
    if not isinstance(decisions, list):
        decisions = []
        state["decisions"] = decisions
    for d in decisions:
        if (
            isinstance(d, dict)
            and str(d.get("kind") or "").strip() == kind
            and str(d.get("todo_id") or "").strip() == todo_id
            and str(d.get("reason") or "").strip() == reason
        ):
            return False
    decisions.append({"ts": now_iso(), "kind": kind, "todo_id": todo_id, "reason": reason})
    return True


def append_evidence_plan(state: dict, todo_id: str, plan: dict) -> None:
    """ORCH M2.3: append a per-item evidence plan to the state dict IN PLACE.

    A mutator fragment for :func:`update_run` / :func:`update_run_cas` —
    the caller runs it inside the lock so the plan lands in the SAME
    atomic write as the queue-position commit that launched the item (the
    decisions' path, M1: one lock hold, one write). Appended, never
    replaced: a retry that re-launches the same item appends a fresh
    snapshot; the surfaces read the current element's (last) entry.

    ``plan`` carries ``{todo_sha, verify, gates, prove_red, note}`` —
    the launch-time snapshot of the TODO's contract + file hash
    (``awf/api/run.py::_todo_evidence_plan``).
    """
    plans = state.get("evidence_plans")
    if not isinstance(plans, list):
        plans = []
        state["evidence_plans"] = plans
    def _str_items(v: object) -> list[str]:
        if not isinstance(v, list):
            return []
        return [x for x in v if isinstance(x, str) and x.strip()]

    plans.append(
        {
            "ts": now_iso(),
            "todo_id": str(todo_id or "").strip(),
            "todo_sha": str(plan.get("todo_sha") or ""),
            "verify": _str_items(plan.get("verify")),
            "gates": _str_items(plan.get("gates")),
            "prove_red": _str_items(plan.get("prove_red")),
            "note": str(plan.get("note") or ""),
        }
    )


def find_revision(state: dict, key: str) -> dict | None:
    """ORCH M3.4: the stored revision with this idempotency key (or None).

    The check runs under the caller's lock hold (inside the mutator), so a
    repeat with the same key is a no-op for serialized writers too."""
    key = str(key or "").strip()
    if not key:
        return None
    for e in state.get("revisions") or []:
        if isinstance(e, dict) and str(e.get("key") or "").strip() == key:
            return e
    return None


def append_revision(
    state: dict,
    key: str,
    reason: str,
    changes: list[dict],
    generation: int,
    resume_from: str = "",
) -> None:
    """ORCH M3.4: append an applied revision to the state dict IN PLACE.

    A mutator fragment for :func:`update_run` / :func:`update_run_cas` —
    the caller runs it inside the lock so the revision lands in the SAME
    atomic write as the queue change it records (the decisions' path, M1:
    one lock hold, one write). The entry is
    ``{ts, key, kind: "revision", reason, changes, generation,
    resume_from}``. ``resume_from`` (ORCH M4.1) names the stage the
    stopped unit resumes from (``""`` when the stop was a no-op). The
    caller checks :func:`find_revision` first — a repeat with the same key
    is a no-op, never a second entry.
    """
    revisions = state.get("revisions")
    if not isinstance(revisions, list):
        revisions = []
        state["revisions"] = revisions
    revisions.append(
        {
            "ts": now_iso(),
            "key": str(key or "").strip(),
            "kind": "revision",
            "reason": str(reason or "").strip(),
            "changes": [
                {
                    "todo_id": str(c.get("todo_id") or "").strip(),
                    "from": str(c.get("from") or ""),
                    "to": str(c.get("to") or ""),
                }
                for c in (changes or [])
                if isinstance(c, dict) and str(c.get("todo_id") or "").strip()
            ],
            "generation": int(generation or 0),
            "resume_from": str(resume_from or "").strip(),
        }
    )


def record_decision(
    project_dir: Path, kind: str, todo_id: str, reason: str
) -> tuple[dict | None, bool]:
    """ORCH M1.1: append a decision to the ACTIVE run in one lock hold.

    Returns ``(written_state, added)``. No run state on disk →
    ``(None, False)`` — decisions belong to a run; outside a run there is
    nothing to record and nothing is created (invariant 2). An inactive
    (closed) run is treated the same way: the state is returned unchanged,
    ``added`` is False.
    """
    if read_run(project_dir) is None:
        return None, False
    result = {"added": False}

    def _mut(state: dict) -> dict:
        if not state.get("active"):
            return state
        result["added"] = append_decision(state, kind, todo_id, reason)
        return state

    return update_run(project_dir, _mut), result["added"]


def run_is_active(project_dir: Path, *, slot: str = "main") -> bool:
    """RUN10 #1: the single source for the "run (забег) is active" decision.

    Every hint and gate that depends on it goes through this one function —
    wait_for_event (timeout wording + the false-``done`` fuse), the
    ``awf_approve``/``awf_wait_for_event`` MCP next_action hints, and
    ``detect_phase``. The duplicated ``read_run(...).get("active")`` checks
    (and the heavy ``run_brief`` probe on the MCP side) are gone.

    ``slot`` (ORCH M5.2): "main" (default — every pre-M5.2 caller) or
    "service" (the service run's own state file).

    Returns False when no run exists or the file is corrupt (the reader
    degrades to "no run" — same as before).
    """
    run = read_run(project_dir, slot=slot)
    return bool(run and run.get("active"))


def clear_run(project_dir: Path, *, slot: str = "main") -> None:
    """Remove the run state file (soft reset) — for the given slot
    (ORCH M5.2: the service slot clears service-run.yaml, never run.yaml)."""
    try:
        run_file(project_dir, slot=slot).unlink()
    except FileNotFoundError:
        pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_started_at(started: str) -> datetime | None:
    """Parse started_at ('%Y-%m-%dT%H:%M:%SZ'). None when absent/unparseable.

    ``str`` coercion first: YAML parses an unquoted date as a date object,
    and strptime on a non-str raises TypeError (AUD02-09).
    """
    try:
        return datetime.strptime(str(started), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None


def started_at_ok(state: dict) -> bool:
    """AUD02-09: is the run clock trustworthy?

    Absent/empty started_at is NOT corrupt (fresh state, no clock yet) —
    the honest 0.0 case. A PRESENT but unparseable value is corrupt: the
    budget gate must degrade to a stop (see awf/api/run.py), not to
    "elapsed 0.0" which silently disables the budget.
    """
    started = state.get("started_at")
    if started is None or str(started).strip() == "":
        return True
    return _parse_started_at(started) is not None


def elapsed_minutes(state: dict) -> float:
    """Minutes since the run started (0.0 when started_at is absent or
    unparseable — display/degradation value. The BUDGET GATE must not rely
    on this: use :func:`started_at_ok` to stop on a corrupt clock
    (AUD02-09)."""
    started = state.get("started_at") or ""
    dt = _parse_started_at(started)
    if dt is None:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def downtime_minutes(state: dict) -> float:
    """B2: recorded non-working minutes (checkpoint waits, salvage handling,
    net-backoff pauses). Defensive: a corrupt value degrades to 0.0."""
    try:
        return float(state.get("downtime_seconds", 0) or 0) / 60.0
    except (TypeError, ValueError):
        return 0.0


def productive_minutes(state: dict) -> float:
    """B2: elapsed minus recorded downtime, clamped at 0.

    The budget gate counts THIS, not wall clock: a run that spent an hour on
    a checkpoint timeout and three silent worker deaths is not one hour of
    autonomous work. (A corrupt clock still stops the run via
    :func:`started_at_ok` — downtime never resurrects a dead clock.)
    """
    return max(0.0, elapsed_minutes(state) - downtime_minutes(state))


def run_slot_for_todo(project_dir: Path, todo_id: str) -> str:
    """ORCH M5.2: the state slot (main/service) whose queue owns ``todo_id``.

    The engine's downtime accounting must land in the run that is actually
    EXECUTING the pipeline, not blindly in the main slot — otherwise a
    service run's salvage / net-backoff would rewrite the main ``run.yaml``
    and break its isolation invariant (the main run stays byte-identical
    through a service run, crash included). A corrupt or absent service file
    degrades to "no service queue" (the main slot wins). A todo in neither
    queue (a pipeline launched outside any run) stays on "main" — the
    pre-M5.2 behavior.
    """
    if not todo_id:
        return "main"
    svc = read_run(project_dir, slot="service")
    for q in (svc or {}).get("queue") or []:
        if isinstance(q, dict) and str(q.get("todo_id", "")) == str(todo_id):
            return "service"
    return "main"


def add_downtime(
    project_dir: Path,
    seconds: float,
    reason: str = "",
    logs_dir: Path | None = None,
    slot: str = "main",
) -> None:
    """B2: accumulate ``seconds`` into the ACTIVE run's ``downtime_seconds``.

    No-op when seconds is not a positive number, when there is no run state
    (absent or corrupt), or when the run is not active — a pipeline outside a
    run must not create or touch run.yaml (hard rule: behavior without an
    active run is unchanged).

    ``slot`` (ORCH M5.2): which run's file to accumulate into — "main"
    (default, every pre-M5.2 caller) or "service" (resolved by the engine
    via :func:`run_slot_for_todo` when the executing stage belongs to the
    service run).

    The read-then-write is deliberate: ``update_run`` overwrites the file
    with an empty dict when ``read_run`` returns None, which would destroy a
    corrupt run.yaml kept for inspection (AUD02-06).
    """
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        return
    if secs <= 0:
        return
    if read_run(project_dir, slot=slot) is None:
        return

    def _mut(state: dict) -> dict:
        if not state.get("active"):
            return state
        try:
            current = float(state.get("downtime_seconds", 0) or 0)
        except (TypeError, ValueError):
            current = 0.0
        state["downtime_seconds"] = current + secs
        return state

    update_run(project_dir, _mut, slot=slot)
    if logs_dir is not None:
        suffix = f" ({reason})" if reason else ""
        _log(logs_dir, f"B2: downtime +{secs:.0f}s{suffix}")


def _downtime_seconds_of(state: dict) -> float:
    """The run's recorded downtime in seconds; a corrupt value degrades
    to 0.0 (the same way :func:`downtime_minutes` reads it)."""
    try:
        return max(0.0, float(state.get("downtime_seconds", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _format_iso(moment: datetime) -> str:
    """The run-state ISO stamp (UTC) — the same format ``now_iso`` writes
    and ``_parse_started_at`` reads."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def owner_idle_threshold_minutes(project_dir: Path) -> float:
    """TODO-0178: ``automation.owner_idle_minutes`` — the owner-idle
    threshold for the run budget.

    A gap between supervisor heartbeats strictly GREATER than this
    threshold (minutes) is owner absence: it is credited into the run's
    ``downtime_seconds``, so the pause stops counting as productive
    budget minutes (``productive = elapsed − downtime`` keeps the
    formula).

    Degradation (never raises):
    - key absent / unreadable config → the default 15 (the feature is on
      by default — the bug it fixes is silent budget burn);
    - a numeric value <= 0 → 0.0, i.e. the feature is DISABLED (an
      explicit opt-out, not an error);
    - a present non-numeric value → the default 15 (a typo should not
      switch accounting off).
    """
    from . import config as _config

    value = _config.get(_config.load(project_dir), "automation.owner_idle_minutes")
    if value is None:
        return 15.0
    if isinstance(value, bool):
        return 15.0
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return 15.0
    return minutes if minutes > 0 else 0.0


def supervisor_beat(project_dir: Path, *, now: datetime | None = None) -> None:
    """TODO-0178: the supervisor heartbeat — "the owner is present" for
    the ACTIVE main run, plus owner-idle accounting.

    Every awf-* tool call for the project passes through here (the
    plugin's ``_exec`` wrapper; the CLI is out of scope). The call
    refreshes ``last_supervisor_beat`` in the run state; when the gap
    since the previous beat exceeds
    :func:`owner_idle_threshold_minutes` (default 15, <= 0 = disabled),
    the gap is added to ``downtime_seconds`` — pauses while the owner
    thinks/is away stop burning the productive budget.

    Credit formula (incremental — no history is scanned):
        credit = max(0, gap_seconds − downtime_added_since_last_beat)
    where the second term is the change of ``downtime_seconds`` since the
    previous beat (the engine's own checkpoint-wait / salvage /
    net-backoff credits, snapshotted in
    ``downtime_seconds_at_last_beat``). That subtraction is the
    double-count protection: a checkpoint wait inside the owner's
    absence was already credited by the engine — only the remainder is
    credited as owner-idle. The gap is measured in whole seconds
    (floor) — minute-level accounting, no sub-second noise.

    No-op (nothing written): no run state (absent or corrupt — a state
    file is never CREATED by a beat), or the run not active. The FIRST
    beat after the feature upgrade (or a corrupt/unparseable
    ``last_supervisor_beat``) only refreshes the reference — no credit
    (the absence before the first beat is unknown). ``now`` is injectable
    for tests.

    The beat is refreshed even when the feature is disabled (threshold
    <= 0) — so re-enabling it later cannot credit a stale multi-day gap.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    threshold = owner_idle_threshold_minutes(project_dir)
    state = read_run(project_dir)
    if state is None or not state.get("active"):
        return

    credited: list[float] = [0.0]

    def _mut(st: dict) -> dict:
        if not st.get("active"):
            return st
        last_raw = st.get("last_supervisor_beat")
        last = _parse_started_at(last_raw) if last_raw not in (None, "") else None
        if last is None:
            # first beat / corrupt reference: refresh, no credit
            st["last_supervisor_beat"] = _format_iso(moment)
            st["downtime_seconds_at_last_beat"] = _downtime_seconds_of(st)
            return st
        raw_gap = (moment - last).total_seconds()
        if raw_gap > 0 and threshold > 0 and int(raw_gap) > int(threshold * 60):
            snapshot_raw = st.get("downtime_seconds_at_last_beat")
            current = _downtime_seconds_of(st)
            try:
                snapshot = (
                    float(snapshot_raw) if snapshot_raw is not None else None
                )
            except (TypeError, ValueError):
                snapshot = None  # corrupt: no credit, refresh the baseline
            if snapshot is not None:
                engine = max(0.0, current - snapshot)
                credit = max(0.0, int(raw_gap) - engine)
                if credit > 0:
                    st["downtime_seconds"] = current + credit
                    credited[0] = credit
            st["downtime_seconds_at_last_beat"] = _downtime_seconds_of(st)
        elif raw_gap > 0:
            # under the threshold (or disabled): keep the snapshot fresh
            st["downtime_seconds_at_last_beat"] = _downtime_seconds_of(st)
        st["last_supervisor_beat"] = _format_iso(moment)
        return st

    update_run(project_dir, _mut)
    if credited[0] > 0:
        _log(
            paths.logs_dir(project_dir),
            f"TODO-0178: owner-idle downtime +{credited[0]:.0f}s "
            f"(gap > {threshold:g} min)",
        )


def position(state: dict) -> str:
    """Human position like ``1/3`` — the RUNNING item's number.

    NEG-2026-09-19 R4: ``run_next`` advances ``index`` at LAUNCH, so
    ``index + 1`` showed the NEXT item (2/4 while the first was running).
    When ``current`` is set, the running item is ``index``; between items
    (no current) the next-to-run is ``index + 1``.
    """
    queue = state.get("queue") or []
    total = len(queue)
    if not total:
        return "0/0"
    idx = int(state.get("index", 0) or 0)
    current = str(state.get("current") or "")
    shown = idx if current else idx + 1
    shown = max(1, min(shown, total))
    return f"{shown}/{total}"


def run_step_after_done(state: dict | None) -> str:
    """ORCH M1.3: the next permitted step once the CURRENT item's decision
    is an APPROVE (done, archived by the commit gate) — '' when the state
    does not force one (the caller keeps its own hint).

    After the approve the queue position is owned by the run: the next
    step is `awf_run_next` (it launches the next queue item, or stops the
    run with a report when the queue is exhausted) — not a generic
    `awf_start`/`awf_dispatch_todo`. A fresh supervisor session must
    restore that step from the state, without guessing (IMPLEMENTATION-
    STRATEGY §1.4, acceptance). The REJECT side is rendered by
    `run_plan_read.next_action_for_record` — the two do not overlap.

    Reads the sanitized state (`read_run` output): a corrupt
    decisions/index value degrades to '' (no forced step), never raises.
    """
    if not state or not state.get("active"):
        return ""
    current = str(state.get("current") or "")
    if not current:
        return ""
    decisions = state.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return ""
    last = decisions[-1]
    if not isinstance(last, dict):
        return ""
    if str(last.get("kind") or "") != "approve":
        return ""
    if str(last.get("todo_id") or "") != current:
        return ""
    queue = state.get("queue") or []
    try:
        index = int(state.get("index", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if index < len(queue):
        item = queue[index]
        next_id = str(item.get("todo_id", "")) if isinstance(item, dict) else str(item)
        return (
            f"{current} is approved (done). Next step: `awf_run_next` — "
            f"it launches queue item {index + 1}/{len(queue)} ({next_id})."
        )
    return (
        f"{current} is approved (done) and the queue is exhausted — "
        f"`awf_run_next` will stop the run with a report (or close it now "
        f"with `awf_run_finish`)."
    )
