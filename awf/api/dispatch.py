"""TODO dispatch: atomic create + baseline + ready signal.

Replaces the 3-step manual workflow:
  1. Write inbox/TODO-NNNN.md
  2. awf_baseline TODO-NNNN (snapshot)
  3. touch inbox/TODO-NNNN.ready (signal)

With one call. Auto-picks next NNNN, creates baseline snapshot, dispatches
the signal. Supervisor gets back the todo_id + baseline_sha to track.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .. import paths
from .._atomic import atomic_write_text
from ._errors import AwfApiError
from ._helpers import require_agentic
from ._results import DispatchTodoResult
from .pipeline import create_baseline


def _next_todo_id(project_dir: Path) -> str:
    """Pick next TODO-NNNN id (max existing + 1).

    Scans inbox, outbox, AND done/ for any TODO-NNNN.* files to avoid
    collisions with already-completed/archived TODOs.
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    done = paths.done_dir(project_dir)

    max_num = 0
    for d in (inbox, outbox, done):
        if not d.is_dir():
            continue
        for f in d.glob("TODO-*.md"):
            m = re.match(r"^TODO-(\d+)$", f.stem)
            if m:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num
        # Also check done/ subdirectories (done/TODO-NNNN/)
        if d == done:
            for sub in d.iterdir():
                if sub.is_dir():
                    m = re.match(r"^TODO-(\d+)$", sub.name)
                    if m:
                        num = int(m.group(1))
                        if num > max_num:
                            max_num = num

    return f"TODO-{max_num + 1:04d}"


def dispatch_todo(
    project_dir: Path,
    content: str,
    *,
    role: str | None = None,
    todo_id: str | None = None,
    pipeline: str | None = None,
    carry_over_from: str | None = None,
) -> DispatchTodoResult:
    """Atomically create a TODO, baseline it, dispatch the signal.

    Three-step manual workflow compressed into one call:
      1. Write ``.agentic/inbox/TODO-NNNN.md`` (atomic).
      2. Create ``.agentic/context/BASELINE-NNNN.{sha,status,tests.log,env.log}``.
      3. Write ``.agentic/inbox/TODO-NNNN.ready`` signal (trigger for plan stage).

    Args:
        project_dir: awf project root (must contain .agentic/).
        content: TODO markdown body (the task description).
        role: optional role hint for logging/debugging (does not affect
            pipeline routing — that's determined by stage order in
            pipeline.yaml). Useful for supervisor context: "this TODO
            is for agent-system-analyst".
        todo_id: override auto-generated id (e.g. "TODO-0007"). If None,
            auto-picks next available NNNN by scanning inbox + outbox.
        pipeline: RUN3 #2 (Part B) — optional pipeline name, written into
            the TODO's contract block as ``pipeline: <name>`` (the block is
            created when absent). When an ``awf_run_next`` launch has no
            queue-level pipeline, it uses this one.
        carry_over_from: RUN5 #1 (leak-gate) — the id of a REJECTED TODO
            whose untracked files this retry re-claims. The paths recorded
            in ``.agentic/context/REJECT-<origin>.files`` are excluded from
            the new baseline's untracked snapshot, so the retry's commit
            includes them. Refused (before any side effect) when the origin
            TODO or its REJECT file does not exist.

    Re-dispatch of a number with stale BLOCKED/ACK closures (RUN3 #4)
    clears them automatically; a DONE closure refuses the dispatch
    (use ``awf restore`` for an archived TODO).

    Returns:
        DispatchTodoResult with todo_id, baseline_sha, files written.

    Raises:
        AwfApiError: if .agentic/ missing, content empty, a DONE
            closure for ``todo_id`` is still in the outbox, or a
            ``carry_over_from`` origin cannot be validated.
    """
    if not content or not content.strip():
        raise AwfApiError("content is required (non-empty TODO body)")

    # RUN3 #2 (Part B): an explicit pipeline= wins over a copy-pasted block
    # — inject first, so the parse below validates the FINAL content.
    if pipeline is not None and str(pipeline).strip():
        from ..unit_contract import inject_pipeline_key

        try:
            content = inject_pipeline_key(content, str(pipeline))
        except ValueError as e:
            raise AwfApiError(f"pipeline: {e}") from None

    # U3: optional unit-contract block at the top of the TODO (--- yaml ---)
    # is validated here, before anything is written. No block = no-op.
    from ..unit_contract import parse_todo_contract
    try:
        _contract, unknown_keys = parse_todo_contract(content)
    except ValueError as e:
        raise AwfApiError(f"TODO contract block: {e}") from None

    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    # RUN5 #1 (leak-gate): validate the carry-over BEFORE reserving the id —
    # a refused carry-over must leave no side effects (no TODO, no baseline).
    carry_over: set[str] = set()
    carry_over_files: list[str] = []
    if carry_over_from:
        from ..reject_files import resolve_carry_over

        carry_over_files = resolve_carry_over(project_dir, carry_over_from)
        carry_over = set(carry_over_files)

    # Whether the CALLER fixed the id (explicit collisions must raise,
    # auto-picked ones may retry with the next free id).
    explicit_id = todo_id is not None
    if todo_id is None:
        todo_id = _next_todo_id(project_dir)
    elif not re.match(r"^TODO-\d{4,}$", todo_id):
        raise AwfApiError(
            f"invalid todo_id '{todo_id}' — expected format 'TODO-NNNN' (4+ digits)"
        )

    inbox = paths.inbox(project_dir)
    inbox.mkdir(parents=True, exist_ok=True)

    # Optional role hint as HTML comment (invisible to LLM reading the .md,
    # visible to grep / debugging).
    body = content
    if role:
        body = f"<!-- role_hint: {role} -->\n" + body

    # AUD14-01: reserve the ID atomically before writing content. The old
    # exists() → write() window let two parallel dispatches pick the same
    # TODO-NNNN and the second silently overwrote the first's content.
    # O_CREAT|O_EXCL makes the claim one syscall: the first writer wins,
    # concurrent callers get FileExistsError and retry with the next id.
    md_path: Path | None = None
    for _attempt in range(16):
        candidate = inbox / f"{todo_id}.md"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if explicit_id:
                raise AwfApiError(
                    f"{candidate.name} already exists in inbox. Use a different "
                    "todo_id or remove the existing file first."
                ) from None
            todo_id = _next_todo_id(project_dir)
            # desynchronize the herd of parallel callers
            import random as _random
            import time as _time
            _time.sleep(_random.uniform(0, 0.02))
            continue
        os.close(fd)
        md_path = candidate
        break
    if md_path is None:
        raise AwfApiError(
            f"could not reserve a free TODO id (16 consecutive collisions, "
            f"last tried {todo_id}) — check .agentic/inbox for stray TODO files"
        )

    # RUN3 #4: never (re)issue a number whose DONE closure is still in the
    # outbox — the fresh TODO would be invisible (todos.is_closed). The
    # archived TODO comes back via awf restore, not a silent re-dispatch.
    from .hygiene import clear_stale_closures, has_done_closure

    if has_done_closure(project_dir, todo_id):
        md_path.unlink(missing_ok=True)
        raise AwfApiError(
            f"outbox has a DONE closure for {todo_id} — the TODO is closed as "
            "finished. Bring it back with awf restore or dispatch a new number."
        )

    # Pre-dispatch check: grep code for key identifiers from TODO content.
    # Warns if patterns already exist in codebase (task may be already done).
    pre_check_warnings: list[str] = []
    try:
        import re as _re
        # Extract identifiers: backtick-quoted, camelCase, snake_case
        identifiers = set()
        for m in _re.finditer(r'`([a-zA-Z_][a-zA-Z0-9_]{2,})`', content):
            identifiers.add(m.group(1))
        for m in _re.finditer(r'\b([a-z][a-zA-Z0-9]*_[a-z][a-zA-Z0-9_]*)\b', content):
            identifiers.add(m.group(1))
        identifiers.discard("todo")  # too generic

        if identifiers:
            import subprocess as _sp
            for ident in list(identifiers)[:5]:  # check max 5
                try:
                    result = _sp.run(
                        ["grep", "-rl", "--include=*.ts", "--include=*.js",
                         "--include=*.py", "--include=*.go", "--include=*.rs",
                         "--include=*.java", "--include=*.rb",
                         "-d", "skip",
                         # AUD15-05: don't scan VCS/dependency dirs — on a
                         # 25k-file tree the old unfiltered grep took up to
                         # 5s per identifier (5 identifiers → ~25s dispatch).
                         "--exclude-dir=.git", "--exclude-dir=node_modules",
                         "--exclude-dir=.venv", "--exclude-dir=venv",
                         "--exclude-dir=vendor",
                         ident, str(project_dir)],
                        capture_output=True, text=True, timeout=2, check=False,
                    )
                    # Filter out .agentic/ matches
                    matches = [line for line in result.stdout.strip().splitlines()
                               if ".agentic/" not in line and "node_modules/" not in line]
                    if matches:
                        pre_check_warnings.append(
                            f"'{ident}' already found in {len(matches)} file(s). "
                            f"Task may be already implemented — verify before running pipeline."
                        )
                except (OSError, _sp.SubprocessError):
                    pass
    except Exception:
        pass  # pre-check is best-effort, never blocks dispatch

    # U3: unknown keys in the contract block are a typo risk — warn, don't fail.
    from ..unit_contract import _CONTRACT_KEYS

    for key in unknown_keys:
        pre_check_warnings.append(
            f"contract block: unknown key '{key}' — known keys: "
            + ", ".join(_CONTRACT_KEYS)
        )

    # Step 1: fill the reserved TODO-NNNN.md (atomic temp+rename over the
    # placeholder — the id is already claimed, no one else can take it).
    atomic_write_text(md_path, body)

    # Step 2: create baseline snapshot (sha + tests.log + env.log + status)
    try:
        baseline = create_baseline(project_dir, todo_id, carry_over=carry_over)
    except Exception:
        # Rollback: remove TODO .md if baseline fails (prevents orphan TODO)
        md_path.unlink(missing_ok=True)
        raise

    # RUN5 #1 (leak-gate): audit trail — which rejected origin this retry's
    # baseline carried over. Best-effort: a failed write must not undo the
    # dispatch (the baseline already excluded the paths).
    if carry_over:
        from .._log import log as _log

        try:
            atomic_write_text(
                paths.context_dir(project_dir) / f"BASELINE-{todo_id}.carry_over",
                f"{carry_over_from}\n",
            )
            _log(
                paths.logs_dir(project_dir),
                f"dispatch: {todo_id} — carried over {len(carry_over_files)} "
                f"untracked file(s) from rejected {carry_over_from} (leak-gate)",
            )
        except OSError as e:
            _log(
                paths.logs_dir(project_dir),
                f"dispatch: {todo_id} — carry-over audit link not written: {e}",
            )
    else:
        # RUN5 #1 (leak-gate): a non-carry-over (re-)dispatch rewrites the
        # baseline WITHOUT the exclusion — drop any stale link from an earlier
        # carry-over dispatch of the same id, so a later re-baseline (awf
        # run_next) does not re-apply an exclusion that no longer holds.
        (
            paths.context_dir(project_dir) / f"BASELINE-{todo_id}.carry_over"
        ).unlink(missing_ok=True)

    # RUN3 #4: re-dispatch of the same number must not stay hidden behind
    # stale BLOCKED/ACK closures — clear them via the shared helper.
    # (Only reachable when inbox had no .md for this id, i.e. a re-issue;
    # a running pipeline keeps its own .md in the inbox, so its signals
    # are never moved out from under the engine.)
    cleared, _trace = clear_stale_closures(project_dir, todo_id)
    if cleared:
        from .._log import log as _log

        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — stale closure cleared on re-dispatch: "
            f"{', '.join(cleared)}",
        )

    # Step 3: dispatch signal
    ready_path = inbox / f"{todo_id}.ready"
    ready_path.touch()

    return DispatchTodoResult(
        todo_id=todo_id,
        baseline_sha=baseline.sha,
        role_hint=role,
        files_written=[
            f".agentic/inbox/{todo_id}.md",
            f".agentic/inbox/{todo_id}.ready",
            *baseline.files_created,
        ],
        pre_check_warnings=pre_check_warnings,
        carry_over_from=carry_over_from,
        carry_over_files=carry_over_files,
    )


__all__ = ["dispatch_todo", "_next_todo_id"]
