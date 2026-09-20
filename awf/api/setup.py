"""Project-setup materialization: apply form submit to awf project files.

Single source of truth for how form submit data (team, context,
instructions) becomes awf-managed files (config.yaml, pipeline.yaml,
supervisor.md). Plugin's submit handler calls :func:`apply_project_setup`
after global custom-role CRUD; plugin never duplicates awf schema knowledge.

Public entry point: :func:`apply_project_setup`.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from .._atomic import atomic_write_text
from ._helpers import require_agentic, slugify_role
from ._results import ApplyProjectSetupResult

log = logging.getLogger(__name__)


# Default agent that loads role .md as instruction. All non-supervisor
# roles run through this — opencode.json only has `worker` (plus
# optionally `reviewer`, `tester` defined per-project).
_DEFAULT_AGENT_FOR_ROLE = "worker"

# Roles that already have an agent_name in config.yaml or are special
# (supervisor = current session, not a subprocess).
_SKIP_ROLE_MAPPING = {"supervisor"}


# ─── Pipeline materialization ───────────────────────────────────────────


def _stage_yaml(name: str, role: str, **extra: Any) -> dict[str, Any]:
    """Build a stage dict for YAML serialization.

    BD-29: no ``action`` field — kind is computed from position by awf.
    """
    stage: dict[str, Any] = {"name": name, "role": role}
    stage.update(extra)
    return stage


# AUD06-01: the single normalization point lives in _helpers — pipeline
# stages, config.yaml models keys, and role-file lookups must all agree on
# the same slug. Kept as an alias for existing importers.
_slugify_role = slugify_role


def build_pipeline_stages(team_order: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build ordered pipeline stages from team selection.

    Wraps the user's team with supervisor at both ends
    (always plan → agents → verify).

    Args:
        team_order: list of team member dicts (from form's team_config JSON).
            Each dict has at least: ``{agent: <role_name>, type: 'default'|'custom', ...}``.

    Returns:
        List of stage dicts:
        ``[plan(supervisor)] + [team stages] + [verify(supervisor)]``.

    Raises:
        ValueError: if team_order is empty or has no valid role entries
            (empty pipeline would be plan→verify with no work — useless).
    """
    valid_roles = [
        _slugify_role(str(m.get("agent") or m.get("role") or ""))
        for m in team_order
    ]
    valid_roles = [r for r in valid_roles if r]
    if not valid_roles:
        raise ValueError(
            "Cannot build pipeline with zero agent roles — at least one "
            "role is required between supervisor plan and verify."
        )

    stages: list[dict[str, Any]] = [
        _stage_yaml(
            "plan",
            "supervisor",
            description="Supervisor studies the plan and creates a TODO",
        ),
    ]

    seen_roles: set[str] = set()
    for member in team_order:
        role = _slugify_role(str(member.get("agent") or member.get("role") or ""))
        if not role:
            continue
        if role in seen_roles:
            log.warning("Duplicate role %r in team_order — skipping", role)
            continue
        seen_roles.add(role)
        stages.append(
            _stage_yaml(
                name=role,
                role=role,
                description=f"{role} executes the TODO",
                on_blocked="escalate",
                # NEG-2 (dogfood-11): explicit safe policies — the implicit
                # default used to point at a non-existent 'implement' stage.
                on_rejected="escalate",
                on_failed="escalate",
                max_retries=3,
            )
        )

    stages.append(
        _stage_yaml(
            "verify",
            "supervisor",
            description="Supervisor verifies the result",
            on_approved="commit_and_next",
            on_rejected="replan",
        )
    )

    return stages


def _active_pipeline_name(project_dir: Path, *, warnings: list[str] | None = None) -> str:
    """AUD06-04: pipeline name the runtime will actually use.

    Mirrors ``resolve_pipeline_file``: ``default_pipeline`` from
    config.yaml wins over "default". The value becomes a file path, so it
    is validated the same way (AUD14-05) — an invalid name falls back to
    "default" with a warning instead of escaping .agentic/pipelines/.
    """
    from .. import config as cfg_mod

    declared = str(
        cfg_mod.get(cfg_mod.load(project_dir), "default_pipeline", "default")
        or "default"
    ).strip()
    if not declared or declared == "default":
        return "default"
    if declared in {".", ".."} or not re.fullmatch(r"[\w.-]+", declared):
        if warnings is not None:
            warnings.append(
                f"default_pipeline {declared!r} in config.yaml is not a valid "
                "pipeline name — the pipeline was written to default.yaml instead"
            )
        return "default"
    return declared


def write_pipeline(
    team_order: list[dict[str, Any]],
    project_dir: Path,
    *,
    warnings: list[str] | None = None,
) -> Path | None:
    """Write the active pipeline file from team selection.

    AUD06-04: writes the file the runtime will actually use — the
    ``default_pipeline`` from config.yaml when it declares a custom one
    (validated), otherwise ``default.yaml``. Writing only default.yaml
    used to silently ignore the form's team in projects with a custom
    default_pipeline (repro S9).

    Args:
        team_order: form team_config list (ordered).
        project_dir: absolute path to awf project.
        warnings: optional list to collect config problems (invalid
            default_pipeline name).

    Returns:
        Path to written file. Backups the existing file to .bak.
    """
    pipelines_dir = project_dir / ".agentic" / "pipelines"
    pipelines_dir.mkdir(parents=True, exist_ok=True)

    stages = build_pipeline_stages(team_order)
    name = _active_pipeline_name(project_dir, warnings=warnings)
    pipeline_dict: dict[str, Any] = {
        "name": name,
        "description": "Generated by awf.api from project-setup form",
        "stages": stages,
    }

    target = pipelines_dir / f"{name}.yaml"
    if target.exists():
        backup = target.with_name(f"{target.name}.bak")
        try:
            target.rename(backup)
            log.info("Backed up existing pipeline to %s", backup)
        except OSError:
            pass

    content = yaml.safe_dump(
        pipeline_dict, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    atomic_write_text(target, content)
    log.info("Wrote pipeline with %d team stages to %s", len(stages) - 2, target)
    return target


# ─── Config.yaml role mapping (BD-12 + BD-32) ───────────────────────────


def update_config_role_mapping(
    team: list[dict[str, Any]],
    project_dir: Path,
    *,
    warnings: list[str] | None = None,
) -> bool:
    """BD-12: ensure each team role has ``models.<role>.agent_name`` in config.yaml.

    For each role in team that is not supervisor and has no agent_name,
    set ``agent_name: "worker"`` (the default agent that loads role .md
    as instruction). Existing agent_name values are preserved. Also
    preserves any sibling keys (model, temperature, description).

    BD-32: preserves model selection from form (was being dropped):
    - ``member["model"]`` missing → leave existing model
    - ``member["model"] = ""`` (user cleared) → delete existing model
    - ``member["model"] = "x"`` (user picked) → set/replace model

    Args:
        team: list of team member dicts with ``role`` key.
        project_dir: awf project root (must contain .agentic/config.yaml).

    Returns:
        True if config.yaml was updated, False otherwise.
    """
    config_path = project_dir / ".agentic" / "config.yaml"
    if not config_path.is_file():
        log.warning("BD-12: no config.yaml at %s — skip role mapping", config_path)
        return False

    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        # AUD06-02: non-UTF-8 bytes and read errors used to escape as
        # UnicodeDecodeError/OSError and crash the whole submit.
        log.error("BD-12: cannot read config.yaml: %s — skip role mapping", e)
        if warnings is not None:
            warnings.append(
                f"config.yaml: unreadable ({type(e).__name__}) — role mapping skipped"
            )
        return False
    if not isinstance(config, dict):
        log.error("BD-12: config.yaml root is not a mapping — skip")
        if warnings is not None:
            warnings.append("config.yaml: root is not a mapping — role mapping skipped")
        return False

    models = config.setdefault("models", {})
    if not isinstance(models, dict):
        log.error("BD-12: config.yaml 'models' is not a mapping — skip")
        return False

    changed = False
    for member in team:
        if not isinstance(member, dict):
            continue
        role = str(member.get("role") or member.get("agent") or "").strip()
        if not role or role in _SKIP_ROLE_MAPPING:
            continue
        # AUD06-01: key models.<role> with the SAME slug the pipeline uses
        # (build_pipeline_stages slugifies) — the runtime reads
        # models.<slug>.agent_name / .model, so a raw-name key would be a
        # silent lookup miss (BD-12/BD-32 never applied).
        role = slugify_role(role)
        entry = models.get(role)
        if not isinstance(entry, dict):
            entry = {}
            models[role] = entry
        if not entry.get("agent_name"):
            entry["agent_name"] = _DEFAULT_AGENT_FOR_ROLE
            changed = True
            log.info("BD-12: mapped role %r -> agent_name=%s", role, _DEFAULT_AGENT_FOR_ROLE)
        if "model" in member:
            member_model = str(member.get("model") or "").strip()
            if member_model:
                if entry.get("model") != member_model:
                    entry["model"] = member_model
                    changed = True
                    log.info("BD-32: role %r -> model=%s", role, member_model)
            elif "model" in entry:
                del entry["model"]
                changed = True
                log.info("BD-32: role %r -> model cleared", role)

    if not changed:
        return False

    # P3: keep latest backup as .bak + timestamped copy for history
    backup = config_path.with_suffix(".yaml.bak")
    try:
        old_content = config_path.read_text(encoding="utf-8")
        backup.write_text(old_content, encoding="utf-8")
        # Also write timestamped backup (keep last 3)
        from datetime import datetime as _dt
        ts_backup = config_path.with_name(
            f"config.yaml.{_dt.now().strftime('%Y%m%d%H%M%S')}.bak"
        )
        ts_backup.write_text(old_content, encoding="utf-8")
        old_baks = sorted(config_path.parent.glob("config.yaml.*.bak"))
        for old in old_baks[:-3]:
            old.unlink(missing_ok=True)
    except OSError as e:
        log.warning("BD-12: backup failed: %s", e)

    atomic_write_text(
        config_path,
        yaml.safe_dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False),
    )
    log.info("BD-12: config.yaml updated with role mappings")
    return True


# ─── Context + supervisor.md patches (UI-2/UI-3) ────────────────────────


# AUD06-08: machine-readable sentinels delimit the UI-2/UI-3 sections.
# The old patterns closed the section at the first '## ' heading, so user
# content containing a markdown heading left orphaned lines on re-submit
# (repro S4). HTML comments cannot collide with user markdown.
_UI2_SENTINEL_PATTERN = re.compile(
    r"\n*<!-- awf:ui2:start -->.*?<!-- awf:ui2:end -->[ \t]*\n?",
    re.DOTALL,
)
_UI3_SENTINEL_PATTERN = re.compile(
    r"\n*<!-- awf:ui3:start -->.*?<!-- awf:ui3:end -->[ \t]*\n?",
    re.DOTALL,
)

# Back-compat: files written before the sentinels carry bare '## ' headings.
# A legacy section runs to the next awf-managed heading (or end of file), so
# user content with '## ' headings inside it is stripped whole.
_AWF_SECTION_HEADINGS = (
    r"\n## Project context \(from user, UI-2\)"
    r"|\n## Additional supervisor instructions \(from user, UI-3\)"
)
_UI2_LEGACY_PATTERN = re.compile(
    r"\n*## Project context \(from user, UI-2\).*?(?=" + _AWF_SECTION_HEADINGS + r"|\Z)",
    re.DOTALL,
)
_UI3_LEGACY_PATTERN = re.compile(
    r"\n*## Additional supervisor instructions \(from user, UI-3\).*?(?=" + _AWF_SECTION_HEADINGS + r"|\Z)",
    re.DOTALL,
)


def save_context_and_instructions(
    project_dir: Path,
    *,
    context_message: str = "",
    supervisor_instructions: str = "",
    warnings: list[str] | None = None,
) -> bool:
    """UI-2/UI-3: persist ``context_message`` + ``supervisor_instructions``.

    Persists to:
    1. ``config.yaml`` — machine-readable (``context.message``,
       ``supervisor.instructions``).
    2. ``supervisor.md`` — appended as markdown sections (supervisor LLM
       sees them via ``--file``).

    Both fields are OPTIONAL — if empty, no changes made.

    Idempotent: re-submitting with new content replaces existing UI-2/UI-3
    sections in supervisor.md — the sentinel-delimited blocks (and the
    legacy bare-heading sections from files written before AUD06-08) are
    stripped before re-appending.

    Returns:
        True if any file was changed, False otherwise.
    """
    context_message = context_message.strip()
    supervisor_instructions = supervisor_instructions.strip()

    if not context_message and not supervisor_instructions:
        return False

    config_path = project_dir / ".agentic" / "config.yaml"
    if config_path.exists():
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
            # AUD06-02: unreadable config must degrade (warning), not crash
            # the submit; AUD06-03: never overwrite a config we cannot parse
            # — that would wipe project/models/phases with one section.
            log.error(
                "UI-2/UI-3: cannot read config.yaml: %s — skipping save", e
            )
            if warnings is not None:
                warnings.append(
                    f"config.yaml: unreadable ({type(e).__name__}) — "
                    "context/instructions save skipped, file left untouched"
                )
            return False
        if config is None:
            config = {}  # empty file — nothing to lose
        if not isinstance(config, dict):
            log.error(
                "UI-2/UI-3: config.yaml root is not a mapping — skipping save"
            )
            if warnings is not None:
                warnings.append(
                    "config.yaml: root is not a mapping — "
                    "context/instructions save skipped, file left untouched"
                )
            return False
    else:
        config = {}

    config_changed = False
    if context_message:
        ctx_section = config.setdefault("context", {})
        if not isinstance(ctx_section, dict):
            ctx_section = {}
            config["context"] = ctx_section
        ctx_section["message"] = context_message
        config_changed = True
        log.info("UI-2: saved context.message to config.yaml (%d chars)", len(context_message))

    if supervisor_instructions:
        sup_section = config.setdefault("supervisor", {})
        if not isinstance(sup_section, dict):
            sup_section = {}
            config["supervisor"] = sup_section
        sup_section["instructions"] = supervisor_instructions
        config_changed = True
        log.info("UI-3: saved supervisor.instructions to config.yaml (%d chars)", len(supervisor_instructions))

    if config_changed:
        atomic_write_text(
            config_path,
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        )

    # 2. Patch supervisor.md (idempotent via UI-2/UI-3 marker sections)
    supervisor_md = project_dir / ".agentic" / "roles" / "supervisor.md"
    if not supervisor_md.exists():
        log.warning("UI-2/UI-3: supervisor.md not found at %s — skipping append", supervisor_md)
        return config_changed

    try:
        current = supervisor_md.read_text(encoding="utf-8")
    except OSError as e:
        log.error("UI-2/UI-3: failed to read supervisor.md: %s", e)
        return config_changed

    # Strip existing UI-2/UI-3 sections (idempotent — re-submit replaces):
    # sentinel-delimited blocks first (new format), then the legacy
    # bare-heading sections (files written before AUD06-08).
    current = _UI2_SENTINEL_PATTERN.sub("", current)
    current = _UI3_SENTINEL_PATTERN.sub("", current)
    current = _UI2_LEGACY_PATTERN.sub("", current)
    current = _UI3_LEGACY_PATTERN.sub("", current)
    current = current.rstrip()

    sections_to_add: list[str] = []
    if context_message:
        sections_to_add.append(
            "\n\n<!-- awf:ui2:start -->\n"
            "## Project context (from user, UI-2)\n\n"
            f"{context_message}\n"
            "<!-- awf:ui2:end -->\n"
        )
    if supervisor_instructions:
        sections_to_add.append(
            "\n\n<!-- awf:ui3:start -->\n"
            "## Additional supervisor instructions (from user, UI-3)\n\n"
            f"{supervisor_instructions}\n"
            "<!-- awf:ui3:end -->\n"
        )

    if sections_to_add:
        new_content = current + "".join(sections_to_add)
        atomic_write_text(supervisor_md, new_content)
        log.info("UI-2/UI-3: appended %d section(s) to supervisor.md", len(sections_to_add))

    return True


def _unresolved_team_roles(team: list[dict[str, Any]], project_dir: Path) -> list[str]:
    """AUD06-15: team roles with no role file (project → global fallback).

    Mirrors ``resolve_role_file`` (awf/supervisor.py) resolution order so
    the check agrees with the runtime. A role that resolves nowhere would
    otherwise crash its first stage with RuntimeError from resolve_role_file.
    """
    from ..supervisor import global_roles_dir  # lazy — avoids import cycle

    roles_dir = project_dir / ".agentic" / "roles"
    gdir = global_roles_dir()
    seen: set[str] = set()
    missing: list[str] = []
    for member in team:
        if not isinstance(member, dict):
            continue
        raw = str(member.get("role") or member.get("agent") or "").strip()
        if not raw or raw in _SKIP_ROLE_MAPPING:
            continue
        role = slugify_role(raw)
        if role in seen:
            continue
        seen.add(role)
        if (roles_dir / f"{role}.md").is_file() or (gdir / f"{role}.md").is_file():
            continue
        missing.append(role)
    return missing


# ─── Public entry point ─────────────────────────────────────────────────


def apply_project_setup(
    project_dir: Path,
    *,
    team: list[dict[str, Any]],
    context_message: str = "",
    supervisor_instructions: str = "",
) -> ApplyProjectSetupResult:
    """Apply a project-setup form submit to awf project files.

    Single source of truth for materializing user's team/context/instructions
    choices into awf-managed files. Called by the plugin's submit handler
    after global custom-role CRUD (which stays in the plugin — UI persistence
    is not awf's concern).

    Side effects (all idempotent):
    1. Writes the active pipeline file from team order — the
       ``default_pipeline`` from config.yaml when it declares a valid
       custom name, otherwise ``default.yaml`` (AUD06-04). Existing file
       backed up to ``.bak``.
    2. Patches ``.agentic/config.yaml``:
        - ``models.<role>.agent_name = "worker"`` for each non-supervisor
          team role (BD-12), keyed by role slug (AUD06-01).
        - ``models.<role>.model`` from member.model (BD-32).
        - ``context.message`` (UI-2) and ``supervisor.instructions`` (UI-3).
    3. Patches ``.agentic/roles/supervisor.md`` — appends/replaces
        UI-2 and UI-3 sections (sentinel-delimited, AUD06-08).
    4. Best-effort: refreshes the BD-31 disambiguation addenda in ALL
        team role files (``analyze_roles_core``) — a rebuilt pipeline
        shifts stage positions, stale addenda would mislead the workers.
    5. Best-effort: advances the phase ``form`` → ``normalize`` when the
        materialized pipeline satisfies the transition pre-check.

    Args:
        project_dir: awf project root (must contain .agentic/).
        team: list of team member dicts from form team_config.
        context_message: optional free-text project context (UI-2).
        supervisor_instructions: optional additional supervisor guidance (UI-3).

    Returns:
        ApplyProjectSetupResult with paths written + flags.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    warnings: list[str] = []
    pipeline_path: Path | None = None

    if team:
        try:
            pipeline_path = write_pipeline(team, project_dir, warnings=warnings)
        except ValueError as e:
            warnings.append(f"pipeline: {e}")
            log.warning("apply_project_setup: %s", e)

        config_updated = update_config_role_mapping(
            team, project_dir, warnings=warnings
        )
        # AUD06-15: report team roles that resolve to no role file — a typo
        # used to surface only at runtime (RuntimeError from
        # resolve_role_file at stage spawn: retries, salvage, crash).
        for role in _unresolved_team_roles(team, project_dir):
            warnings.append(
                f"role '{role}' has no role file (checked .agentic/roles/ and "
                "the global awf roles dir) — its stage will fail at runtime; "
                f"create .agentic/roles/{role}.md or fix the team"
            )
    else:
        config_updated = False
        # AUD06-12: explain the consequence, not just the skipped steps —
        # without a pipeline the phase cannot leave 'form' (detect_phase:
        # goal set + no pipeline → form), and the user needs to know why.
        warnings.append(
            "team is empty — no pipeline was written and no role mapping "
            "done; the phase stays 'form' until a submit with at least one "
            "role creates the pipeline"
        )

    sup_md_updated = save_context_and_instructions(
        project_dir,
        context_message=context_message,
        supervisor_instructions=supervisor_instructions,
        warnings=warnings,
    )

    # D9 (topic-trainer spec): a rebuilt pipeline shifts stage positions, but
    # the BD-31 disambiguation addenda in role files still say "position 2/3".
    # Refresh them right after materialization — idempotent, best-effort.
    if pipeline_path:
        try:
            from .roles import analyze_roles_core

            analyze_roles_core(project_dir, dry_run=False)
        except Exception as e:  # noqa: BLE001 — analysis must not fail setup
            log.warning("apply_project_setup: role disambiguation refresh skipped: %s", e)

    # SMO: advance phase form→normalize after successful materialization.
    # Dogfood #4: supervisor had to call confirm_normalized TWICE because
    # form submission didn't advance the phase.
    if pipeline_path:
        try:
            from ..phase import advance_phase, detect_phase
            if detect_phase(project_dir) == "form":
                # AUD02-05: declare the documented transition (form →
                # normalize); the pre-check above makes a refusal impossible
                # except in a race.
                advance_phase(project_dir, from_phase="form", setup_done=True)
        except Exception:
            pass  # phase advance is best-effort, not critical

    return ApplyProjectSetupResult(
        pipeline_file=str(pipeline_path) if pipeline_path else None,
        config_updated=config_updated or bool(context_message) or bool(supervisor_instructions),
        supervisor_md_updated=sup_md_updated,
        warnings=warnings,
    )


__all__ = [
    "apply_project_setup",
    "write_pipeline",
    "build_pipeline_stages",
    "update_config_role_mapping",
    "save_context_and_instructions",
]
