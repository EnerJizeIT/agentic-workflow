"""A29: one arbiter per unit — a human verdict outranks the auto subprocess.

Incident 01.11 (TODO-0321): the owner approved a green unit through
``awf_approve``; the engine then spawned its own verify subprocess, that
one rejected, and the engine replanned an already accepted unit. The
reverse order (auto approves, the owner wants to reject) is the same
conflict, so the rule is symmetric: the human decision wins.

Two halves live here:

- the source label — ``awf_approve`` writes ``{"source": "human"}`` into
  the APPROVE signal, ``awf_reject`` prefixes the REVIEW body with an HTML
  comment. The auto subprocess writes plain hand-made files, so a marked
  file is always the owner's;
- :func:`human_verify_decision` — the verdict valid for the current
  attempt, or ``None``. The supervisor stage asks it before spawning (a
  human already decided — no subprocess) and after the subprocess returns
  (a human verdict written during the run outranks the auto one);
- :func:`await_human_override` — the A29 window (TODO-0364): after taking
  the auto verdict the engine waits ``wait.human_override_seconds`` and
  re-asks :func:`human_verify_decision`, so a verdict issued seconds later
  still wins. The window adds no second definition of "valid": it asks the
  same arbiter with ``require_current_binding=True``.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import commit_plan, paths
from ._log import log as _log

#: The label ``awf_approve``/``awf_reject`` put on their signals.
HUMAN = "human"
#: REVIEW files are Markdown, so the label is an invisible HTML comment.
REVIEW_MARKER = "<!-- awf-source: human -->"

_SOURCE_RE = re.compile(r"<!--\s*awf-source:\s*(\w+)\s*-->")


def mark_human_review(text: str) -> str:
    """Prefix a REVIEW body with the human label (idempotent)."""
    if _SOURCE_RE.search(text):
        return text
    return f"{REVIEW_MARKER}\n\n{text}"


def signal_source(path: Path) -> str:
    """The source label carried by a decision file — "human", else "".

    APPROVE is JSON (a binding, or the bare ``{"source": "human"}`` marker);
    REVIEW is Markdown with the comment. An unreadable or unlabelled file
    is the auto subprocess's (the old behavior) — "".
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if path.name.startswith("APPROVE-"):
        try:
            data = json.loads(raw) if raw.strip() else None
        except ValueError:
            return ""
        if isinstance(data, dict) and data.get("source") == HUMAN:
            return HUMAN
        return ""
    match = _SOURCE_RE.search(raw)
    return match.group(1) if match else ""


def decision_is_stale(
    path: Path,
    signal: str,
    accepted_decision: str | None,
    accepted_decision_mtime: float | None,
) -> bool:
    """U6b: the same physical file the engine already accepted is a leftover.

    ``accepted_decision``/``accepted_decision_mtime`` is the decision the
    engine ACCEPTED in a previous verify cycle (recorded at acceptance,
    cleared at consumption — pipeline_engine._record_verify_decision /
    _consume_verify_decision). A kill between acceptance and consumption
    leaves the decision file on disk; the next cycle would re-accept it by
    mere existence (auto-commit on a dead approval).

    The leftover is the SAME physical file as the accepted one: same signal
    name, mtime no newer than the recorded one (whole-second resolution,
    same convention as _decision_signal_fresh). A newer mtime means the
    owner re-approved — that is a fresh decision, not the leftover.

    Single owner of the rule: the supervisor's auto fallback and this
    module's human-verdict reader both call it, so the record shape cannot
    drift between them.
    """
    if not accepted_decision or accepted_decision_mtime is None:
        return False
    if accepted_decision != signal:
        return False
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return int(mtime) <= int(accepted_decision_mtime)


def _decision_path(project_dir: Path, signal: str) -> Path | None:
    """The file a decision signal names, or ``None`` for an unknown name."""
    if signal.startswith("REVIEW-"):
        return paths.outbox(project_dir) / f"{signal}.md"
    if signal.startswith(("ACK-", "APPROVE-")):
        return paths.inbox(project_dir) / f"{signal}.ready"
    return None


def discard_verdict(
    project_dir: Path, signal: str, logs_dir: Path | None = None
) -> None:
    """Remove an overruled auto verdict so it cannot act on a later cycle.

    Without this the ignored ``REVIEW`` would still be the first file
    ``_detect_supervisor_signal`` sees on a re-verify of the same unit — a
    dead refusal would replan the unit the owner just approved. The unlink
    goes through the same no-follow barrier as the engine's consumption
    (TODO-0235): a symlinked surface leaves the file where it is.
    """
    from .infra import managed_fs

    path = _decision_path(project_dir, signal)
    if path is None:
        return
    try:
        managed_fs.assert_no_symlink_escape(
            path, root=managed_fs.agentic_root_for(path)
        )
    except Exception as exc:  # noqa: BLE001 — a probe must never fail the stage
        _log(logs_dir, f"A29: ignored verdict {path.name} left in place: {exc}")
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _log(logs_dir, f"A29: ignored verdict {path.name} not removed: {exc}")
        return
    _log(logs_dir, f"A29: ignored verdict {path.name} removed")


def overruled_auto_verdicts(
    project_dir: Path, todo_id: str, keep: str
) -> list[str]:
    """The auto (unlabelled) decisions the human verdict ``keep`` overrules.

    Named separately from the removal so the caller can put the ignored
    signals into the decision log line without a second scan.
    """
    found: list[str] = []
    for template in ("APPROVE-{todo}", "ACK-{todo}", "REVIEW-{todo}"):
        signal = template.format(todo=todo_id)
        if signal == keep:
            continue
        path = _decision_path(project_dir, signal)
        if path is None or not path.is_file():
            continue
        if signal_source(path) == HUMAN:
            continue
        found.append(signal)
    return found


def discard_overruled(
    project_dir: Path, todo_id: str, keep: str, logs_dir: Path | None = None
) -> None:
    """Remove EVERY auto verdict the human decision overrules, not just one.

    The overruled file is not necessarily the verdict the engine picked up:
    on the fallback path ``_detect_supervisor_signal`` scans REVIEW first,
    so a human REVIEW is returned as the "auto" signal while the auto
    APPROVE stays in the inbox. That leftover is invisible to the freshness
    gate of the interactive wait but not to the auto fallback (which has no
    mtime gate) — a later re-verify of the same unit could commit on the
    approval the owner just overruled, or replan on the refusal they
    accepted. Hence the sweep by decision NAME, not by the one file the
    scan happened to return.

    A human-labelled file of the other kind is left alone: the engine
    consumes the verdict it acted on, and deleting the owner's own files is
    not this module's job.
    """
    for signal in overruled_auto_verdicts(project_dir, todo_id, keep):
        discard_verdict(project_dir, signal, logs_dir)


def _approve_is_current(
    project_dir: Path, todo_id: str, logs_dir: Path | None
) -> bool:
    """A23/REPORTS29: the binding matches the attempt the gate will commit."""
    try:
        status, _reason = commit_plan.preexisting_approval_status(
            project_dir,
            todo_id,
            commit_plan.read_baseline_sha(project_dir, todo_id),
        )
    except Exception as exc:  # noqa: BLE001 — a probe must never fail the stage
        # ``_log`` needs a real directory: passing None would raise and turn
        # "the probe failed" into "the verify stage crashed".
        if logs_dir is not None:
            _log(logs_dir, f"A29: approve binding probe failed for {todo_id}: {exc}")
        return False
    return status == "valid"


def _mtime(path: Path) -> float | None:
    """The file's mtime, or ``None`` when it is absent/unreadable.

    One stat instead of ``is_file()`` + ``stat()``: the owner may consume a
    decision between the two calls, and a probe must never raise there.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _approve_tree_moved(
    project_dir: Path, todo_id: str, logs_dir: Path | None
) -> bool:
    """Whether a BOUND approve names a tree that is no longer this attempt's.

    A plain marker (``{"source": "human"}``, a legacy approve) carries no
    tree, so there is nothing to compare — False. An approve that names its
    ``verified_sha`` is a statement about a tree: once the tree moved, the
    statement is about an earlier one.
    """
    binding = commit_plan.read_approval_binding(project_dir, todo_id) or {}
    if not binding.get("verified_sha"):
        return False
    return not _approve_is_current(project_dir, todo_id, logs_dir)


def human_verify_decision(
    project_dir: Path,
    todo_id: str,
    wait_start: float,
    logs_dir: Path | None = None,
    *,
    require_current_binding: bool = False,
) -> tuple[str, float] | None:
    """The owner's verify verdict for THIS attempt, or ``None``.

    ``wait_start`` is the second the caller started waiting (whole-second
    resolution, the AUD04-04 convention). An APPROVE counts when its
    binding still matches the attempt OR when it was written during this
    wait (a just-issued ``awf_approve`` without ``verified_sha`` carries
    only the source marker, so there is no binding to validate). A REVIEW
    counts on presence — a rejection needs no binding, and the engine
    consumes the file when it acts on it.

    A decision the engine already accepted (the U6b record) is a leftover
    and is ignored. Returns ``(signal_name, mtime)`` of the newest valid
    human decision; on a tie REVIEW wins — reading a conflict as a
    rejection is the safe direction.

    ``require_current_binding`` is the A29 window's stricter reading of the
    APPROVE half: there the owner's verdict must OVERRULE an already-taken
    auto one, so a bound approve counts only while its tree is still the
    attempt's — even a just-written one is a statement about a tree that
    moved. The default keeps the pre-spawn/wait reading (a fresh approve
    counts; the commit gate validates it afterwards). One function decides
    both, so "the owner's verdict counts" has a single definition.
    """
    from .pipeline_state import read_state

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    state = read_state(project_dir) or {}
    accepted = state.get("accepted_decision")
    accepted_mtime = state.get("accepted_decision_mtime")

    found: list[tuple[float, int, str]] = []

    approve = inbox / f"APPROVE-{todo_id}.ready"
    signal = f"APPROVE-{todo_id}"
    mtime = _mtime(approve)
    if mtime is not None and signal_source(approve) == HUMAN:
        if not decision_is_stale(approve, signal, accepted, accepted_mtime):
            fresh = int(mtime) >= int(wait_start)
            current = _approve_is_current(project_dir, todo_id, logs_dir)
            counts = fresh or current
            tree_moved = False
            if require_current_binding:
                # A29 window: overruling an already-taken auto verdict needs
                # a verdict about THIS tree — a bound approve that no longer
                # matches cannot win, however fresh it is. A marker names no
                # tree, so the ordinary freshness rule stands.
                tree_moved = _approve_tree_moved(
                    project_dir, todo_id, logs_dir
                )
                counts = counts and not tree_moved
            if counts:
                found.append((mtime, 0, signal))
            elif logs_dir is not None and tree_moved:
                _log(
                    logs_dir,
                    f"A29: human {signal} ignored in the override window — "
                    "its verified_sha does not match this attempt (the tree "
                    "moved after verification)",
                )
            elif logs_dir is not None:
                _log(
                    logs_dir,
                    f"A29: human {signal} ignored — its binding does not "
                    "match the current attempt and it predates this wait",
                )

    review = outbox / f"REVIEW-{todo_id}.md"
    signal = f"REVIEW-{todo_id}"
    mtime = _mtime(review)
    if mtime is not None and signal_source(review) == HUMAN:
        if not decision_is_stale(review, signal, accepted, accepted_mtime):
            found.append((mtime, 1, signal))

    if not found:
        return None
    mtime, _priority, signal = max(found)
    return signal, mtime


def await_human_override(
    project_dir: Path,
    todo_id: str,
    auto_signal: str,
    wait_start: float,
    window: float,
    logs_dir: Path | None = None,
    poll: float = 1.0,
) -> tuple[str, float] | None:
    """A29: the overlap window between the auto verdict and the owner's.

    The auto supervisor subprocess may reject a unit seconds before the
    owner's ``awf_approve`` lands (incident 03.11, TODO-0359). The engine
    must not turn that race into a replan, so after taking an auto verdict
    it gives the owner ``window`` seconds to overrule it.

    The decision is :func:`human_verify_decision` with
    ``require_current_binding`` — the same arbiter as every other path, so
    "the owner wins" and "the approve names this attempt" each have one
    definition. A verdict equal to ``auto_signal`` is returned at once: the
    caller's "the scan picked up the owner's own file" branch must still
    run (it sweeps the other auto verdicts), and there is nothing to wait
    for.

    ``window <= 0`` still performs one check (the pre-existing-signal case
    stays instant, requirement 4) and returns without sleeping.

    Returns ``(signal_name, mtime)`` of the human verdict, or ``None`` when
    the window expired with no valid human decision — the caller then
    applies the auto verdict as before.
    """
    deadline = time.monotonic() + max(0.0, window)
    while True:
        human = human_verify_decision(
            project_dir, todo_id, wait_start, logs_dir,
            require_current_binding=True,
        )
        if human is not None:
            if human[0] != auto_signal and auto_signal and logs_dir is not None:
                _log(
                    logs_dir,
                    f"human override: {human[0]} wins over auto "
                    f"{auto_signal} (window {window:g} s)",
                )
            return human
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # ``poll`` is the resolution of the window, not a knob: one long
        # sleep would see a verdict issued at second 1 only when the window
        # is over — the incident this window exists for.
        time.sleep(min(poll, remaining))
