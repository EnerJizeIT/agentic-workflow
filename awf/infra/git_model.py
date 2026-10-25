"""R2.1 (roadmap R2): the git model the commit path reads, without strings.

The commit path used to read git through human-facing output
(``git status --porcelain`` short lines, ``--name-only`` lists): a path with
a tab or a newline is C-quoted there, a rename is split into two printed
tokens, and a stray space breaks the parse. This module is the FOUNDATION
of the R2 ownership model: it reads the machine protocol (NUL-separated
``--porcelain=v2 -z`` / ``ls-files -z`` / ``diff --name-status -z``) and
keeps every path as the exact BYTES git reported. Nothing here strips, splits on
spaces or C-decodes a name — a display form and a UI-escaped form are
separate, explicit methods.

What it provides:

* :class:`GitRepository` — a read-only view of one working tree: HEAD /
  index / worktree facts, one :class:`ChangeRecord` per change, a typed
  :func:`preflight` that refuses states the model cannot represent
  (unmerged index, sparse checkout, split index) BEFORE any side effect.
* :class:`ChangeRecord` — the raw-path facts of one change: kind,
  old/new path bytes, base/index/work mode and blob, and provenance.
* :class:`BaselineManifest` — an immutable, versioned snapshot of HEAD,
  the index tree and entries, the worktree commit, the SELECTED untracked
  blobs/modes and the excluded paths with a reason. :meth:`capture_baseline`
  re-reads the tree fingerprint before and after; a tree that moves under
  capture is retried a bounded number of times and then refused.
* :func:`operations_from_changes` / :func:`attribute_ownership` — a change
  is a unit of ownership; a RENAME is ONE operation over a pair of paths,
  and its ownership comes from the saved baseline facts (the S2-01 rename
  case). A copy is NOT a pair: its source is a similarity guess, so the
  destination stands alone and never drags a foreign path into the unit.

This is the model only. Rewriting the commit gate on top of it is R2.3;
the gate, ``commit_plan`` and ``git_utils`` are untouched here.
"""
from __future__ import annotations

import base64
import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from .._path_link import decode_path_link

SCHEMA_VERSION = 1

GIT_TIMEOUT = 30

# mode -> blob-kind note; gitlink (submodule) is deliberately NOT modeled:
# its ownership/typechange semantics are out of scope for R2.1 and must be
# an explicit refusal rather than silent legacy behavior.
GITLINK_MODE = "160000"

# A similarity match over zero bytes is not evidence of a copy (every empty
# file would match every other), so an ``added`` file whose blob IS the
# repository's empty blob is never reclassified as a copy — the fresh
# ``__init__.py`` case. The empty-blob oid is DERIVED from the repository
# (`GitRepository._empty_blob`: ``git hash-object --stdin`` on empty input),
# never a hardcoded sha1 constant: a sha256 repository has a different empty
# oid, and a hardcoded one would silently re-fabricate the empty-file pair — the
# same class of bug as the fabricated copy pair itself.


class GitModelError(RuntimeError):
    """Base class for every refusal of the model."""


class UnmergedIndexError(GitModelError):
    """The index holds unmerged (conflicted) entries — refuse before any
    side effect: an ownership decision over a conflicted path is undefined."""


class UnsupportedSparseCheckoutError(GitModelError):
    """Sparse checkout is active — the worktree is a projection, not the
    full tree; the model refuses rather than mis-read missing paths."""


class UnsupportedSplitIndexError(GitModelError):
    """The split index shares entries with a base file — reading it as one
    self-contained index would mis-report paths; refuse explicitly."""


class UnsupportedChangeError(GitModelError):
    """A change the model cannot represent faithfully (e.g. a submodule
    gitlink) — refuse instead of silently degrading to the legacy path."""


class UnstableTreeError(GitModelError):
    """The working tree moved under every capture attempt — no stable
    baseline could be taken; refuse rather than record a torn snapshot."""


class UnsupportedManifestVersionError(GitModelError):
    """A serialized baseline whose ``version`` is not the supported
    ``SCHEMA_VERSION`` — refuse rather than parse a foreign schema as the
    current one and silently mis-read its ownership facts."""


class _UntrackedVanished(Exception):
    """An untracked path disappeared between the ``ls-files`` listing and
    the ``lstat`` (M2): recorded as excluded with a reason, never silently
    selected with a guessed mode — the silent-legacy class the model
    exists to remove."""


class _UntrackedNotRepresentable(Exception):
    """An untracked path git cannot represent as a content blob — a
    directory / nested repository, listed as ``dir/`` (F-QA2). Recorded as
    excluded with a reason: selecting it with a fabricated blob mode
    (``st_mode & 0o100`` is always set on a directory) is the same
    silent-legacy class as reading a gitlink as ordinary content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

# ─── display / escaping (separate from the raw bytes) ────────────────────


def display_path(path: bytes) -> str:
    """Lossless display decoding of a git path.

    ``surrogateescape`` round-trips ANY byte sequence back to the original
    bytes (``s.encode("utf-8", "surrogateescape") == path``): a name that is
    not valid UTF-8 is shown, never dropped and never mangled.
    """
    return path.decode("utf-8", "surrogateescape")


def raw_identity(printed: str) -> str:
    """Bridge a PRINTED ``git`` path to the raw display identity.

    A baseline snapshot written before the raw format keeps git's printed
    form; under the default ``core.quotePath=true`` a non-ASCII name arrives
    C-quoted (``"\\321\\204..."``) while the model hands back the RAW path.
    Comparing raw to printed silently loses the match, so the legacy reader
    decodes the C-quote back to the string :func:`display_path` would produce
    for the same bytes. A line that is not quoted (ASCII, or an already-raw
    name under ``core.quotePath=false``) is returned unchanged.
    """
    if len(printed) < 2 or printed[0] != '"' or printed[-1] != '"':
        return printed
    body = printed[1:-1]
    out = bytearray()
    simple = {
        "a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A,
        "v": 0x0B, "f": 0x0C, "r": 0x0D, '"': 0x22, "\\": 0x5C,
    }
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "\\" and i + 1 < n:
            nxt = body[i + 1]
            if "0" <= nxt <= "7":
                j = i + 1
                digits = ""
                while j < n and len(digits) < 3 and "0" <= body[j] <= "7":
                    digits += body[j]
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            out.append(simple.get(nxt, ord(nxt)))
            i += 2
            continue
        out.extend(ch.encode("utf-8", "surrogateescape"))
        i += 1
    return out.decode("utf-8", "surrogateescape")


def escape_for_ui(path: bytes) -> str:
    """UI escaping — a SEPARATE concern from the stored bytes.

    Control characters (tab/newline/CR), a backslash and other C0 codes are
    turned into visible escapes for logs and tables. A byte >= 0x80 that is
    not valid UTF-8 arrives as a lone surrogate (``surrogateescape``) and is
    escaped by its byte value — the result is ALWAYS encodable by a strict
    UTF-8 sink (``json.dumps(..., ensure_ascii=False)`` / log / file), so the
    refusal message never breaks the very output it is built for. This never
    feeds the digest or an ownership decision.
    """
    out: list[str] = []
    for ch in display_path(path):
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif 0xDC80 <= ord(ch) <= 0xDCFF:
            out.append(f"\\x{ord(ch) - 0xDC00:02x}")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def escape_str_for_ui(text: str) -> str:
    """UI escaping of an already display-decoded path string (F3).

    The ``str`` counterpart of :func:`escape_for_ui`: a display string carries
    the original bytes through ``surrogateescape``, so it re-encodes
    losslessly before escaping. Used at every render boundary that holds a
    display string rather than the raw bytes (the R2.2 dirty / isolate /
    claim surfaces), so a name with a non-UTF-8 byte never breaks the strict
    UTF-8 sink the message is built for. Never feeds the digest or an
    ownership decision — those stay on the raw bytes.
    """
    return escape_for_ui(text.encode("utf-8", "surrogateescape"))


def read_untracked_snapshot(
    project_dir: Path | str, todo_id: str
) -> set[str] | None:
    """The ``BASELINE-{todo_id}.untracked`` paths in RAW display identity.

    One reader for the fact (M4 review, TODO-0228): the writer stores the
    paths NUL-terminated (``_path_link`` — a name may contain a newline or an
    edge space, and ``splitlines``/``strip`` rewrote it). Files written before
    the migration keep git's PRINTED form and are decoded lazily
    (:func:`raw_identity`), so an old snapshot compares equal to the model's
    raw paths too. ``None`` when the file is missing or unreadable — the
    caller's documented "no snapshot" fallback.
    """
    if not todo_id:
        return None
    snapshot = Path(project_dir) / ".agentic" / "context" / f"BASELINE-{todo_id}.untracked"
    if not snapshot.is_file():
        return None
    try:
        data = snapshot.read_bytes()
    except OSError:
        return None
    while data.endswith((b"\n", b"\r")):
        data = data[:-1]
    if b"\x00" in data:
        decoded = decode_path_link(data)
        return None if decoded is None else set(decoded)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return {raw_identity(line) for line in text.split("\n") if line}


def _render_command(args: tuple[str, ...]) -> str:
    """Join argv for a REFUSAL message so the result is ALWAYS strict-UTF-8
    encodable — the same invariant ``escape_for_ui`` enforces on the unmerged
    message (F-QA1), applied to the OTHER refusal surface: ``_run``.

    An argument that is a raw path arrives as a ``str`` decoded with
    ``surrogateescape`` (``_stat_blob`` passes one to ``hash-object``), so a
    byte ``>= 0x80`` that is not valid UTF-8 is a lone surrogate U+DC80..U+DCFF.
    Interpolated RAW into the message it breaks the very sink the refusal is
    built for (``json.dumps(..., ensure_ascii=False)`` / a strict UTF-8 writer),
    exactly the F-QA1 failure — but through a different message. Escape it by
    byte value here so no message this module raises can leak a surrogate."""
    return " ".join(
        escape_for_ui(a.encode("utf-8", "surrogateescape")) for a in args
    )


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


# ─── records ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChangeRecord:
    """One change, read straight from the NUL protocol.

    ``path`` is the raw trailing path bytes; a rename/copy carries the
    previous name in ``old_path`` (``path`` is the destination then).
    Modes and blobs are the head (``base_*``) and index (``index_*``)
    values git reported; ``work_*`` stays ``None`` where the machine
    protocol does not carry it. ``provenance`` names where the fact was
    observed. Nothing here is stripped or C-decoded.
    """

    path: bytes
    kind: str
    old_path: bytes | None = None
    base_mode: str | None = None
    index_mode: str | None = None
    work_mode: str | None = None
    base_blob: str | None = None
    index_blob: str | None = None
    work_blob: str | None = None
    provenance: str = "index"
    #: The raw two-letter ``XY`` status of the machine protocol: X is the
    #: index side vs HEAD, Y the worktree side vs the index, ``?`` untracked.
    #: Kept so a consumer asking "did the user already stage this?" does not
    #: have to infer staging from blob equality (a staged rename of an
    #: unchanged file has equal blobs but a non-blank X).
    xy: str = ""

    @property
    def new_path(self) -> bytes:
        """The destination path (== :attr:`path`), named for rename pairs."""
        return self.path

    @property
    def mode_change(self) -> bool:
        """True when the change is a pure mode flip (content unchanged).

        Only for a ``modified`` record. The mode fact may sit in the index
        (a ``git add``-ed ``chmod``) or in the worktree — either counts,
        because the index is what the unit commits: a staged flip the
        worktree reverted (``1 MM ... 100644 100755 100644``) is still a
        mode change. It is *pure* only while the content did not move
        (``base_blob == index_blob``): a ``chmod`` bundled with an edit is
        a content change, not a chmod (F-QA5). A type change has its own
        kind.
        """
        if self.kind != "modified" or self.base_mode is None:
            return False
        mode_moved = (
            self.index_mode is not None and self.index_mode != self.base_mode
        ) or (
            self.work_mode is not None and self.work_mode != self.base_mode
        )
        return mode_moved and self.base_blob == self.index_blob

    @property
    def display_path(self) -> str:
        """The lossless display decoding of :attr:`path` (separate from the
        stored bytes)."""
        return display_path(self.path)

    def digest(self) -> str:
        """A digest over the REAL bytes, the kind and the modes.

        Two names that differ only in a space, a tab or a non-ASCII byte
        get different digests — the model never normalizes a path.
        """
        h = hashlib.sha256()
        h.update(self.kind.encode("ascii"))
        h.update(b"\0")
        h.update(self.old_path if self.old_path is not None else b"")
        h.update(b"\0")
        h.update(self.path)
        h.update(b"\0")
        for mode in (self.base_mode, self.index_mode, self.work_mode):
            h.update((mode or "").encode("ascii"))
            h.update(b"\0")
        return h.hexdigest()


@dataclass(frozen=True)
class IndexEntry:
    """One ``git ls-files --stage -z`` entry: mode, blob oid, stage, path."""

    path: bytes
    mode: str
    blob: str
    stage: int

    def to_dict(self) -> dict:
        return {
            "path": _b64e(self.path),
            "mode": self.mode,
            "blob": self.blob,
            "stage": self.stage,
        }

    @staticmethod
    def from_dict(data: dict) -> IndexEntry:
        return IndexEntry(
            path=_b64d(data["path"]),
            mode=data["mode"],
            blob=data["blob"],
            stage=int(data["stage"]),
        )


@dataclass(frozen=True)
class UntrackedEntry:
    """A SELECTED untracked file: its mode and (best-effort) blob.

    Symlinks carry mode ``120000`` and the blob of the link target string
    (never the bytes behind the link — no filesystem read follows it).
    """

    path: bytes
    mode: str
    blob: str | None

    def to_dict(self) -> dict:
        return {"path": _b64e(self.path), "mode": self.mode, "blob": self.blob}

    @staticmethod
    def from_dict(data: dict) -> UntrackedEntry:
        return UntrackedEntry(
            path=_b64d(data["path"]), mode=data["mode"], blob=data["blob"]
        )


@dataclass(frozen=True)
class _UntrackedResolution:
    """The outcome of resolving ONE untracked path — the single place the two
    internal control-flow exceptions (:class:`_UntrackedVanished`,
    :class:`_UntrackedNotRepresentable`) become a fact.

    THREE consumers read the same resolution: the manifest's ``excluded``
    list (:meth:`GitRepository._classify_untracked`), the change set
    (:meth:`GitRepository.changes`, which DROPS a path the resolution does
    not select — F-QA4), and the stability fingerprint
    (:func:`tree_fingerprint`). The first two go through
    :meth:`GitRepository._untracked_selection` (the ownership table, which
    also excludes a secret-looking name); the fingerprint reads the inner
    :meth:`GitRepository._resolve_untracked` on purpose — it hashes the
    WHOLE tree, secret content included, so a secret edit still moves the
    baseline. One resolver for the exception translation means a new
    unsupported untracked case cannot be handled in one consumer and
    silently mishandled in the other. ``reason`` is set for an excluded
    path, ``mode``/``blob`` for a SELECTED one.
    """

    mode: str | None = None
    blob: str | None = None
    reason: str | None = None

    @property
    def selected(self) -> bool:
        return self.reason is None


# ─── baseline manifest ───────────────────────────────────────────────────


@dataclass(frozen=True)
class BaselineManifest:
    """R2.1: an immutable, versioned snapshot of the ownership baseline.

    ``head`` / ``index_tree`` / ``entries`` are the committed and staged
    facts; ``worktree_snapshot`` is the commit object a ``git stash
    create`` produced for the tracked worktree ("" when nothing to
    snapshot); ``untracked`` holds the SELECTED untracked blobs/modes and
    ``excluded`` names every path left out with a reason (``ignored`` /
    ``secret``). ``dirty`` is the set of paths that were already dirty at
    capture — the baseline facts a rename pair's ownership is read from.
    ``fingerprint`` is the tree fingerprint re-checked for stability.
    """

    version: int
    head: str
    index_tree: str
    worktree_snapshot: str
    entries: tuple[IndexEntry, ...]
    untracked: tuple[UntrackedEntry, ...]
    excluded: tuple[tuple[bytes, str], ...]
    dirty: tuple[bytes, ...]
    fingerprint: str

    def owns_path(self, path: bytes) -> bool:
        """A path the unit may claim: NOT dirty at baseline.

        A path the baseline already carried as dirty is pre-existing work
        the unit does not own.
        """
        return path not in self.dirty

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "head": self.head,
            "index_tree": self.index_tree,
            "worktree_snapshot": self.worktree_snapshot,
            "entries": [e.to_dict() for e in self.entries],
            "untracked": [u.to_dict() for u in self.untracked],
            "excluded": [[_b64e(p), reason] for p, reason in self.excluded],
            "dirty": [_b64e(p) for p in self.dirty],
            "fingerprint": self.fingerprint,
        }

    @staticmethod
    def from_dict(data: dict) -> BaselineManifest:
        # "versioned" is a contract, not a label: a payload written by a
        # schema this reader does not know must be REFUSED, not parsed as if
        # it were current — a silently mis-read baseline would mis-attribute
        # ownership, the very class of degradation the model removes. A newer
        # migration layer dispatches on ``version`` before calling this.
        version = int(data["version"])
        if version != SCHEMA_VERSION:
            raise UnsupportedManifestVersionError(
                f"baseline manifest version {version} is not the supported "
                f"version {SCHEMA_VERSION} — refusing to parse a foreign "
                "schema as the current snapshot"
            )
        return BaselineManifest(
            version=version,
            head=data["head"],
            index_tree=data["index_tree"],
            worktree_snapshot=data["worktree_snapshot"],
            entries=tuple(IndexEntry.from_dict(e) for e in data["entries"]),
            untracked=tuple(UntrackedEntry.from_dict(u) for u in data["untracked"]),
            excluded=tuple((_b64d(p), reason) for p, reason in data["excluded"]),
            dirty=tuple(_b64d(p) for p in data["dirty"]),
            fingerprint=data["fingerprint"],
        )


def tree_fingerprint(root: Path | str) -> str:
    """Content-addressed fingerprint of the exact working-tree state.

    Read through the model's OWN NUL protocol — never the string-based
    ``git_utils.tree_fingerprint`` (H1). That legacy reader took untracked
    names from a printed ``ls-files``: a non-ASCII name arrived C-quoted (a
    hash key no other reader uses) and under ``core.quotePath=false`` a
    non-UTF-8 name raised ``UnicodeDecodeError`` and aborted the whole
    fingerprint. The two functions still produce DIFFERENT digests; keeping
    the legacy value for ``awf tree-sha``/``VERIFIED-*.sha`` is recorded as
    debt in ``docs/design/single-owners.md`` (a digest change needs its own
    migration unit, not a refactor).

    Covers HEAD, the staged and unstaged tracked content (binary-safe
    ``git diff --binary``), and every untracked non-ignored file with its
    mode and content blob, keyed by the RAW path bytes — secret-looking
    names INCLUDED: stability covers the whole tree, unlike the ownership
    readers (:meth:`_classify_untracked` / :meth:`changes`) that exclude
    them. Same state → same digest; a new commit, an edited tracked file, or
    a new/changed/deleted untracked file → different digest. No path is ever
    parsed from printed output, so C-quoting and newlines cannot hide a
    change.
    """
    repo = GitRepository(root)
    h = hashlib.sha256()
    h.update(b"head\0" + repo.head_sha().encode("ascii") + b"\0")
    # staged (index vs HEAD) and unstaged (worktree vs index) content; the
    # raw diff bytes are hashed as-is, so a path is never split out of them
    h.update(b"staged\0")
    h.update(repo._run("diff", "--cached", "--binary", check=False).stdout)
    h.update(b"\0unstaged\0")
    h.update(repo._run("diff", "--binary", check=False).stdout)
    h.update(b"\0untracked\0")
    for path in sorted(repo.untracked_paths()):
        h.update(path + b"\0")
        resolution = repo._resolve_untracked(path)
        if not resolution.selected:
            h.update(b"excluded\0" + resolution.reason.encode("utf-8") + b"\0")
            continue
        h.update(resolution.mode.encode("ascii") + b"\0")
        h.update((resolution.blob or "").encode("ascii") + b"\0")
    return h.hexdigest()


# ─── repository view ─────────────────────────────────────────────────────

_SECRET_NAMES = frozenset(
    {
        ".env",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        ".netrc",
        ".npmrc",
        ".pgpass",
    }
)
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
# A bare ``credentials`` file is a common fixture/documentation name, so it
# alone is NOT treated as a secret (M3) — only the well-known credential
# stores, matched by relative path suffix, are.
_SECRET_PATH_SUFFIXES = (".aws/credentials",)


def _is_secret_like(path: bytes) -> bool:
    name = os.path.basename(path).decode("utf-8", "surrogateescape")
    lowered = name.lower()
    if lowered in _SECRET_NAMES:
        return True
    if lowered.startswith(".env."):
        return True
    if lowered.endswith(_SECRET_SUFFIXES):
        return True
    joined = path.decode("utf-8", "surrogateescape").lower()
    return any(joined.endswith(s) for s in _SECRET_PATH_SUFFIXES)


class GitRepository:
    """A read-only, byte-faithful view of one working tree.

    Every read goes through the NUL machine protocol; the class never runs
    a mutation (no add/commit/checkout). :meth:`preflight` refuses the
    states the model cannot represent, and :meth:`capture_baseline` is the
    one stable-snapshot entry point.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # -- low level -------------------------------------------------------

    def _run(
        self,
        *args: str,
        check: bool = True,
        input: bytes | None = None,
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT,
                input=input,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            # fail CLOSED with the model's own type, matching every sibling
            # git helper (commit_gate / commit_plan catch the same pair): a
            # hung git (TimeoutExpired, M1) OR an unusable git — a missing
            # binary is an OSError, not a SubprocessError — must not escape
            # as an untyped crash. A timeout or a missing git is a refusal.
            raise GitModelError(
                f"git {_render_command(args)} could not run (timeout "
                f"{GIT_TIMEOUT}s or git unavailable): {exc}"
            ) from exc
        if check and result.returncode != 0:
            raise GitModelError(
                f"git {_render_command(args)} failed (rc={result.returncode}): "
                f"{result.stderr.decode('utf-8', 'replace').strip()}"
            )
        return result

    def _config_bool(self, key: str) -> bool:
        result = self._run("config", "--bool", key, check=False)
        return result.stdout.strip() == b"true"

    # -- refusals --------------------------------------------------------

    def preflight(self) -> None:
        """Refuse the states the model cannot represent, before any read
        that would depend on them.

        Order: an unmerged index first (the most common conflict state),
        then sparse checkout (an incomplete worktree) and a split index
        (a shared entry store). A gitlink (submodule) is refused when it
        actually appears in :meth:`changes`.
        """
        unmerged = self._run("ls-files", "--unmerged", "-z", check=False)
        if unmerged.returncode == 0 and unmerged.stdout:
            names: list[str] = []
            for entry in unmerged.stdout.split(b"\0"):
                if not entry:
                    continue
                _meta, sep, name = entry.partition(b"\t")
                if sep and name:
                    # a refusal message is a UI surface: escape control
                    # characters (tab/newline) rather than leaking them into
                    # the log — the raw bytes stay in the records, never here
                    shown = escape_for_ui(name)
                    if shown not in names:
                        names.append(shown)
                if len(names) >= 5:
                    break
            raise UnmergedIndexError(
                "the index holds unmerged entries"
                + (f" (e.g. {', '.join(names)})" if names else "")
                + " — resolve the conflict before the model can attribute "
                "ownership"
            )
        self._refuse_sparse_or_split()

    def _refuse_sparse_or_split(self) -> None:
        """Refuse an incomplete or shared index store (M2).

        Called from :meth:`preflight` AND from :meth:`changes`: a direct
        change read on a sparse checkout would otherwise silently read a
        projection (missing paths) — the silent-legacy class the model
        exists to remove.
        """
        if self._config_bool("core.sparseCheckout") or self._config_bool(
            "core.sparseCheckoutCone"
        ):
            raise UnsupportedSparseCheckoutError(
                "core.sparseCheckout is enabled — the working tree is a "
                "projection, not the full tree; the model refuses to read "
                "it as complete"
            )
        if self._config_bool("core.splitIndex") or self._has_shared_index():
            raise UnsupportedSplitIndexError(
                "the git index is split (core.splitIndex / a shared index "
                "file) — entries live in a shared store the model will not "
                "read as one self-contained index"
            )

    def _has_shared_index(self) -> bool:
        gitdir = self._run("rev-parse", "--git-dir", check=False)
        if gitdir.returncode != 0:
            return False
        # byte-faithful like every other path read: strip only git's trailing
        # newline, never whitespace (or a byte) that is part of the name — a
        # "replace" decode could point the glob at the wrong directory and
        # hide a real split index (silent under-refusal)
        raw = gitdir.stdout.rstrip(b"\n")
        if not raw:
            return False
        base = self.root / os.fsdecode(raw)
        if not base.is_dir():
            base = Path(os.fsdecode(raw))
        try:
            return any(base.glob("sharedindex.*"))
        except OSError:
            return False

    # -- facts -----------------------------------------------------------

    def head_sha(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.decode("ascii").strip()

    def index_tree(self) -> str:
        """The tree oid of the current index (``git write-tree``).

        Best-effort: writing the tree object is inert (no ref, no index and
        no worktree change); "" when the index cannot be written (an
        unmerged index, say — the caller preflights first anyway).
        """
        result = self._run("write-tree", check=False)
        if result.returncode != 0:
            return ""
        return result.stdout.decode("ascii").strip()

    def index_entries(self) -> list[IndexEntry]:
        result = self._run("ls-files", "--stage", "-z")
        entries: list[IndexEntry] = []
        for raw in result.stdout.split(b"\0"):
            if not raw:
                continue
            meta, sep, path = raw.partition(b"\t")
            if not sep:
                continue
            mode, blob, stage = meta.split(b" ")
            entries.append(
                IndexEntry(
                    path=path,
                    mode=mode.decode("ascii"),
                    blob=blob.decode("ascii"),
                    stage=int(stage),
                )
            )
        return entries

    def worktree_snapshot(self) -> str:
        """A commit object of the tracked worktree (``git stash create``).

        An inert object — no stash ref is written, the index and worktree
        are untouched. "" when there is nothing to snapshot (clean tree).
        """
        result = self._run("stash", "create", check=False)
        if result.returncode != 0:
            return ""
        return result.stdout.decode("ascii").strip()

    def untracked_paths(self, paths: tuple[str, ...] = ()) -> list[bytes]:
        """Untracked paths, optionally limited to ``paths`` (pathspec).

        ``-z`` keeps the NUL protocol; a pathspec never widens the read, so
        a caller that only cares about ``.agentic/roles`` still gets the
        byte-faithful identity instead of opening a second printed reader.
        """
        args = ["ls-files", "--others", "--exclude-standard", "-z"]
        if paths:
            args += ["--", *paths]
        result = self._run(*args)
        return [p for p in result.stdout.split(b"\0") if p]

    def ignored_paths(self) -> list[bytes]:
        result = self._run(
            "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
        )
        return [p for p in result.stdout.split(b"\0") if p]

    # -- path sets vs a revision (NUL protocol) --------------------------

    def changed_paths(self, rev: str, paths: tuple[str, ...] = ()) -> list[bytes]:
        """Paths whose tracked content differs between ``rev`` and the
        working tree (``git diff --name-only -z rev``) — the byte-faithful
        replacement for the printed ``--name-only`` listing.

        Both staged and unstaged edits against ``rev`` are included, exactly
        like the printed command. Rename detection follows git's own
        ``diff.renames`` (on by default), so a rename is listed as its
        DESTINATION path only — the same command the legacy parser ran, so
        the file set does not drift; the ownership pair itself comes from
        :meth:`changes` (porcelain v2 carries both paths). ``paths`` limits
        the read by pathspec without leaving the NUL protocol.
        """
        args = ["diff", "--name-only", "-z", rev]
        if paths:
            args += ["--", *paths]
        result = self._run(*args)
        return [p for p in result.stdout.split(b"\0") if p]

    def staged_paths(self, rev: str = "") -> list[bytes]:
        """Paths staged against ``rev`` (default HEAD): the index side of the
        working tree, ``git diff --cached --name-only -z``. Byte-faithful
        for the same reason as :meth:`changed_paths`."""
        args = ("diff", "--cached", "--name-only", "-z") + ((rev,) if rev else ())
        result = self._run(*args)
        return [p for p in result.stdout.split(b"\0") if p]

    # -- changes ---------------------------------------------------------

    def changes(self) -> list[ChangeRecord]:
        """One :class:`ChangeRecord` per change, from ``--porcelain=v2 -z``.

        A rename/copy is ONE record carrying both paths — the machine
        protocol already groups the pair, so the printed-token split of the
        old short format is gone. An unmerged (``u``) record is refused.

        ``git status`` does not emit copy records, so a staged copy is
        detected separately (``git diff --cached --find-copies-harder``)
        and an ``added`` record with a detected source becomes ``copied``
        over the pair — an INFORMATIONAL classification only: a copy's
        source is chosen by a similarity heuristic, so it never becomes an
        ownership pair (see :func:`operations_from_changes`). An empty
        destination (the empty blob) is never reclassified: zero bytes match
        every other empty file, which fabricates a pair the unit never made.
        When the repository cannot report its empty-blob oid the guard FAILS
        CLOSED — reclassification is skipped rather than run against an
        unknown oid that would match every record (AR-6).

        The status read passes an EXPLICIT ``--find-renames`` (M1): the
        short config ``status.renames=false`` would otherwise split a staged
        rename into a delete+add pair, silently degrading requirement 4
        (the ownership pair would never assemble).

        Sparse/split checkouts are refused here too (M2): the inline
        unmerged refusal below covers a conflicted index, but a sparse
        worktree would otherwise be read as if it were the full tree.

        An untracked record passes the SAME resolution the manifest reads
        (``_untracked_selection``): a path the model cannot own — a
        directory / nested repository with no content blob (F-QA4), a
        secret-looking name, a path that vanished mid-read — is dropped
        before it can become an ownership operation. The invariant holds on
        BOTH readers instead of the manifest alone.
        """
        self._refuse_sparse_or_split()
        result = self._run(
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--find-renames",
        )
        records = self._selected_untracked(self._parse_status_v2(result.stdout))
        copies = self._staged_copy_pairs()
        if copies:
            # the empty-blob guard is per-repository (sha1 vs sha256), so it
            # is resolved once here rather than compared to a fixed constant.
            # When the repository cannot report its empty oid, the guard FAILS
            # CLOSED: reclassification is skipped entirely, so a fresh empty
            # file is never paired with a foreign committed empty one (AR-6 —
            # the F1 fabrication class the guard exists to prevent). A guard
            # that silently turns itself off would be the very silent
            # degradation this module removes.
            empty = self._empty_blob()
            if empty is not None:
                records = [
                    (
                        replace(r, kind="copied", old_path=copies[r.path])
                        if r.kind == "added"
                        and r.path in copies
                        and r.index_blob != empty
                        else r
                    )
                    for r in records
                ]
        return records

    def _staged_copy_pairs(self) -> dict[bytes, bytes]:
        """Staged copy detection: destination -> source.

        ``--find-copies-harder`` lets git match a copy against an UNMODIFIED
        source (the common ``cp`` + ``git add`` case). Best-effort: a failed
        diff yields no pairs (the record stays ``added``).
        """
        result = self._run(
            "diff",
            "--cached",
            "--name-status",
            "-z",
            "--find-copies-harder",
            "-M",
            check=False,
        )
        pairs: dict[bytes, bytes] = {}
        if result.returncode != 0:
            return pairs
        tokens = result.stdout.split(b"\0")
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if not token:
                i += 1
                continue
            code = token[:1]
            if code in (b"R", b"C"):
                src = tokens[i + 1] if i + 1 < len(tokens) else b""
                dst = tokens[i + 2] if i + 2 < len(tokens) else b""
                if code == b"C" and dst:
                    pairs[dst] = src
                i += 3
            else:
                i += 2
        return pairs

    @staticmethod
    def _parse_status_v2(data: bytes) -> list[ChangeRecord]:
        fields = data.split(b"\0")
        records: list[ChangeRecord] = []
        i = 0
        while i < len(fields):
            field = fields[i]
            if not field:
                i += 1
                continue
            tag = field[:1]
            if tag == b"u":
                raise UnmergedIndexError(
                    "the index holds an unmerged entry — resolve the "
                    "conflict before the model can read the change set"
                )
            if tag == b"1":
                parts = field.split(b" ", 8)
                records.append(GitRepository._record(parts[8], parts[1:8]))
            elif tag == b"2":
                parts = field.split(b" ", 9)
                old_path = fields[i + 1] if i + 1 < len(fields) else b""
                i += 1  # consume the original-path field
                records.append(
                    GitRepository._record(parts[9], parts[1:9], old_path=old_path)
                )
            elif tag == b"?":
                records.append(
                    ChangeRecord(
                        path=field[2:],
                        kind="untracked",
                        provenance="worktree",
                        xy="??",
                    )
                )
            # '!' (ignored) records are not requested and skipped here
            i += 1
        return records

    def _selected_untracked(
        self, records: list[ChangeRecord]
    ) -> list[ChangeRecord]:
        """Keep only untracked records the model can OWN — the SAME
        resolution the manifest reads (``_untracked_selection``).

        ``git status`` lists a nested repository as an untracked ``dir/``,
        but git cannot store a directory as a content blob (``git add``
        would make a gitlink ``160000``, which the index branch already
        refuses). F-QA2 recorded that path as excluded in the MANIFEST but
        the change set still carried it, so
        :func:`operations_from_changes` built an ownership operation over a
        directory — the silent-legacy class, on the other reader (F-QA4).
        A secret-looking name and a path that vanished mid-read drop here
        too: both readers now decide untracked paths by ONE rule. The blob
        is NOT needed for this decision, so the resolver runs without
        hashing (``need_blob=False``)."""
        return [
            r
            for r in records
            if r.kind != "untracked"
            or self._untracked_selection(r.path, need_blob=False).selected
        ]

    @staticmethod
    def _record(
        path: bytes, meta: list[bytes], old_path: bytes | None = None
    ) -> ChangeRecord:
        """Build one index-backed :class:`ChangeRecord` from the shared
        porcelain-v2 meta fields (``XY sub mH mI mW hH hI``).

        Formats ``1`` and ``2`` carry the SAME six mode/blob facts; format
        ``2`` only adds the original-path field and puts the score before the
        path. Building both here keeps the facts in ONE place (AR-5): a fix or
        a new field cannot land in one branch only. ``meta[0]`` is ``XY``; the
        gitlink refusal runs here, once, for every index-backed record.
        """
        GitRepository._refuse_gitlink(meta[2], meta[3], meta[4])
        return ChangeRecord(
            path=path,
            kind=GitRepository._kind(meta[0]),
            old_path=old_path,
            base_mode=meta[2].decode("ascii"),
            index_mode=meta[3].decode("ascii"),
            work_mode=meta[4].decode("ascii"),
            base_blob=meta[5].decode("ascii"),
            index_blob=meta[6].decode("ascii"),
            provenance="index",
            xy=meta[0].decode("ascii"),
        )

    @staticmethod
    def _refuse_gitlink(*modes: bytes) -> None:
        """A submodule (gitlink, mode ``160000``) has no ownership or
        typechange semantics in R2.1 — refuse explicitly instead of reading
        it as ordinary content (the silent-legacy behaviour the model exists
        to remove). Any of the base/index/work modes carrying the gitlink
        marker is enough."""
        if GITLINK_MODE.encode("ascii") in modes:
            raise UnsupportedChangeError(
                "a submodule (gitlink, mode 160000) is present — its ownership "
                "and typechange semantics are out of R2.1 scope; refusing "
                "rather than reading it as a silent legacy change"
            )

    @staticmethod
    def _kind(xy: bytes) -> str:
        x = xy[0:1].decode("ascii")
        y = xy[1:2].decode("ascii")
        if x == "R":
            return "renamed"
        if x == "C":
            return "copied"
        if x == "T" or y == "T":
            return "typechange"
        if x == "D" or y == "D":
            return "deleted"
        if x == "A" or y == "A":
            # y == "A" is the worktree-side add: ``git add -N``
            # (intent-to-add, used by ``git add -p``/``-i`` and some
            # editors) leaves ``x == "."`` but the path is NEW, not a
            # modification (short form `` A new.txt``). Reading it as
            # "modified" would invent a base version that HEAD never had.
            return "added"
        return "modified"

    # -- baseline --------------------------------------------------------

    def capture_baseline(self, max_attempts: int = 3) -> BaselineManifest:
        """Take a stable, immutable baseline snapshot.

        The tree fingerprint is read BEFORE the snapshot and re-checked
        AFTER it; if the tree moved under capture the snapshot is retried,
        up to ``max_attempts`` total reads. Every attempt still showing
        movement ends in :class:`UnstableTreeError` — a torn baseline is
        never recorded. (AWF cannot promise atomicity against an editor
        that does not take the AWF lock; stability is checked, not assumed.)
        """
        self.preflight()
        before = tree_fingerprint(self.root)
        for _ in range(max_attempts):
            head = self.head_sha()
            index_tree = self.index_tree()
            entries = self.index_entries()
            snapshot = self.worktree_snapshot()
            untracked, excluded = self._classify_untracked()
            dirty = self._dirty_paths()
            after = tree_fingerprint(self.root)
            if after == before:
                return BaselineManifest(
                    version=SCHEMA_VERSION,
                    head=head,
                    index_tree=index_tree,
                    worktree_snapshot=snapshot,
                    entries=tuple(entries),
                    untracked=tuple(untracked),
                    excluded=tuple(excluded),
                    dirty=tuple(sorted(dirty)),
                    fingerprint=after,
                )
            before = after
        raise UnstableTreeError(
            f"the working tree moved under {max_attempts} capture attempts — "
            "no stable baseline could be taken"
        )

    def _dirty_paths(self) -> set[bytes]:
        """Paths already dirty at capture: every changed tracked path, every
        rename pair member, and every untracked path the model can OWN — the
        secret-looking and directory/nested-repository untracked paths
        ``changes()`` drops never become an operation, so they are not
        pre-existing facts either. These are the pre-existing facts a rename
        pair's ownership is attributed from.

        The footprint comes from :func:`operations_from_changes` — the ONE
        place that decides what a change owns — so a copy's similarity-guessed
        source never leaks in as a pre-existing fact (the same rule that keeps
        a fabricated copy pair out of ownership). The untracked files are
        already in that change set (:meth:`changes` reads the status with
        ``--untracked-files=all``), so a second ``untracked_paths`` sweep
        would re-derive the same set from a second source — the footprint
        stays single-sourced."""
        dirty: set[bytes] = set()
        for op in operations_from_changes(self.changes()):
            dirty.update(op.paths)
        return dirty

    def _classify_untracked(
        self,
    ) -> tuple[list[UntrackedEntry], list[tuple[bytes, str]]]:
        """Split untracked files into SELECTED entries and excluded paths
        with a reason.

        Ignored files (per ``.gitignore``) and secret-looking names are
        NEVER swept into the baseline by a global ``add -A`` — they are
        reported as excluded, each with the reason ``ignored``/``secret``.
        """
        excluded: list[tuple[bytes, str]] = []
        for path in self.ignored_paths():
            excluded.append((path, "ignored"))
        selected: list[UntrackedEntry] = []
        for path in self.untracked_paths():
            resolution = self._untracked_selection(path)
            if resolution.selected:
                selected.append(
                    UntrackedEntry(
                        path=path, mode=resolution.mode, blob=resolution.blob
                    )
                )
            else:
                excluded.append((path, resolution.reason))
        return selected, excluded

    def _untracked_selection(
        self, path: bytes, *, need_blob: bool = True
    ) -> _UntrackedResolution:
        """The ONE table: untracked path class -> outcome.

        Both READERS of the untracked tree consult it — the manifest
        (:meth:`_classify_untracked` turns a NOT selected resolution into an
        ``excluded`` entry with its ``reason``) and the change set
        (:meth:`changes` drops a NOT selected untracked record before it can
        become an ownership operation, F-QA4). The classes:

        * file / symlink — a SELECTED entry (mode + blob);
        * secret-looking name — excluded ``secret``: never swept into
          ownership by a global ``add -A``;
        * directory / nested repository — excluded ``directory/gitlink:
          not representable as content blob`` (F-QA2): git stores no
          content blob for a directory;
        * vanished mid-read — excluded ``vanished during capture`` (M2).

        One rule, both readers: a new unsupported class cannot be handled in
        the manifest and silently mishandled in the change set. ``need_blob``
        is False for the change set, which only decides selected/not and does
        not pay for hashing every untracked file.
        """
        if _is_secret_like(path):
            return _UntrackedResolution(reason="secret")
        return self._resolve_untracked(path, need_blob=need_blob)

    def _resolve_untracked(
        self, path: bytes, *, need_blob: bool = True
    ) -> _UntrackedResolution:
        """Resolve one untracked path to a SELECTED entry or an excluded
        reason.

        The ONE place the two internal control-flow exceptions become a
        fact: :meth:`_untracked_selection` (manifest + change set) and
        :func:`tree_fingerprint` (stability) all read through here, so a
        new unsupported case cannot be handled in one consumer and
        silently mishandled in the other.
        """
        try:
            mode, blob = self._stat_blob(path, need_blob=need_blob)
        except _UntrackedVanished:
            return _UntrackedResolution(reason="vanished during capture")
        except _UntrackedNotRepresentable as exc:
            return _UntrackedResolution(reason=exc.reason)
        return _UntrackedResolution(mode=mode, blob=blob)

    def worktree_mode_blob(
        self, path: bytes, *, need_blob: bool = True
    ) -> tuple[str | None, str | None]:
        """Mode and blob Git will store for the WORKTREE path ``path`` — the
        ONE worktree-content rule, the delegation target for consumers that
        used to re-derive it by hand (R2.3 REVIEW-M3).

        Same rule as an untracked path's stored facts (:meth:`_stat_blob`):
        a symlink keeps ``120000`` and hashes its link TARGET STRING (never
        the bytes behind the link), the executable bit is git's OWNER bit,
        and a directory / nested repository is a REFUSAL
        (:class:`UnsupportedChangeError`) — git cannot store it as a content
        blob, so there is never a fabricated file entry. ``(None, None)``
        when the path vanished (best effort).
        """
        try:
            return self._stat_blob(path, need_blob=need_blob)
        except _UntrackedVanished:
            return None, None
        except _UntrackedNotRepresentable as exc:
            raise UnsupportedChangeError(
                f"cannot store {display_path(path)!r} as a content blob: "
                f"{exc.reason}"
            ) from exc

    def _stat_blob(
        self, path: bytes, *, need_blob: bool = True
    ) -> tuple[str, str | None]:
        """Mode and (best-effort) blob of an untracked path.

        A symlink keeps its link mode; the blob hashes the TARGET STRING,
        never the bytes behind the link (no read follows a symlink). The
        hash goes through ``git hash-object --stdin`` (M4) — correct for
        any object format, unlike a self-made sha1 header.

        A path that vanished between the listing and the ``lstat`` raises
        :class:`_UntrackedVanished` (M2): the caller records it as excluded
        with a reason instead of silently selecting it with a guessed mode.

        A directory (git lists a nested repository as ``dir/``) raises
        :class:`_UntrackedNotRepresentable` (F-QA2): git cannot store a
        directory as a content blob (``git add`` would make a gitlink the
        index branch already refuses), so the caller excludes it with a
        reason instead of fabricating an executable file entry.

        ``need_blob`` False returns the mode without hashing (F-QA4): the
        change set only decides selected/not, so it does not pay for a
        ``hash-object`` subprocess per untracked file.
        """
        full = self.root / os.fsdecode(path)
        try:
            st = os.lstat(full)
        except OSError as exc:
            raise _UntrackedVanished(path) from exc
        if stat.S_ISDIR(st.st_mode):
            raise _UntrackedNotRepresentable(
                "directory/gitlink: not representable as content blob"
            )
        if os.path.islink(full):
            if not need_blob:
                return "120000", None
            try:
                target = os.readlink(full)
            except OSError:
                return "120000", None
            raw = os.fsencode(target)
            return "120000", self._hash_stdin(raw)
        # git's rule: the executable bit is the OWNER bit (0100), not any
        # of the three (M3) — a file like 0611 is 100644 for git.
        mode = "100755" if (st.st_mode & 0o100) else "100644"
        if not need_blob:
            return mode, None
        result = self._run(
            "hash-object",
            "--",
            path.decode("utf-8", "surrogateescape"),
            check=False,
        )
        blob = (
            result.stdout.decode("ascii").strip()
            if result.returncode == 0
            else None
        )
        return mode, blob

    def _hash_stdin(self, raw: bytes) -> str | None:
        """The blob oid of ``raw`` through ``git hash-object --stdin``.

        The repository's object format decides the digest (sha1 or sha256);
        ``None`` when the call fails (best-effort, like the file hash)."""
        result = self._run("hash-object", "--stdin", check=False, input=raw)
        if result.returncode != 0:
            return None
        return result.stdout.decode("ascii").strip()

    def _empty_blob(self) -> str | None:
        """The repository's empty-blob oid, DERIVED (never hardcoded).

        ``git hash-object --stdin`` on empty input — the object format
        (sha1 / sha256) decides the digest, mirroring the symlink-blob fix.
        A hardcoded sha1 constant would silently re-fabricate the empty-file
        copy pair in a sha256 repository. ``None`` when the call fails: the
        caller then SKIPS the copy reclassification (fail-closed, AR-6) —
        an unknown empty oid must not let the guard match every record.
        """
        return self._hash_stdin(b"")


# ─── operations and ownership ────────────────────────────────────────────


@dataclass(frozen=True)
class PathOperation:
    """A unit of ownership: one path, or a rename over a pair.

    ``paths`` is the whole footprint of the operation — a rename owns BOTH
    its source and its destination, so ownership is decided for the pair
    as one thing, never for a lone half. A copy is NOT a pair here: its
    destination stands alone (see :func:`operations_from_changes`).
    """

    kind: str
    old_path: bytes | None
    new_path: bytes

    @property
    def paths(self) -> tuple[bytes, ...]:
        if self.old_path is not None:
            return (self.old_path, self.new_path)
        return (self.new_path,)


@dataclass
class OwnershipResult:
    """The attribution of a change set against the saved baseline.

    ``owned`` — operations the unit may commit; ``foreign`` — operations
    that were already dirty at baseline (pre-existing work); ``conflicts``
    — a pair whose two paths fall on opposite sides (intersection of
    ownership is not allowed, so the whole pair is escalated).
    """

    owned: list[PathOperation]
    foreign: list[PathOperation]
    conflicts: list[PathOperation]


def operations_from_changes(changes: list[ChangeRecord]) -> list[PathOperation]:
    """Group the change records into ownership operations.

    Only a RENAME is a pair operation: git grouped its two paths from the
    index state (the source is genuinely gone), so the unit owns the pair as
    one thing. A COPY's source comes from a similarity heuristic, never from
    the baseline facts, so it is NOT part of the ownership footprint: the
    destination is an independent ``added`` path and the heuristic source is
    dropped (R2.1 requirement 4 — a foreign source must not escalate the
    unit's own new file into a conflict)."""
    return [
        PathOperation(
            kind=r.kind,
            old_path=r.old_path if r.kind == "renamed" else None,
            new_path=r.path,
        )
        for r in changes
    ]


def attribute_ownership(
    baseline: BaselineManifest, operations: list[PathOperation]
) -> OwnershipResult:
    """Attribute each operation from the BASELINE facts, not the similarity
    heuristic of the current diff.

    A path is foreign when the baseline already carried it as dirty. For a
    single-path operation that decides it directly. For a rename the pair is
    decided as a WHOLE: both foreign → foreign, both owned → owned, and a
    mixed pair is a conflict (excluding one half would split a rename git
    will apply atomically).
    """
    owned: list[PathOperation] = []
    foreign: list[PathOperation] = []
    conflicts: list[PathOperation] = []
    for op in operations:
        dirty_flags = [not baseline.owns_path(p) for p in op.paths]
        if all(dirty_flags):
            foreign.append(op)
        elif any(dirty_flags):
            conflicts.append(op)
        else:
            owned.append(op)
    return OwnershipResult(owned=owned, foreign=foreign, conflicts=conflicts)
