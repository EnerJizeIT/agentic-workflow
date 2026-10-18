"""R2.2 (roadmap R2): explicit dispatch — the dirty-source-tree policy.

The dispatch used to proceed on ANY working tree: it warned about a dirty
tree (TODO-0195/TODO-0204) but always reserved the TODO and took a baseline.
Roadmap R2.2 makes the choice EXPLICIT and versioned:

* a project declares a config version (``config_version`` in config.yaml).
  The current version (``CONFIG_VERSION``) defaults the policy to
  ``require_clean`` — a dispatch on a dirty user source tree is REFUSED,
  before any side effect (no TODO, no baseline file);
* ``dirty_policy=isolate`` is the conscious opt-out: the dispatch proceeds,
  snapshots the tree, and reports the excluded operations plus a dependency
  warning (what was left out and what may depend on it);
* a LEGACY project (no ``config_version``) keeps the pre-R2.2 behaviour
  (``warn``) — the default change never silently rewrites an existing
  project. The migration is explicit and announced (``migrated_legacy``).

The check reads the USER source tree through the R2.1 git model
(:mod:`awf.infra.git_model`), not the printed short status: tracked / staged
/ untracked byte-faithful paths, with the managed AWF runtime artifacts
(``.agentic/`` runtime dirs) documented as exceptions — those are written by
awf itself and must not count as the user's dirt.

This module is the POLICY and the CHECK. The transfer of a rejected unit's
code is a separate operation (:mod:`awf.claim_changes`) — the finding
carry-over (R5, ``include_untracked``/``carry_over_from``) keeps its own
semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import git_utils, paths
from ._log import log as _log

#: The current config schema version. A project writing this version gets
#: ``dirty_policy=require_clean`` unless it declares its own policy.
CONFIG_VERSION = 1

REQUIRE_CLEAN = "require_clean"
ISOLATE = "isolate"
WARN = "warn"

#: The pre-R2.2 behaviour, kept for legacy projects (no ``config_version``):
#: warn about a dirty tree and proceed. Never the default for a versioned
#: project — the roadmap retires the silent legacy path. An ALIAS of
#: :data:`WARN`: one behavior has one spelling, so the two can never drift.
LEGACY_POLICY = WARN

VALID_POLICIES = (REQUIRE_CLEAN, ISOLATE, WARN)

#: Managed AWF runtime artifacts — documented exceptions of the source-tree
#: check. These directories are written by awf itself (signals, baselines,
#: logs, state), so their being dirty is never the USER's uncommitted work.
#: Derived from :data:`awf.paths.RUNTIME_DIRS` — the single source shared with
#: the init wipe and the .gitignore block, so the three can never drift apart.
#: Relative POSIX prefixes, matched against the byte-decoded path.
MANAGED_RUNTIME_PREFIXES = tuple(f".agentic/{d}/" for d in paths.RUNTIME_DIRS)


def is_managed_runtime_path(path_bytes: bytes) -> bool:
    """True for a repo-relative path under a managed .agentic/ runtime dir.

    Byte input (the git model's raw path) so the check is byte-faithful — the
    same predicate serves the policy check, the dirty warning and the claim
    selection, instead of each re-spelling the managed prefixes.
    """
    text = path_bytes.decode("utf-8", "surrogateescape")
    return any(text.startswith(prefix) for prefix in MANAGED_RUNTIME_PREFIXES)


@dataclass(frozen=True)
class DirtyPolicyDecision:
    """The effective policy for a project plus how it was reached.

    ``migrated_legacy`` is True when the project carries no
    ``config_version``: the effective policy is :data:`LEGACY_POLICY` and
    ``warning`` explains that the default moved to ``require_clean`` — the
    project keeps its old behaviour until the owner opts in.
    """

    policy: str
    config_version: int
    migrated_legacy: bool = False
    warning: str = ""


def resolve_dirty_policy(config: dict) -> DirtyPolicyDecision:
    """The dirty-source-tree policy for the project's config.

    * ``config_version`` missing/older than :data:`CONFIG_VERSION` → legacy:
      policy :data:`LEGACY_POLICY`, ``migrated_legacy=True`` and an
      explicit warning. The default change never silently rewrites a
      project that predates it.
    * versioned → ``discipline.dirty_policy`` or, absent, the version default
      (:data:`REQUIRE_CLEAN`).
    """
    raw_version = config.get("config_version") if isinstance(config, dict) else None
    try:
        version = int(raw_version) if raw_version is not None else 0
    except (TypeError, ValueError):
        version = 0
    if version < CONFIG_VERSION:
        return DirtyPolicyDecision(
            policy=LEGACY_POLICY,
            config_version=version,
            migrated_legacy=True,
            warning=(
                "dirty_policy default changed to 'require_clean' since "
                f"config_version {CONFIG_VERSION}. This project has no "
                "config_version, so it keeps the pre-R2.2 behaviour ('warn': "
                "a dirty tree is warned about, not refused). To opt in, set "
                "config_version and discipline.dirty_policy in .agentic/"
                "config.yaml."
            ),
        )
    declared = ""
    if isinstance(config, dict):
        discipline = config.get("discipline")
        if isinstance(discipline, dict):
            value = discipline.get("dirty_policy")
            if isinstance(value, str):
                declared = value.strip()
    policy = declared if declared in VALID_POLICIES else REQUIRE_CLEAN
    return DirtyPolicyDecision(policy=policy, config_version=version)


@dataclass(frozen=True)
class SourceTreeReport:
    """The user source tree state at dispatch time (managed artifacts out).

    ``paths`` are the dirty user paths (display form, sorted); ``managed``
    are the managed AWF runtime paths that were excluded from the check (kept
    for diagnostics/tests — they are awf's own noise, never listed as the
    user's dirt); ``error`` is set when the tree cannot be read (a git
    failure, an unmerged index) — a tree that cannot be read is NOT clean.
    """

    paths: tuple[str, ...] = ()
    managed: tuple[str, ...] = ()
    error: str = ""

    @property
    def clean(self) -> bool:
        return not self.paths and not self.error


def check_source_tree(project_dir: Path) -> SourceTreeReport:
    """Read the dirty user source paths via the R2.1 git model.

    Tracked/staged changes and untracked non-ignored files, byte-faithful,
    minus the managed AWF runtime prefixes. Ignored files are the user's
    own choice and never count. Not a git repo → clean (nothing to check).
    A tree the model refuses (unmerged/sparse/split) → ``error``: it is
    definitely not a clean source tree.
    """
    from .infra.git_model import GitModelError, GitRepository, display_path

    project_dir = Path(project_dir)
    if not git_utils.is_git_repo(project_dir):
        return SourceTreeReport()
    try:
        repo = GitRepository(project_dir)
        tracked = [r.path for r in repo.changes() if r.kind != "untracked"]
        untracked = repo.untracked_paths()
    except GitModelError as exc:
        return SourceTreeReport(error=str(exc))

    dirty: list[str] = []
    managed: list[str] = []
    for raw in sorted({*tracked, *untracked}):
        (managed if is_managed_runtime_path(raw) else dirty).append(display_path(raw))
    return SourceTreeReport(paths=tuple(sorted(dirty)), managed=tuple(sorted(managed)))


def format_dirty_refusal(report: SourceTreeReport) -> str:
    """The refusal message for ``require_clean`` on a dirty tree.

    Names the offending paths and the way out (isolate the dirt, or claim
    the predecessor's unchanged blob via claim_changes). Refusal happens
    BEFORE the id is reserved, so it must also state that nothing was
    created. The paths render through :func:`escape_str_for_ui` (F3): a name
    with a non-UTF-8 byte must not break the strict-UTF-8 sink (JSON/CLI) the
    message is emitted through.
    """
    from .infra.git_model import escape_str_for_ui

    cap = 10
    shown = [escape_str_for_ui(p) for p in report.paths[:cap]]
    more = len(report.paths) - len(shown)
    tail = f" (…{more} more)" if more else ""
    if report.error:
        return (
            f"dispatch refused by dirty_policy=require_clean: the source tree "
            f"could not be read as clean ({report.error}). Nothing was "
            "reserved (no TODO, no baseline)."
        )
    return (
        f"dispatch refused by dirty_policy=require_clean: the source tree is "
        f"dirty ({len(report.paths)} path(s): {', '.join(shown)}{tail}). "
        "Commit or stash your work, set discipline.dirty_policy=isolate to "
        "dispatch anyway, or adopt what this unit owns: a predecessor's "
        "unchanged CODE with claim_changes, or an untracked FINDING with "
        "include_untracked / carry_over_from. "
        "Nothing was reserved (no TODO, no baseline)."
    )


def format_isolate_report(
    todo_id: str, report: SourceTreeReport
) -> tuple[str, str]:
    """The ``isolate`` artefacts for a dirty tree: (warning, dependency note).

    ``warning`` lists the dirty operations the unit will NOT own and the
    dependency note says what was excluded and that its dependents may not
    build at candidate time. ``("", "")`` on a clean tree. The listed paths
    render through :func:`escape_str_for_ui` (F3) — strict-UTF-8 encodable
    for ANY name bytes.

    A tree the model REFUSES (``report.error`` — unmerged/sparse/split) is NOT
    a known exclusion set: the artefact says so honestly and names the error,
    instead of the false "0 pre-existing path(s)" a bare ``paths`` count would
    print. The refusal formatter's invariant ("a tree that cannot be read is
    NOT clean") holds on this surface too — the isolate path proceeds by
    policy, but it must not claim it knows what it excluded.
    """
    from .infra.git_model import escape_str_for_ui

    if report.clean:
        return "", ""
    if report.error:
        warning = (
            "⚠ dirty_policy=isolate: the source tree could NOT be read "
            f"({report.error}) — the exclude set is UNKNOWN; the dispatch "
            "proceeds and snapshots whatever git can see."
        )
        dep = (
            "isolate dependency warning: the tree could not be read, so the "
            "pre-existing operations LEFT OUT of this unit are not known; any "
            "file that depends on uncommitted changes may fail to build in the "
            "unit's candidate. Resolve the tree state (e.g. the conflict) "
            "before relying on the isolation."
        )
        return warning, dep
    cap = 10
    shown = [escape_str_for_ui(p) for p in report.paths[:cap]]
    more = len(report.paths) - len(shown)
    tail = f" (…{more} more)" if more else ""
    warning = (
        f"ℹ dirty_policy=isolate: {len(report.paths)} pre-existing path(s) "
        f"stay OUTSIDE the {todo_id} unit ({', '.join(shown)}{tail}); the "
        "dispatch proceeds and snapshots the tree."
    )
    dep = (
        "isolate dependency warning: the excluded operations ("
        f"{', '.join(shown)}{tail}) are NOT part of this unit; any file that "
        "depends on them may fail to build in the unit's candidate. Reclaim a "
        "needed predecessor change with claim_changes, or include_untracked "
        "for an untracked file."
    )
    return warning, dep


def log_policy_decision(project_dir: Path, todo_id: str, decision: DirtyPolicyDecision) -> None:
    """Best-effort audit line — a policy decision must leave a trace."""
    try:
        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — dirty_policy={decision.policy} "
            f"(config_version={decision.config_version}"
            + (", migrated legacy" if decision.migrated_legacy else "")
            + ")",
        )
    except OSError:
        pass


__all__ = [
    "CONFIG_VERSION",
    "ISOLATE",
    "LEGACY_POLICY",
    "MANAGED_RUNTIME_PREFIXES",
    "REQUIRE_CLEAN",
    "VALID_POLICIES",
    "WARN",
    "DirtyPolicyDecision",
    "SourceTreeReport",
    "check_source_tree",
    "format_dirty_refusal",
    "format_isolate_report",
    "is_managed_runtime_path",
    "log_policy_decision",
    "resolve_dirty_policy",
]
