"""Pipeline parsing — Stage dataclass, load_stages, resolve_pipeline_file.

BD-29: pipeline stages no longer have an `action` field. Awf computes the
kind (plan / execute / verify) from the stage's position:

- Stage index 0           → kind="plan"   (supervisor creates TODO)
- Stage index N-1         → kind="verify" (supervisor verifies + commits)
- All stages between      → kind="execute" (agents do work)

For backward compat, pipeline.yaml files written before BD-29 may still
carry an `action:` field — it's read but ignored. kind always wins.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths


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
    on_passed: str = "next"
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
    "on_passed": "next",
    "on_failed": "escalate",
    "max_retries": 1,
    "max_rollbacks": 3,
}

_POLICY_KEYS = [
    "on_blocked", "on_approved", "on_rejected", "on_passed", "on_failed",
]

# AUD04-02: allowed plain words per policy key (rollback_to:<stage> is
# checked separately). The resolver used to silently reinterpret unknown
# words (on_approved/on_passed → "next", on_rejected/on_failed → "escalate"),
# which hid typos — now the loader warns.
_POLICY_ALLOWED = {
    "on_blocked": {"escalate", "stop"},
    "on_approved": {"next", "commit_and_next", "commit_and_report"},
    "on_rejected": {"escalate", "replan"},
    "on_passed": {"next", "commit_and_next", "commit_and_report"},
    "on_failed": {"escalate", "replan"},
}


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


def load_stages(pipeline_file: str | Path) -> list[Stage]:
    """Parse a pipeline YAML file and return a list of Stage objects.

    BD-29: kind is computed from position; `action:` field in YAML is read
    for back-compat but ignored.
    """
    import yaml

    p = Path(pipeline_file)
    try:
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"ERROR: pipeline file {p.name} is malformed: {e}", file=sys.stderr)
        return []

    raw_stages = (data or {}).get("stages") or []
    result: list[Stage] = []
    total = len([s for s in raw_stages if isinstance(s, dict)])

    dict_index = 0  # position among dict-entries only (for _compute_kind)
    for s in raw_stages:
        if not isinstance(s, dict):
            continue
        kwargs: dict[str, Any] = {}
        for key in ("name", "role", "description"):
            kwargs[key] = s.get(key, "")
        for pk in _POLICY_KEYS:
            kwargs[pk] = s.get(pk, _DEFAULTS[pk])
        mr = s.get("max_retries", _DEFAULTS["max_retries"])
        try:
            kwargs["max_retries"] = int(mr)
        except (ValueError, TypeError):
            kwargs["max_retries"] = _DEFAULTS["max_retries"]
        mrb = s.get("max_rollbacks", _DEFAULTS["max_rollbacks"])
        try:
            kwargs["max_rollbacks"] = int(mrb)
        except (ValueError, TypeError):
            kwargs["max_rollbacks"] = _DEFAULTS["max_rollbacks"]
        kwargs["kind"] = _compute_kind(dict_index, total)
        result.append(Stage(**kwargs))
        dict_index += 1

    # NEG-2 (dogfood-11): rollback targets must exist. Warn at load time
    # instead of discovering a broken target mid-run (the transition then
    # escalates instead of hard-stopping).
    stage_names = {st.name for st in result}
    for st in result:
        for policy in (st.on_blocked, st.on_rejected, st.on_failed):
            if policy.startswith("rollback_to:"):
                rb_target = policy.split(":", 1)[1]
                if rb_target not in stage_names:
                    print(
                        f"WARNING: stage '{st.name}' rolls back to '{rb_target}' which "
                        f"is not a stage in this pipeline — a rejection will escalate "
                        f"to the supervisor instead.",
                        file=sys.stderr,
                    )

    # AUD04-02: unknown policy words used to be silently reinterpreted by the
    # resolver (on_approved/on_passed → "next", on_rejected/on_failed →
    # "escalate"), which hid typos like "on_blocked: halt". Warn at load time.
    for st in result:
        for pk in _POLICY_KEYS:
            value = getattr(st, pk)
            if not isinstance(value, str) or not value:
                continue
            if value.startswith("rollback_to:"):
                continue  # target existence checked above
            if value not in _POLICY_ALLOWED[pk]:
                print(
                    f"WARNING: stage '{st.name}' has {pk}={value!r} — expected one of: "
                    f"{', '.join(sorted(_POLICY_ALLOWED[pk]))}. The transition resolver "
                    f"will fall back to the default policy for {pk}.",
                    file=sys.stderr,
                )

    # Day-2 spec (second tier): stage roles must resolve to a role .md
    # (project .agentic/roles/ or the global awf roles dir). A typo used to
    # produce a stage that fails only at runtime with an opaque error.
    project_dir = p.parent.parent.parent  # <project>/.agentic/pipelines/x.yaml
    from .supervisor import resolve_role_file  # lazy — avoids import cycle

    for st in result:
        role = st.role or ""
        if not role:
            print(
                f"WARNING: stage '{st.name}' has no role — it will fail at runtime.",
                file=sys.stderr,
            )
            continue
        try:
            resolve_role_file(role, project_dir)
        except RuntimeError:
            print(
                f"WARNING: stage '{st.name}' uses role '{role}' but no role file "
                f"was found (project .agentic/roles/ or global awf roles) — the "
                f"stage will fail at runtime.",
                file=sys.stderr,
            )

    return result


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

    candidate = pipelines_dir / f"{name}.yaml"
    if candidate.exists():
        return candidate

    if name != "default":
        fallback = pipelines_dir / "default.yaml"
        if fallback.exists():
            return fallback

    raise FileNotFoundError(
        f"No pipeline file found. Expected {candidate}"
        + (f" or {fallback}" if name != "default" else "")
    )
