"""R2.5 (roadmap R2): publication and archive as a resumable operation.

The unit commit used to be one opaque ``git commit`` through a throwaway
index: if the process died anywhere in the middle, the next attempt could
not tell whether the ref had already moved, whether the user's index was
synced, or whether the archive happened. R2.5 splits the publish into named,
individually-durable steps and makes the whole sequence restartable:

  1. **baseline** — the plan the gate already assembled (files, candidate
     tree, degraded paths).
  2. **commit object** — the tree is written from the isolated index
     (``write-tree``) and the commit object is created (``commit-tree``).
     The object exists in the odb, but **the ref has not moved yet**.
     ``commit-tree`` bypasses porcelain, so the hooks are run explicitly
     through ``git hook run`` (git >= 2.36): ``pre-commit``, ``commit-msg``
     (a rejection aborts before the object; a rewrite IS the committed
     message) and ``post-commit`` (after the ref and the index are in step;
     its exit code is ignored, as git does).
  3. **intent** — right here, before the ref update, the operation intent
     is written to ``context/PUBLISH-INTENT-<todo>.json`` carrying the new
     commit OID. A crash after this point is recognizable by a **specific
     OID plus the intent file**, never by substring-matching a commit
     message.
  4. **update-ref** — the ref moves with a compare-and-swap on the expected
     HEAD. A lost race refuses instead of committing on top of a moved HEAD.
  5. **index sync** — the user's real index is re-pointed at the new HEAD
     **only for the plan's own files whose index entry still equals the
     content captured before the operation**. A foreign entry, or a file
     the user changed while the operation was in flight, is left untouched
     and the receipt is marked ``committed_with_reconciliation_required``.
  6. **receipt** — ``context/RECEIPT-<todo>.json`` records the commit OID,
     candidate tree, evidence refs, transfers, residuals and the
     disposition. Only then is the intent removed.

Resuming (``resume_if_pending``) picks the sequence up at the exact step the
crash interrupted: ref already at the intent's OID → jump to index sync and
receipt; ref still at the expected HEAD → redo the CAS; ref somewhere else →
refuse (no second commit).

Archive (``archive_after_receipt``) is a separate step, allowed only when a
receipt says ``committed`` / ``committed_with_reconciliation_required`` /
``explicitly_verified_no_code``. A no-code / audit unit records its own
valid receipt (evidence required — an unexplained empty plan is refused),
never an artificial empty commit. The archive move itself is idempotent and
never deletes an existing ``done/<todo>``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import commit_delta, commit_plan, paths
from ._log import log as _log
from .infra import managed_fs
from .infra.git_model import GitModelError, GitRepository, escape_str_for_ui
from .signals import short_id

#: The commit path's ONE git runner (commit_delta owns it, commit_gate borrows
#: it under the same name); ``env`` carries the isolated index when the call
#: must not touch the real one, ``None`` when it must read the real index.
_git = commit_delta._git_isolated

# ─── crash-injection stages (durable points of the protocol) ─────────────

CRASH_BASELINE = "baseline"
CRASH_COMMIT_OBJECT = "commit_object"
CRASH_UPDATE_REF = "update_ref"
CRASH_INDEX_SYNC = "index_sync"
CRASH_ARCHIVE = "archive"

#: The all-zero object id ``git update-ref`` reads as "the ref must still not
#: exist" — the CAS old-value for a publication planned against an unborn HEAD.
_ZERO_OID = "0" * 40


class PublishCrash(RuntimeError):
    """The simulated crash raised by ``_maybe_crash`` (test-only)."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"simulated crash after publish step {stage!r}")


def _maybe_crash(stage: str, crash_at: str = "") -> None:
    """Raise :class:`PublishCrash` when ``stage`` is the requested crash
    point. The caller (tests / the commit gate's ``crash_at`` argument)
    names the stage explicitly; production leaves it empty."""
    if crash_at == stage:
        raise PublishCrash(stage)


class PublishError(Exception):
    """A refusal from the publication/archive protocol (fail-closed)."""


# ─── dispositions ────────────────────────────────────────────────────────

DISPOSITION_COMMITTED = "committed"
DISPOSITION_RECONCILIATION = "committed_with_reconciliation_required"
DISPOSITION_NO_CODE = "explicitly_verified_no_code"

#: The dispositions that authorize archiving. ``committed`` and the
#: reconciliation variant mean the work is in HEAD; ``no_code`` means the
#: unit legitimately produced no code and carries its own evidence. A refused
#: or errored publish leaves NO receipt — an empty plan of unknown reason or
#: the mere presence of a DONE file is not an archive authorization.
ARCHIVE_DISPOSITIONS = (
    DISPOSITION_COMMITTED,
    DISPOSITION_RECONCILIATION,
    DISPOSITION_NO_CODE,
)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _relative(project_dir: Path, path: Path) -> str:
    """A path as seen from the project root (the shape every fact uses)."""
    try:
        return str(path.relative_to(project_dir))
    except ValueError:
        return str(path)


# ─── on-disk loci ────────────────────────────────────────────────────────


def intent_file(project_dir: Path, todo_id: str) -> Path:
    return paths.context_dir(project_dir) / f"PUBLISH-INTENT-{todo_id}.json"


def receipt_file(project_dir: Path, todo_id: str) -> Path:
    return paths.context_dir(project_dir) / f"RECEIPT-{todo_id}.json"


def archive_fact_file(project_dir: Path, todo_id: str) -> Path:
    return paths.context_dir(project_dir) / f"ARCHIVE-{todo_id}.json"


def superseded_file(project_dir: Path, todo_id: str) -> Path:
    """The durable fact that a STALE pending intent was abandoned.

    Written when a resume refuses because HEAD moved elsewhere: the intent
    is dropped (so the TODO is not wedged) and this fact records why, so a
    later ``Replan against the new HEAD`` has the trace of the abandoned
    operation (R2.5 REVIEW, iter 2).
    """
    return paths.context_dir(project_dir) / f"PUBLISH-SUPERSEDED-{todo_id}.json"


# ─── dataclasses ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublishIntent:
    """The durable "I am about to move the ref to this OID" record.

    Written BEFORE ``update-ref``. Its ``commit_oid`` is what a resume keys
    on: a crash between the ref update and the receipt is recognized by the
    ref being at exactly this OID, not by looking for the TODO id in a
    commit message substring.
    """

    todo_id: str
    generation: int
    expected_head: str
    tree_oid: str
    commit_oid: str
    candidate_tree: str
    files: tuple[str, ...]
    delta_files: tuple[str, ...]
    degraded: tuple[str, ...]
    message: str
    index_orig: dict[str, str]
    evidence_refs: tuple[str, ...]
    transfers: tuple[str, ...]
    residuals: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        for key in ("files", "delta_files", "degraded", "evidence_refs",
                    "transfers", "residuals"):
            data[key] = list(data[key])
        return data

    @classmethod
    def from_dict(cls, d: dict) -> PublishIntent:
        return cls(
            todo_id=d["todo_id"],
            generation=int(d.get("generation", 0)),
            expected_head=d.get("expected_head", ""),
            tree_oid=d.get("tree_oid", ""),
            commit_oid=d.get("commit_oid", ""),
            candidate_tree=d.get("candidate_tree", ""),
            files=tuple(d.get("files", ())),
            delta_files=tuple(d.get("delta_files", ())),
            degraded=tuple(d.get("degraded", ())),
            message=d.get("message", ""),
            index_orig=dict(d.get("index_orig", {})),
            evidence_refs=tuple(d.get("evidence_refs", ())),
            transfers=tuple(d.get("transfers", ())),
            residuals=tuple(d.get("residuals", ())),
            created_at=d.get("created_at", ""),
        )


@dataclass(frozen=True)
class UnitReceipt:
    """The fact that the unit's publication reached a terminal disposition.

    ``disposition`` is one of :data:`ARCHIVE_DISPOSITIONS` (or, on a refusal
    recorded for the record, another string). ``commit_oid`` / ``files`` are
    empty on a no-code receipt. ``index_sync`` is ``synced`` /
    ``reconciliation_required`` / ``skipped``.
    """

    todo_id: str
    generation: int
    disposition: str
    commit_oid: str
    candidate_tree: str
    expected_head: str
    evidence_refs: tuple[str, ...]
    transfers: tuple[str, ...]
    residuals: tuple[str, ...]
    degraded: tuple[str, ...]
    index_sync: str
    files: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        for key in ("evidence_refs", "transfers", "residuals", "degraded", "files"):
            data[key] = list(data[key])
        return data

    @classmethod
    def from_dict(cls, d: dict) -> UnitReceipt:
        return cls(
            todo_id=d["todo_id"],
            generation=int(d.get("generation", 0)),
            disposition=d.get("disposition", ""),
            commit_oid=d.get("commit_oid", ""),
            candidate_tree=d.get("candidate_tree", ""),
            expected_head=d.get("expected_head", ""),
            evidence_refs=tuple(d.get("evidence_refs", ())),
            transfers=tuple(d.get("transfers", ())),
            residuals=tuple(d.get("residuals", ())),
            degraded=tuple(d.get("degraded", ())),
            index_sync=d.get("index_sync", ""),
            files=tuple(d.get("files", ())),
            created_at=d.get("created_at", ""),
        )


# ─── git runner / index read ─────────────────────────────────────────────


#: A full object id: 40 hex (sha1) or 64 hex (sha256). ``update-ref`` accepts
#: only a real object; anything shorter/foreign is an unreadable fact, not a
#: pending operation (R2.5 REVIEW, iter 9, A2).
_OID_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def _is_oid_form(oid: str) -> bool:
    return bool(_OID_RE.fullmatch(oid or ""))


def _zero_oid(project_dir: Path) -> str:
    """The CAS old-value "the ref must still not exist", in the repo's format.

    ``update-ref`` validates the old value against the repository's object
    format: a hard-coded 40-zero value is rejected as ``not a valid old
    SHA1`` in a sha256 repository, and the refusal used to look like a
    retryable CAS race — the first commit wedged forever (R2.5 REVIEW, iter
    10, A-1). Ask git for the format and match the width; unreadable format
    keeps the sha1 default (the same fail-closed retryable behaviour).
    """
    try:
        r = _git(project_dir, ["rev-parse", "--show-object-format"])
    except (subprocess.SubprocessError, OSError):
        return _ZERO_OID
    fmt = r.stdout.strip() if r.returncode == 0 else ""
    return "0" * (64 if fmt == "sha256" else 40)


def _read_head(project_dir: Path) -> tuple[bool, str]:
    """Read HEAD, distinguishing "unborn/empty" from "could not read".

    ``commit_delta.head_sha`` degrades every failure to ``""``; the resume
    path used that as "HEAD is not the expected commit" and superseded a
    LIVE intent on a transient git error (R2.5 REVIEW, iter 9, A4). The
    typed read keeps the two apart: ``(True, "")`` is a legitimately unborn
    HEAD, ``(False, "")`` is a read failure the caller must not act on.
    """
    try:
        r = _git(project_dir, ["rev-parse", "--verify", "-q", "HEAD"])
    except (subprocess.SubprocessError, OSError):
        return False, ""
    if r.returncode == 0:
        return True, r.stdout.strip()
    # ``rev-parse --verify -q`` returns 1 for a missing ref (unborn HEAD);
    # anything else is an I/O/object-store failure.
    return (True, "") if r.returncode == 1 else (False, "")


def _run_hook(
    project_dir: Path, name: str, env: dict[str, str], args: list[str] | None = None
) -> subprocess.CompletedProcess:
    """Run one git hook through the ONE runner (R2.5 REVIEW, iter 9, A1)."""
    argv = ["hook", "run", "--ignore-missing", name]
    if args:
        argv += ["--", *args]
    return _git(project_dir, argv, env=env)


def _proc_detail(proc: subprocess.CompletedProcess) -> str:
    """One git/hook failure detail, safe for every strict UTF-8 sink.

    The shared runner decodes with ``surrogateescape`` (F3): a hook echoing
    a non-UTF-8 file name yields lone surrogates in ``stderr``. They survive
    the decode, but the detail is interpolated into an outcome reason and
    then reaches a STRICT sink — the log writer and JSON — which raises
    ``UnicodeEncodeError`` (a ``ValueError``) past every typed boundary.
    That is the same byte class F3 closed on the decode side, reopened on the
    render side (R2.5 architector-review). ``escape_str_for_ui`` renders the
    bytes as visible ``\\xNN`` escapes, so every message THIS module builds
    is always encodable.
    """
    raw = proc.stderr.strip() or proc.stdout.strip()
    return escape_str_for_ui(raw) if raw else ""


def _short(project_dir: Path, oid: str) -> str:
    """The short form of a specific object id (HEAD-independent)."""
    if not oid:
        return ""
    r = _git(project_dir, ["rev-parse", "--short", oid])
    return r.stdout.strip() if r.returncode == 0 else oid[:12]


def _is_ancestor(project_dir: Path, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant`` (git-native)."""
    if not ancestor or not descendant:
        return False
    r = _git(project_dir, ["merge-base", "--is-ancestor", ancestor, descendant])
    return r.returncode == 0


def _commit_landed(project_dir: Path, commit_oid: str, head: str) -> bool:
    """Did the publication's ref move land — is the commit in history?

    A crash after ``update-ref`` is normally recognized by ``HEAD ==
    commit_oid``. But any commit landing ON TOP before the resume (a second
    actor, a push round-trip) leaves HEAD a DESCENDANT of the intent's OID:
    the ref move DID land, the unit's work IS in history, yet an exact-match
    check classifies it as «HEAD moved elsewhere», supersedes the intent and
    loses the receipt — the strict archive path then refuses the reachable
    artifact (R2.5 REVIEW, iter 5). The ancestor test closes that gap: HEAD
    equal to, or a descendant of, the intent's commit means LANDED.
    """
    if not commit_oid:
        return False
    if head == commit_oid:
        return True
    return _is_ancestor(project_dir, commit_oid, head)


def _index_entries(project_dir: Path, files: list[str] | tuple[str, ...]) -> dict[str, str]:
    """The REAL index ``(mode, blob)`` (stage 0) per path, ``""`` when absent.

    One parser for the whole codebase: the read delegates to
    :class:`~awf.infra.git_model.GitRepository` (R2.1 — the single
    byte-faithful git model) instead of re-parsing ``ls-files -s -z`` here.
    A read failure degrades to "all absent" exactly as the old inline
    parser did on ``rc != 0`` — this lookup is advisory (the plan's staging
    and preflight have already refused a broken index), so it stays neutral
    rather than raising a second exception type into the gate.

    The value is ``"<mode> <blob>"`` (R2.5 REVIEW, iter 6, E2): the mode is
    part of the entry — a chmod-only change is a real index change, and a
    blob-only comparison used to treat it as untouched and reset it away.
    """
    if not files:
        return {}
    wanted = set(files)
    try:
        entries = GitRepository(project_dir).index_entries()
    except GitModelError:
        return {f: "" for f in files}
    # Keep ONLY the requested paths (R2.5 architector-review): the intent
    # records the PLAN files' own entries, not a snapshot of the whole index.
    # Reading every stage-0 entry and then ``setdefault``-ing the requested
    # ones used to serialize the entire real index into PUBLISH-INTENT — the
    # fact would grow with an unrelated repository and stop describing what
    # it says it describes.
    out: dict[str, str] = {}
    for entry in entries:
        if entry.stage != 0:
            continue
        path = entry.path.decode("utf-8", "surrogateescape")
        if path in wanted:
            out[path] = f"{entry.mode} {entry.blob}"
    for f in files:
        out.setdefault(f, "")
    return out


# ─── intent / receipt IO ─────────────────────────────────────────────────


def dumps_fact(data: dict) -> str:
    """Serialize a publication fact to UTF-8-safe JSON text (R2.5 REVIEW
    iter 8, Q5-1).

    R2.1 keeps git file names as BYTES (``surrogateescape``): a name with a
    non-UTF-8 byte is a legal path and reaches the plan unchanged. Such a
    name decodes to a lone surrogate, which CANNOT be encoded as UTF-8 — so
    ``ensure_ascii=False`` makes the write itself raise
    ``UnicodeEncodeError`` (a ``ValueError``, neither ``OSError`` nor
    ``ManagedPathError``). That used to escape every typed boundary; on the
    publication path it killed the stage BEFORE the intent (the resumable
    anchor) landed, and on the best-effort audit path it crashed a writer
    documented to log and move on.

    Fall back to ``ensure_ascii=True`` exactly when the readable text does
    not encode (the ``claim_changes`` digest precedent): ``json.loads``
    returns the same surrogates, index sync/reset work on the decoded paths,
    and the fact stays writable. Normal facts keep the readable form."""
    text = json.dumps(data, ensure_ascii=False, indent=1)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(data, ensure_ascii=True, indent=1)
    return text + "\n"


def _write_json(path: Path, data: dict) -> None:
    """Publish a durable publication fact (intent / receipt / superseded /
    archive) — through the SAME managed no-follow writer as the state
    projection and the log/dispatch writers (R1 carry a). The publication
    facts are the state this unit owns, so a symlinked ``.agentic`` /
    ``context`` must be refused before a byte lands instead of the plain
    atomic write following the link out of the project."""
    try:
        managed_fs.managed_write_text(
            path,
            dumps_fact(data),
            root=managed_fs.agentic_root_for(path.parent),
        )
    except managed_fs.ManagedPathError as e:
        # R2.5 REVIEW (iter 5, D3): the managed no-follow barrier refuses a
        # symlinked ``.agentic`` / ``context`` with ``ManagedPathError`` — NOT
        # an ``OSError``. On the publication path that refusal is a publication
        # refusal (fail-closed): translate it to the protocol's own type so
        # every caller handles it as a typed outcome instead of a bare
        # exception escaping the gate/archive (the pre-fix asymmetry with
        # ``pipeline_state``'s shared catch-list).
        raise PublishError(
            f"publication fact {path.name} refused by the managed-path "
            f"barrier: {e}"
        ) from e
    except OSError as e:
        # R2.5 architector-review: this is the ONE writer of every publication
        # fact (intent / receipt / superseded / archive), so its whole failure
        # family maps to the protocol's own type — the barrier case above AND
        # the generic I/O refusal (disk full, EACCES, a vanished directory).
        # Leaving a bare ``OSError`` to escape was the asymmetry: the gate
        # caught it for its own call sites, but the resume and archive paths
        # saw neither the barrier nor a generic write failure as a typed
        # outcome — a crash in the exact step the protocol exists to make
        # resumable. Fail-closed: no partial fact lands, the operation stays
        # resumable, and every caller handles one type.
        raise PublishError(
            f"publication fact {path.name} could not be written: {e}"
        ) from e


def _read_json(path: Path) -> dict | None:
    """Read one publication fact. Absent, unreadable OR not a JSON object →
    ``None`` — the same "an uninterpretable fact is not a fact" degradation
    the sibling reader (``api/run._read_commit_audit``) already uses. Valid
    JSON of the wrong shape (a list/string, or an object written by another
    schema) used to raise ``KeyError``/``TypeError`` out of the commit gate
    while this reader's contract said "unreadable → None" (R2.5
    architect-review)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_intent(project_dir: Path, todo_id: str) -> PublishIntent | None:
    data = _read_json(intent_file(project_dir, todo_id))
    if not data:
        return None
    try:
        intent = PublishIntent.from_dict(data)
    except (KeyError, TypeError, ValueError):
        # A fact whose shape is not the intent's cannot describe a pending
        # operation (see ``_read_json``) — the caller runs fresh instead of
        # dying on a schema mismatch. The quiet-diff / own-commit guard still
        # refuses a second commit when the ref already carries the work.
        return None
    if intent.todo_id != todo_id:
        # The fact is addressed by FILE NAME but its body names the unit too.
        # A copy/restore under another name describes ANOTHER operation: it
        # used to resume under this id, write a receipt under the body's id
        # and never drop this id's intent — a permanent pending (R2.5 REVIEW,
        # iter 10, F1). A foreign fact is not a fact of this unit.
        return None
    if not _is_oid_form(intent.commit_oid):
        # The intent's whole point is «I am about to move the ref to THIS
        # OID». A fact without one does not describe a live operation: a
        # resume used to skip the ref move and write a ``committed`` receipt
        # with an empty sha — a false full success that authorized the
        # archive (R2.5 REVIEW, iter 7, Q4-1). Requirement 3: committed ⇒ a
        # non-empty OID reachable from HEAD. Treat the fact as absent and
        # let the caller run fresh (the fresh path overwrites it).
        #
        # A malformed-but-nonempty OID (iter 9, A2) is the same case: a
        # half-written or hand-edited intent with ``"deadbeef"`` used to be
        # accepted, ``update-ref`` refused it on every resume and the unit
        # was wedged forever. Only the full object-id form describes a real
        # object.
        return None
    return intent


def read_receipt(project_dir: Path, todo_id: str) -> UnitReceipt | None:
    data = _read_json(receipt_file(project_dir, todo_id))
    if not data:
        return None
    try:
        receipt = UnitReceipt.from_dict(data)
    except (KeyError, TypeError, ValueError):
        # A malformed receipt authorizes nothing: no archive, no "already
        # published" short-circuit — fail closed by treating it as absent.
        return None
    if receipt.todo_id != todo_id:
        # Same identity rule as the intent reader (R2.5 REVIEW, iter 10,
        # F2): a receipt under this file name whose body names another unit
        # proves nothing about THIS unit's work — it must not authorize its
        # archive or report its commit (requirement 3: archived only behind a
        # committed receipt OF THIS UNIT).
        return None
    if (
        receipt.disposition in (DISPOSITION_COMMITTED, DISPOSITION_RECONCILIATION)
        and not receipt.commit_oid
    ):
        # R2.5 architect-review: the SAME identity invariant as Q4-1, on the
        # READER that authorizes the archive. ``committed`` without an OID
        # does not describe a commit — a hand-written or damaged fact would
        # otherwise authorize archiving a unit whose work is not in HEAD.
        # ``read_intent`` already drops an OID-less intent; the receipt must
        # not be the one fact that launders it back in. No-code receipts keep
        # their legitimately empty OID.
        return None
    return receipt


def _unlink_fact(project_dir: Path, path: Path, label: str) -> None:
    """Drop one publication fact — the destructive sibling of :func:`_write_json`.

    Carries the SAME no-follow barrier as the writer: a symlinked
    ``.agentic`` / ``context`` is refused before the unlink (mirrors
    ``state_store.remove_state_file``), so a fact cleanup can never delete a
    file outside the project. Idempotent (a missing fact is the "nothing to
    clean" case) and best-effort (a refusal is logged, not raised: the unit
    is not blocked by a failed *cleanup* of a fact whose absence is the
    desired end state). Both fact-removal sites share this ONE routine
    rather than a copy each (R2.5 architector-review).
    """
    try:
        managed_fs.assert_no_symlink_escape(
            path, root=managed_fs.agentic_root_for(path.parent)
        )
        path.unlink()
    except FileNotFoundError:
        pass
    except (OSError, managed_fs.ManagedPathError) as e:
        _log(paths.logs_dir(project_dir), f"R2.5: {label} cleanup failed: {e}")


def _remove_intent(project_dir: Path, todo_id: str) -> None:
    """Drop the pending-intent fact (idempotent) — see :func:`_unlink_fact`."""
    _unlink_fact(project_dir, intent_file(project_dir, todo_id), "intent")


def _clear_superseded(project_dir: Path, todo_id: str) -> None:
    """Drop a stale ``superseded`` fact once the unit publishes successfully.

    ``PUBLISH-SUPERSEDED-{todo}.json`` records an ABANDONED intent. A newer
    successful receipt for the same unit makes that fact stale: left in
    place, ``publication_facts`` reports ``superseded=True`` next to a live
    ``commit`` / ``receipt committed`` line — a false, contradictory fact in
    status and RUN-REPORT (requirement 4; R2.5 REVIEW, iter 5). Idempotent;
    the barrier mechanics live in :func:`_unlink_fact`."""
    _unlink_fact(project_dir, superseded_file(project_dir, todo_id), "superseded")


def _record_superseded(
    project_dir: Path, intent: PublishIntent, reason: str
) -> None:
    """Abandon a stale intent: write the ``superseded`` fact, drop the intent.

    The intent is the PENDING signal a resume keys on. When HEAD moved
    elsewhere the operation cannot resume (no second commit), but leaving
    the intent behind would make EVERY later ``maybe_commit`` refuse on the
    same TODO-id — neither resumable nor abandonable. Dropping it (with a
    durable fact) lets the next commit-path replan against the new HEAD.
    """
    _write_json(
        superseded_file(project_dir, intent.todo_id),
        {
            "todo_id": intent.todo_id,
            "disposition": "superseded",
            "reason": reason,
            "expected_head": intent.expected_head,
            "commit_oid": intent.commit_oid,
            "created_at": _now(),
        },
    )
    _remove_intent(project_dir, intent.todo_id)


def abandon_pending(
    project_dir: Path, todo_id: str, *, reason: str = ""
) -> bool:
    """Explicitly abandon a pending publication intent (True if one existed).

    The manual counterpart of the automatic supersede: a supervisor who
    decides the stale operation must be dropped does not have to wait for
    the next commit attempt to trip the refusal. Idempotent — no intent,
    no-op.

    SPECULATIVE / manual reserve (R2.5 REVIEW, iter 5, T1): exported for a
    future explicit abandon entry point (CLI/MCP); it currently has NO
    in-tree caller — the automatic supersede inside :func:`resume_if_pending`
    covers the pipeline path. Not wired deliberately: wiring a new command is
    out of this unit's scope.
    """
    intent = read_intent(project_dir, todo_id)
    if intent is None:
        return False
    _record_superseded(
        project_dir, intent, reason or "abandoned by the supervisor"
    )
    return True


# ─── publication ─────────────────────────────────────────────────────────


def publish_from_env(
    project_dir: Path,
    plan: commit_plan.CommitPlan,
    env: dict[str, str],
    message: str,
    logs_dir: Path | None = None,
    *,
    crash_at: str = "",
    evidence_refs: tuple[str, ...] = (),
    transfers: tuple[str, ...] = (),
    residuals: tuple[str, ...] = (),
) -> commit_plan.CommitOutcome:
    """Publish a plan whose files are already staged in the isolated index.

    ``env`` must carry ``GIT_INDEX_FILE`` pointing at the throwaway index
    that :func:`awf.commit_delta.stage_plan_into_index` just populated. The
    real index is never opened except for the targeted, content-verified
    sync in step 5.
    """
    todo_id = plan.todo_id
    _maybe_crash(CRASH_BASELINE, crash_at)

    # Read HEAD through the TYPED reader before anything else (R2.5
    # architector-review: the A4 invariant «an unreadable HEAD is not a HEAD
    # state» is the same on the fresh path, not only on resume). Proceeding
    # with ``expected_head=""`` after a transient read failure would build the
    # commit against an unborn-HEAD CAS that cannot match the real ref. No
    # object, intent or hook side effect happens before this point.
    head_ok, expected_head = _read_head(project_dir)
    if not head_ok:
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_ERROR,
            f"cannot read HEAD to plan the publication for {todo_id}; "
            "nothing was committed and no intent was written — retry when "
            "git is readable.",
        )

    # ``commit-tree`` creates the object without porcelain, so it does not
    # run the pre-commit hook the way ``git commit`` used to. Run it here,
    # against the isolated index (``env``) — a failing hook aborts before
    # any object or intent is written, preserving the A-01 failure contract.
    hook = _run_hook(project_dir, "pre-commit", env)
    if hook.returncode != 0:
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_ERROR,
            f"pre-commit hook failed (rc={hook.returncode}): "
            f"{_proc_detail(hook)}",
        )

    # ``commit-tree`` does not run ``commit-msg`` either (iter 9, A1): a repo
    # that rejects or rewrites messages through that hook lost the policy
    # silently. Run it here, before the object and the intent exist, against
    # a throwaway message file. A rejection aborts with the hook's own text —
    # nothing reached the odb or the ref.
    #
    # The file is written with ``surrogateescape`` (architector-review): the
    # plan may carry a byte-faithful (non-UTF-8) file name, and the message
    # lists the plan's files. A strict UTF-8 write raised ``UnicodeEncodeError``
    # — a ``ValueError`` outside every typed boundary — BEFORE the intent
    # (the resumable anchor), exactly the Q5-1 regression the unit exists to
    # prevent. ``surrogateescape`` writes the original bytes, the same shape
    # the old ``git commit -m`` argv carried.
    #
    # A successful hook may REWRITE the message (its documented second job:
    # ``Change-Id`` and friends). ``commit-tree`` is then fed the text the
    # hook produced, not the pre-hook copy — otherwise the automation is
    # still silently lost, which was the A1 finding itself.
    fd, msg_path = tempfile.mkstemp(
        prefix=f"awf-commit-msg-{todo_id}-", suffix=".txt"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(message)
        hook = _run_hook(project_dir, "commit-msg", env, [msg_path])
        if hook.returncode == 0:
            message = Path(msg_path).read_text(
                encoding="utf-8", errors="surrogateescape"
            )
    finally:
        try:
            os.unlink(msg_path)
        except OSError:
            pass
    if hook.returncode != 0:
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_ERROR,
            f"commit-msg hook failed (rc={hook.returncode}): "
            f"{_proc_detail(hook)}",
        )

    files = list(plan.files)
    index_orig = _index_entries(project_dir, files)
    # Requirement 3: the UnitReceipt carries the unit's OWN evidence refs. A
    # caller may pass them explicitly; otherwise derive them from the SAME
    # helper the no-code receipt uses. Before this the committed path left
    # the field empty, so every published receipt claimed "no evidence"
    # (R2.5 architector-review: the field was plumbed, no producer filled it).
    if not evidence_refs:
        evidence_refs = tuple(_evidence_refs(project_dir, todo_id))

    written = _git(project_dir, ["write-tree"], env=env)
    if written.returncode != 0:
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_ERROR,
            f"git write-tree failed (rc={written.returncode}): "
            f"{_proc_detail(written)}",
        )
    tree_oid = written.stdout.strip()

    commit_args = ["commit-tree", tree_oid]
    if expected_head:
        commit_args += ["-p", expected_head]
    commit_args += ["-m", message]
    created = _git(project_dir, commit_args)
    if created.returncode != 0:
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_ERROR,
            f"git commit-tree failed (rc={created.returncode}): "
            f"{_proc_detail(created)}",
        )
    commit_oid = created.stdout.strip()

    intent = PublishIntent(
        todo_id=todo_id,
        generation=plan.generation,
        expected_head=expected_head,
        tree_oid=tree_oid,
        commit_oid=commit_oid,
        candidate_tree=plan.candidate_tree,
        files=tuple(files),
        delta_files=tuple(plan.delta_files),
        degraded=tuple(plan.degraded),
        message=message,
        index_orig=index_orig,
        evidence_refs=tuple(evidence_refs),
        transfers=tuple(transfers),
        residuals=tuple(residuals),
        created_at=_now(),
    )
    _write_json(intent_file(project_dir, todo_id), intent.to_dict())
    _maybe_crash(CRASH_COMMIT_OBJECT, crash_at)

    outcome = _advance_publication(project_dir, intent, logs_dir, crash_at=crash_at)
    return outcome


def _advance_publication(
    project_dir: Path,
    intent: PublishIntent,
    logs_dir: Path | None,
    *,
    crash_at: str = "",
) -> commit_plan.CommitOutcome:
    """Finish a publication from its intent: CAS ref, sync index, receipt."""
    todo_id = intent.todo_id

    if not intent.commit_oid:
        # Belt-and-braces for a caller-built intent (``read_intent`` already
        # drops such a fact, Q4-1): without an OID there is nothing to move
        # the ref to, and the receipt would say ``committed`` about a commit
        # that does not exist (requirement 3). Refuse explicitly; the pending
        # fact stays on disk but is not read as a live intent.
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_REFUSED,
            f"publication for {todo_id} has no commit object; refusing to "
            "record a committed receipt without one.",
        )

    # The landed test and the CAS both need HEAD. Read it through the SAME
    # typed reader the resume entry point uses (R2.5 architector-review: the
    # A4 invariant must hold on the whole publication path, not only on the
    # first read). A transient failure degrades to `""` in the old
    # ``head_sha`` wrapper — here that would silently classify a landed
    # commit as «not landed» and report a misleading CAS refusal. Fail the
    # step as a typed ERROR instead; the intent stays on disk and a later
    # publish resumes safely.
    head_ok, head = _read_head(project_dir)
    if not head_ok:
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_ERROR,
            f"cannot read HEAD while finishing the publication for {todo_id}; "
            "the pending intent is preserved and a later publish resumes "
            "safely.",
        )

    # Skip the CAS when the ref ALREADY carries the object — HEAD is the
    # intent's commit OR a DESCENDANT of it (a crash after ``update-ref`` plus
    # a later commit on top). Only a ref that does not yet reach the object
    # needs the update (R2.5 REVIEW, iter 5, D1). Moving HEAD back onto the
    # intent's commit when it is already an ancestor would DROP the commits
    # that landed on top.
    if not _commit_landed(project_dir, intent.commit_oid, head):
        ref = ["update-ref", "-m", f"awf publish {todo_id}",
               "HEAD", intent.commit_oid]
        # CAS old-value: the expected HEAD, or the ZERO OID when the plan was
        # built against an UNBORN HEAD (no commit yet). The zero OID means
        # "the ref must still not exist", so a first commit created by another
        # actor between ``commit-tree`` and ``update-ref`` makes the CAS fail
        # (refused) instead of being silently overwritten and orphaned — the
        # unborn-HEAD branch was the one place the CAS was skipped (R2.5
        # REVIEW, iter 3).
        ref.append(intent.expected_head or _zero_oid(project_dir))
        moved = _git(project_dir, ref)
        if moved.returncode != 0:
            detail = _proc_detail(moved)
            # A well-formed OID that is NOT an object cannot become a retry:
            # every later resume refuses the same way and the unit is wedged
            # forever (R2.5 REVIEW, iter 9, A2). A missing object is not a
            # lost race — drop the intent with a durable fact so the next
            # commit-path replans. A CAS race (expected != actual HEAD) is
            # retryable and keeps the intent.
            obj = _git(
                project_dir, ["cat-file", "-e", f"{intent.commit_oid}^{{commit}}"]
            )
            if obj.returncode != 0:
                _record_superseded(
                    project_dir,
                    intent,
                    f"commit object {intent.commit_oid} is not in the "
                    "repository; the intent cannot be resumed and was "
                    "superseded.",
                )
                return commit_plan.CommitOutcome(
                    commit_plan.OUTCOME_REFUSED,
                    f"update-ref refused for {todo_id}: commit object "
                    f"{intent.commit_oid[:12]} does not exist ({detail}). "
                    "The stale intent was superseded; replan against HEAD.",
                )
            # Re-read for the diagnosis (the CAS lost, so HEAD moved since
            # the read above). A failed re-read renders as «unreadable»
            # instead of a false `<none>` from the degrading wrapper.
            found_ok, found = _read_head(project_dir)
            found_label = (
                (found[:12] or "<none>") if found_ok else "<unreadable>"
            )
            return commit_plan.CommitOutcome(
                commit_plan.OUTCOME_REFUSED,
                f"update-ref CAS refused for {todo_id} (expected HEAD "
                f"{intent.expected_head[:12] or '<none>'}, found "
                f"{found_label}): {detail}. "
                "The commit object exists but the ref did not move; no "
                "second commit was created.",
            )
    _maybe_crash(CRASH_UPDATE_REF, crash_at)

    conflicts = _sync_owned_index(project_dir, intent)
    _maybe_crash(CRASH_INDEX_SYNC, crash_at)

    # ``commit-tree`` + ``update-ref`` do not run ``post-commit`` (iter 9,
    # A1). Run it once the ref carries the commit and the index is in step;
    # a hook failure is logged, not turned into a commit failure — git
    # itself ignores the exit code of post-commit.
    post = _run_hook(project_dir, "post-commit", os.environ.copy())
    if post.returncode != 0:
        _log(
            logs_dir or paths.logs_dir(project_dir),
            f"R2.5: post-commit hook failed for {todo_id} "
            f"(rc={post.returncode}): {_proc_detail(post)}",
        )

    disposition = DISPOSITION_RECONCILIATION if conflicts else DISPOSITION_COMMITTED
    index_sync = "reconciliation_required" if conflicts else "synced"
    receipt = UnitReceipt(
        todo_id=todo_id,
        generation=intent.generation,
        disposition=disposition,
        commit_oid=intent.commit_oid,
        candidate_tree=intent.candidate_tree,
        expected_head=intent.expected_head,
        evidence_refs=intent.evidence_refs,
        transfers=intent.transfers,
        residuals=intent.residuals,
        degraded=intent.degraded,
        index_sync=index_sync,
        files=intent.files,
        created_at=_now(),
    )
    _write_json(receipt_file(project_dir, todo_id), receipt.to_dict())
    _remove_intent(project_dir, todo_id)
    _clear_superseded(project_dir, todo_id)

    reason = ""
    if conflicts:
        reason = (
            f"{DISPOSITION_RECONCILIATION}: the commit is in HEAD, but "
            f"{len(conflicts)} plan file(s) changed in the user's index "
            f"during the operation and were left untouched: {conflicts}. "
            "Reconcile the index manually; the unit is NOT re-committed."
        )
    _log(
        logs_dir or paths.logs_dir(project_dir),
        f"R2.5: published {todo_id} as {disposition} "
        f"({intent.commit_oid[:12]}, {index_sync})",
    )
    return commit_plan.CommitOutcome(
        commit_plan.OUTCOME_COMMITTED, reason, _short(project_dir, intent.commit_oid)
    )


def _entry_matches(current: str, original: str) -> bool:
    """Does the index entry equal the recorded one, MODE included?

    Both sides are ``"<mode> <blob>"`` (R2.5 REVIEW, iter 6, E2). An intent
    written before this fix stores a blob-only original — its mode is
    unknown, so that comparison stays blob-only (never a false conflict on
    a resume across the upgrade).
    """
    if current == original:
        return True
    if " " not in original:
        return _blob_of(current) == original
    return False


def _blob_of(entry: str) -> str:
    """The blob part of ``"<mode> <blob>"`` (``""`` / a legacy blob)."""
    mode, _, blob = entry.partition(" ")
    return blob if blob else mode


def _sync_owned_index(project_dir: Path, intent: PublishIntent) -> list[str]:
    """Re-point the real index at the new HEAD for OWNED, UNCHANGED files.

    A plan file is "owned and unchanged" when its current real-index entry
    (mode INCLUDED — a chmod-only change is the user's, not ours) still
    equals the entry captured before the operation. An entry that already
    equals the unit's own committed version is treated as synced ONLY when
    it also matches the CURRENT HEAD for that path: on a resumed attempt
    HEAD may have moved to a DESCENDANT (the D1 landed-commit case), and
    ``"equals our commit"`` then leaves the index at the ancestor version —
    a phantom staged revert while the receipt claims ``synced`` (R2.5
    REVIEW, iter 7, Q4-2). The entry is proven ours, so re-pointing it at
    HEAD cannot clobber foreign work. Anything else — a foreign entry, or a
    file the user staged/changed while the operation was in flight — is
    left exactly as it is and reported as a conflict (the caller records
    ``committed_with_reconciliation_required``).
    """
    conflicting: list[str] = []
    committed_entry: dict[str, str] = {}
    owned: list[str] = []
    # Read the real index ONCE for the whole plan: each path is visited a
    # single time and a ``git reset`` for one path never touches another's
    # entry, so one snapshot is exactly what the per-path reads produced
    # before (one git call instead of N).
    index_now = _index_entries(project_dir, intent.files)
    for path in intent.files:
        current = index_now.get(path, "")
        if _entry_matches(current, intent.index_orig.get(path, "")):
            owned.append(path)
            continue
        if path not in committed_entry:
            committed_entry[path] = _committed_entry(
                project_dir, intent.commit_oid, path
            )
        if _entry_matches(current, committed_entry[path]):
            # Our own write from the crashed attempt — but "equals our
            # commit" is not "in step with HEAD" when HEAD moved on to a
            # descendant (Q4-2). Compare with the CURRENT head and re-point
            # only the proven-owned entry.
            if _entry_matches(
                current, _committed_entry(project_dir, "HEAD", path)
            ):
                continue  # already in step with the current HEAD
            owned.append(path)
            continue
        conflicting.append(path)
    # One ``git reset`` for the whole owned set (iter 9, A5): per-path resets
    # were linear (300 paths ≈ 0.73 s). On refusal fall back to per-path so
    # only the entries git actually rejects are reported as conflicts.
    if owned:
        batch = _git(project_dir, ["reset", "-q", "HEAD", "--", *owned])
        if batch.returncode != 0:
            for path in owned:
                r = _git(project_dir, ["reset", "-q", "HEAD", "--", path])
                if r.returncode != 0:
                    conflicting.append(path)
    return conflicting


def _committed_entry(project_dir: Path, commit_oid: str, path: str) -> str:
    """The ``"<mode> <blob>"`` entry git stores for ``path`` in the commit's
    tree (``""`` when the path is absent — a deletion, or an unborn commit)."""
    if not commit_oid:
        return ""
    r = _git(project_dir, ["ls-tree", commit_oid, "--", path])
    if r.returncode != 0:
        return ""
    line = r.stdout.strip()
    if not line:
        return ""
    meta = line.partition("\t")[0]
    fields = meta.split()
    if len(fields) < 3:
        return ""
    return f"{fields[0]} {fields[2]}"


def resume_if_pending(
    project_dir: Path,
    todo_id: str,
    logs_dir: Path | None = None,
    *,
    crash_at: str = "",
) -> commit_plan.CommitOutcome | None:
    """Continue a publication a crash interrupted, or return ``None``.

    The PENDING signal is the intent file alone — a receipt without an
    intent is a finished fact, not a pending operation, so a later cycle
    that re-verifies the same TODO runs fresh instead of short-circuiting.

    - no intent → ``None`` (nothing to resume; the caller runs fresh).
    - intent + receipt whose commit OID MATCHES the intent → the publication
      finished and the crash landed between the receipt write and the intent
      removal; drop the intent and report the committed OID. A receipt that
      does NOT match (a stale no-code receipt of an earlier attempt) is
      ignored — the intent stays master and the sequence is resumed.
    - intent only, ref already at the intent's OID **or a DESCENDANT of it**
      → resume at index sync and receipt (the ref update happened before the
      crash; a later commit on top left HEAD a descendant — R2.5 REVIEW,
      iter 5).
    - intent only, ref still at the expected HEAD → redo the CAS update-ref.
    - intent only, ref moved elsewhere → refuse (someone else moved HEAD;
      no second commit is created). The stale intent is SUPERSEDED here —
      a durable fact records the move and the intent is dropped, so the
      same TODO-id is not wedged: the next commit-path replans against the
      new HEAD (see :func:`abandon_pending` for the manual drop).
    """
    if not todo_id:
        return None
    intent = read_intent(project_dir, todo_id)
    if intent is None:
        return None
    done = read_receipt(project_dir, todo_id)
    # The receipt only COMPLETES the pending operation when it is about THIS
    # intent — same commit OID. A receipt left by an EARLIER attempt (an
    # ``explicitly_verified_no_code`` from before a restore/re-run, with an
    # empty commit OID) says nothing about the live intent: treating its mere
    # presence as "done" drops the intent (the unit's commit never reaches
    # HEAD) and lets the stale receipt authorize the archive — a lost artifact
    # plus a false success (R2.5 REVIEW, iter 3). An incoherent receipt is
    # ignored and the intent stays master.
    if done is not None and done.commit_oid and done.commit_oid == intent.commit_oid:
        # The receipt landed; a crash between the receipt write and the
        # intent removal leaves both files. Finish the SAME tail
        # ``_advance_publication`` runs after its receipt (intent removal,
        # then clearing the superseded fact): the success invariant «a live
        # committed receipt never coexists with an abandoned-intent fact»
        # must also hold on this resume surface, not only on the normal
        # success path (R2.5 architect-review — the D2 invariant, second
        # surface).
        _remove_intent(project_dir, todo_id)
        _clear_superseded(project_dir, todo_id)
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_SKIPPED,
            f"publication for {todo_id} already reached a receipt "
            f"({done.disposition})",
            _short(project_dir, done.commit_oid),
        )

    head_ok, head = _read_head(project_dir)
    if not head_ok:
        # A transient git/object-store failure is not «HEAD moved»: acting on
        # it supersedes a LIVE intent (the commit object is still recoverable)
        # and loses the resumable anchor (R2.5 REVIEW, iter 9, A4). Surface a
        # typed ERROR and keep the intent; a later publish resumes.
        _log(
            logs_dir or paths.logs_dir(project_dir),
            f"R2.5: cannot read HEAD while resuming {todo_id}; "
            "the pending intent is preserved.",
        )
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_ERROR,
            f"cannot read HEAD to resume the pending publication for "
            f"{todo_id}; the intent is preserved and a later publish "
            "resumes safely.",
        )
    try:
        # LANDED = HEAD is the intent's commit or a DESCENDANT of it. The
        # descendant case is the crash-after-``update-ref`` recovery when a
        # later commit (another actor, a push round-trip) landed on top: the
        # ref DID move, the work IS in history — completing the receipt here
        # is what keeps the artifact from being lost (R2.5 REVIEW, iter 5, D1).
        if _commit_landed(project_dir, intent.commit_oid, head):
            return _advance_publication(
                project_dir, intent, logs_dir, crash_at=crash_at
            )
        if head == intent.expected_head or (not head and not intent.expected_head):
            return _advance_publication(
                project_dir, intent, logs_dir, crash_at=crash_at
            )
        # HEAD moved to a THIRD commit while the object sat unapplied: no
        # resume is possible and no second commit may be created. Supersede
        # the stale intent (durable fact + drop) BEFORE returning, otherwise
        # every later maybe_commit on this TODO-id trips the same refusal
        # forever (R2.5 REVIEW, iter 2: «Replan against the new HEAD»).
        reason = (
            f"pending publication for {todo_id} cannot resume: HEAD moved from "
            f"{intent.expected_head[:12] or '<none>'} to {head[:12] or '<none>'} "
            f"while the commit object {intent.commit_oid[:12]} sat unapplied. "
            "Replan against the new HEAD; no second commit was created. The "
            "stale intent was superseded and can no longer block the TODO."
        )
        _record_superseded(project_dir, intent, reason)
    except PublishError as e:
        # D3 (R2.5 REVIEW, iter 5): a refused publication fact
        # (intent/receipt/superseded) — the managed no-follow barrier OR a
        # generic I/O refusal, both typed by ``_write_json``. Return a typed
        # ERROR so the pending operation surfaces instead of crashing the
        # stage — the ref may already carry the unit's commit; a later
        # publish resumes.
        _log(
            logs_dir or paths.logs_dir(project_dir),
            f"R2.5: pending publication for {todo_id} could not advance: {e}",
        )
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_ERROR,
            f"pending publication for {todo_id} could not advance (a later "
            f"publish resumes safely): {e}",
        )
    except (subprocess.SubprocessError, OSError) as e:
        # R2.5 architector-review: the resume entry point owns the SAME typed
        # boundary as the fresh publish (``_commit_via_isolated_index``).
        # Before this, only ``maybe_commit`` wrapped the call, so any other
        # caller of the protocol entry point got a raw git launch/read
        # failure — the boundary was written NEXT TO the protocol instead of
        # in it. The durable intent survives, so this is a typed ERROR.
        _log(
            logs_dir or paths.logs_dir(project_dir),
            f"R2.5: pending publication for {todo_id} could not advance "
            f"(a later publish resumes safely): {e}",
        )
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_ERROR,
            f"pending publication for {todo_id} could not advance (the ref "
            f"may already carry the unit's commit; a later publish resumes "
            f"safely): {e}",
        )
    _log(logs_dir or paths.logs_dir(project_dir),
         f"R2.5: superseded stale publish intent for {todo_id}")
    return commit_plan.CommitOutcome(commit_plan.OUTCOME_REFUSED, reason)


# ─── no-code / audit outcome ─────────────────────────────────────────────


def _evidence_refs(project_dir: Path, todo_id: str) -> list[str]:
    """The unit's own evidence artifacts (relative paths), when present."""
    outbox = paths.outbox(project_dir)
    context = paths.context_dir(project_dir)
    # The legacy short form comes from its owner (``signals.short_id``) — the
    # same helper ``todos``/``signals`` use for DONE/VERIFIED lookups, instead
    # of a second split written next to the evidence list (R2.5
    # architect-review).
    short = short_id(todo_id)
    candidates = [
        outbox / f"DONE-{todo_id}.json",
        outbox / f"DONE-{todo_id}.md",
        outbox / f"DONE-{short}.json",
        outbox / f"DONE-{short}.md",
        context / f"VERIFIED-{todo_id}.sha",
        context / f"CANDIDATE-{todo_id}.json",
        context / f"GATES-{todo_id}.md",
    ]
    refs = []
    for c in candidates:
        if c.is_file():
            try:
                refs.append(str(c.relative_to(project_dir)))
            except ValueError:
                refs.append(str(c))
    return refs


def record_no_code_receipt(
    project_dir: Path,
    todo_id: str,
    *,
    reason: str = "",
    generation: int = 0,
) -> UnitReceipt:
    """Record the valid no-code / audit outcome — evidence required.

    A no-code unit is a real outcome, not an artificial empty commit. It is
    only recorded when the unit carries its OWN evidence artifacts; an empty
    plan of unknown reason (no DONE, no VERIFIED, no gates report) is refused
    — that is exactly the case the receipt must not launder into "done".
    """
    refs = tuple(_evidence_refs(project_dir, todo_id))
    if not refs:
        raise PublishError(
            f"no-code receipt for {todo_id} refused: no evidence artifacts "
            "found (DONE report, VERIFIED fingerprint or gates report). An "
            "empty plan of unknown reason is not a valid no-code outcome."
        )
    # A committed receipt is a durable fact. A later no-code recording for the
    # same unit (an empty-plan retry) must not downgrade it to
    # ``explicitly_verified_no_code`` with an empty commit OID — that erases
    # the published commit from every fact ``publication_facts`` reports
    # (R2.5 REVIEW, iter 3). Refuse loudly; the caller logs and moves on.
    existing = read_receipt(project_dir, todo_id)
    if existing is not None and existing.disposition in (
        DISPOSITION_COMMITTED,
        DISPOSITION_RECONCILIATION,
    ):
        raise PublishError(
            f"no-code receipt for {todo_id} refused: a "
            f"{existing.disposition!r} receipt already records the unit's "
            f"commit {existing.commit_oid[:12]}; a no-code recording must "
            "not erase it."
        )
    receipt = UnitReceipt(
        todo_id=todo_id,
        generation=generation,
        disposition=DISPOSITION_NO_CODE,
        commit_oid="",
        candidate_tree="",
        expected_head="",
        evidence_refs=refs,
        transfers=(),
        residuals=(reason,) if reason else (),
        degraded=(),
        index_sync="skipped",
        files=(),
        created_at=_now(),
    )
    _write_json(receipt_file(project_dir, todo_id), receipt.to_dict())
    _clear_superseded(project_dir, todo_id)
    return receipt


# ─── archive ─────────────────────────────────────────────────────────────


def archive_after_receipt(
    project_dir: Path,
    todo_id: str,
    *,
    crash_at: str = "",
    allow_no_receipt: bool = False,
) -> Path | None:
    """Archive the unit — allowed only behind a committed / no-code receipt.

    Refuses without a receipt or with a disposition that does not authorize
    archiving (a refused or errored publish leaves no receipt at all). The
    move is delegated to :func:`awf.todos.archive_todo`, which is idempotent
    and never overwrites an existing ``done/<todo>`` — so a crash mid-archive
    resumes by simply re-running this function.

    ``allow_no_receipt`` is the narrow legacy carve-out the pipeline sets for
    a policy that does NOT commit at all (``on_approved: next``) or a
    non-git project: there is no publication to stand on, so the historical
    archive behavior is kept. It is deliberately NOT set for the case the
    receipt exists to block — an empty plan of unknown reason, whose skip
    reason is ``no changes to commit`` and which the caller passes through
    the strict path. A receipt, when present, is still enforced even here.
    """
    receipt = read_receipt(project_dir, todo_id)
    if receipt is None:
        if not allow_no_receipt:
            raise PublishError(
                f"archive of {todo_id} refused: no UnitReceipt — archiving "
                "requires a committed or explicitly_verified_no_code outcome, "
                "not the presence of a DONE file or an unexplained empty plan."
            )
    elif receipt.disposition not in ARCHIVE_DISPOSITIONS:
        raise PublishError(
            f"archive of {todo_id} refused: receipt disposition "
            f"{receipt.disposition!r} does not authorize archiving."
        )
    from .todos import archive_todo

    try:
        archived = archive_todo(project_dir, todo_id)
    except OSError as e:
        # R2.5 architector-review: the archive MOVE is the archive protocol's
        # own step, so its I/O refusal is a typed ``PublishError`` too — the
        # sibling of the fact-writer family, and the caller (the pipeline
        # archive point) handles exactly this type, logging a refusal and
        # leaving the unit unarchived instead of dying on a raw traceback.
        # ``archive_todo`` is idempotent and never overwrites an existing
        # ``done/<todo>``, so a failed move re-runs safely.
        raise PublishError(
            f"archive of {todo_id} failed while moving the unit's files "
            f"(the move is idempotent; re-run to resume): {e}"
        ) from e
    # ``archive_todo`` returns None on an idempotent re-call (sources already
    # consumed). The archive tree is still the durable destination, so a
    # resumed archive reports it instead of a false "nothing happened".
    if archived is None:
        dest = paths.done_dir(project_dir) / todo_id
        archived = dest if dest.is_dir() else None
    _maybe_crash(CRASH_ARCHIVE, crash_at)
    # R2.5 architector-review: the fact stores the SAME locus shape
    # ``publication_facts`` reports — a project-relative path to the archive
    # DIRECTORY (``archive_todo``'s return). Before this, the fact held an
    # absolute directory while the status reader reported a relative
    # ``done/<id>/TODO.md`` file: one archive, two shapes (requirement 4 —
    # the fact must be one).
    #
    # BEST-EFFORT (R2.5 REVIEW, iter 10, A-2): the MOVE already happened, so
    # a refused fact write must not turn into «TODO NOT archived» — that text
    # contradicted the tree and repeated on every retry. ``publication_facts``
    # derives the archive from the tree when the fact is absent, so the fact
    # is an accelerator, not the only proof. Log and return the real move.
    try:
        _write_json(
            archive_fact_file(project_dir, todo_id),
            {
                "todo_id": todo_id,
                "archived": _relative(project_dir, archived) if archived else "",
                "disposition": receipt.disposition if receipt else "legacy_no_receipt",
                "created_at": _now(),
            },
        )
    except PublishError as e:
        _log(
            paths.logs_dir(project_dir),
            f"R2.5: archive fact for {todo_id} could not be written "
            f"(the move itself succeeded; publication_facts derives it from "
            f"the tree): {e}",
        )
    return archived


def publication_facts(project_dir: Path, todo_id: str) -> dict:
    """The three SEPARATE facts for status / RUN-REPORT.

    ``commit`` (short OID from the receipt), ``receipt`` (disposition) and
    ``archived`` (path) are distinct — a missing receipt and a missing
    archive tree are not the same fact, and neither is inferred from the
    other. ``archived`` is one shape: the project-relative archive directory
    (the durable fact written by :func:`archive_after_receipt`), derived from
    the tree only when the fact is absent (an archive from a pre-R2.5 tree).
    """
    receipt = read_receipt(project_dir, todo_id)
    fact = _read_json(archive_fact_file(project_dir, todo_id)) or {}
    archived = str(fact.get("archived") or "")
    if not archived:
        dest = paths.done_dir(project_dir) / todo_id
        if dest.is_dir():
            archived = _relative(project_dir, dest)
    superseded = _read_json(superseded_file(project_dir, todo_id)) or {}
    return {
        "commit": (receipt.commit_oid[:12] if receipt else ""),
        "receipt": (receipt.disposition if receipt else ""),
        "index_sync": (receipt.index_sync if receipt else ""),
        "archived": archived,
        # ONE definition of a live intent: the same ``read_intent`` the resume
        # path keys on. A raw file-existence check would report a pending
        # operation for a malformed or OID-less fact that ``read_intent``
        # already drops (Q4-1) — the status would contradict the protocol
        # (R2.5 architect-review).
        "pending_intent": read_intent(project_dir, todo_id) is not None,
        "superseded": bool(superseded),
        "superseded_reason": str(superseded.get("reason", "")),
    }


__all__ = [
    "CRASH_BASELINE",
    "CRASH_COMMIT_OBJECT",
    "CRASH_UPDATE_REF",
    "CRASH_INDEX_SYNC",
    "CRASH_ARCHIVE",
    "DISPOSITION_COMMITTED",
    "DISPOSITION_RECONCILIATION",
    "DISPOSITION_NO_CODE",
    "ARCHIVE_DISPOSITIONS",
    "PublishCrash",
    "PublishError",
    "PublishIntent",
    "UnitReceipt",
    "intent_file",
    "receipt_file",
    "archive_fact_file",
    "superseded_file",
    "read_intent",
    "read_receipt",
    "publish_from_env",
    "resume_if_pending",
    "abandon_pending",
    "record_no_code_receipt",
    "archive_after_receipt",
    "publication_facts",
]
