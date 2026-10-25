"""Path resolution for .agentic/ directories and the shared awf temp base."""
from __future__ import annotations

import tempfile
from pathlib import Path

#: SPEC-2: awf's throwaway WORKTREES and isolated git indexes live under
#: ``/tmp/opencode``, not the shared system temp. ONE source for every caller —
#: two literals drift the moment one of them moves. The remaining throwaway
#: file (publish's commit-msg) joins this policy with the unified temp module
#: in TODO-0243, not by a fourth per-call literal.
TMP_BASE = "/tmp/opencode"


def temp_base() -> Path:
    """The temp base, created on demand; the system temp when it cannot be.

    Best-effort by design: a scratch directory is a commodity — an
    environment where the base cannot be created must not fail the check
    that asked for it. The probe catches the case the ``mkdir`` above
    cannot: the base may already exist and be unwritable (another user's
    ``/tmp/opencode``) — ``exist_ok=True`` succeeds silently, and the first
    ``mkdtemp`` inside would raise. A throwaway probe directory proves the
    base is actually usable before it is returned.
    """
    base = Path(TMP_BASE)
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = Path(tempfile.mkdtemp(dir=base))
        probe.rmdir()
    except OSError:
        return Path(tempfile.gettempdir())
    return base


#: W7 (TODO-0231): the seeded stage-checkpoint template, `.agentic`-relative
#: (the anchor itself lives in :func:`agentic_dir`). The init seed, the
#: seed-pending preflight and the RUN-REPORT link all read this one literal —
#: three spellings of the same path are a dead link waiting for one of them
#: to move (architect TODO-0231).
CHECKPOINT_TEMPLATE = "docs/checkpoint-template.md"

#: The `.agentic`-relative SURFACE the init preflight names — the template's
#: own directory, derived so the two cannot disagree.
CHECKPOINT_SURFACE = CHECKPOINT_TEMPLATE.split("/", 1)[0]


def agentic_dir(project_dir: str | Path = ".") -> Path:
    """Return the .agentic/ directory for the given project."""
    return Path(project_dir).resolve() / ".agentic"


#: The .agentic/ RUNTIME subdirectories awf writes itself (signals, baselines,
#: logs, state, …). ONE source for three consumers that must not drift apart:
#: the init/force-init runtime wipe (``api._destructive.INIT_RUNTIME_DIRS``),
#: the .gitignore block (``api._templates.update_gitignore``) and the
#: "managed artifacts are not the user's dirt" check
#: (``dispatch_policy.MANAGED_RUNTIME_PREFIXES``). The workflow DEFINITIONS
#: (roles/pipelines/phases, config.yaml) are deliberately NOT here — they are
#: committed and belong to the user.
RUNTIME_DIRS: tuple[str, ...] = (
    "inbox",
    "outbox",
    "handoff",
    "done",
    "state",
    "logs",
    "context",
    "dashboards",
    "inputs",
    # F-B (review-fix 10): role DRAFTS are awf-written runtime (``api/roles``
    # add-role --draft, the service-run candidate). A live draft used to count
    # as the USER's dirt under require_clean and leak into the dirty tables;
    # one source keeps it out of all three consumers at once.
    "roles/draft",
)


def inbox(project_dir: str | Path = ".") -> Path:
    """Return .agentic/inbox/."""
    return agentic_dir(project_dir) / "inbox"


def outbox(project_dir: str | Path = ".") -> Path:
    """Return .agentic/outbox/."""
    return agentic_dir(project_dir) / "outbox"


def context_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/context/."""
    return agentic_dir(project_dir) / "context"


def done_dir(project_dir: str | Path = ".") -> Path:
    """DF6-1: Return .agentic/done/ — archive for completed TODOs."""
    return agentic_dir(project_dir) / "done"


def handoff_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/handoff/."""
    return agentic_dir(project_dir) / "handoff"


def config_file(project_dir: str | Path = ".") -> Path:
    """Return .agentic/config.yaml."""
    return agentic_dir(project_dir) / "config.yaml"


def roles_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/roles/."""
    return agentic_dir(project_dir) / "roles"


def checklists_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/checklists/ — the project's DoD waves (TODO-0244)."""
    return agentic_dir(project_dir) / "checklists"


def waves_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/checklists/waves/ — one file per DoD wave."""
    return checklists_dir(project_dir) / "waves"


def logs_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/logs/."""
    return agentic_dir(project_dir) / "logs"


# R1.1/S9-03: the worker log name used when the caller gives no explicit
# ``log_name``. Kept here so the writer (signal_watch) and the reset
# preflight (lifecycle) share one literal — the preflight must guard the
# same leaf the writer opens.
WORKER_FALLBACK_LOG = "worker-output.out"


def state_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/state/."""
    return agentic_dir(project_dir) / "state"


# R1.5 (roadmap R1, iter-3): the ``state/``-child paths get ONE owner here,
# so the StateStore package stops being the only place that spells them out
# (the divergent-convention risk: two sources of a path drift apart). The
# Store's ``_io`` now reads these; the guard test knows the same owners.
def state_control_file(project_dir: str | Path = ".") -> Path:
    """Return .agentic/state/control.json — the control snapshot."""
    return state_dir(project_dir) / "control.json"


def state_records_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/state/records/ — immutable digest-addressed records."""
    return state_dir(project_dir) / "records"


def state_operations_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/state/operations/ — the durable operation journal."""
    return state_dir(project_dir) / "operations"


def state_migration_manifest(project_dir: str | Path = ".") -> Path:
    """Return .agentic/state/migration-manifest.json."""
    return state_dir(project_dir) / "migration-manifest.json"


def pipelines_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/pipelines/."""
    return agentic_dir(project_dir) / "pipelines"


def phases_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/phases/."""
    return agentic_dir(project_dir) / "phases"


def dashboards_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/dashboards/."""
    return agentic_dir(project_dir) / "dashboards"


def inputs_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/inputs/."""
    return agentic_dir(project_dir) / "inputs"


# П2/П6: candidate filenames in priority order. The numbered prefix on
# PRODUCT-VISION matches the convention used in user's pet-projects
# (forces sort order at top of file listing).
_VISION_CANDIDATES = (
    "1. PRODUCT-VISION.md",
    "PRODUCT-VISION.md",
    "VISION.md",
    "vision.md",
    "README.md",
    "README.MD",
    "readme.md",
)


def find_vision_file(project_dir: str | Path = ".") -> Path | None:
    """П2/П6: locate the project's high-level vision/context document.

    Searches project root (NOT subdirs — keeps it fast and avoids noise
    from node_modules/README, etc).

    Returns the first match from ``_VISION_CANDIDATES`` or ``None``.
    Callers (cmd_init for plan.md stub, supervisor.build_prompt for
    context injection) use this to give LLM-supervisor a starting point
    instead of an empty plan.md / generic prompt.
    """
    root = Path(project_dir).resolve()
    for name in _VISION_CANDIDATES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None

