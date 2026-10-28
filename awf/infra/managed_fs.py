"""Managed filesystem boundaries for destructive and log-writer operations.

R1.1 (roadmap R1-SAFETY-AND-STATE; audit S9 findings S9-01/S9-03/S9-04):
``reset``, ``remove_orphans`` and the worker fallback log all trusted the
on-disk structure. A runtime directory or the fallback log that was a
symlink let a destructive op follow the link out of the project.

This module is the single place that:

- canonicalizes the project root once (callers pass a resolved root);
- validates public ids (TODO id) BEFORE they become a path component;
- walks managed paths component by component and refuses symlinks — no
  follow on intermediate components *and* the leaf;
- opens log files with ``O_NOFOLLOW`` and verifies the real type with
  ``fstat`` (POSIX), so a validate-then-open TOCTOU swap is caught;
- refuses a dangerous operation on a platform without the safe no-follow
  primitives instead of silently running a weak implementation.

The functions raise :class:`ManagedPathError`; callers translate it to
their own public error type (``AwfApiError`` for the API layer).
"""
from __future__ import annotations

import contextlib
import errno
import os
import stat
from collections.abc import Iterable
from pathlib import Path

from .. import todo_ids


class ManagedPathError(Exception):
    """A managed path is unsafe (symlink escape, wrong type, bad id)."""


# ── platform ────────────────────────────────────────────────────────────


def is_posix() -> bool:
    """True when the safe no-follow primitives are available."""
    return os.name == "posix"


def require_posix(operation: str) -> None:
    """Refuse a dangerous operation on an unsupported platform.

    A weak fallback is deliberately not provided: a destructive op that
    silently loses its symlink guarantee is worse than an explicit refusal
    (roadmap R1.1: "на неподдерживаемой платформе опасная операция
    ОТКАЗЫВАЕТ с причиной; слабая реализация не включается молча").
    """
    if not is_posix():
        raise ManagedPathError(
            f"{operation}: no-follow file operations need POSIX; platform "
            f"{os.name!r} is unsupported — refusing instead of silently "
            "running a weaker implementation"
        )


# ── id / component validation ───────────────────────────────────────────


def validate_todo_id(todo_id: str) -> str:
    """Return the TODO id or raise — a public id becomes a path component.

    Uses the single public parser (:mod:`awf.todo_ids`): a traversal like
    ``../../../disposable`` (S9-04) is rejected before any path is built.
    """
    if not todo_ids.is_valid_todo_id(todo_id):
        raise ManagedPathError(
            f"invalid TODO id {todo_id!r}: expected TODO-<digits>"
        )
    return todo_id


def agentic_root_for(path: Path) -> Path:
    """Anchor the no-follow walk above the ``.agentic`` component.

    Managed paths live at ``<root>/.agentic/...`` (``logs``, ``state``,
    ``context``, ...); the walk must start at ``<root>`` so the ``.agentic``
    component itself is checked (a symlinked ``.agentic`` is the S9-03
    escape). Walk up to the nearest component named ``.agentic`` and return
    its parent, whatever the leaf directory is called. For any other shape
    there is no known project root, so anchor at the path's own parent —
    never ``None``: the parent/leaf no-follow still runs instead of
    silently degrading to leaf-only (the F3 gap).
    """
    for anchor in (path, *path.parents):
        if anchor.name == ".agentic":
            return anchor.parent
    return path.parent


# ── two-basis path projection ───────────────────────────────────────────


def raw_root_for(project_dir: Path) -> Path:
    """Project root exactly as the caller spelled it — no ``resolve()``.

    The no-follow walk must start from the caller's own spelling: resolving
    the project would erase a symlinked parent the walk is allowed to cross
    (Q4b) and the internal links it must refuse (Q5).
    """
    raw = agentic_root_for(Path(project_dir) / ".agentic")
    return raw if raw.is_absolute() else Path.cwd() / raw


def surface_on_root_basis(surface: Path) -> Path:
    """Place a surface on the root's basis without resolving its leaf.

    A relative project (``Path("proj")``) or an absolute one reached through
    a symlinked parent (``/…/link/proj``, ``link -> real``) used to read as
    "escapes managed root" — a false refusal on a project the baseline wrote
    (Q4/Q4b).

    Resolving the PARENT gives the canonical basis the root lives on.
    Resolving the leaf would follow the very symlink the walk must refuse,
    so the leaf stays as spelled and the no-follow ``lstat`` sees it.

    Q6: ``resolve()`` raises ``RuntimeError`` on a symlink loop
    (``pipelines -> pipelines``). That is the same unsafe path, not an
    unexpected traceback — it becomes the barrier's own refusal type.
    """
    absolute = surface if surface.is_absolute() else Path.cwd() / surface
    try:
        parent = absolute.parent.resolve()
    except (OSError, RuntimeError) as exc:
        raise ManagedPathError(
            f"cannot resolve managed path parent {absolute.parent}: {exc}"
        ) from exc
    return parent / absolute.name


def surface_on_raw_basis(surface: Path, *, raw_root: Path) -> Path:
    """Re-anchor the surface's OWN components on the raw project root.

    Q5: the containment check resolves the surface's parent, so a symlink
    component INSIDE the project (``roles/draft -> roles``) resolves into
    the root and passes. That is fine for a writer
    (``managed_write_text`` walks the raw leaf), but ``refuse_unsafe_surfaces``
    also guards DESTRUCTIVE ops that then work on the raw path
    (``unlink``/``shutil.move``) — the internal link sent them at a live
    file.

    The chain is taken from the surface's own spelling (no resolve) and
    re-anchored at the raw root, so the walk is purely lexical and every
    component the caller named — leaf included — is ``lstat``-ed. The
    project root's own spelling stays outside the walk (a symlinked parent
    above the project is not an escape — Q4b).

    A surface ALREADY under ``raw_root`` keeps its own chain: anchoring it
    by the nearest ``.agentic`` would collapse a nested component
    (``.agentic/sub/.agentic/x`` -> ``.agentic/x``) and the walk — and the
    writer behind it — would touch a path the caller never named. The
    nearest-``.agentic`` anchor is only for a surface spelled through
    another name of the same project (a resolved root vs a relative or
    symlinked one).
    """
    absolute = surface if surface.is_absolute() else Path.cwd() / surface
    try:
        chain = absolute.relative_to(raw_root)
    except ValueError:
        anchor = agentic_root_for(absolute)
        try:
            chain = absolute.relative_to(anchor)
        except ValueError:
            chain = Path(absolute.name)
    return raw_root / chain


def assert_managed_surfaces(
    surfaces: Iterable[Path],
    *,
    raw_root: Path,
    root: Path,
    operation: str,
) -> None:
    """POSIX + two-basis no-follow preflight over ``surfaces``.

    ONE assembly of the invariant: no surface may cross a symlink on the
    caller's spelling (``raw_root`` basis) or land outside the resolved
    managed root (``root`` basis). Both barriers run through it — the API
    layer (``api._helpers.refuse_unsafe_surfaces``) and the draft writer
    (``todo_draft.make_todo_draft``, which adds only its own ``.agentic``
    containment). A check added here cannot be missed by one of them.

    ``surface_on_root_basis`` resolves the PARENT only, so a symlinked
    leaf stays visible to the ``lstat`` walk. Raises
    :class:`ManagedPathError`; the caller translates it to its own error
    type.
    """
    require_posix(operation)
    for surface in surfaces:
        assert_no_symlink_escape(
            surface_on_raw_basis(surface, raw_root=raw_root), root=raw_root
        )
        assert_no_symlink_escape(surface_on_root_basis(surface), root=root)


# ── symlink-free walks ──────────────────────────────────────────────────


def _components_under(root: Path, path: Path) -> list[Path]:
    """Every path from ``root`` down to ``path`` (root excluded)."""
    comps: list[Path] = []
    cur = root
    for part in path.relative_to(root).parts:
        cur = cur / part
        comps.append(cur)
    return comps


def assert_no_symlink_escape(path: Path, *, root: Path) -> None:
    """Refuse when ``path`` or any existing component under ``root`` is a
    symlink.

    ``path`` must lie under ``root`` lexically (callers build it that way).
    Only existing components are checked — a not-yet-created target is
    fine. This is the preflight for destructive ops: the check runs BEFORE
    the first unlink, so a symlinked ``inbox``/``outbox``/``context``/
    ``logs`` (S9-01) or a symlinked fallback log (S9-03) aborts with no
    side effect.
    """
    try:
        parts = _components_under(root, path)
    except ValueError as exc:  # path not under root — refuse
        raise ManagedPathError(f"{path} escapes managed root {root}") from exc
    for comp in parts:
        try:
            st = os.lstat(comp)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ManagedPathError(f"cannot stat managed component {comp}: {exc}") from exc
        if stat.S_ISLNK(st.st_mode):
            raise ManagedPathError(
                f"managed path component is a symlink: {comp}"
            )


def assert_managed_dir(path: Path, *, root: Path) -> None:
    """Refuse when ``path`` (an existing managed directory) is a symlink or
    is not a directory. Missing path is allowed (nothing to clean)."""
    assert_no_symlink_escape(path, root=root)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ManagedPathError(f"cannot stat managed dir {path}: {exc}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise ManagedPathError(f"managed path is not a directory: {path}")


# ── safe log opening ────────────────────────────────────────────────────


def _open_dir_chain(
    root: Path, path: Path, *, create: bool = False
) -> tuple[int, list[int]]:
    """Open ``path.parent`` one component at a time from ``root``.

    Every component below the trusted ``root`` is opened
    ``O_DIRECTORY|O_NOFOLLOW`` relative to the previous fd, so a symlink
    swapped in on ANY intermediate component (the validate-then-open
    TOCTOU of S9-03, F-B) raises ``ELOOP`` at open time — a preflight
    ``lstat`` alone left ``.agentic`` unprotected. Returns
    ``(leaf_dir_fd, fds_to_close)``; the caller closes every fd.

    ``create=True`` makes missing intermediate directories (``mkdirat``)
    instead of failing — used by the safe writers so callers no longer
    reach for a symlink-following ``Path.mkdir``.
    """
    try:
        rel = path.relative_to(root)
    except ValueError as exc:
        raise ManagedPathError(f"{path} escapes managed root {root}") from exc
    fds: list[int] = [os.open(root, os.O_RDONLY | os.O_DIRECTORY)]
    fd = fds[0]
    for part in rel.parts[:-1]:
        if part in ("", "."):
            continue
        if part == os.pardir:
            raise ManagedPathError(f"{path} escapes managed root {root}")
        try:
            nfd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                # R1.4: два процесса могли пройти create-ветку одновременно
                # (launch lease + interlock) — каталог уже создан соседом,
                # это не ошибка, следующий open его откроет.
                pass
            nfd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ManagedPathError(
                    f"managed path component is a symlink: {part}"
                ) from exc
            raise
        fds.append(nfd)
        fd = nfd
    return fd, fds


def _open_no_follow(
    dir_fd: int, name: str, *, flags: int, mode: int = 0o600
) -> int:
    """``os.open`` relative to ``dir_fd`` with ``O_NOFOLLOW`` on the leaf."""
    try:
        return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ManagedPathError(
                f"refusing to open a symlinked log leaf: {name}"
            ) from exc
        raise


def open_append_log(
    path: Path, *, root: Path | None = None, create: bool = False
) -> tuple[int, Path]:
    """Open a managed log for append: created 0600, no-follow, own inode.

    Returns ``(fd, actual_path)``. The caller owns the fd (``os.close``).

    Safety contract (roadmap R1.1):

    - POSIX only (``require_posix``) — a weaker platform is refused;
    - every component from the project root down to the log directory is
      opened ``O_DIRECTORY|O_NOFOLLOW`` (``_open_dir_chain``) — a symlinked
      ``.agentic`` or ``logs`` aborts before any write, even if swapped in
      after the preflight (F-B);
    - the leaf is opened with ``O_NOFOLLOW`` — a symlink (S9-03) aborts
      before any write;
    - ``create=True`` creates a missing log directory through ``mkdirat``
      on the already-verified parent (``_open_dir_chain``) — callers stop
      reaching for a symlink-following ``Path.mkdir`` after the preflight
      (audit 0215 F2), so a ``.agentic`` swapped to a symlink between the
      preflight and the directory creation is refused at the walk (F-B);
    - ``fstat`` on the opened fd verifies a regular file (TOCTOU between a
      resolve check and a plain ``open`` is not enough);
    - a hardlinked legacy log (``st_nlink > 1``) is never appended to or
      chmod-ed — a fresh sibling file is created instead so the shared
      inode stays untouched.

    ``root`` anchors the intermediate-component walk. It is never treated
    as "skip the check": ``None`` falls back to ``agentic_root_for(path.parent)``,
    so the guard always runs (F3).
    """
    require_posix("append worker log")
    if root is None:
        root = agentic_root_for(path.parent)
    assert_no_symlink_escape(path, root=root)

    # F-B: component-by-component no-follow open. A single
    # ``os.open(path.parent, O_NOFOLLOW)`` only guarded the leaf component
    # (``logs``); a ``.agentic`` swapped to a symlink between the preflight
    # and here still escaped.
    dir_fd, dir_fds = _open_dir_chain(root, path, create=create)
    try:
        fd = _open_no_follow(
            dir_fd,
            path.name,
            flags=os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            mode=0o600,
        )
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise ManagedPathError(
                f"managed log is not a regular file: {path}"
            )
        if st.st_nlink > 1:
            # Legacy log shared with another name — do not touch this
            # inode. Start a fresh sibling created INSIDE the verified
            # directory (``openat``+``O_EXCL``) — a path ``mkstemp`` would
            # re-resolve ``path.parent`` and could follow a swap outward,
            # defeating the walk that just ran.
            os.close(fd)
            legacy_name = f".{path.stem}-legacy-{os.urandom(4).hex()}.out"
            lfd = os.open(
                legacy_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            os.fchmod(lfd, 0o600)
            return lfd, path.parent / legacy_name
    finally:
        for f in dir_fds:
            os.close(f)

    os.fchmod(fd, 0o600)
    return fd, path


def open_managed(
    path: Path,
    *,
    flags: int,
    mode: int = 0o600,
    root: Path | None = None,
    create: bool = True,
) -> int:
    """Open ``path`` with no-follow on every component and the leaf → fd.

    General managed opener for writers that are not append logs (e.g. the
    state advisory lock). Refuses a symlinked intermediate component or leaf
    before creating anything; the caller owns the returned fd.
    """
    require_posix("open managed file")
    if root is None:
        root = agentic_root_for(path.parent)
    assert_no_symlink_escape(path, root=root)
    dir_fd, dir_fds = _open_dir_chain(root, path, create=create)
    try:
        return _open_no_follow(dir_fd, path.name, flags=flags, mode=mode)
    finally:
        for f in dir_fds:
            os.close(f)


def managed_mkdir(path: Path, *, root: Path | None = None) -> None:
    """Create a managed directory — no-follow on every component.

    Replaces a bare ``Path.mkdir(parents=True, exist_ok=True)`` at a writer
    whose directory lives under a symlink-reachable ``.agentic`` (audit
    0239): a symlinked component sends the new directory outside the
    project, and ``mkdir`` happily follows it. Components are opened
    ``O_DIRECTORY|O_NOFOLLOW`` from the project root, the leaf is created
    with ``mkdirat`` on the verified parent and then re-opened with
    ``O_NOFOLLOW`` — a link swapped in on any component aborts before the
    target directory appears; an existing leaf that is not a real directory
    is refused too.

    ``root`` defaults to :func:`agentic_root_for` of the target — never
    "skip the check".
    """
    require_posix("create managed directory")
    if root is None:
        root = agentic_root_for(path)
    assert_no_symlink_escape(path, root=root)
    if path == root:
        return
    dir_fd, dir_fds = _open_dir_chain(root, path, create=True)
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            pass
        try:
            leaf = os.open(
                path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=dir_fd,
            )
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ManagedPathError(
                    f"managed path component is not a directory: {path.name}"
                ) from exc
            raise
        os.close(leaf)
    finally:
        for f in dir_fds:
            os.close(f)


def managed_write_text(
    path: Path,
    content: str,
    *,
    root: Path,
    mode: int = 0o600,
    encoding: str = "utf-8",
    durable: bool = False,
    errors: str = "strict",
) -> None:
    """Atomically write ``content`` to a managed path — no-follow all along.

    Replaces a bare ``atomic_write_text`` at log/report writers that live
    under a symlink-reachable ``.agentic`` (F-A class). The temp file is
    created INSIDE the verified directory (``openat``+``O_EXCL``) and moved
    onto the leaf with ``renameat``, so a concurrent reader never sees a
    half-written file and a symlinked intermediate component is refused
    before a single byte lands. Missing directories are created through
    ``mkdirat`` on the already-verified parent — never a path ``mkdir``
    that could follow a link outward.

    ``durable`` (TODO-0227 REVIEW F1): fsync the file before the rename and
    the already-verified directory fd after it, so the write survives a
    HOST crash, not merely a process crash. Off by default — the historical
    bar for log/report writers stays as it was; the state-store JSON
    publisher opts in (it used to get this from
    ``_atomic.atomic_write_text(durable=True)``).
    """
    require_posix("write managed file")
    assert_no_symlink_escape(path, root=root)
    dir_fd, dir_fds = _open_dir_chain(root, path, create=True)
    try:
        tmp_name = f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=dir_fd,
        )
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding=encoding, errors=errors) as f:
                fd = -1  # fdopen owns it now
                f.write(content)
                if durable:
                    f.flush()
                    os.fsync(f.fileno())
            os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except Exception:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp_name, dir_fd=dir_fd)
            raise
        if durable:
            # The rename is durable only once the directory entry is fsynced.
            with contextlib.suppress(OSError):
                os.fsync(dir_fd)
    finally:
        for f in dir_fds:
            os.close(f)


def managed_write_under_agentic(
    path: Path, content: str, *, errors: str = "strict"
) -> None:
    """Create the parents and write a file under the project's ``.agentic``.

    The shared managed mkdir+write pair of the ``context``/``state`` writers
    (baseline-adjacent links, claim records, ``last-kill``): the root is
    derived from the leaf like every other managed writer, so a symlinked
    component refuses before the first byte lands.
    """
    managed_mkdir(path.parent)
    managed_write_text(path, content, root=agentic_root_for(path), errors=errors)


__all__ = [
    "ManagedPathError",
    "is_posix",
    "require_posix",
    "validate_todo_id",
    "agentic_root_for",
    "raw_root_for",
    "surface_on_root_basis",
    "surface_on_raw_basis",
    "assert_managed_surfaces",
    "assert_no_symlink_escape",
    "assert_managed_dir",
    "open_append_log",
    "open_managed",
    "managed_mkdir",
    "managed_write_text",
    "managed_write_under_agentic",
]
