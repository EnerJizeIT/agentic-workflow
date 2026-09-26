"""Pipeline parsing — Stage dataclass, load_stages, resolve_pipeline_file.

BD-29: pipeline stages no longer have an `action` field. Awf computes the
kind (plan / execute / verify) from the stage's position:

- Stage index 0           → kind="plan"   (supervisor creates TODO)
- Stage index N-1         → kind="verify" (supervisor verifies + commits)
- All stages between      → kind="execute" (agents do work)

A-06 (audit 2026-09-25): the loader validates the document FORM before
building Stage objects (validate_pipeline_document) — the same rule set
as the write path (awf.api.write_pipeline, shared validator). Legacy
`action:`/`kind:` keys in YAML are rejected as unknown keys (they were
read-but-ignored; kind is computed from position, action was never a
runtime input).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths
from ._errors import AwfApiError


@dataclass
class Stage:
    """A single stage in a pipeline.

    `kind` is computed at load time from position (see module docstring).
    It is NOT in pipeline.yaml.
    """
    name: str
    role: str
    description: str = ""
    on_blocked: str = "escalate"
    on_approved: str = "next"
    on_rejected: str = "escalate"
    # AUD16-03: reserved — the TEST-FAILED signal it used to drive no longer
    # exists, so no transition reads this field. Kept (not removed) because
    # pipeline.yaml files still carry it; the loader keeps accepting it.
    on_failed: str = "escalate"
    max_retries: int = 1
    # AUD03-01: separate budget for rollback transitions (the escalate budget
    # is max_retries; rollbacks used to be unbounded).
    max_rollbacks: int = 3
    # Computed at load time — not in YAML.
    kind: str = "execute"  # "plan" | "execute" | "verify"


_DEFAULTS: dict[str, Any] = {
    "on_blocked": "escalate",
    "on_approved": "next",
    # NEG-2 (dogfood-11): the old default was ``rollback_to:implement`` — a
    # fixed stage name that form-generated pipelines never have (their stages
    # are role-named, e.g. ``agent-implementer``). Any rejection then hit
    # "Rollback target 'implement' not found" and hard-stopped the pipeline.
    # Escalating is the safe default: the supervisor gets the rejection and
    # replans. Authors who want a rollback set ``rollback_to:<stage-name>``
    # explicitly (validated with a warning at load).
    "on_rejected": "escalate",
    # AUD16-03: reserved — accepted from pipeline.yaml (files still carry it),
    # but no signal drives it: TEST-FAILED no longer exists, so the resolver
    # never reads this policy.
    "on_failed": "escalate",
    "max_retries": 1,
    "max_rollbacks": 3,
}

# AUD16-03: on_passed removed — TEST-PASSED never existed as an emitted
# signal, so its policy had no transition to drive. YAML keys named
# on_passed are now ignored by the loader.
_POLICY_KEYS = [
    "on_blocked", "on_approved", "on_rejected", "on_failed",
]

# AUD04-02: allowed plain words per policy key (rollback_to:<stage> is
# checked separately). The resolver used to silently reinterpret unknown
# words (on_approved/on_passed → "next", on_rejected/on_failed → "escalate"),
# which hid typos — now the loader warns.
_POLICY_ALLOWED = {
    "on_blocked": {"escalate", "stop"},
    "on_approved": {"next", "commit_and_next", "commit_and_report"},
    "on_rejected": {"escalate", "replan"},
    # AUD16-03: reserved — kept in the allowed list so existing pipeline.yaml
    # files load without warnings; the signal it drove no longer exists.
    "on_failed": {"escalate", "replan"},
}

# FU-19 (AUD03-02 tail): the policy keys that may carry ``rollback_to:<stage>``
# — exactly the keys the transition resolver reads the prefix on
# (awf/transitions.py: on_blocked, on_rejected). On any other key the value
# is ignored at runtime, so A-06 rejects it at load (it used to warn).
_ROLLBACK_TO_KEYS = ("on_blocked", "on_rejected")

# A-06 (audit 2026-09-25, layer 1): stage keys the pipeline schema accepts.
# Single source of truth — the write path (awf/api/pipelines.py) imports
# this set, so a hand-written YAML and an API write are held to the same
# schema. Any other key (a typo, or the legacy action:/kind:) is REJECTED
# with a clear error instead of being silently ignored and surfacing at
# runtime.
ALLOWED_STAGE_KEYS = frozenset(
    {
        "name",
        "role",
        "description",
        "on_blocked",
        "on_approved",
        "on_rejected",
        "on_failed",
        "max_retries",
        "max_rollbacks",
    }
)


def _compute_kind(position: int, total: int) -> str:
    """BD-29: compute kind from position in the pipeline.

    - First stage  → "plan"
    - Last stage   → "verify"
    - Middle       → "execute"
    - Single stage → "plan" (degenerate — no agents, just supervisor creating+committing)

    Special case: if the stage's role is "supervisor" AND it's not the first
    or last, kind is still computed by position — but callers should validate
    that supervisor only appears at endpoints.
    """
    if total <= 1:
        return "plan"
    if position == 0:
        return "plan"
    if position == total - 1:
        return "verify"
    return "execute"


def validate_pipeline_stages(stages: Any, source: str) -> list[dict[str, Any]]:
    """A-06: form check for a pipeline stage list.

    Shared by load_stages (hand-written YAML) and the write path
    (awf.api.write_pipeline) — one rule set, no duplicated checks.
    Type-level checks only: name uniqueness, rollback-target existence
    and role-file resolution stay load-time warnings in load_stages.

    Raises:
        AwfApiError: stages not a non-empty list, a stage not a mapping,
            unknown keys, a missing/empty/non-string role, a policy that
            is not an allowed string (rollback_to:<stage> only on
            on_blocked/on_rejected), or a negative/non-integer budget.
    """
    if not isinstance(stages, list) or not stages:
        raise AwfApiError(
            f"Pipeline {source}: 'stages' must be a non-empty list of stage "
            "mappings (each with at least 'role')."
        )
    result: list[dict[str, Any]] = []
    for i, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise AwfApiError(
                f"Pipeline {source}: stage #{i} must be a mapping, "
                f"got {type(stage).__name__}."
            )
        label = str(stage.get("name") or f"#{i}")
        unknown = set(stage) - ALLOWED_STAGE_KEYS
        if unknown:
            raise AwfApiError(
                f"Pipeline {source}: stage '{label}' uses unknown keys: "
                f"{sorted(unknown)}. Allowed keys: {sorted(ALLOWED_STAGE_KEYS)}."
            )
        role = stage.get("role")
        if not isinstance(role, str) or not role.strip():
            raise AwfApiError(
                f"Pipeline {source}: stage '{label}' has no non-empty 'role' "
                f"— every stage needs one (got {role!r})."
            )
        for pk in _POLICY_KEYS:
            if pk in stage and stage[pk] is not None:
                value = stage[pk]
                if not isinstance(value, str) or not value:
                    raise AwfApiError(
                        f"Pipeline {source}: stage '{label}' {pk} must be a "
                        f"string policy, got {value!r}. Expected one of: "
                        f"{', '.join(sorted(_POLICY_ALLOWED[pk]))}."
                    )
                if value.startswith("rollback_to:"):
                    if pk not in _ROLLBACK_TO_KEYS:
                        raise AwfApiError(
                            f"Pipeline {source}: stage '{label}' {pk}="
                            f"{value!r} — rollback_to:<stage> is only "
                            f"supported for {'/'.join(_ROLLBACK_TO_KEYS)} "
                            f"(the transition resolver ignores it on {pk})."
                        )
                elif value not in _POLICY_ALLOWED[pk]:
                    raise AwfApiError(
                        f"Pipeline {source}: stage '{label}' {pk}={value!r} "
                        f"is not a valid policy. Expected one of: "
                        f"{', '.join(sorted(_POLICY_ALLOWED[pk]))}."
                    )
        for bk in ("max_retries", "max_rollbacks"):
            if bk in stage and stage[bk] is not None:
                value = stage[bk]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise AwfApiError(
                        f"Pipeline {source}: stage '{label}' {bk} must be an "
                        f"integer, got {value!r}."
                    )
                if value < 0:
                    raise AwfApiError(
                        f"Pipeline {source}: stage '{label}' {bk} must be "
                        f"non-negative, got {value}."
                    )
        result.append(stage)
    return result


def validate_pipeline_document(data: Any, source: str) -> list[dict[str, Any]]:
    """A-06: validate a parsed pipeline document (root + stage list).

    Root must be a mapping; ``stages`` must be a non-empty list of stage
    mappings (validate_pipeline_stages). Raises AwfApiError on any form
    error — before any Stage is built, so a bad file has no side effects.
    """
    if not isinstance(data, dict):
        raise AwfApiError(
            f"Pipeline {source}: the document root must be a mapping "
            f"(top-level keys like 'name' and 'stages'), got "
            f"{type(data).__name__}."
        )
    return validate_pipeline_stages(data.get("stages"), source)


def load_stages(pipeline_file: str | Path) -> list[Stage]:
    """Parse a pipeline YAML file and return a list of Stage objects.

    BD-29: kind is computed from position.
    A-06: the document form is validated (validate_pipeline_document)
    before any Stage is built — a malformed hand-written YAML raises
    AwfApiError at load instead of surfacing mid-run. Byte-level
    corruption (non-UTF-8, broken YAML) keeps the AUD06-16 degrade:
    reason to stderr, empty list.
    """
    import yaml

    p = Path(pipeline_file)
    try:
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, UnicodeDecodeError, OSError) as e:
        # AUD06-16: non-UTF-8 bytes and read errors used to escape past
        # `except yaml.YAMLError` (UnicodeDecodeError is a ValueError) and
        # crash the caller with a raw traceback.
        print(f"ERROR: pipeline file {p.name} is malformed: {e}", file=sys.stderr)
        return []

    raw_stages = validate_pipeline_document(data, p.name)
    result: list[Stage] = []
    total = len(raw_stages)

    for dict_index, s in enumerate(raw_stages):
        kwargs: dict[str, Any] = {}
        for key in ("name", "role", "description"):
            kwargs[key] = s.get(key, "")
        for pk in _POLICY_KEYS:
            kwargs[pk] = s.get(pk, _DEFAULTS[pk])
        kwargs["max_retries"] = s.get("max_retries", _DEFAULTS["max_retries"])
        kwargs["max_rollbacks"] = s.get("max_rollbacks", _DEFAULTS["max_rollbacks"])
        kwargs["kind"] = _compute_kind(dict_index, total)
        result.append(Stage(**kwargs))

    # NEG-2 (dogfood-11): rollback targets must exist. Warn at load time
    # instead of discovering a broken target mid-run (the transition then
    # escalates instead of hard-stopping).
    # FU-19 (AUD03-02 tail): only the keys the resolver actually reads the
    # prefix on (on_blocked/on_rejected) get the target-existence check.
    stage_names = {st.name for st in result}
    for st in result:
        for pk in _ROLLBACK_TO_KEYS:
            policy = getattr(st, pk)
            if policy.startswith("rollback_to:"):
                rb_target = policy.split(":", 1)[1]
                if rb_target not in stage_names:
                    print(
                        f"WARNING: stage '{st.name}' rolls back to '{rb_target}' which "
                        f"is not a stage in this pipeline — a rejection will escalate "
                        f"to the supervisor instead.",
                        file=sys.stderr,
                    )

    # AUD03-06: duplicate stage names used to load silently. The engine
    # always resolves the FIRST match (_find_stage_index), so later
    # duplicates are unreachable (rollback targets, dashboard labels).
    name_counts: dict[str, int] = {}
    for st in result:
        name_counts[st.name] = name_counts.get(st.name, 0) + 1
    for name, count in name_counts.items():
        if count > 1:
            print(
                f"WARNING: duplicate stage name '{name}' (used {count} times) — "
                "the engine always resolves the first match; the later "
                "stage(s) are unreachable. Give each stage a unique name.",
                file=sys.stderr,
            )

    # A-06: policy words are validated in validate_pipeline_document —
    # unknown words, non-string values and rollback_to: on the wrong key
    # are load errors now (AUD04-02/FU-19 used to warn).

    # Day-2 spec (second tier): stage roles must resolve to a role .md
    # (project .agentic/roles/ or the global awf roles dir). A typo used to
    # produce a stage that fails only at runtime with an opaque error.
    project_dir = p.parent.parent.parent  # <project>/.agentic/pipelines/x.yaml
    from .supervisor import resolve_role_file  # lazy — avoids import cycle

    for st in result:
        # A-06: role is guaranteed non-empty by validate_pipeline_document.
        try:
            resolve_role_file(st.role, project_dir)
        except RuntimeError:
            print(
                f"WARNING: stage '{st.name}' uses role '{st.role}' but no role file "
                f"was found (project .agentic/roles/ or global awf roles) — the "
                f"stage will fail at runtime.",
                file=sys.stderr,
            )

    return result


def active_pipeline_name(
    project_dir: str | Path,
    config: dict | None = None,
) -> str:
    """The pipeline name the runtime uses when none is given explicitly.

    Mirrors :func:`resolve_pipeline_file` (step 2): ``default_pipeline``
    from config.yaml wins over "default"; a value that is not a valid
    pipeline name (AUD14-05 rule) falls back to "default". Shared by
    ``awf_status`` / ``run_brief`` (RUN3 #1) so the displayed name always
    agrees with the file the engine would load.
    """
    if config is None:
        config = cfg_mod.load(Path(project_dir).resolve())
    declared = str(
        cfg_mod.get(config, "default_pipeline", "default") or "default"
    ).strip()
    if not declared or declared == "default":
        return "default"
    if declared in {".", ".."} or not re.fullmatch(r"[\w.-]+", declared):
        return "default"
    return declared


def list_pipeline_names(project_dir: str | Path) -> list[str]:
    """Sorted names of the pipeline files in .agentic/pipelines/.

    Only ``*.yaml`` files count — ``.bak`` backups (setup form, AUD06-04)
    are not pipelines. Returns an empty list when the directory is absent.
    """
    pipelines_dir = paths.agentic_dir(Path(project_dir).resolve()) / "pipelines"
    if not pipelines_dir.is_dir():
        return []
    return sorted(p.stem for p in pipelines_dir.glob("*.yaml") if p.is_file())


def resolve_pipeline_file(
    project_dir: str | Path,
    pipeline_name: str | None = None,
    config: dict | None = None,
) -> Path:
    """Determine which pipeline file to use.

    Order:
    1. Explicit *pipeline_name* → .agentic/pipelines/<name>.yaml
    2. default_pipeline from config.yaml → .agentic/pipelines/<that>.yaml
    3. Fallback .agentic/pipelines/default.yaml
    """
    project_dir = Path(project_dir).resolve()
    agentic = paths.agentic_dir(project_dir)
    pipelines_dir = agentic / "pipelines"

    if config is None:
        config = cfg_mod.load(project_dir)

    name = pipeline_name or cfg_mod.get(config, "default_pipeline", "default") or "default"

    # AUD14-05: pipeline_name is public input (awf_start(pipeline=...),
    # awf continue --pipeline, CLI). Reject names that could escape
    # .agentic/pipelines/ — "../../evil" used to load an external YAML.
    # Lazy import: awf.api pulls this module at package init.
    if name in {".", ".."} or not re.fullmatch(r"[\w.-]+", name):
        from .api._errors import AwfApiError

        raise AwfApiError(
            f"Invalid pipeline name {name!r}: use letters, digits, '_', '.' or "
            "'-' (no path separators), e.g. 'default' or 'custom-name'."
        )

    candidate = pipelines_dir / f"{name}.yaml"
    if candidate.exists():
        return candidate

    # RUN3 #1: an EXPLICIT name (awf_start(pipeline=...), awf start
    # --pipeline, continue --pipeline) that does not exist is a user error.
    # The old silent fallback to default.yaml ran the wrong pipeline — the
    # user asked for 'audit-llm' and got 'default'. Now: a clear error with
    # the list of what actually exists. A config-declared name
    # (default_pipeline) keeps the legacy fallback (pinned by
    # test_fallback_to_default_yaml).
    if pipeline_name:
        from .api._errors import AwfApiError

        available = list_pipeline_names(project_dir)
        raise AwfApiError(
            f"Pipeline {name!r} not found: no {candidate.name} in "
            f"{pipelines_dir}. "
            f"Available pipelines: {', '.join(available) if available else '(none)'}."
        )

    if name != "default":
        fallback = pipelines_dir / "default.yaml"
        if fallback.exists():
            return fallback

    raise FileNotFoundError(
        f"No pipeline file found. Expected {candidate}"
        + (f" or {fallback}" if name != "default" else "")
    )
