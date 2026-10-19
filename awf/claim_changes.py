"""R2.2 (roadmap R2): ``claim_changes`` — the CODE transfer of a unit.

A unit is rejected or interrupted; its changes stay in the working tree.
A retry must not silently swallow them (the candidate would be incomplete —
roadmap R2 acceptance 3) and must not silently OWN them either. The transfer
is a separate, consent-based operation:

* :func:`plan_claim_changes` reads the current changes (tracked, untracked,
  delete, rename, executable bit — via the R2.1 git model), binds them to the
  source unit's baseline, and returns a :class:`ClaimPlan` — the PLAN. Nothing
  is written.
* :func:`apply_claim_changes` writes the plan to
  ``.agentic/context/CLAIM-<source>.json`` ONLY when the caller confirms the
  exact plan digest AND the working tree still matches the fingerprint taken
  at plan time. A tree that moved between plan and apply invalidates the plan
  (:class:`ClaimInvalidatedError`) — consent is to THESE changes.

DATA transfer vs FINDING transfer (roadmap R2.2): this is the code channel
(``kind="code"``). The R5 leak-gate (``include_untracked`` /
``carry_over_from``) transfers a REJECTED unit's files as findings and keeps
its own semantics — the shared word "carry over" does not blur the two. A
``kind="finding"`` request is refused here and pointed at that channel.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from . import git_utils, paths
from ._atomic import atomic_write_text
from ._errors import AwfApiError
from ._log import log as _log
from .todo_ids import is_valid_todo_id

#: Only code transfer lives here; findings go through the R5 channel.
CODE_KIND = "code"
FINDING_KIND = "finding"

_ALLOWED_KINDS = (CODE_KIND,)


class ClaimInvalidatedError(AwfApiError):
    """The tree moved (or the consent digest mismatched) since the plan —
    the plan is void and must be re-previewed."""


@dataclass(frozen=True)
class ClaimedFile:
    """One claimed change: what it is, where, its committed mode and digest.

    ``raw_path`` / ``raw_old_path`` are the IDENTITY: display forms
    (``surrogateescape`` round-trips the real bytes), compared and digested
    as-is — never the UI-escaped form. ``path`` / ``old_path`` are the DISPLAY
    render (F3, :func:`escape_str_for_ui`) for the CLI / JSON surfaces; a name
    with a non-UTF-8 byte is escaped there so it stays strict-UTF-8 encodable,
    while the digest keeps the exact path bytes via ``raw_path``.
    """

    raw_path: str
    op: str
    raw_old_path: str | None
    mode: str | None
    blob: str | None
    digest: str

    @property
    def path(self) -> str:
        """The UI-escaped path (F3) — safe for the strict-UTF-8 sinks."""
        from .infra.git_model import escape_str_for_ui

        return escape_str_for_ui(self.raw_path)

    @property
    def old_path(self) -> str | None:
        """The UI-escaped previous path (rename/copy source), or None."""
        from .infra.git_model import escape_str_for_ui

        return (
            escape_str_for_ui(self.raw_old_path)
            if self.raw_old_path is not None
            else None
        )

    def to_dict(self) -> dict:
        # The record stores the RAW identity (json escaped by the writer), so
        # from_dict rebuilds a byte-faithful ClaimedFile; ``path`` re-renders.
        return {
            "path": self.raw_path,
            "op": self.op,
            "old_path": self.raw_old_path,
            "mode": self.mode,
            "blob": self.blob,
            "digest": self.digest,
        }

    @staticmethod
    def from_dict(data: dict) -> ClaimedFile:
        return ClaimedFile(
            raw_path=data["path"],
            op=data["op"],
            raw_old_path=data.get("old_path"),
            mode=data.get("mode"),
            blob=data.get("blob"),
            digest=data["digest"],
        )


@dataclass(frozen=True)
class ClaimPlan:
    """The plan of a code transfer — bound to a source unit and a tree state.

    ``fingerprint`` is :func:`awf.infra.git_model.tree_fingerprint` at plan
    time; ``digest`` covers the source, the kind, the baseline sha, the
    fingerprint and every file — the exact changes the owner consents to.
    """

    source_todo: str
    kind: str
    baseline_sha: str
    fingerprint: str
    files: tuple[ClaimedFile, ...]

    @property
    def digest(self) -> str:
        h = hashlib.sha256()
        h.update(self.source_todo.encode("utf-8"))
        h.update(b"\0" + self.kind.encode("ascii"))
        h.update(b"\0" + self.baseline_sha.encode("ascii"))
        h.update(b"\0" + self.fingerprint.encode("ascii"))
        for f in sorted(self.files, key=lambda x: x.raw_path):
            h.update(
                ("\0" + json.dumps(f.to_dict(), sort_keys=True, ensure_ascii=True)).encode(
                    "ascii"
                )
            )
        return h.hexdigest()

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "source_todo": self.source_todo,
            "kind": self.kind,
            "baseline_sha": self.baseline_sha,
            "fingerprint": self.fingerprint,
            "files": [f.to_dict() for f in self.files],
            "digest": self.digest,
        }

    @staticmethod
    def from_dict(data: dict) -> ClaimPlan:
        return ClaimPlan(
            source_todo=data["source_todo"],
            kind=data["kind"],
            baseline_sha=data["baseline_sha"],
            fingerprint=data["fingerprint"],
            files=tuple(ClaimedFile.from_dict(f) for f in data.get("files", [])),
        )


def _validate_source_todo(source_todo: str) -> str:
    """The source id is part of a path — validate it as a public name.

    ``CLAIM-<source>.json`` / ``BASELINE-<source>.sha`` interpolate the id
    into a filename, so an unvalidated input (``../../x``) would leave
    ``.agentic/context/`` — the same contract
    ``docs/contracts/public-name-validation.md`` holds for every public name,
    and the same guard ``resolve_carry_over`` applies to its origin.
    """
    value = str(source_todo or "").strip()
    if not is_valid_todo_id(value):
        raise AwfApiError(
            f"claim_changes: invalid source TODO '{source_todo}' — expected a "
            "unit id (format TODO-NNNN)"
        )
    return value


def claim_record_path(project_dir: Path, source_todo: str) -> Path:
    """``.agentic/context/CLAIM-<source>.json`` for an applied claim."""
    return paths.context_dir(Path(project_dir)) / f"CLAIM-{source_todo}.json"


def _source_baseline_sha(project_dir: Path, source_todo: str) -> str:
    f = paths.context_dir(project_dir) / f"BASELINE-{source_todo}.sha"
    if not f.is_file():
        raise AwfApiError(
            f"claim_changes: no baseline for {source_todo} "
            f"(BASELINE-{source_todo}.sha not found) — a claim must be bound "
            "to the source unit's baseline"
        )
    try:
        return f.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        raise AwfApiError(f"claim_changes: cannot read the source baseline: {e}") from e


def _current_files(
    project_dir: Path, wanted: list[str] | None
) -> list[ClaimedFile]:
    """The current source-tree changes as :class:`ClaimedFile`s.

    Reads through the R2.1 model (byte-faithful). Managed AWF runtime paths
    are not claimable code. ``wanted`` (display paths) narrows the selection;
    a wanted path that is not among the current changes is refused — the
    claim must name an operation that exists NOW.
    """
    from .dispatch_policy import is_managed_runtime_path
    from .infra.git_model import GitModelError, GitRepository, display_path

    repo = GitRepository(project_dir)
    try:
        records = repo.changes()
    except GitModelError as e:
        raise AwfApiError(f"claim_changes: cannot read the working tree: {e}") from e

    files: list[ClaimedFile] = []
    by_path: dict[str, ClaimedFile] = {}
    for r in records:
        if is_managed_runtime_path(r.path):
            continue
        # The effective mode is the one that MOVED from the base (a worktree
        # chmod the unit would commit is 100755 even though the index still
        # shows 100644); otherwise the index mode is the committed one.
        mode = None
        for candidate in (r.index_mode, r.work_mode):
            if candidate and candidate != r.base_mode:
                mode = candidate
                break
        if mode is None:
            mode = r.index_mode or r.work_mode
        blob = r.index_blob or r.work_blob
        cf = ClaimedFile(
            raw_path=display_path(r.path),
            op=r.kind,
            raw_old_path=display_path(r.old_path) if r.old_path is not None else None,
            mode=mode,
            blob=blob,
            digest=r.digest(),
        )
        files.append(cf)
        by_path[cf.raw_path] = cf
    files.sort(key=lambda f: f.raw_path)

    if wanted is None:
        return files
    selected: list[ClaimedFile] = []
    for p in wanted:
        match = by_path.get(p)
        if match is None:
            raise AwfApiError(
                f"claim_changes: path '{p}' is not among the current changes — "
                "the claim must name a change present in the working tree"
            )
        selected.append(match)
    return sorted(selected, key=lambda f: f.raw_path)


def plan_claim_changes(
    project_dir: Path,
    source_todo: str,
    *,
    paths: list[str] | None = None,
    kind: str = CODE_KIND,
) -> ClaimPlan:
    """Build the PLAN of a code transfer from ``source_todo``.

    Writes nothing. A commit is never created and the user's index is never
    touched. ``paths`` (display form) narrows to specific operations.

    Raises:
        AwfApiError: an unknown ``kind`` (findings belong to the R5 channel),
            a missing source baseline, or a ``paths`` entry that is not a
            current change.
    """
    if kind not in _ALLOWED_KINDS:
        if kind == FINDING_KIND:
            raise AwfApiError(
                "claim_changes is the CODE channel; a finding transfer belongs "
                "to the R5 leak-gate (include_untracked / carry_over_from)"
            )
        raise AwfApiError(
            f"claim_changes: unknown kind '{kind}' (allowed: {', '.join(_ALLOWED_KINDS)})"
        )
    project_dir = Path(project_dir)
    source_todo = _validate_source_todo(source_todo)
    if not git_utils.is_git_repo(project_dir):
        raise AwfApiError("claim_changes: not a git repository")
    baseline_sha = _source_baseline_sha(project_dir, source_todo)
    files = _current_files(project_dir, paths)

    from .infra.git_model import GitModelError, tree_fingerprint

    try:
        fingerprint = tree_fingerprint(project_dir)
    except (GitModelError, OSError) as e:
        raise AwfApiError(
            f"claim_changes: cannot read the working tree: {e}"
        ) from e
    return ClaimPlan(
        source_todo=source_todo,
        kind=kind,
        baseline_sha=baseline_sha,
        fingerprint=fingerprint,
        files=tuple(files),
    )


def apply_claim_changes(
    project_dir: Path, plan: ClaimPlan, *, confirmed_digest: str
) -> Path:
    """Apply a previewed plan — consent to THESE exact changes.

    Refuses (no side effect) when:

    * ``confirmed_digest`` does not equal the plan's digest — consent was for
      a different plan;
    * the working tree fingerprint no longer matches the plan's — the tree
      moved after preview, so the plan is void.

    On success writes ``CLAIM-<source>.json`` and returns its path.
    """
    from .infra.git_model import GitModelError, tree_fingerprint

    project_dir = Path(project_dir)
    if confirmed_digest != plan.digest:
        raise ClaimInvalidatedError(
            "claim_changes: consent digest does not match the plan — the owner "
            "must confirm the exact plan digest (re-preview)"
        )
    _validate_source_todo(plan.source_todo)
    try:
        current = tree_fingerprint(project_dir)
    except (GitModelError, OSError) as e:
        raise AwfApiError(
            f"claim_changes: cannot read the working tree: {e}"
        ) from e
    if current != plan.fingerprint:
        raise ClaimInvalidatedError(
            "claim_changes: the working tree changed after the preview — the "
            "plan is invalidated; re-preview before applying"
        )
    path = claim_record_path(project_dir, plan.source_todo)
    atomic_write_text(
        path, json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n"
    )
    try:
        _log(
            paths.logs_dir(project_dir),
            f"claim_changes: {plan.source_todo} — {len(plan.files)} code "
            f"change(s) claimed (digest {plan.digest[:12]})",
        )
    except OSError:
        pass
    return path


def read_claim(project_dir: Path, source_todo: str) -> ClaimPlan | None:
    """Read an applied claim record; None when absent, unreadable, or the id
    is not a valid unit name (an invalid id cannot name a record — the
    lenient direction keeps a tampered link from breaking a re-baseline)."""
    if not is_valid_todo_id(str(source_todo or "").strip()):
        return None
    f = claim_record_path(Path(project_dir), source_todo)
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return ClaimPlan.from_dict(data)
    except (KeyError, TypeError):
        return None


def claim_link_path(project_dir: Path, todo_id: str) -> Path:
    """``.agentic/context/BASELINE-<todo>.claim`` — the CODE-channel audit
    link, the claim parallel to the R5 ``.carry_over`` / ``.include`` links.

    A dispatch with ``claim_from`` excludes the claimed paths from
    ``BASELINE-<todo>.untracked``; a LATER re-baseline (``awf run_next``)
    re-applies the exclusion from this link, or a claimed predecessor blob
    is re-listed as pre-existing and silently dropped from the unit commit
    (TODO-0221 M2).
    """
    return paths.context_dir(Path(project_dir)) / f"BASELINE-{todo_id}.claim"


def write_claim_link(project_dir: Path, todo_id: str, source_todo: str) -> None:
    """Record the claim origin for a re-baseline. Best-effort, the same
    contract as the carry-over/include links: a failed write logs but must
    not undo the dispatch (the baseline snapshot already excluded the paths)."""
    try:
        atomic_write_text(claim_link_path(project_dir, todo_id), f"{source_todo}\n")
        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — claim link written (origin {source_todo})",
        )
    except OSError as e:
        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — claim audit link not written: {e}",
        )


def read_claim_link(project_dir: Path, todo_id: str) -> str | None:
    """The claim origin recorded for a unit, or None (no claim / unreadable).

    ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``: a link
    whose bytes are not valid UTF-8 must degrade to None like an unreadable
    one, or the exception escapes ``hygiene.transfer_exclusions`` and kills
    the ``awf run_next`` re-baseline its callers document as best-effort."""
    f = claim_link_path(Path(project_dir), todo_id)
    if not f.is_file():
        return None
    try:
        origin = f.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return origin or None


def clear_claim_link(project_dir: Path, todo_id: str) -> None:
    """Drop a stale claim link: a (re-)dispatch without ``claim_from`` rewrites
    the baseline without the exclusion, so a later re-baseline must not
    re-apply a claim that no longer holds (same lifecycle as the carry-over
    and include links)."""
    claim_link_path(Path(project_dir), todo_id).unlink(missing_ok=True)


def claim_exclusion_paths(project_dir: Path, source_todo: str) -> list[str]:
    """The claimed paths of an applied claim (the re-baseline exclusion);
    ``[]`` when the origin's ``CLAIM-<source>.json`` record is gone.

    Raw identity paths (the same form the baseline exclusion compares against),
    never the UI-escaped render.
    """
    plan = read_claim(project_dir, source_todo)
    return sorted(f.raw_path for f in plan.files) if plan is not None else []


def claim_drift(project_dir: Path, plan: ClaimPlan) -> list[str]:
    """The plan's paths whose CURRENT change no longer matches the plan.

    Consumption-time check (``dispatch_todo(claim_from=...)``): the owner
    consented to the exact blobs the plan names, so an edit AFTER the consent
    invalidates it. Two facts are read:

    * the plan's ``fingerprint`` binds the exact working tree — any edit to a
      claimed file moves it. This is the CONTENT-bearing check: the R2.1
      per-change digest covers kind/old_path/path/mode but NOT the worktree
      content (git's porcelain protocol does not carry the work blob);
    * a claimed path that vanished, or changed kind / mode / index blob since
      the plan.

    Returns the (sorted) claimed paths that drifted; ``[]`` when the plan still
    holds.

    Raises:
        AwfApiError: the current tree cannot be read (the R2.1 model refused it)
            — a tree that cannot be read cannot be proven to still match.
    """
    from .infra.git_model import GitModelError, tree_fingerprint

    project_dir = Path(project_dir)
    planned = sorted(f.raw_path for f in plan.files)
    try:
        current_fp = tree_fingerprint(project_dir)
    except (GitModelError, OSError) as e:
        raise AwfApiError(
            f"claim_changes: cannot read the working tree: {e}"
        ) from e
    if current_fp == plan.fingerprint:
        return []
    try:
        current = {f.raw_path: f for f in _current_files(project_dir, None)}
    except AwfApiError:
        return planned
    drifted = [
        f.raw_path
        for f in plan.files
        if current.get(f.raw_path) is None
        or _change_identity(current[f.raw_path]) != _change_identity(f)
    ]
    # The tree moved, but the claimed records look identical (e.g. a worktree
    # content edit the protocol does not expose): the plan is stale anyway —
    # name every claimed path rather than claim a false "still holds".
    return sorted(drifted) or planned


def resolve_claim_changes(project_dir: Path, claim_from: str) -> list[str]:
    """Validate the claim a dispatch adopts; return its claimed display paths.

    The CODE channel's dispatch-side entry point — the parallel of
    :func:`awf.reject_files.resolve_carry_over` and
    :func:`awf.include_untracked.resolve_include_untracked`: the module that
    owns a channel also owns its validation, so the dispatch resolves all three
    transfers the same way instead of inlining one channel's contract. A
    missing applied claim record, or a claimed change whose plan drifted since
    the owner's consent, refuses before any side effect.

    Raises:
        AwfApiError: no applied claim for ``claim_from``, or the confirmed plan
            is invalidated by a later change to the tree.
    """
    project_dir = Path(project_dir)
    claim_from = _validate_source_todo(claim_from)
    plan = read_claim(project_dir, claim_from)
    if plan is None:
        raise AwfApiError(
            f"claim_from '{claim_from}': no applied claim record — run "
            "claim_changes (preview + apply) first"
        )
    drift = claim_drift(project_dir, plan)
    if drift:
        from .infra.git_model import escape_str_for_ui

        named = ", ".join(escape_str_for_ui(p) for p in drift)
        raise AwfApiError(
            f"claim_from '{claim_from}': claimed change(s) no longer match "
            f"the confirmed plan ({named}) — the preview is "
            "invalidated by a later change to the tree; re-preview the claim"
        )
    return sorted(f.raw_path for f in plan.files)


def _change_identity(f: ClaimedFile) -> tuple:
    """The content-bearing identity of a claimed change (no digest): consumed
    only to name the drifted paths in :func:`claim_drift`. On the RAW fields —
    the UI-escaped render must never decide ownership."""
    return (f.op, f.raw_old_path, f.mode, f.blob)



__all__ = [
    "CODE_KIND",
    "FINDING_KIND",
    "ClaimInvalidatedError",
    "ClaimPlan",
    "ClaimedFile",
    "apply_claim_changes",
    "claim_drift",
    "claim_exclusion_paths",
    "claim_link_path",
    "claim_record_path",
    "clear_claim_link",
    "plan_claim_changes",
    "read_claim",
    "read_claim_link",
    "resolve_claim_changes",
    "write_claim_link",
]
