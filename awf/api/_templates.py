"""Templates used by :func:`awf.api.init_project` and :func:`awf.api.add_role`.

Single quotes used for YAML command values so that commands containing
double quotes (e.g. ``pytest -k "not slow"``) don't break YAML parsing.
"""
from __future__ import annotations

from pathlib import Path

from .. import paths
from ._helpers import is_slug_name

_CONFIG_TEMPLATE = '''project:
  name: '{project_name}'
  root: "."

# R2.2 (roadmap R2): the config schema version. A project WITHOUT this key is
# a legacy project and keeps the pre-R2.2 dirty-tree behaviour ('warn').
config_version: {config_version}

discipline:
  # The dirty-source-tree policy for a dispatch. The VERSION default (when
  # this key is absent) is 'require_clean' — a dirty user source tree is
  # refused before anything is reserved (no TODO, no baseline). A freshly
  # initialized project legitimately holds uncommitted AWF skeleton
  # (config.yaml, roles, phases, .gitignore), so init seeds the pre-R2.2-
  # compatible 'isolate' discipline instead: dispatch proceeds and reports
  # the excluded operations. Owners who want the strict discipline set
  # 'require_clean' here (or drop this key and keep config_version).
  #   require_clean — refuse a dirty source tree (no TODO, no baseline);
  #   isolate       — dispatch anyway: snapshot + excluded table;
  #   warn          — the legacy behaviour (warn and proceed).
  dirty_policy: 'isolate'

models:
  supervisor:
    description: "Current session model"
# Agent roles (system-analyst, developer, qa, project-auditor, ...) are added
# dynamically by the project-setup form on first submit (BD-12 auto-maps each
# role to agent_name="worker"). Don't pre-populate here.

verification:
  test_cmd: '{test_cmd}'
  lint_cmd: '{lint_cmd}'
  typecheck_cmd: '{typecheck_cmd}'
  build_cmd: '{build_cmd}'

engine:
  # Engine liveness (TODO-0241/0242; docs/design.md, USAGE.md). The interval
  # is clamped to at most 30s (a larger value warns); the staleness threshold
  # defaults to max(3 x heartbeat_seconds, 120) unless set explicitly.
  heartbeat_seconds: 30
  heartbeat_stale_seconds: 120
  # true = the engine dies with its launcher. false is the durable-run
  # default: an opencode/MCP restart must not kill a running stage.
  kill_with_parent: false

limits:
  # TODO-0262: how many `## REVIEW-фикс` rounds a unit may take (the sections
  # the supervisor appends). At the cap the engine stops the unit at verify
  # entry, awf continue refuses the implementer stage and awf run_next stops
  # the run; outbox/BUDGET-<id>.md names the price. 0 disables the limit.
  # Valid range: 0..20.
  max_review_passes: 3

checklist:
  # CLOSER IMP4: how many COMPLETED closer runs one unit may spend while its
  # definition-of-done gate is still red. A crash without a verdict, a
  # salvaged signal and a skip do not count. At the cap the unit is BLOCKED
  # for the supervisor (no third run). Valid range: 1..2.
  closer_attempts: 1

phases:
  current: ".agentic/phases/plan.md"

default_pipeline: "default"
'''
# AUD06-09: coverage_cmd / retry.max_attempts / retry.backoff_seconds removed
# from the template — zero readers anywhere in the repo (the engine's retry
# budgets are per-stage max_retries/max_rollbacks in pipeline.yaml). Keys
# nobody reads advertised configurability that did nothing.


_ROLE_TEMPLATE = """# ROLE: {role_name}

**Role:** {description}
**Runs as:** `opencode run --agent {role_name}`
**Модель:** {model}

## 1. Who you are

<describe the responsibility of this role>

## 2. Input

This role receives:
- <what comes from the previous stage>

## 3. Actions

<describe what to do with the input>

## 4. Output

When finished, create one of:
- `.agentic/outbox/DONE-NNNN.md` + `.ready` — success
- `.agentic/outbox/BLOCKED-NNNN.md` + `.ready` — needs supervisor

## 5. Prohibitions

<list what this role must NOT do>
"""


def update_gitignore(project_dir: Path) -> None:
    """Idempotent: add .agentic runtime dirs to .gitignore.

    Two checks: first for the main runtime block (inbox/outbox/context/...),
    second for the plugin's inputs/dashboards dirs. Avoids duplicating either.
    """
    # AUD06-14: the .bak patterns are anchored under .agentic/ — an unanchored
    # '*.bak' made git untrack the USER's backup files repo-wide after init
    # (e.g. a Rails project's config.ru.bak at the root).
    # The runtime-dir lines come from the ONE list (paths.RUNTIME_DIRS), the
    # same source the init wipe and the dirty-policy check read.
    gitignore_block = (
        "".join(f".agentic/{d}/\n" for d in paths.RUNTIME_DIRS)
        + ".agentic/**/*.bak\n.agentic/**/*.bak-*\n"
    )
    gitignore = project_dir / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if ".agentic/inbox/" not in content:
            gitignore.write_text(
                content + "\n# Agentic workflow runtime files\n" + gitignore_block + "\n",
                encoding="utf-8",
            )
        elif ".agentic/handoff/" not in content:
            gitignore.write_text(
                content + "\n# Agentic workflow runtime (updated)\n"
                ".agentic/handoff/\n.agentic/state/\n"
                ".agentic/**/*.bak\n.agentic/**/*.bak-*\n",
                encoding="utf-8",
            )
        elif ".agentic/inputs/" not in content:
            gitignore.write_text(
                content + "\n# agent-workflow-ui runtime\n.agentic/inputs/\n.agentic/dashboards/\n",
                encoding="utf-8",
            )
    else:
        gitignore.write_text(
            "# Agentic workflow runtime files\n" + gitignore_block + "\n",
            encoding="utf-8",
        )


def awf_templates_dir() -> Path:
    """Root of the templates the awf package ships — the canonical builder.

    Callers that need the package templates ask for this path instead of
    spelling ``<package>/templates`` themselves. It is not a claim about
    every module: a few sites (dashboard HTML, supervisor phases, builtin
    doctrine) still resolve their own subdirectory, and this function does
    not migrate them.

    TODO-0230 (Y2): the repo-root ``templates/`` symlink is gone. A
    checkout and a wheel both read ``<awf package>/templates`` next to
    ``__file__``; there is no second tree to keep in sync.
    """
    return Path(__file__).resolve().parent.parent / "templates"


def package_role_template(role_name: str) -> Path | None:
    """The role file the awf package ships for *role_name*, or None.

    One spelling of the package templates path for every caller: add_role's
    built-in fallback (which narrows the names it accepts) and the setup
    flow-variant materialization (any stage role). The file is package data
    (``pyproject`` ``templates/**/*``), so a wheel and a checkout agree.

    REVIEW W4: ``role_name`` is fail-closed — a name that is not a slug
    (absolute path, ``..``) yields None instead of resolving outside the
    templates dir. Callers are trusted today; the guard keeps a future one
    from turning a repo-controlled name into a read primitive.
    """
    if not is_slug_name(role_name):
        return None
    candidate = awf_templates_dir() / "roles" / f"{role_name}.md"
    return candidate if candidate.is_file() else None


__all__ = [
    "_CONFIG_TEMPLATE",
    "_ROLE_TEMPLATE",
    "package_role_template",
    "awf_templates_dir",
    "update_gitignore",
]
