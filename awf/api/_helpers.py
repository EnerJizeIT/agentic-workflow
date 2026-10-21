"""Internal helpers for awf.api: precondition checks + safe file reads.

All functions here are private (``_`` prefix). Public API functions in
:mod:`awf.api.lifecycle`, :mod:`awf.api.pipeline`, :mod:`awf.api.roles`
use them as building blocks.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import git_utils, paths
from ..infra import managed_fs
from ._errors import AwfApiError

# Cyrillic transliteration for role slugs. Canonical home (FU-14): the
# plugin's _slugify imports slugify() from here, so core and plugin
# produce one slug for the same name. The JS slugify() in
# project-setup.html.j2 must keep the same map (cross-check:
# tests/agent_workflow_ui/test_slugify.py).
_CYRILLIC_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}


def slugify(name: str) -> str:
    """Transliterate Cyrillic to Latin, then reduce to a filesystem slug.

    Lowercases, maps Cyrillic via ``_CYRILLIC_MAP``, replaces every
    remaining character outside ``[a-z0-9_-]`` with a dash (no collapse),
    strips edge dashes. Returns ``""`` when nothing usable remains.
    """
    lower = name.strip().lower()
    transliterated = "".join(_CYRILLIC_MAP.get(c, c) for c in lower)
    return re.sub(r"[^a-z0-9_-]", "-", transliterated).strip("-")


def slugify_role(name: str) -> str:
    """Normalize a role name to its saved file slug.

    Single normalization point for role names (AUD06-01): the pipeline
    stages, the config.yaml ``models.<role>`` keys, and the role-file
    lookup must all agree on the same slug, or runtime lookups miss.

    Custom roles are saved by the plugin via the same transliteration
    (FU-14: the plugin's ``_slugify`` delegates to :func:`slugify`), so
    both sides produce one slug — ``QA Лид`` → ``qa-lid`` everywhere.
    Falls back to the raw name when nothing usable remains (the pipeline
    loader warns about the mismatch).
    """
    slug = slugify(name)
    return slug or name.strip()


def require_agentic(project_dir: Path) -> Path:
    """Ensure .agentic/ exists. Raise AwfApiError if not.

    Returns the resolved .agentic/ Path on success.
    """
    agentic = paths.agentic_dir(project_dir)
    if not agentic.is_dir():
        raise AwfApiError(
            f"No .agentic/ found at {project_dir}. Run 'awf init' first."
        )
    return agentic


def require_awf_project(project_dir: Path) -> Path:
    """Strict check for WRITE entry points: .agentic/ AND config.yaml.

    ``require_agentic`` (bare .agentic/ existence) stays for read-only
    commands. A write entry point (dispatch_todo, create_baseline) must
    land in an initialized project: a bare .agentic/ without config.yaml
    is a stale directory (e.g. an old ~/.agentic under the MCP process
    cwd, the 2026-10-07 incident) — refuse loudly BEFORE any side effect
    instead of silently writing a unit into it.

    Returns the resolved (absolute) project_dir Path on success.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)
    config = paths.config_file(project_dir)
    if not config.is_file():
        raise AwfApiError(
            f".agentic/ found at {project_dir} but config.yaml is missing — "
            "not an initialized awf project (possibly a stale directory). "
            f"Run 'awf init' there, or pass the real project_dir "
            f"(the MCP default is the process cwd, usually not the project)."
        )
    return project_dir


def require_git_repo(project_dir: Path) -> None:
    """Ensure project is a git repo. Raise AwfApiError if not."""
    if not git_utils.is_git_repo(project_dir):
        raise AwfApiError(
            f"Not a git repository: {project_dir}. Run 'git init' first."
        )


def _surface_on_root_basis(surface: Path) -> Path:
    """Place a surface on the root's basis without resolving its leaf.

    ``refuse_unsafe_surfaces`` compares a RESOLVED root with the caller's
    own spelling of the surface. A relative project (``Path("proj")``) or an
    absolute one reached through a symlinked parent (``/…/link/proj``,
    ``link -> real``) used to read as "escapes managed root" — a false
    refusal on a project the baseline wrote (Q4/Q4b).

    Resolving the PARENT gives the canonical basis the root lives on.
    Resolving the leaf would follow the very symlink the walk must refuse,
    so the leaf stays as spelled and the no-follow ``lstat`` sees it.

    Q6 (виток 7): ``resolve()`` raises ``RuntimeError`` on a symlink loop
    (``pipelines -> pipelines``). That is the same unsafe path, not an
    unexpected traceback — it becomes the barrier's own refusal type.
    """
    absolute = surface if surface.is_absolute() else Path.cwd() / surface
    try:
        parent = absolute.parent.resolve()
    except (OSError, RuntimeError) as exc:
        raise managed_fs.ManagedPathError(
            f"cannot resolve managed path parent {absolute.parent}: {exc}"
        ) from exc
    return parent / absolute.name


def _raw_root_for(project_dir: Path) -> Path:
    """Project root exactly as the caller spelled it — no ``resolve()``."""
    raw = managed_fs.agentic_root_for(Path(project_dir) / ".agentic")
    return raw if raw.is_absolute() else Path.cwd() / raw


def _surface_on_raw_basis(surface: Path, *, raw_root: Path) -> Path:
    """Re-anchor the surface's OWN components on the raw project root.

    Q5 (виток 7): the containment check above resolves the surface's
    parent, so a symlink component INSIDE the project
    (``roles/draft -> roles``) resolves into the root and passes. That is
    fine for a writer (``managed_write_text`` walks the raw leaf), but
    ``refuse_unsafe_surfaces`` also guards DESTRUCTIVE ops that then work
    on the raw path (``unlink``/``shutil.move``) — the internal link sent
    them at a live file.

    The chain is taken from the surface's own spelling (no resolve) and
    re-anchored at the raw root, so the walk is purely lexical and every
    component the caller named — leaf included — is ``lstat``-ed. The
    project root's own spelling stays outside the walk (a symlinked parent
    above the project is not an escape — Q4b).
    """
    absolute = surface if surface.is_absolute() else Path.cwd() / surface
    anchor = managed_fs.agentic_root_for(absolute)
    try:
        chain = absolute.relative_to(anchor)
    except ValueError:
        chain = Path(absolute.name)
    return raw_root / chain


def refuse_unsafe_surfaces(
    project_dir: Path, operation: str, *surfaces: Path
) -> None:
    """No-follow preflight over the managed surfaces ONE operation touches.

    TODO-0233 (M3b): the same S9-01 class ``archive_todo`` closed in
    TODO-0227 — a symlinked ``.agentic``/``inbox``/``outbox``/``done``/
    ``context`` used to send ``shutil.move`` / ``unlink`` outside the
    project. The barrier runs before the first effect.

    The caller passes its OWN surface paths — the ones it reads, creates or
    moves into, LEAVES included (``done/<id>``, ``context/<trace>``,
    ``done/<id>/RETIRED-<ts>.md``). There is deliberately no union list
    here: REVIEW G1 — ``clear_stale_closures`` never touches ``done``, so
    checking it refused on a foreign surface and reported a misleading
    "unsafe done" for an operation that would not have gone near it. A
    mover that starts touching another surface must pass it in, otherwise
    the barrier does not cover it.

    One barrier for the whole API layer: ``dispatch_todo`` and the
    ``hygiene`` movers call THIS helper — a private copy in ``hygiene``
    imported by ``dispatch`` was a second implementation of the same
    invariant (REVIEW architect-review).

    TWO checks, both required (REVIEW виток 7):

    1. Containment on the root's canonical basis (Q4): the surface's
       PARENT resolves, the LEAF does not. A relative project or one
       reached through a symlinked parent stops reading as an escape.
    2. No-follow on the caller's RAW components from the RAW root (Q5):
       any symlink component — an internal one like
       ``roles/draft -> roles`` included — refuses. Writers would refuse
       anyway, but the destructive ops this helper guards work on the raw
       path after the preflight, so resolution alone is not enough.
    """
    raw_root = _raw_root_for(project_dir)
    root = managed_fs.agentic_root_for(paths.agentic_dir(project_dir))
    try:
        # H1 (TODO-0233, виток 4): the no-follow guarantee does not exist
        # off POSIX — the policy other managed barriers already follow
        # (``_wipe``/``_lease``). Refuse BEFORE any surface walk, so a
        # platform without lstat/O_NOFOLLOW never gets a weaker barrier.
        managed_fs.require_posix(operation)
        for surface in surfaces:
            # Raw first: an internal symlink (Q5) and a symlink loop (Q6)
            # are caught by ``lstat`` before any ``resolve()`` runs.
            managed_fs.assert_no_symlink_escape(
                _surface_on_raw_basis(surface, raw_root=raw_root), root=raw_root
            )
            managed_fs.assert_no_symlink_escape(
                _surface_on_root_basis(surface), root=root
            )
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(
            f"{operation} refused — unsafe managed path: {exc}"
        ) from exc


def managed_write_api_text(
    path: Path,
    content: str,
    *,
    operation: str,
    mode: int = 0o600,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> None:
    """Write a project file through the managed no-follow barrier.

    TODO-0234 (M3c): the API writers under ``.agentic`` (config,
    supervisor, roles, pipelines, plan, evidence, TODO edits) used the bare
    ``atomic_write_text`` — a symlinked ``.agentic`` or component sent the
    write outside the project. The barrier runs from the project root; its
    ``ManagedPathError`` becomes the API's own refusal type, like
    :func:`refuse_unsafe_surfaces`.

    The root is derived from the LEAF (:func:`managed_fs.agentic_root_for`)
    — ONE convention for the whole API layer, the same one
    :func:`refuse_unsafe_surfaces` uses. A separate ``project_dir`` argument
    was a second spelling of one root: ``paths.*`` resolves the project while
    a caller's own spelling may stay relative, so a correct path read as
    "escapes managed root" (the C4 class). Deriving both from the leaf makes
    the pair agree by construction; resolving the leaf instead is not an
    option — that would follow the very symlinks the walk must refuse.

    ``mode`` defaults to 0600 — generated state (config, supervisor, roles,
    pipelines, evidence, caches) is owner-only; REVIEW K4 accepted the flip
    over an existing 0644. The two artifacts the owner edits by hand — the
    task file and the plan — pass ``_atomic.MODE_OWNER_EDITABLE`` at their
    call sites, so every writer of the same artifact agrees on its mode.
    """
    try:
        managed_fs.managed_write_text(
            path,
            content,
            root=managed_fs.agentic_root_for(path),
            mode=mode,
            encoding=encoding,
            errors=errors,
        )
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(
            f"{operation} refused — unsafe managed path: {exc}"
        ) from exc


def read_file_text(path: Path, max_chars: int | None = None) -> str:
    """Read file as text, optionally truncated to ``max_chars``.

    Returns a placeholder string on OSError (never raises) — used by
    init_project which prefers degraded content over total failure.
    AUD06-17: non-UTF-8 bytes are replaced (errors="replace") instead of
    raising UnicodeDecodeError, so the "never raises" promise holds.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"(error reading {path.name}: {e})"
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"
    return text


def read_log_tail(log_file: Path, n: int) -> str | None:
    """Read last N lines of a log file. Returns None if file is absent.

    dogfood-11: consecutive duplicate lines are collapsed to ``line  (×N)`` —
    retry loops repeat the same warning verbatim and flood the tail that the
    supervisor reads.
    """
    if not log_file.is_file():
        return None
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines:
        return ""
    tail = lines[-n:] if len(lines) > n else lines
    collapsed: list[tuple[str, int]] = []
    for line in tail:
        if collapsed and collapsed[-1][0] == line:
            collapsed[-1] = (line, collapsed[-1][1] + 1)
        else:
            collapsed.append((line, 1))
    out = [f"{line}  (×{count})" if count > 1 else line for line, count in collapsed]
    return "\n".join(out)


__all__ = [
    "require_agentic",
    "require_awf_project",
    "require_git_repo",
    "refuse_unsafe_surfaces",
    "managed_write_api_text",
    "read_file_text",
    "read_log_tail",
]
