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

from .. import config as cfg_mod
from ..config_write import patch_config_values, patch_mapping
from ..pipeline import active_pipeline_name, validate_pipeline_document
from ._errors import AwfApiError
from ._helpers import managed_write_api_text, refuse_unsafe_surfaces, require_agentic, slugify_role
from ._results import ApplyProjectSetupResult, WritePipelineResult
from ._templates import awf_templates_dir, package_role_template
from .pipelines import dump_pipeline_document
from .pipelines import write_pipeline as _write_named_pipeline
from .roles import materialize_builtin_role_content

log = logging.getLogger(__name__)


# Default agent that loads role .md as instruction. All non-supervisor
# roles run through this — opencode.json only has `worker` (plus
# optionally `reviewer`, `tester` defined per-project).
_DEFAULT_AGENT_FOR_ROLE = "worker"

# Roles that already have an agent_name in config.yaml or are special
# (supervisor = current session, not a subprocess).
_SKIP_ROLE_MAPPING = {"supervisor"}

# TODO-0173 (quick tier): the QA role stages dropped from the quick
# pipeline. The quick tier is for S-class units (docs, shield tests,
# counters, one-line fixes) — the full QA stage there is pure overhead.
_QA_ROLE_SLUGS = frozenset({"agent-qa-review"})

# Fixed file name of the quick variant: .agentic/pipelines/quick.yaml.
# It is an ALTERNATE pipeline (selected per unit via the TODO's
# `pipeline: quick` or a run-queue item) — never the active one.
_QUICK_PIPELINE_NAME = "quick"


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

# CLOSER IMP7 (R7): the definition-of-done stage every composed pipeline
# carries — except the quick tier (IMP7/TODO-0290: quick stays short). The
# canonical contract is docs/spec/closer-stage K6; the local
# .agentic/pipelines/default.yaml and the shipped templates hold the same
# block, so the generator is the third surface of one composition.
_CLOSER_STAGE_NAME = "checklist-close"
_CLOSER_STAGE_TASK = (
    "Закрой ТОЛЬКО пункты `## Чек-лист завершения` с пробами (`missing`); "
    "`manual` вне scope, блок не править; не меняй скоуп и контракт; "
    "запусти `awf checklist check <todo>`; отчитайся пунктами."
)
# TODO-0304: the follow-up check right after the close stage. It reviews
# ONLY the closer's diff and is skipped when the closer did not run.
_CHECK_STAGE_NAME = "checklist-check"
_CHECK_STAGE_TASK = (
    "Проверь ТОЛЬКО дифф checklist-close: изменённые им файлы и целевые "
    "тесты, без полного круга; находки — обычным `## REVIEW-фикс`."
)
# R7: the closer runs after the last stage that may write the unit. Roles
# below only report findings (no code edits), so they stay after it.
# TODO-0304: the follow-up check runs right after the closer, before it.
_POST_CLOSER_ROLES = frozenset({"agent-project-auditor"})


def _closer_stage() -> dict[str, Any]:
    """The closer stage block — one builder for the generator path."""
    return _stage_yaml(
        _CLOSER_STAGE_NAME,
        "agent-implementer",
        id=_CLOSER_STAGE_NAME,
        task=_CLOSER_STAGE_TASK,
        # IMP5 (TODO-0304): the close continues the implementer's session.
        session_key="agent-implementer",
        skip_if="checklist_green",
        write_check="off",
    )


def _check_stage() -> dict[str, Any]:
    """TODO-0304: the follow-up check of the closer's diff."""
    return _stage_yaml(
        _CHECK_STAGE_NAME,
        "agent-qa-review",
        id=_CHECK_STAGE_NAME,
        task=_CHECK_STAGE_TASK,
        # IMP5 (TODO-0304): the check continues the QA session.
        session_key="agent-qa-review",
        skip_if="checklist_close_skipped",
    )


#: TODO-0230 (M6): built-in pipeline starters shipped with awf. The form's
#: "flow variant" choice picks one instead of the generated team pipeline.
#: REVIEW W5: this mapping is the ONE registry — the CLI ``--variant``
#: choices and the form radio (through ``flow_variants``) read it, so a new
#: starter appears everywhere without a second hardcoded list. The value is
#: the human label the form shows.
BUILTIN_PIPELINES = {
    "full": "Полная проверка: implementer → architect → QA → auditor",
    "code-health": "Оздоровление кода: architect → refactoring → verify",
    "spec": "Пакет спецификации: шесть авторских стадий → готовность",
}

#: The spec template names its package directory with this token; the
#: ``awf spec init <slug>`` entry substitutes a concrete scope. The
#: template itself never names a package — one template serves every slug.
SPEC_SLUG_TOKEN = "{{spec_slug}}"
#: Scope used when the spec variant is installed WITHOUT a slug (the setup
#: form / ``awf pipeline-write --variant spec``). A named package is
#: generated by ``awf spec init <slug>``.
DEFAULT_SPEC_SLUG = "spec"
#: A spec slug is one path segment and becomes part of the pipeline name.
_SPEC_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _builtin_pipeline_path(name: str) -> Path:
    """Where a shipped pipeline starter lives (one layout fact, one place)."""
    return awf_templates_dir() / "pipelines" / f"{name}.yaml"


def _builtin_pipeline_template(name: str) -> Path | None:
    """Ship-with-awf pipeline starter, or None for an unknown name."""
    if name not in BUILTIN_PIPELINES:
        return None
    path = _builtin_pipeline_path(name)
    return path if path.is_file() else None


def _require_builtin_pipeline(name: str) -> Path:
    """The template file of a known variant — ValueError otherwise.

    REVIEW R3 (TODO-0230): "there is no such variant" and "the variant is
    known but its installed template is missing" are different problems
    for the user — a typo versus a broken installation. One message for
    both sent people looking for a name that was already right.
    """
    if name not in BUILTIN_PIPELINES:
        raise ValueError(
            f"Unknown flow variant {name!r}; available: "
            + ", ".join(BUILTIN_PIPELINES)
        )
    template = _builtin_pipeline_template(name)
    if template is None:
        expected = _builtin_pipeline_path(name)
        raise ValueError(
            f"Flow variant {name!r} is known but its template is not "
            f"installed ({expected}) — reinstall awf or pick one of: "
            + ", ".join(BUILTIN_PIPELINES)
        )
    return template


def _builtin_pipeline_stages(template: Path) -> list[dict[str, Any]]:
    """Stages of a shipped template — checked by the SHARED document validator.

    ``validate_pipeline_document`` is the same rule set the loader and every
    other write path use (A-06: unknown keys, empty ids, bad policies).
    A second, softer check here would let a malformed template reach disk and
    fail only when the pipeline starts. The error type stays ``ValueError``:
    ``apply_project_setup`` reports it as a warning.
    """
    try:
        data = yaml.safe_load(template.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ValueError(f"Cannot read pipeline template {template}: {e}") from e
    try:
        return validate_pipeline_document(data, template.name)
    except AwfApiError as e:
        raise ValueError(str(e)) from e


def _builtin_pipeline_source(name: str, template: Path) -> str:
    """The ``description`` line an applied variant carries."""
    return f"Built-in flow variant {name!r} ({template.name})"


def _builtin_role_template(role: str) -> Path | None:
    """Role file the awf package ships for *role* (or None).

    U1: a starter pipeline is only "ready to run" when its
    stage roles resolve. The package ships short, skill-free role
    templates so setup can materialize the missing ones instead of
    writing a pipeline that dies on stage 2. The lookup itself lives in
    ``_templates.package_role_template`` — the same one add_role uses.
    """
    return package_role_template(role)


def _stage_roles(stages: list[dict[str, Any]]) -> list[str]:
    """Distinct non-supervisor roles named by a pipeline's stages."""
    roles = {
        str(stage.get("role") or "").strip()
        for stage in stages
        if isinstance(stage, dict)
    }
    return sorted(r for r in roles if r and r not in _SKIP_ROLE_MAPPING)


def _roles_without_source(roles: list[str], project_dir: Path) -> list[str]:
    """Roles with neither a resolving file nor a package template.

    The one rule behind both "hide the variant in the form" and "refuse the
    write" — two hand-written list comprehensions would be free to drift.
    """
    return [
        role
        for role in roles
        if not _role_resolves(role, project_dir)
        and _builtin_role_template(role) is None
    ]


def _runtime_unresolved_roles(
    stages: list[dict[str, Any]], project_dir: Path
) -> list[str]:
    """Roles of *stages* whose file resolves nowhere — the write warning list.

    Distinct from :func:`_roles_without_source`: a role that has a package
    template but no file yet still counts here. The named-pipeline writers
    do NOT materialize roles, so nothing would create it before the stage
    runs — its stage would fail at runtime. One rule for both writers
    (``write_variant_pipeline`` and ``write_spec_pipeline``).
    """
    return [
        role for role in _stage_roles(stages)
        if not _role_resolves(role, project_dir)
    ]


def _variant_roles_error(variant: str, missing: list[str]) -> ValueError:
    """The refusal for a variant whose stage roles cannot be materialized."""
    return ValueError(
        f"flow variant {variant!r} needs role files that are not "
        f"installed: {', '.join(missing)} — create them or pick "
        "another composition"
    )


def variant_unavailable_roles(variant: str, project_dir: Path) -> list[str]:
    """Stage roles of *variant* unattainable even after materialization.

    A role is available when its file resolves (project → global, the
    runtime order) or the package ships a template for it. A non-empty
    answer means the variant is a dead end: its stage would fail at
    runtime, so the form hides it and the write path refuses.
    """
    roles = _stage_roles(_variant_composition(variant)[0])
    return _roles_without_source(roles, project_dir)


def _materialize_variant_roles(variant: str, project_dir: Path) -> bool:
    """Create the missing role files of a shipped variant, then verify.

    U1: copy from the package templates through the managed
    writer, never overwriting an existing file. If a role still does not
    resolve (no template, unreadable project, monkeypatched resolution),
    refuse with the roles named — a pipeline that cannot start is worse
    than an explicit "not available".

    REVIEW V4/V5/V6: the project copy is what makes the project portable —
    a global role that happens to resolve on this machine is not enough.
    The written file carries the managed blocks and the model line through
    :func:`awf.api.roles.materialize_builtin_role_content` (the create path's
    OWN transformation — never a second copy of that rule set) and gets its
    ``models.<role>.agent_name`` mapping, like an add_role-created role.

    REVIEW architect: every source is read and every write TARGET is
    preflighted before the first write. A missing or unreadable template, or
    a symlinked role path, is a refusal with NO partial materialization —
    the preflight and the mutation are not interleaved.
    """
    roles = _stage_roles(_variant_composition(variant)[0])
    pending: list[tuple[Path, str]] = []
    for role in roles:
        role_path = project_dir / ".agentic" / "roles" / f"{role}.md"
        if role_path.exists():
            continue
        source = _builtin_role_template(role)
        if source is None:
            continue
        try:
            raw = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ValueError(
                f"flow variant {variant!r}: cannot read the role template "
                f"for '{role}' ({source}): {e}"
            ) from e
        pending.append(
            (
                role_path,
                materialize_builtin_role_content(project_dir, role, raw),
            )
        )
    missing = _roles_without_source(roles, project_dir)
    if missing:
        raise _variant_roles_error(variant, missing)
    if pending:
        # The write targets go through the same no-follow barrier as the
        # pipeline file: a symlinked role path must refuse BEFORE the first
        # write, or a multi-role variant lands half-materialized.
        refuse_unsafe_surfaces(
            project_dir,
            f"materialize roles for flow variant {variant!r}",
            *[path for path, _ in pending],
        )
    # INV1/AUD2: the config mapping refusal (flow-style/indentation) is
    # checked before the first role file lands. The setup form preflights the
    # same team one level up, but ``setup.write_pipeline(flow_variant=...)``
    # is a public writer (exported, called directly) — it must not write
    # roles and then refuse the config.
    preflight_config_role_mapping([{"role": role} for role in roles], project_dir)
    for role_path, body in pending:
        managed_write_api_text(
            role_path, body, operation=f"materialize role {role_path.stem}"
        )
    config_updated = update_config_role_mapping(
        [{"role": role} for role in roles], project_dir
    )
    missing = [
        role for role in roles if not _role_resolves(role, project_dir)
    ]
    if missing:
        raise _variant_roles_error(variant, missing)
    # REVIEW W3: the caller (apply_project_setup) reports "config: role
    # mapping" from this flag — the mapping is written here, not in its
    # team-path call, so the variant must say whether it changed anything.
    return config_updated



def build_pipeline_stages(
    team_order: list[dict[str, Any]],
    *,
    exclude_roles: frozenset[str] | None = None,
    stage_mode: str = "chain",
    primary_role: str = "",
    include_qa: bool = False,
    include_closer: bool = True,
    warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build ordered pipeline stages from team selection.

    Wraps the user's team with supervisor at both ends
    (always plan → agents → verify; the closer gate sits before ``verify``
    unless ``include_closer=False``).

    REPORTS33 (TODO-0209): ``stage_mode`` picks the working part — the
    composition is a conscious choice, not a surprise:
    - ``"chain"`` (default, the pre-REPORTS33 behavior): every team role
      in order — each unit passes through all of them;
    - ``"single"``: one working role per unit — ``primary_role`` (or the
      first team role) plus, when ``include_qa`` is set and the team has
      a QA role that is not the primary, that QA role. A primary that is
      already the QA role adds no duplicate stage. This is NOT "one agent
      only": the checklist-close/check pair and ``verify`` still
      run, so a single unit keeps its definition-of-done check.
    - ``include_closer`` (IMP7, TODO-0290): ``True`` (default) inserts
      the ``checklist-close``/``checklist-check`` pair; the quick tier passes
      ``False`` —
      quick stays the short S-class set.

    Args:
        team_order: list of team member dicts (from form's team_config JSON).
            Each dict has at least: ``{agent: <role_name>, type: 'default'|'custom', ...}``.
        exclude_roles: role slugs to skip (TODO-0173: the quick variant
            passes the QA slugs). None/empty — the full team, unchanged.
        stage_mode: ``"chain"`` (default) or ``"single"``; an unknown
            value degrades to ``"chain"`` with a warning.
        primary_role: ``"single"`` mode only — the primary role (name or
            slug). Empty — the first team role; a role not in the team —
            the first team role + a warning (degrade, not fail).
        include_qa: ``"single"`` mode only — add the team's QA role
            (``_QA_ROLE_SLUGS``) after the primary.
        warnings: optional list collecting the degradations above.

    Returns:
        List of stage dicts: ``[plan(supervisor)] + [working stages] +
        [checklist-close] + [checklist-check] + [verify(supervisor)]`` (both are
        omitted when ``include_closer=False``). CLOSER IMP7 (R7): the
        closer stands after the last writing stage — before the
        findings-only tail (``_POST_CLOSER_ROLES``).

    Raises:
        ValueError: if team_order is empty or has no valid role entries
            (empty pipeline would be plan→verify with no work — useless),
            or every role is in ``exclude_roles`` (nothing to run).
    """
    exclude = set(exclude_roles or ())
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
    if exclude and all(r in exclude for r in valid_roles):
        raise ValueError(
            "Cannot build pipeline: every team role is excluded "
            f"({', '.join(sorted(set(valid_roles) & exclude))}) — at least "
            "one non-excluded role is required between plan and verify."
        )

    mode = str(stage_mode or "chain").strip().lower()
    if mode not in ("chain", "single"):
        log.warning("Unknown stage_mode %r — using the linear chain", stage_mode)
        if warnings is not None:
            warnings.append(
                f"unknown stage_mode {stage_mode!r} — the linear chain "
                "(all team roles) is used"
            )
        mode = "chain"

    work_roles: list[str] = []
    if mode == "single":
        primary = (
            _slugify_role(primary_role) if str(primary_role or "").strip() else ""
        )
        if primary and primary not in valid_roles:
            log.warning(
                "Primary role %r is not in the team — using %r",
                primary,
                valid_roles[0],
            )
            if warnings is not None:
                warnings.append(
                    f"primary role {primary!r} is not in the team — "
                    f"the first team role ({valid_roles[0]!r}) is the primary"
                )
            primary = ""
        if not primary:
            primary = valid_roles[0]
        work_roles.append(primary)
        if include_qa:
            qa_role = next((r for r in valid_roles if r in _QA_ROLE_SLUGS), None)
            if qa_role is None:
                log.warning("include_qa requested but the team has no QA role")
                if warnings is not None:
                    warnings.append(
                        "include_qa requested but the team has no QA role "
                        f"({', '.join(sorted(_QA_ROLE_SLUGS))}) — the QA "
                        "stage is not added"
                    )
            elif qa_role != primary:
                work_roles.append(qa_role)
            # qa_role == primary: the primary stage IS the QA — no duplicate
    else:
        seen_roles: set[str] = set()
        for role in valid_roles:
            if role in exclude:
                continue
            if role in seen_roles:
                log.warning("Duplicate role %r in team_order — skipping", role)
                continue
            seen_roles.add(role)
            work_roles.append(role)

    stages: list[dict[str, Any]] = [
        _stage_yaml(
            "plan",
            "supervisor",
            description="Supervisor studies the plan and creates a TODO",
        ),
    ]

    # CLOSER IMP7 (R7): after the last writing stage, before the
    # findings-only tail. One insertion after the loop: ``closer_at`` may
    # equal ``len(work_roles)`` (append), and a single call site keeps the
    # position rule in one place.
    closer_at = len(work_roles)
    while closer_at > 0 and work_roles[closer_at - 1] in _POST_CLOSER_ROLES:
        closer_at -= 1

    for role in work_roles:
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
    if include_closer:
        # TODO-0304: close, then the check of its diff — one insertion so
        # the order cannot drift from the templates.
        at = 1 + closer_at
        stages[at:at] = [_closer_stage(), _check_stage()]

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


def _dump_pipeline(name: str, description: str, stages: list[dict[str, Any]]) -> str:
    """The pipeline document as YAML text (the write path's exact dump)."""
    return dump_pipeline_document(name, stages, description)


def pipeline_yaml_text(
    team_order: list[dict[str, Any]],
    *,
    name: str = "default",
    description: str = "Generated by awf.api from project-setup form",
    stage_mode: str = "chain",
    primary_role: str = "",
    include_qa: bool = False,
    warnings: list[str] | None = None,
) -> str:
    """REPORTS33 (TODO-0209): the exact YAML text ``write_pipeline`` writes.

    The project-setup form's preview renders this text — the same builder
    and dump as the write path, so what the user sees before saving is
    byte-identical to the resulting ``.agentic/pipelines/<name>.yaml``.
    """
    stages = build_pipeline_stages(
        team_order,
        stage_mode=stage_mode,
        primary_role=primary_role,
        include_qa=include_qa,
        warnings=warnings,
    )
    return _dump_pipeline(name, description, stages)


def _variant_composition(
    variant: str, *, spec_slug: str = DEFAULT_SPEC_SLUG
) -> tuple[list[dict[str, Any]], str]:
    """Stages + description of a built-in variant — the ONE composition.

    The preview builder, the setup write and the CLI write all get the
    variant from here; a second ``_require_builtin_pipeline`` +
    ``_builtin_pipeline_stages`` pair at a call site would be the "written
    next to" copy that drifts (REVIEW architect).

    The spec template is slug-parameterized: its package paths carry
    :data:`SPEC_SLUG_TOKEN`. Without an explicit slug the variant is still
    installable (the setup form path) with :data:`DEFAULT_SPEC_SLUG`;
    ``awf spec init <slug>`` passes the user's scope.
    """
    template = _require_builtin_pipeline(variant)
    stages = _builtin_pipeline_stages(template)
    if variant == "spec":
        stages = [_render_spec_slug(stage, spec_slug) for stage in stages]
    return (
        stages,
        _builtin_pipeline_source(variant, template),
    )


def _render_spec_slug(value: Any, slug: str) -> Any:
    """Substitute :data:`SPEC_SLUG_TOKEN` in every string (containers recurse).

    One rule for the whole stage object: a scalar is replaced, a list or a
    dict is rebuilt element by element, anything else passes through. The
    stage itself is a dict, so the caller needs no separate stage-level
    renderer.
    """
    if isinstance(value, str):
        return value.replace(SPEC_SLUG_TOKEN, slug)
    if isinstance(value, list):
        return [_render_spec_slug(item, slug) for item in value]
    if isinstance(value, dict):
        return {key: _render_spec_slug(item, slug) for key, item in value.items()}
    return value


def validate_spec_slug(slug: Any) -> str:
    """Validate a spec package slug (one safe path segment). Returns it.

    Raises:
        AwfApiError: empty, path-like, or containing characters outside
            ``[A-Za-z0-9._-]`` (a leading dot/dash is refused too: the
            slug names a directory and the ``spec-<slug>`` pipeline file).
    """
    slug = str(slug or "").strip()
    if not slug or not _SPEC_SLUG_RE.fullmatch(slug):
        raise AwfApiError(
            f"Invalid spec slug {slug!r}: use letters, digits, '_', '.' or "
            "'-' (no path separators, no spaces), e.g. 'carry-cd'."
        )
    return slug


def write_spec_pipeline(
    project_dir: Path,
    slug: str,
    *,
    force: bool = False,
) -> tuple[WritePipelineResult, list[str]]:
    """Write ``.agentic/pipelines/spec-<slug>.yaml`` from the spec template.

    The ONE entry behind ``awf spec init <slug>``: the template's package
    token becomes *slug*, so the six author stages write
    ``docs/spec/<slug>/<slug>.*``. Only the named pipeline file is
    touched; an existing one is refused without *force*.

    Returns:
        ``(write_result, unresolved_roles)`` — the caller prints the roles
        whose .md resolves nowhere (the stage would fail at runtime).

    Raises:
        AwfApiError: invalid slug, no .agentic/, or an existing pipeline
            without force.
        ValueError: the packaged template is malformed.
    """
    slug = validate_spec_slug(slug)
    stages, description = _variant_composition("spec", spec_slug=slug)
    result = _write_named_pipeline(
        project_dir,
        f"spec-{slug}",
        stages,
        force=force,
        description=description,
    )
    return result, _runtime_unresolved_roles(stages, project_dir)


def pipeline_variant_yaml_text(
    variant: str,
    *,
    name: str = "default",
) -> str:
    """The exact YAML text ``write_pipeline(flow_variant=...)`` writes.

    The project-setup preview and the write path must agree — what the
    user sees is what will be written. One builder for the variant whose
    text reaches the form, never a second rendering of the template.
    """
    stages, description = _variant_composition(variant)
    return _dump_pipeline(name, description, stages)


def write_variant_pipeline(
    project_dir: Path,
    name: str,
    variant: str,
    *,
    force: bool = False,
) -> tuple[WritePipelineResult, list[str]]:
    """Write a built-in starter as a standalone named pipeline.

    The CLI (``awf pipeline-write --variant``) and the setup form must
    compose a variant the same way — template lookup, stage validation and
    the description all live in :func:`_variant_composition`, never in a
    second caller that reaches into this module's helpers. The side-effect
    contract is the named pipeline writer's: only
    ``.agentic/pipelines/<name>.yaml`` is touched; role files are NOT
    materialized (that belongs to the setup flow) and a role that resolves
    nowhere comes back in the warning list.

    Returns:
        ``(write_result, unresolved_roles)``.

    Raises:
        ValueError: unknown variant, or a known one whose template is gone.
        AwfApiError: invalid name/stages, no .agentic/, or an existing
            pipeline without ``force``.
    """
    stages, description = _variant_composition(variant)
    result = _write_named_pipeline(
        project_dir,
        name,
        stages,
        force=force,
        description=description,
    )
    return result, _runtime_unresolved_roles(stages, project_dir)


def _active_pipeline_name(project_dir: Path, *, warnings: list[str] | None = None) -> str:
    """AUD06-04: pipeline name the runtime will actually use.

    The value itself comes from :func:`awf.pipeline.active_pipeline_name` —
    the resolver the runtime and the setup preview use — so the written
    file and the preview cannot disagree on the name. This wrapper only
    adds the form's warning for an invalid ``default_pipeline``.
    """
    config = cfg_mod.load(project_dir)
    name = active_pipeline_name(project_dir, config)
    declared = str(
        cfg_mod.get(config, "default_pipeline", "default") or "default"
    ).strip()
    if (
        warnings is not None
        and declared
        and declared != "default"
        and name == "default"
    ):
        warnings.append(
            f"default_pipeline {declared!r} in config.yaml is not a valid "
            "pipeline name — the pipeline was written to default.yaml instead"
        )
    return name


def write_pipeline(
    team_order: list[dict[str, Any]],
    project_dir: Path,
    *,
    stage_mode: str = "chain",
    primary_role: str = "",
    include_qa: bool = False,
    flow_variant: str = "",
    warnings: list[str] | None = None,
    config_updates: list[bool] | None = None,
) -> Path | None:
    """Write the active pipeline file from team selection.

    AUD06-04: writes the file the runtime will actually use — the
    ``default_pipeline`` from config.yaml when it declares a custom one
    (validated), otherwise ``default.yaml``. Writing only default.yaml
    used to silently ignore the form's team in projects with a custom
    default_pipeline (repro S9).

    REPORTS33 (TODO-0209): ``stage_mode``/``primary_role``/``include_qa``
    pass the form's composition choice to :func:`build_pipeline_stages`
    (default ``"chain"`` — the pre-REPORTS33 linear composition).

    Args:
        team_order: form team_config list (ordered).
        project_dir: absolute path to awf project.
        stage_mode: ``"chain"`` (default) or ``"single"``. ``"single"``
            is one working role per unit — NOT one agent: the
            closer gate and ``verify`` still run.
        primary_role: ``"single"`` mode — the primary role (name/slug).
        include_qa: ``"single"`` mode — add the team's QA role.
        flow_variant: built-in starter pipeline (``full`` / ``code-health`` / ``spec``);
            replaces the team composition. Missing role files are copied
            from the package templates; a role with neither a file nor a
            template refuses the write (ValueError, roles named) — the
            alternative is an opaque runtime failure.
        warnings: optional list to collect config problems (invalid
            default_pipeline name), composition degradations and template
            roles that do not resolve.

    Returns:
        Path to written file. Backups the existing file to .bak.
    """
    pipelines_dir = project_dir / ".agentic" / "pipelines"

    # REVIEW architect: the preflight runs BEFORE the variant
    # materialization below. A refused write (unsafe/symlinked target) must
    # not leave freshly written role files and config mapping behind —
    # preflight and mutation are not interleaved.
    name = _active_pipeline_name(project_dir, warnings=warnings)
    target = pipelines_dir / f"{name}.yaml"
    backup = target.with_name(f"{target.name}.bak")
    # Q3 class (REVIEW architect): the preflight covers exactly what this
    # branch touches — the target always, the backup only when an existing
    # file is moved aside. A stray symlinked ``.bak`` with no target is not
    # an escape this write would perform.
    surfaces = [target]
    if target.exists():
        surfaces.append(backup)
    refuse_unsafe_surfaces(project_dir, "write pipeline", *surfaces)

    source = "Generated by awf.api from project-setup form"
    if flow_variant:
        # TODO-0230 (M6): a starter template replaces the generated team
        # composition. The raw stage order is validated by the loader; the
        # template's own ``name:`` is rewritten to the project's active one.
        stages, source = _variant_composition(flow_variant)
        # U1: materialize missing role files from the package;
        # anything still unresolved refuses the write (ValueError, named
        # roles) instead of producing a pipeline that dies at stage 2.
        changed = _materialize_variant_roles(flow_variant, project_dir)
        if config_updates is not None:
            config_updates.append(changed)
    else:
        stages = build_pipeline_stages(
            team_order,
            stage_mode=stage_mode,
            primary_role=primary_role,
            include_qa=include_qa,
            warnings=warnings,
        )
    if target.exists():
        try:
            target.rename(backup)
            log.info("Backed up existing pipeline to %s", backup)
        except OSError:
            pass

    content = _dump_pipeline(name, source, stages)
    managed_write_api_text(target, content, operation="write pipeline")
    log.info("Wrote pipeline with %d stages to %s", len(stages), target)
    return target


def _existing_quick_problem(target: Path) -> str | None:
    """Why an existing ``quick.yaml`` cannot be adopted — ``None`` when it can.

    TODO-0290: "keep the user's quick.yaml" is only honest for a file the
    engine can run. An empty, truncated or hand-broken file (or a
    directory on that path) used to be reported as a working quick tier:
    ``quick_pipeline_file`` was set and the unit failed later at
    ``load_stages`` with no hint of the real cause. A file that does not
    parse as a pipeline document is not "the user's quick tier", so the
    caller names it instead of adopting it.
    """
    if not target.is_file():
        return f"existing {target.name} is not a regular file"
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, OSError) as e:
        # A YAML parser error is multi-line and points at columns; the
        # warning is one line for the user, so only the first line travels.
        first = str(e).strip().splitlines()
        detail = first[0] if first else type(e).__name__
        return f"existing {target.name} is unreadable ({detail})"
    try:
        validate_pipeline_document(data, target.name)
    except AwfApiError as e:
        return f"existing {target.name} is not a valid pipeline ({e})"
    return None


def write_quick_pipeline(
    team_order: list[dict[str, Any]],
    project_dir: Path,
    *,
    warnings: list[str] | None = None,
) -> Path | None:
    """Write ``.agentic/pipelines/quick.yaml`` — the team without QA stages.

    TODO-0173 (quick tier): the S-class variant of the same team — the
    QA role stages (``_QA_ROLE_SLUGS``) and the closer gate removed:
    plan(supervisor) → worker stages → verify(supervisor). The normal
    pipeline and ``default_pipeline`` are untouched; quick is an
    alternate pipeline selected per unit (``pipeline: quick`` in the
    TODO front-matter or a run-queue item).

    IMP7 (TODO-0290): quick stays the short set — no ``checklist-close``/
    ``checklist-check`` gate; the closer belongs to the full/default/single compositions.

    Skipped (``None`` + a warning naming the reason) when:
    - no worker stage survives the QA removal (QA-only team), or
    - the ACTIVE pipeline already has the name ``quick`` — that file holds
      the full composition this setup wrote, so the QA-free variant must
      not replace it.

    An EXISTING quick.yaml is never overwritten (TODO-0290): it may be
    the user's own file (including one written by 2.1.0 with the closer),
    so it is returned as is. That also keeps old quick pipelines running
    exactly as before — no closer is injected into a local file. Only a
    file that parses as a pipeline document is adopted: an empty, broken
    or non-file path is a NAMED skip (``None`` + a warning), never a
    "quick is ready" report over something ``load_stages`` would reject.
    """
    pipelines_dir = project_dir / ".agentic" / "pipelines"

    if _active_pipeline_name(project_dir) == _QUICK_PIPELINE_NAME:
        reason = (
            "the active pipeline is named 'quick' — it holds the full "
            "composition from this setup, so the QA-free variant is not written"
        )
        log.warning("write_quick_pipeline: %s — not written", reason)
        if warnings is not None:
            warnings.append(f"quick pipeline: {reason}, quick.yaml not written")
        return None

    target = pipelines_dir / f"{_QUICK_PIPELINE_NAME}.yaml"
    # Target is the only surface: there is no backup branch anymore. The
    # barrier runs BEFORE the adopt branch (TODO-0290 REVIEW CTX3): an
    # existing file reached through a symlinked `.agentic/pipelines` must
    # be refused, not reported as a ready quick tier with a path outside
    # the project — that is what the write path refused before the adopt
    # branch existed.
    refuse_unsafe_surfaces(project_dir, "write quick pipeline", target)
    if target.exists():
        problem = _existing_quick_problem(target)
        if problem is None:
            log.info("Quick pipeline already exists at %s — kept as is", target)
            return target
        # Never overwritten (TODO-0290) — but never reported as ready either.
        log.warning("write_quick_pipeline: %s — quick.yaml left as is", problem)
        if warnings is not None:
            warnings.append(
                f"quick pipeline: {problem} — quick.yaml left as is; "
                "delete it to rebuild the quick tier"
            )
        return None

    try:
        stages = build_pipeline_stages(
            team_order,
            exclude_roles=_QA_ROLE_SLUGS,
            include_closer=False,
        )
    except ValueError as e:
        reason = str(e)
        log.warning("write_quick_pipeline: %s — not written", reason)
        if warnings is not None:
            warnings.append(f"quick pipeline: {reason} — quick.yaml not written")
        return None

    content = _dump_pipeline(
        _QUICK_PIPELINE_NAME,
        "Quick tier (S-class units): the team without the QA stage or closer gate",
        stages,
    )
    managed_write_api_text(
        target, content, operation="write quick pipeline"
    )
    log.info("Wrote quick pipeline with %d team stages to %s", len(stages) - 2, target)
    return target


# ─── Config.yaml role mapping (BD-12 + BD-32) ───────────────────────────


def _prepare_role_mapping(
    team: list[dict[str, Any]],
    project_dir: Path,
    *,
    warnings: list[str] | None = None,
) -> tuple[Path, str, str, dict[str, dict[str, Any]]] | None:
    """BD-12/BD-32: compute the config.yaml text, touching no file (INV1).

    Returns ``(config_path, raw, new_text, updates)`` or ``None`` when there
    is nothing to write (absent/unreadable config, non-dict root, no change).
    The patch refusal (flow-style, broken indentation) raises
    :class:`AwfApiError` here — before the caller writes any pipeline or
    role file.
    """
    config_path = project_dir / ".agentic" / "config.yaml"
    if not config_path.is_file():
        log.warning("BD-12: no config.yaml at %s — skip role mapping", config_path)
        return None

    try:
        raw = config_path.read_text(encoding="utf-8")
        config = yaml.safe_load(raw) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        # AUD06-02: non-UTF-8 bytes and read errors used to escape as
        # UnicodeDecodeError/OSError and crash the whole submit.
        log.error("BD-12: cannot read config.yaml: %s — skip role mapping", e)
        if warnings is not None:
            warnings.append(
                f"config.yaml: unreadable ({type(e).__name__}) — role mapping skipped"
            )
        return None
    if not isinstance(config, dict):
        log.error("BD-12: config.yaml root is not a mapping — skip")
        if warnings is not None:
            warnings.append("config.yaml: root is not a mapping — role mapping skipped")
        return None

    models = config.get("models") or {}
    if not isinstance(models, dict):
        log.error("BD-12: config.yaml 'models' is not a mapping — skip")
        return None

    updates: dict[str, dict[str, Any]] = {}
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
        change = updates.setdefault(role, {})
        if not entry.get("agent_name"):
            change["agent_name"] = _DEFAULT_AGENT_FOR_ROLE
        if "model" in member:
            member_model = str(member.get("model") or "").strip()
            # Empty string = the user cleared the model (BD-32) — the
            # writer removes the key; a non-empty value replaces it.
            change["model"] = member_model or None

    # R10: patch only the target scalars — comments, blank lines and the
    # order of every other key survive. Flow-style or non-standard files
    # are refused by the writer before anything is written.
    new_text = patch_mapping(raw, updates)
    if new_text == raw:
        return None
    return config_path, raw, new_text, updates


def _write_role_mapping(
    prepared: tuple[Path, str, str, dict[str, dict[str, Any]]],
) -> bool:
    config_path, raw, new_text, updates = prepared
    for role, change in updates.items():
        if "agent_name" in change:
            log.info(
                "BD-12: mapped role %r -> agent_name=%s",
                role,
                _DEFAULT_AGENT_FOR_ROLE,
            )
        if "model" in change:
            if change["model"] is None:
                log.info("BD-32: role %r -> model cleared", role)
            else:
                log.info("BD-32: role %r -> model=%s", role, change["model"])

    # P3: keep latest backup as .bak + timestamped copy for history
    backup = config_path.with_suffix(".yaml.bak")
    try:
        old_content = raw
        # AUD14-06: backups hold the full config (may contain keys) —
        # the managed writer creates them 0600; write_text left them 0644.
        managed_write_api_text(
            backup, old_content, operation="config backup"
        )
        # Also write timestamped backup (keep last 3)
        from datetime import datetime as _dt
        ts_backup = config_path.with_name(
            f"config.yaml.{_dt.now().strftime('%Y%m%d%H%M%S')}.bak"
        )
        managed_write_api_text(
            ts_backup, old_content, operation="config backup"
        )
        old_baks = sorted(config_path.parent.glob("config.yaml.*.bak"))
        for old in old_baks[:-3]:
            old.unlink(missing_ok=True)
    except (OSError, AwfApiError) as e:
        log.warning("BD-12: backup failed: %s", e)

    managed_write_api_text(
        config_path,
        new_text,
        operation="update config.yaml",
    )
    log.info("BD-12: config.yaml updated with role mappings")
    return True


def preflight_config_role_mapping(
    team: list[dict[str, Any]],
    project_dir: Path,
    *,
    warnings: list[str] | None = None,
) -> None:
    """INV1/AUD2: run the R10 refusal before any pipeline or role is written."""
    _prepare_role_mapping(team, project_dir, warnings=warnings)


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
    prepared = _prepare_role_mapping(team, project_dir, warnings=warnings)
    if prepared is None:
        return False
    return _write_role_mapping(prepared)


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


def _prepare_context_patch(
    project_dir: Path,
    context_message: str,
    supervisor_instructions: str,
    warnings: list[str] | None,
) -> tuple[Path, str, str] | None:
    """Compute the UI-2/UI-3 ``config.yaml`` text, touching no file (INV1).

    Returns ``(config_path, raw, new_text)`` or ``None`` when there is
    nothing to write: empty inputs, or a config that cannot be read/parsed
    (that damage goes to *warnings* — the submit must survive it, AUD06-02/03).

    Raises :class:`AwfApiError` when the R10 writer refuses (flow-style
    ``context:``/``supervisor:`` header, broken indentation, bad round-trip).
    The caller must not write any file in that case.
    """
    if not context_message and not supervisor_instructions:
        return None

    config_path = project_dir / ".agentic" / "config.yaml"
    raw = ""
    if config_path.exists():
        try:
            raw = config_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
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
            return None
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            log.error(
                "UI-2/UI-3: cannot parse config.yaml: %s — skipping save", e
            )
            if warnings is not None:
                warnings.append(
                    f"config.yaml: unreadable ({type(e).__name__}) — "
                    "context/instructions save skipped, file left untouched"
                )
            return None
        if parsed is not None and not isinstance(parsed, dict):
            log.error(
                "UI-2/UI-3: config.yaml root is not a mapping — skipping save"
            )
            if warnings is not None:
                warnings.append(
                    "config.yaml: root is not a mapping — "
                    "context/instructions save skipped, file left untouched"
                )
            return None

    updates: dict[str, dict[str, Any]] = {}
    if context_message:
        updates.setdefault("context", {})["message"] = context_message
    if supervisor_instructions:
        updates.setdefault("supervisor", {})["instructions"] = (
            supervisor_instructions
        )

    # R10/AUD1: patch only the two keys — comments, blank lines and the
    # order of every other key survive (the old safe_dump rewrote the
    # whole file, comments included).
    new_text = patch_config_values(raw, updates, operation="UI-2/UI-3")
    return config_path, raw, new_text


def preflight_context_config(
    project_dir: Path,
    *,
    context_message: str = "",
    supervisor_instructions: str = "",
) -> None:
    """INV1/AUD0286-2: run the UI-2/UI-3 refusal before any other write.

    ``apply_project_setup`` otherwise writes the pipeline, roles and the
    role mapping first, and only then discovers that ``context.message``
    cannot be patched — the submit would be half applied. A degraded
    (unreadable/unparseable) config is NOT a refusal: it stays a warning
    later (AUD06-02/03), so the preflight silently lets it through.
    """
    _prepare_context_patch(
        project_dir,
        context_message.strip(),
        supervisor_instructions.strip(),
        None,
    )


def _save_context_and_instructions(
    project_dir: Path,
    *,
    context_message: str = "",
    supervisor_instructions: str = "",
    warnings: list[str] | None = None,
) -> tuple[bool, bool]:
    """Persist UI-2/UI-3; return ``(config_changed, supervisor_md_changed)``.

    A refused config patch writes nothing at all — not even supervisor.md.
    Old code reported the config as updated and patched supervisor.md after
    the refusal, so the two surfaces diverged (AUD0286-2). The same
    all-or-nothing rule holds for a DEGRADED config (unreadable, broken or
    non-dict root): its warning already says "save skipped, file left
    untouched", so supervisor.md must not be patched either (AUD06-02/03).
    """
    context_message = context_message.strip()
    supervisor_instructions = supervisor_instructions.strip()

    if not context_message and not supervisor_instructions:
        return False, False

    try:
        prepared = _prepare_context_patch(
            project_dir,
            context_message,
            supervisor_instructions,
            warnings,
        )
    except AwfApiError as e:
        log.error("UI-2/UI-3: config.yaml not patched: %s", e)
        if warnings is not None:
            warnings.append(str(e))
        return False, False

    if prepared is None:
        # AUD06-02/03: empty inputs returned above, so None here means the
        # config is degraded (unreadable/broken/non-dict root). The warning
        # is already recorded — do not patch the second surface either.
        return False, False

    config_path, raw, new_text = prepared
    config_changed = new_text != raw
    if config_changed:
        managed_write_api_text(
            config_path,
            new_text,
            operation="save context/instructions",
        )
        if context_message:
            log.info(
                "UI-2: saved context.message to config.yaml (%d chars)",
                len(context_message),
            )
        if supervisor_instructions:
            log.info(
                "UI-3: saved supervisor.instructions to config.yaml (%d chars)",
                len(supervisor_instructions),
            )

    # 2. Patch supervisor.md (idempotent via UI-2/UI-3 marker sections)
    supervisor_md = project_dir / ".agentic" / "roles" / "supervisor.md"
    if not supervisor_md.exists():
        log.warning("UI-2/UI-3: supervisor.md not found at %s — skipping append", supervisor_md)
        return config_changed, False

    try:
        current = supervisor_md.read_text(encoding="utf-8")
    except OSError as e:
        log.error("UI-2/UI-3: failed to read supervisor.md: %s", e)
        return config_changed, False

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

    if not sections_to_add:
        return config_changed, False

    new_content = current + "".join(sections_to_add)
    managed_write_api_text(
        supervisor_md,
        new_content,
        operation="save supervisor.md",
    )
    log.info("UI-2/UI-3: appended %d section(s) to supervisor.md", len(sections_to_add))
    return config_changed, True


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

    A refused config patch (R10/AUD0286-2) leaves both surfaces untouched.

    Returns:
        True if any file was changed, False otherwise.
    """
    config_changed, md_changed = _save_context_and_instructions(
        project_dir,
        context_message=context_message,
        supervisor_instructions=supervisor_instructions,
        warnings=warnings,
    )
    return config_changed or md_changed


def _role_resolves(role: str, project_dir: Path) -> bool:
    """True when the runtime would find a role file for *role*.

    Delegates to :func:`awf.supervisor.resolve_role_file` — the runtime's
    OWN resolution (project → global) — instead of spelling the lookup
    order a second time: setup deciding "the role is missing" on rules
    the engine does not use is the AUD06-15 class of bug.
    """
    # Runtime dependency: imported here (not at module load) so that
    # importing awf.api.setup does not pull the supervisor/doctrine stack.
    from ..supervisor import resolve_role_file

    try:
        resolve_role_file(role, project_dir)
    except RuntimeError:
        return False
    return True


def _unresolved_team_roles(team: list[dict[str, Any]], project_dir: Path) -> list[str]:
    """AUD06-15: team roles with no role file (project → global fallback).

    Mirrors ``resolve_role_file`` (awf/supervisor.py) resolution order so
    the check agrees with the runtime. A role that resolves nowhere would
    otherwise crash its first stage with RuntimeError from resolve_role_file.
    """
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
        if not _role_resolves(role, project_dir):
            missing.append(role)
    return missing


# ─── Public entry point ─────────────────────────────────────────────────


def apply_project_setup(
    project_dir: Path,
    *,
    team: list[dict[str, Any]],
    context_message: str = "",
    supervisor_instructions: str = "",
    stage_mode: str = "chain",
    primary_role: str = "",
    include_qa: bool = False,
    flow_variant: str = "",
) -> ApplyProjectSetupResult:
    """Apply a project-setup form submit to awf project files.

    Single source of truth for materializing user's team/context/instructions
    choices into awf-managed files. Called by the plugin's submit handler
    after global custom-role CRUD (which stays in the plugin — UI persistence
    is not awf's concern).

    REPORTS33 (TODO-0209): ``stage_mode``/``primary_role``/``include_qa``
    carry the form's composition choice. The API default is ``"chain"`` —
    a submit without the field (older form) keeps the pre-REPORTS33 linear
    composition; the NEW form defaults its radio to ``"single"`` (one
    working role per unit) and always sends the field.

    Side effects (all idempotent):
    1. Writes the active pipeline file from team order — the
       ``default_pipeline`` from config.yaml when it declares a valid
       custom name, otherwise ``default.yaml`` (AUD06-04). Existing file
       backed up to ``.bak``. Also writes ``pipelines/quick.yaml`` — the
       S-class variant without the QA stages or the closer gate
       (TODO-0173/TODO-0290); a skip (QA-only team / active pipeline named
       'quick') is a warning, not a failure.
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
        stage_mode: ``"chain"`` (default) or ``"single"``. ``"single"``
            is one working role per unit — NOT one agent: the
            closer gate and ``verify`` still run.
        primary_role: ``"single"`` mode — the primary role (name/slug).
        include_qa: ``"single"`` mode — add the team's QA role.
        flow_variant: built-in starter pipeline (``full`` / ``code-health`` / ``spec``);
            replaces the team composition. An unknown name is a warning, not
            a crash — and it is named even when the team is empty (the write
            below is skipped then, so nothing else would report it).

    Returns:
        ApplyProjectSetupResult with paths written + flags.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    warnings: list[str] = []
    pipeline_path: Path | None = None

    if flow_variant:
        # The team guard below skips write_pipeline entirely — validate the
        # variant here, or an empty team turns a typo into silence.
        try:
            _require_builtin_pipeline(flow_variant)
        except ValueError as e:
            warnings.append(f"pipeline: {e}")
            flow_variant = ""

    # REVIEW R2 (TODO-0230): a flow variant is a complete composition — it
    # needs no team to be written. The old ``if team:`` guard silently
    # dropped a valid variant submit with an empty team.
    quick_path: Path | None = None
    if flow_variant and (stage_mode != "chain" or primary_role or include_qa):
        # The variant replaces the team composition: those knobs cannot
        # shape it. Name the loss instead of ignoring the input silently.
        warnings.append(
            f"pipeline: flow variant {flow_variant!r} replaces the team "
            "composition — stage_mode/primary_role/include_qa are not applied"
        )
    # REVIEW W3: the variant branch writes models.<role>.agent_name inside
    # write_pipeline; the out-list carries that fact to the report flag.
    variant_config_updates: list[bool] = []
    # R10/INV1 (AUD0286-2): the UI-2/UI-3 patch is the last writer of the
    # submit — run its refusal first so a refused context leaves no
    # pipeline, no roles and no supervisor.md behind.
    preflight_context_config(
        project_dir,
        context_message=context_message,
        supervisor_instructions=supervisor_instructions,
    )
    if team or flow_variant:
        # R10/INV1 (AUD2): the team's config patch happens after the
        # pipeline write — compute its refusal first, so a refused submit
        # leaves the project untouched. Variant roles are preflighted by
        # ``_materialize_variant_roles`` right before its own first write;
        # deriving the variant composition here again would be a second
        # owner of the same stage list.
        preflight_config_role_mapping(team, project_dir)
        try:
            pipeline_path = write_pipeline(
                team,
                project_dir,
                stage_mode=stage_mode,
                primary_role=primary_role,
                include_qa=include_qa,
                flow_variant=flow_variant,
                warnings=warnings,
                config_updates=variant_config_updates,
            )
        except ValueError as e:
            warnings.append(f"pipeline: {e}")
            log.warning("apply_project_setup: %s", e)

        if pipeline_path:
            # TODO-0173 (quick tier): the S-class variant of the same team
            # — the QA role stages removed. Best-effort: the variant is
            # non-essential, so neither a skip nor a hard write failure
            # may fail the submit — both are named warnings.
            # REVIEW R2 (TODO-0230): quick is built from the team, so an
            # empty team (flow variant only) is a named skip, not a crash.
            if team:
                try:
                    quick_path = write_quick_pipeline(
                        team, project_dir, warnings=warnings
                    )
                except Exception as e:  # noqa: BLE001 — the quick variant must not fail setup
                    quick_path = None
                    warnings.append(f"quick pipeline: {e}")
                    log.warning("apply_project_setup: %s", e)
            else:
                warnings.append(
                    "quick pipeline: team is empty — quick.yaml not written "
                    "(a flow variant carries no team to derive it from)"
                )

        config_updated = update_config_role_mapping(
            team, project_dir, warnings=warnings
        ) or any(variant_config_updates)
        # AUD06-15: report team roles that resolve to no role file — a typo
        # used to surface only at runtime (RuntimeError from
        # resolve_role_file at stage spawn: retries, salvage, crash).
        for role in _unresolved_team_roles(team, project_dir):
            warnings.append(
                f"role '{role}' has no role file (checked .agentic/roles/ and "
                "the global awf roles dir) — its stage will fail at runtime; "
                f"run `awf add-role {role}` (or fix the team)"
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

    context_config_changed, sup_md_updated = _save_context_and_instructions(
        project_dir,
        context_message=context_message,
        supervisor_instructions=supervisor_instructions,
        warnings=warnings,
    )
    config_updated = config_updated or context_config_changed

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
        quick_pipeline_file=str(quick_path) if quick_path else None,
        config_updated=config_updated,
        supervisor_md_updated=sup_md_updated,
        warnings=warnings,
    )


__all__ = [
    "BUILTIN_PIPELINES",
    "apply_project_setup",
    "write_pipeline",
    "write_quick_pipeline",
    "build_pipeline_stages",
    "pipeline_yaml_text",
    "pipeline_variant_yaml_text",
    "update_config_role_mapping",
    "save_context_and_instructions",
    "variant_unavailable_roles",
    "write_variant_pipeline",
    "write_spec_pipeline",
    "validate_spec_slug",
    "SPEC_SLUG_TOKEN",
]
