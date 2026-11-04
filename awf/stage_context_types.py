"""Stage type as data (IMP1, R1/R7/R14).

The type of a stage is not computed from its position in the pipeline: it is
read from ``stage_types.<stage name>`` in ``.agentic/config.yaml``.  This
module is the ONE owner of that vocabulary:

- :data:`FAMILIES` — five families and their rows (``reviewer`` carries the
  extra ``checklist`` row);
- :func:`resolve` — config value → ``(family, row)`` for a stage name/role;
- :func:`generate_stage_types` — the ``init`` mapping from pipelines;
- :func:`engine_kind` — the engine's three kinds for a family.

Position is nowhere in this module.  A name outside ``stage_types`` starts
with the role's family and a ``CTX-TYPE-DEFAULT`` warning (``strict: true``
turns that warning into :class:`StageTypeUndeclared`); a missing or empty
key, or a value outside the closed list, is :class:`StageTypeMissing` — the
subprocess does not start.
"""
from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from ._errors import AwfApiError

#: The five families and their rows.  An empty tuple means "the family is
#: its own row"; ``reviewer`` additionally carries the ``checklist`` row
#: (rendered as ``reviewer/checklist``).  Six rows in total.
FAMILIES: dict[str, tuple[str, ...]] = {
    "plan": (),
    "execute": (),
    "reviewer": ("checklist",),
    "supervisor": (),
    "spec": (),
}

#: Generation rule (R14): the roles whose ``agent-*`` stage is a reviewer.
REVIEWER_ROLES = frozenset({
    "agent-architect-review",
    "agent-qa-review",
    "agent-project-auditor",
    "agent-requirements-reviewer",
})

#: ``engine_kind`` values the rest of the engine knows.
ENGINE_KINDS = frozenset({"plan", "execute", "verify"})


class StageTypeError(AwfApiError):
    """Base: the stage's type cannot be established."""


class StageTypeMissing(StageTypeError):
    """No/empty ``stage_types`` key, or a value outside the closed list."""


class StageTypeUndeclared(StageTypeError):
    """``stage_types.strict`` is on and the stage name is not declared."""


def _valid_values() -> frozenset[str]:
    values = set(FAMILIES)
    for family, rows in FAMILIES.items():
        values.update(f"{family}/{row}" for row in rows)
    return frozenset(values)


VALID_VALUES = _valid_values()


def split_value(value: str) -> tuple[str, str]:
    """Config value → ``(family, row)``; ``row`` is ``""`` for the base row."""
    family, _, row = value.partition("/")
    return family, row


def display_type(family: str, row: str) -> str:
    """The CTX spelling: ``reviewer`` or ``reviewer/checklist``."""
    return f"{family}/{row}" if row else family


def engine_kind(family: str) -> str:
    """K4: the engine kind for a family (signals and the commit gate).

    ``plan`` stages are supervisor-planned units, ``supervisor`` is the
    verify stage; every authoring/review/checklist row runs a worker and is
    therefore ``execute``.
    """
    if family == "plan":
        return "plan"
    if family == "supervisor":
        return "verify"
    return "execute"


def role_family(role: str) -> str:
    """D8: the runtime fallback family for a role.

    Reviewers are ``reviewer``; the supervisor is ``supervisor``; every other
    ``agent-*`` role (and an unknown role at runtime) authors/works, so it is
    ``execute``.
    """
    if role == "supervisor":
        return "supervisor"
    if role in REVIEWER_ROLES:
        return "reviewer"
    return "execute"


def family_for_stage(name: str, role: str, *, strict: bool = False) -> str:
    """The R14 rule applied without a config: stage name first, then role.

    ``plan`` → ``plan``, ``verify`` → ``supervisor``, ``spec-*`` → ``spec``,
    ``checklist-*`` → ``reviewer`` (the ``checklist`` row belongs to
    :func:`value_for_stage`), otherwise by :func:`role_family`.  ``strict``
    (the generator) refuses a role the rule does not know; the runtime
    fallback degrades to ``execute`` (D8) and lets :func:`resolve` warn.
    """
    if name == "plan":
        return "plan"
    if name == "verify":
        return "supervisor"
    if name.startswith("spec-"):
        return "spec"
    if name.startswith("checklist-"):
        return "reviewer"
    if role in REVIEWER_ROLES or role.startswith("agent-"):
        return role_family(role)
    if strict:
        raise AwfApiError(
            f"stage '{name}': role '{role}' is not in the stage-type rule "
            f"(known: supervisor, {', '.join(sorted(REVIEWER_ROLES))}, "
            f"agent-*)"
        )
    return role_family(role)


def value_for_stage(name: str, role: str, *, strict: bool = False) -> str:
    """The config spelling of the R14 rule: ``checklist-*`` keeps its row."""
    family = family_for_stage(name, role, strict=strict)
    row = "checklist" if name.startswith("checklist-") else ""
    return display_type(family, row)


def _config_path_text(config_path: str | Path | None) -> str:
    return str(config_path) if config_path is not None else ".agentic/config.yaml"


def _missing(what: str, config_path: str | Path | None) -> StageTypeMissing:
    path = _config_path_text(config_path)
    return StageTypeMissing(
        f"CTX-TYPE-MISSING: {what} (config: {path}) — run 'awf init' to "
        f"generate stage_types"
    )


def resolve(
    config: dict[str, Any] | None,
    stage_name: str,
    role: str,
    *,
    config_path: str | Path | None = None,
    warn: bool = True,
) -> tuple[str, str]:
    """K3: ``(family, row)`` for *stage_name* from ``stage_types``.

    Raises :class:`StageTypeMissing` when the key is absent/empty or the
    value is outside :data:`VALID_VALUES`, and :class:`StageTypeUndeclared`
    when ``strict: true`` and the name is not declared.  An undeclared name
    in the default mode falls back to the role's family and prints
    ``CTX-TYPE-DEFAULT`` (a warning, not a failure).
    """
    types = config.get("stage_types") if isinstance(config, dict) else None
    if not isinstance(types, dict) or not types:
        raise _missing("no stage_types key (or it is empty)", config_path)

    strict = types.get("strict") is True
    value = types.get(stage_name)
    if value is None:
        if strict:
            raise StageTypeUndeclared(
                f"CTX-TYPE-STRICT: stage '{stage_name}' (role '{role}') is "
                f"not declared in stage_types and stage_types.strict is on "
                f"(config: {_config_path_text(config_path)})"
            )
        family = role_family(role)
        if warn:
            print(
                f"WARN CTX-TYPE-DEFAULT: stage '{stage_name}' (role '{role}') "
                f"is not declared in stage_types — using family '{family}' "
                f"from the role (config: {_config_path_text(config_path)})",
                file=sys.stderr,
            )
        return family, ""

    if not isinstance(value, str) or value not in VALID_VALUES:
        raise _missing(
            f"stage_types.{stage_name} = {value!r} is not one of "
            f"{', '.join(sorted(VALID_VALUES))}",
            config_path,
        )
    return split_value(value)


def generate_stage_types(
    pipelines: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """K1: ``{(stage name, role)}`` → ``{stage name: value}``.

    The generator refuses a role the rule does not know: silently writing a
    wrong type would make every later ``doctor`` run disagree with the
    pipeline that produced it.
    """
    mapping: dict[str, str] = {}
    for name, role in pipelines:
        if not name:
            continue
        mapping[name] = value_for_stage(name, role, strict=True)
    return mapping


def write_stage_types(config_text: str, mapping: dict[str, str]) -> str:
    """K2: replace ``stage_types`` in *config_text*, idempotently.

    Only the ``stage_types`` scalars change: comments, blank lines and key
    order keep their bytes (the shared ``config_write`` patcher).  A stale
    stage name is removed; ``stage_types.strict`` belongs to the owner and
    is never touched.  An unchanged mapping returns *config_text* itself, so
    a second ``awf init`` writes nothing.
    """
    from . import config_write

    try:
        parsed = yaml.safe_load(config_text) or {}
    except yaml.YAMLError as e:
        raise AwfApiError(
            f"config.yaml: cannot read stage_types before patching: {e}"
        ) from e
    existing = parsed.get("stage_types") if isinstance(parsed, dict) else None
    if not isinstance(existing, dict):
        existing = {}
    change: dict[str, str | None] = {}
    for name, value in mapping.items():
        if existing.get(name) != value:
            change[name] = value
    for name in existing:
        if name != "strict" and name not in mapping:
            change[name] = None
    if not change:
        return config_text
    return config_write.patch_config_values(
        config_text,
        {"stage_types": change},
        operation="stage_types",
    )


def mapping_for_project(project_dir: str | Path, fallback_dir: str | Path | None = None) -> dict[str, str]:
    """``stage_types`` for a project: its own pipelines, else *fallback_dir*."""
    return generate_stage_types(pairs_for_project(project_dir, fallback_dir))


def pairs_for_project(
    project_dir: str | Path, fallback_dir: str | Path | None = None
) -> list[tuple[str, str]]:
    """``(stage name, role)`` pairs for ``init``/``doctor``.

    The "a fresh project has no ``.agentic/pipelines/`` yet" rule lives here
    alone: the shipped templates are the starting set.  ``init`` and the
    doctor both ask this function instead of walking the directories twice.
    """
    from . import paths

    pairs = read_pipelines(paths.pipelines_dir(project_dir))
    if not pairs and fallback_dir is not None:
        pairs = read_pipelines(fallback_dir)
    return pairs


def load_family(
    config: dict[str, Any] | None,
    stage_name: str,
    role: str,
    *,
    config_path: str | Path | None = None,
) -> str:
    """The family a pipeline loader needs from a project config.

    A table that IS present is authoritative: an undeclared name warns (or
    refuses under ``strict``), a value outside the closed list refuses.  A
    project initialised before IMP1 has no key yet — its structural readers
    keep the R14 rule so status/brief/analyze-roles still open the tree; a
    surface that must refuse instead calls :func:`resolve` directly.
    """
    types = config.get("stage_types") if isinstance(config, dict) else None
    if not isinstance(types, dict) or not types:
        return family_for_stage(stage_name, role)
    family, _row = resolve(config, stage_name, role, config_path=config_path)
    return family


def read_pipelines(pipelines_dir: str | Path) -> list[tuple[str, str]]:
    """``(stage name, role)`` pairs of every ``*.yaml`` pipeline in a dir.

    A malformed file is skipped like :func:`awf.pipeline.load_stages` does
    for broken bytes: ``init``/``doctor`` must not crash on a half-written
    pipeline, they simply do not see it.
    """
    directory = Path(pipelines_dir)
    if not directory.is_dir():
        return []
    pairs: list[tuple[str, str]] = []
    for path in sorted(directory.glob("*.yaml")):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        stages = data.get("stages") if isinstance(data, dict) else None
        if not isinstance(stages, list):
            continue
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            name = stage.get("id") or stage.get("name") or ""
            role = stage.get("role") or ""
            if name:
                pairs.append((str(name), str(role)))
    return pairs
