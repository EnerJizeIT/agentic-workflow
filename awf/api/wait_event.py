"""Supervisor wake-up: block until pipeline event (no more polling loops).

Replaces the ``while True: sleep(30); awf_status()`` pattern with a
single blocking call. Supervisor calls ``wait_for_event``, tool blocks
inside plugin (polling the state file every ``poll_interval`` seconds,
default 3), returns immediately when an interesting event happens:

- ``verify`` — pipeline reached verify stage (supervisor must act)
- ``blocked`` — worker wrote BLOCKED signal
- ``checkpoint`` — BD-36 checkpoint form opened (tell user)
- ``done`` — pipeline cycle complete: stage state cleared and the cycle is
  confirmed (``done/<id>/`` archive or an awf commit) — the message names
  the TODO and the exact next command (``awf_run_next`` in a run,
  ``awf_dispatch_todo`` outside; RUN6 #1)
- ``timeout`` — no event within timeout seconds (poll again)

Token savings: ONE tool call with one response (vs N calls with N
sleep+status cycles). Supervisor doesn't burn tokens on idle polling.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from ..pipeline_state import read_state
from ._helpers import require_agentic
from ._results import WaitEventResult

# B3 (run2 report): the default MCP client transport (~60s timeout) cuts a
# single wait at ~55s — the measured working cap. This is the DEFAULT; when
# the owner's transport tolerates a longer single wait, the cap is raised
# via .agentic/config.yaml `wait.cap_seconds` or env AWF_WAIT_CAP (env wins,
# RUN6 #3) — see wait_cap(). suggested_timeout must stay at or under it, or
# the supervisor's next call dies mid-wait (-32001).
TRANSPORT_CAP = 55
MIN_WAIT = 30


def _parse_cap(value: Any) -> int | None:
    """A positive int usable as a wait cap; None when not a valid one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
    elif isinstance(value, str):
        try:
            n = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    return n if n > 0 else None


def wait_cap(project_dir: Path | None = None) -> int:
    """RUN6 #3: the honest single-wait cap for this project.

    Priority: env ``AWF_WAIT_CAP`` > ``wait.cap_seconds`` in
    ``.agentic/config.yaml`` > ``TRANSPORT_CAP`` (default 55s). An invalid
    value falls through to the next source with a stderr warning, so the
    default always works.
    """
    raw = os.environ.get("AWF_WAIT_CAP")
    if raw is not None:
        cap = _parse_cap(raw)
        if cap is not None:
            return cap
        print(
            f"WARNING: AWF_WAIT_CAP={raw!r} is not a positive integer — "
            "falling back to the config/default cap",
            file=sys.stderr,
        )
    if project_dir is not None:
        from .. import config as _cfg

        value = _cfg.get(_cfg.load(project_dir), "wait.cap_seconds")
        if value is not None:
            cap = _parse_cap(value)
            if cap is not None:
                return cap
            print(
                f"WARNING: wait.cap_seconds={value!r} is not a positive "
                "integer — using the default cap",
                file=sys.stderr,
            )
    return TRANSPORT_CAP


# RUN6 #1: the signs of a completed pipeline cycle. The commit comes from
# commit_gate.maybe_commit (``awf(<stage>): TODO-NNNN``), the archive from
# todos.archive_todo (``.agentic/done/<todo>/``) — either one is enough.
_TODO_ID_RE = re.compile(r"^TODO-\d{4,}$")
_AWF_COMMIT_RE = re.compile(r"^awf\([^)]+\):\s*(TODO-\d{4,})\s*$")


def _suggest_timeout(project_dir: Path, *, default: int = TRANSPORT_CAP) -> int:
    """SPEC A-run: size the next wait from measured stage durations.

    Consecutive ``Stage N/M:`` lines in orchestrator.log give the duration of
    the previous stage. Median of the last few, divided by 3 (wake ~3x per
    stage), clamped to [MIN_WAIT, cap] seconds — never above the project's
    wait cap (wait_cap: env/config, default TRANSPORT_CAP), which would cut
    the next call mid-wait. Falls back to ``default`` (capped) when the log
    is missing or has too little history.

    AUD15-08: the stage stamps come from the shared incremental reader
    (awf/_log_reader.py) — no 5th full read of the log per wait_for_event.
    """
    from .. import paths
    from .._log_reader import read_log_snapshot

    cap = wait_cap(project_dir)
    log_file = paths.agentic_dir(project_dir) / "logs" / "orchestrator.log"
    stamps = read_log_snapshot(log_file).stage_stamps
    if len(stamps) < 2:
        return min(default, cap)
    deltas = [b - a for a, b in zip(stamps, stamps[1:])]
    deltas = [d for d in deltas if 0 < d < 3600][-5:]
    if not deltas:
        return min(default, cap)
    deltas.sort()
    median = deltas[len(deltas) // 2]
    return max(MIN_WAIT, min(cap, int(median / 3)))


def _cap_advice(project_dir: Path | None = None) -> str:
    """B3/RUN6 #3: the suggested wait hit the cap — advise smaller steps."""
    cap = wait_cap(project_dir)
    if cap == TRANSPORT_CAP:
        return (
            f" Transport cap: a single wait above {cap}s is cut by the "
            "MCP client (-32001) — wait in smaller steps (suggested_timeout)."
        )
    return (
        f" Wait cap: a single wait above {cap}s is cut on this setup "
        "(wait.cap_seconds / AWF_WAIT_CAP) — wait in smaller steps "
        "(suggested_timeout)."
    )


def _state_describes_live_pipeline(state: dict[str, Any] | None) -> bool:
    """True when state still carries stage markers (pipeline mid-run).

    RUN6 #1: the orchestrator's clean exit CLEARS the stage markers but
    rewrites the file with ``phase=done`` + goal/normalized (AUD02-04) —
    the file exists, ``read_state()`` is not None, yet no pipeline is
    running. That leftover used to drain a full wait timeout with a null
    snapshot (owner report 22.09, 5/5 repeats). Absent markers = exited.
    """
    if not state:
        return False
    return bool(
        state.get("stage_kind") or state.get("stage_name") or state.get("todo_id")
    )


def _state_is_exited(state: dict[str, Any] | None) -> bool:
    """RUN7 #1: does this state shape mean 'the pipeline cycle ended'?

    Only two shapes count as exited:

    - state is ABSENT (the file was removed — ``clear_state``), or
    - the clean-exit leftover: no stage markers but ``phase == "done"``
      (the orchestrator's exit rewrites the file with phase=done —
      AUD02-04 / RUN6 #1).

    A marker-less leftover WITHOUT ``phase=done`` is NOT exited — notably
    the kill leftover from ``_clear_state_keep_salvage`` (awf/api/
    pipeline.py): a kill after a salvage attempt clears the stage state
    but keeps only salvage_count/salvage_count_key. The cycle evidence
    behind a ``done`` (``done/<id>/`` or an awf commit) is project-wide,
    not cycle-scoped, so declaring such a state exited would hand the
    supervisor a FALSE ``done`` for a PREVIOUS cycle's TODO; in a run the
    loop would then call awf_run_next, which refuses (the killed TODO is
    not finished). That shape keeps the old wait behavior (idle/timeout)
    — never a ``done`` (TODO-0061, the QA TODO-0056 finding).

    Do not collapse this into ``not _state_describes_live_pipeline`` — the
    kill leftover is exactly the marker-less shape that predicate cannot
    tell apart from a clean exit.
    """
    if not state:
        return True
    if _state_describes_live_pipeline(state):
        return False
    return state.get("phase") == "done"


def _last_completed_todo(project_dir: Path) -> tuple[str, str] | None:
    """Newest completed-cycle sign: ``(todo_id, kind)`` or None.

    kind is ``"commit+archive"`` (both signs, same TODO), ``"commit"``
    (newest ``awf(<stage>): TODO-NNNN`` commit) or ``"archive"`` (newest
    ``done/TODO-*/`` directory — non-git projects). The commit is the
    first sign of a finished cycle (the archive follows it in the same
    cycle), so it wins when present; the archive is the fallback.
    """
    from .. import git_utils, paths

    commit_id: str | None = None
    try:
        out = git_utils.git_stdout(
            project_dir, "log", "-n", "30", "--pretty=%s", check=False
        )
    except (RuntimeError, OSError):
        out = ""
    for line in out.splitlines():
        m = _AWF_COMMIT_RE.match(line.strip())
        if m:
            commit_id = m.group(1)
            break

    archive_id: str | None = None
    done_dir = paths.done_dir(project_dir)
    if done_dir.is_dir():
        candidates = [
            d for d in done_dir.iterdir()
            if d.is_dir() and _TODO_ID_RE.match(d.name)
        ]
        if candidates:
            newest = max(candidates, key=lambda d: d.stat().st_mtime)
            archive_id = newest.name

    if commit_id and commit_id == archive_id:
        return commit_id, "commit+archive"
    if commit_id:
        return commit_id, "commit"
    if archive_id:
        return archive_id, "archive"
    return None


def _run_allows_done(project_dir: Path) -> bool:
    """RUN10 #1 fuse (RUN9 incident): may a ``done`` be handed out now?

    Outside a run: yes — the previous behavior, unchanged. Inside an ACTIVE
    run: only when the run's current element itself is finished (archived
    and not active again). Otherwise the pipeline DIED mid-iteration: the
    project-wide cycle evidence (``done/<id>/`` or an awf commit) belongs to
    a PREVIOUS cycle, and a ``done`` would drag the run loop into
    ``awf_run_next`` — which refuses ("previous TODO is not finished") and
    stops the run (RUN9: the false done after the kill-race death).

    A run without a recorded ``current`` (hand-written/legacy state) is
    treated as "not finished" too — ``run_next`` always records it.
    """
    from ..run_state import read_run, run_is_active

    if not run_is_active(project_dir):
        return True
    run = read_run(project_dir)
    current = str((run or {}).get("current") or "")
    if not current:
        return False
    # Same finished definition the run gates use (awf/api/run.py).
    from .run import _todo_finished

    return _todo_finished(project_dir, current)


def _cycle_done_event(project_dir: Path) -> WaitEventResult | None:
    """RUN6 #1: the finished-cycle wake-up (the ``done`` the owner had to
    read out of ``git log`` after every approve).

    Fires ONLY when the pipeline is provably not running — no stage
    markers in state AND the process is dead (shared liveness resolver) —
    AND the state shape means the cycle ended (``_state_is_exited``,
    RUN7 #1) AND the project shows a completed cycle (``done/<id>/`` or an
    awf commit). Otherwise returns None and the caller keeps the old
    behavior (idle / timeout), so a live pipeline or a mid-run state can
    never get a false ``done``. The message carries the completed TODO and
    the exact next command: ``awf_run_next`` inside a run,
    ``awf_dispatch_todo`` outside it.

    RUN10 #1 fuse: inside an ACTIVE run the finished-cycle sign must be
    the run's CURRENT element, and that element must be finished
    (``_run_allows_done``) — a dead pipeline mid-iteration never yields a
    ``done`` for a previous cycle (RUN9 incident).
    """
    from ..run_state import read_run, run_is_active
    from ._liveness import resolve

    running, _pid, _source = resolve(project_dir)
    if running:
        return None
    state = read_state(project_dir)
    if not _state_is_exited(state):
        # RUN7 #1: a marker-less kill leftover (the salvage counter only)
        # is not a finished cycle — the project-wide evidence would name a
        # PREVIOUS cycle's TODO. Keep the old wait behavior, never a
        # false 'done' for a past cycle.
        return None
    if not _run_allows_done(project_dir):
        # RUN10 #1 fuse: an active run whose current element is NOT finished
        # means the pipeline died mid-iteration — never a 'done' for a
        # previous cycle (the caller keeps the old wait: idle/timeout).
        return None
    evidence = _last_completed_todo(project_dir)
    if evidence is None:
        return None
    todo_id, kind = evidence
    if run_is_active(project_dir):
        # The finished-cycle sign must be the run's CURRENT element — a
        # sign for any other TODO is a previous cycle, not this one.
        run = read_run(project_dir)
        if todo_id != str((run or {}).get("current") or ""):
            return None

    verb = {
        "commit+archive": "committed and archived",
        "commit": "committed",
        "archive": "archived",
    }[kind]
    if run_is_active(project_dir):
        next_step = (
            "Next step: awf_run_next(project_dir) to launch the next queued TODO."
        )
    else:
        next_step = (
            "Next step: awf_dispatch_todo(project_dir, content) for the next task."
        )

    snapshot = _state_to_dict(state or {})
    snapshot["todo_id"] = todo_id
    return WaitEventResult(
        event_type="done",
        message=f"{todo_id} {verb} — pipeline cycle complete. {next_step}",
        state_snapshot=snapshot,
    )


def wait_for_event(
    project_dir: Path,
    *,
    timeout: int = 30,
    poll_interval: int = 3,
    actionable_only: bool = False,
) -> WaitEventResult:
    """Block until pipeline event or timeout.

    Polls ``.agentic/state/current.yaml`` every ``poll_interval`` seconds.
    Returns immediately when:

    - Stage kind = ``verify`` → event_type ``verify``
    - ``last_signal`` starts with ``BLOCKED-`` → event_type ``blocked``
    - ``checkpoint_pending`` = True → event_type ``checkpoint``
    - Stage state cleared (pipeline exited; the clean-exit ``phase=done``
      leftover counts as cleared — RUN6 #1) → event_type ``done``. When the
      cycle is confirmed (``done/<id>/`` or an ``awf(<stage>): TODO-NNNN``
      commit) the message names the completed TODO and the exact next
      command — ``awf_run_next`` inside a run, ``awf_dispatch_todo``
      outside it. Without a confirmed cycle the pipeline is simply not
      running → event_type ``idle`` (no more full-timeout-on-null-state).
      A marker-less kill leftover without ``phase=done`` (only the salvage
      counter — RUN7 #1) is NOT a finished cycle: the old wait behavior
      (timeout), never a ``done`` for a past cycle. RUN10 #1 fuse: inside
      an ACTIVE run a ``done`` requires the run's CURRENT element to be
      finished (archived, not active again) — a pipeline death mid-
      iteration keeps the old wait behavior (timeout/idle), never a
      ``done`` for a previous cycle. Outside a run: unchanged.
    - Timeout reached → event_type ``timeout``

    SPEC A-run: in a run (забег) loop pass ``timeout`` from the previous
    result's ``suggested_timeout`` (computed from measured stage durations,
    never above the project's wait cap — ``wait.cap_seconds`` config /
    ``AWF_WAIT_CAP`` env, default ``TRANSPORT_CAP``; the transport cuts
    longer single waits, B3 / RUN6 #3). The call runs in a worker thread, so
    long waits do NOT freeze other MCP tools — the old "single-thread limit"
    note was stale.

    Args:
        project_dir: awf project root.
        timeout: max seconds to block (default 30 — reactive mode).
        poll_interval: seconds between state checks (default 3).
        actionable_only: suppress ``stage_changed`` returns — only events
            that need supervisor action end the wait. Keeps a run loop quiet.

    Returns:
        WaitEventResult with event_type + current state snapshot.

    Raises:
        AwfApiError: if .agentic/ missing.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    suggested = _suggest_timeout(project_dir)
    cap = wait_cap(project_dir)
    deadline = time.monotonic() + timeout
    prev_state: dict[str, Any] | None = read_state(project_dir)

    # No stage markers AND the state shape means 'cycle ended' — either the
    # file is absent or only the clean-exit leftover (phase=done) remains
    # (RUN6 #1 / RUN7 #1). A finished cycle answers 'done' immediately;
    # with a dead process and no sign the pipeline is simply not running
    # ('idle') — the old full-timeout-on-null-state drain. A marker-less
    # leftover WITHOUT phase=done (the kill leftover with only the salvage
    # counter, _clear_state_keep_salvage) is NOT exited — it falls through
    # to the poll loop (the pre-RUN6 wait: timeout), never a 'done' for a
    # past cycle. A LIVE process without markers is mid-shutdown — the old
    # wait behavior is kept (fall through to the poll loop), no false
    # 'done', no false 'not running'.
    if (
        not _state_describes_live_pipeline(prev_state)
        and _state_is_exited(prev_state)
    ):
        from ._liveness import resolve

        running, _pid, _source = resolve(project_dir)
        if not running:
            done = _cycle_done_event(project_dir)
            if done:
                return done
            if not prev_state:
                return WaitEventResult(
                    event_type="idle",
                    message="No pipeline state found — pipeline not running.",
                )
            return WaitEventResult(
                event_type="idle",
                message=(
                    "Pipeline not running — stage state cleared. "
                    "Check awf_status for the project state."
                ),
            )

    # Check current state for immediate events
    result = _check_for_event(prev_state, project_dir)
    if result:
        return result

    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        current_state = read_state(project_dir)

        # Stage markers gone (file cleared, or only the phase=done leftover
        # remains — RUN6 #1). A confirmed finished cycle answers with the
        # TODO + next step. Without a sign, declare "exited" only when the
        # process is actually dead AND the state shape means the cycle
        # ended — a live process is mid-shutdown (the clear happens a
        # moment before the exit), and a marker-less kill leftover (the
        # salvage counter only — RUN7 #1) keeps polling: a 'done' there
        # would name a PREVIOUS cycle's TODO.
        if not _state_describes_live_pipeline(current_state):
            done = _cycle_done_event(project_dir)
            if done:
                return done
            from ._liveness import resolve

            if (
                not resolve(project_dir)[0]
                and _state_is_exited(current_state)
                # RUN10 #1 fuse: this evidence-free 'done' must not fire
                # inside an active run whose current element is not
                # finished either — same death-mid-iteration shape.
                and _run_allows_done(project_dir)
            ):
                return WaitEventResult(
                    event_type="done",
                    message="Pipeline exited (state file cleared). Check awf_report.",
                )

        # Check for actionable events FIRST (verify/blocked/checkpoint/salvage).
        # These require supervisor action and must not be masked by stage_changed.
        result = _check_for_event(current_state, project_dir)
        if result:
            return result

        # SELF-1: detect stage transitions (agent finished → next agent started).
        # Only returned when no actionable event is pending — avoids masking
        # verify/blocked events that happen to coincide with a stage transition.
        prev_stage = prev_state.get("stage_name") if prev_state else None
        curr_stage = current_state.get("stage_name") if current_state else None
        if prev_stage and curr_stage and prev_stage != curr_stage and not actionable_only:
            message = (
                f"Stage transition: '{prev_stage}' → '{curr_stage}'. "
                f"Previous stage completed. Poll again to wait for next event."
            )
            if suggested >= cap:
                message += _cap_advice(project_dir)
            return WaitEventResult(
                event_type="stage_changed",
                message=message,
                state_snapshot=_state_to_dict(current_state),
                suggested_timeout=suggested,
            )

        prev_state = current_state

    # Timeout
    final_state = read_state(project_dir) or {}
    # Race window: the cycle finished between the last poll and the
    # deadline — answer 'done' instead of a misleading timeout.
    if not _state_describes_live_pipeline(final_state):
        done = _cycle_done_event(project_dir)
        if done:
            return done
    message = (
        f"No event within {timeout}s. Current stage: {final_state.get('stage_name', '?')}. "
        f"Poll again."
    )
    if suggested >= cap:
        message += _cap_advice(project_dir)
    return WaitEventResult(
        event_type="timeout",
        message=message,
        state_snapshot=_state_to_dict(final_state),
        suggested_timeout=suggested,
    )


def _check_for_event(state: dict[str, Any] | None, project_dir: Path | None = None) -> WaitEventResult | None:
    """Check if current state has an interesting event. Return result or None."""
    # RUN10 #1: a fully cleared state file (None) now reaches this check on
    # the run-fuse path (the old code answered 'done' before it) — treat
    # absent state as 'no event'.
    state = state or {}
    stage_kind = state.get("stage_kind", "")
    last_signal = state.get("last_signal", "")
    checkpoint_pending = bool(state.get("checkpoint_pending", False))
    salvage_needed = bool(state.get("salvage_needed", False))

    # DF5-4: salvage has highest priority — worker didn't signal
    if salvage_needed:
        return WaitEventResult(
            event_type="salvage",
            message=(
                f"Salvage needed: stage '{state.get('salvage_stage', '?')}' ran but "
                f"didn't produce a signal. Read .agentic/inbox/SALVAGE-*.md "
                f"for details. Review git diff, then ACK or REVIEW."
            ),
            state_snapshot=_state_to_dict(state),
        )

    if checkpoint_pending:
        return WaitEventResult(
            event_type="checkpoint",
            message=(
                "BD-36 checkpoint form open in user's browser. "
                f"Tell user to approve at: {state.get('checkpoint_form_url', '?')}"
            ),
            state_snapshot=_state_to_dict(state),
        )

    if last_signal and last_signal.startswith("BLOCKED-"):
        return WaitEventResult(
            event_type="blocked",
            message=(
                f"Worker blocked: {last_signal}. Read BLOCKED file in outbox, "
                "decide how to proceed (fix TODO, change role, or rollback)."
            ),
            state_snapshot=_state_to_dict(state),
        )

    if stage_kind == "verify":
        snapshot = _state_to_dict(state)
        # SPEC A-run (second tier): the verify callback carries the diff-stat —
        # the supervisor sees the shape of the change before opening it.
        todo_id = str(state.get("todo_id") or "")
        if project_dir and todo_id:
            try:
                from ..verify import diff_stat_for_todo

                snapshot["diff_stat"] = diff_stat_for_todo(project_dir, todo_id)
            except Exception:
                snapshot["diff_stat"] = ""
        return WaitEventResult(
            event_type="verify",
            message=(
                "Pipeline reached VERIFY stage. Read DONE reports "
                "(outbox/DONE-*.md), check git diff, verify quality. "
                "Then awf_approve or write REVIEW-*.md."
            ),
            state_snapshot=snapshot,
        )

    return None


def _state_to_dict(state: dict[str, Any]) -> dict[str, Any]:
    """Extract supervisor-relevant fields from state for the result payload."""
    return {
        "stage_name": state.get("stage_name"),
        "stage_kind": state.get("stage_kind"),
        "stage_idx": state.get("stage_idx"),
        "todo_id": state.get("todo_id"),
        "last_signal": state.get("last_signal"),
        "checkpoint_pending": state.get("checkpoint_pending", False),
        "checkpoint_form_url": state.get("checkpoint_form_url"),
        "salvage_needed": state.get("salvage_needed", False),
        "salvage_stage": state.get("salvage_stage"),
    }


__all__ = ["wait_for_event"]
