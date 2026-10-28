"""R1 (ops reliability): the ONE resolver for a stage's time cap.

Priority: explicit caller value (CLI ``--timeout`` / MCP ``timeout``) →
stage key ``timeout`` → ``automation.stage_timeout_seconds`` → 3600.

``None`` means "not given": the CLI and MCP defaults are ``None`` so an
explicit 3600 is distinguishable from the default (RR6). Anything else
must be a positive integer number of seconds — ``bool`` is refused (in
Python ``True`` is an int) and so is a string. Every refusal names the
input it saw: ``--timeout`` / ``timeout``, the stage key or the project
key.

A bad stage key is a pipeline load error (``pipeline.validate_pipeline_stages``);
a bad project key refuses the launch before the lease, the PID file and
the first spawn (INV8).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config as _config
from ._errors import AwfApiError

DEFAULT_SECONDS = 3600
PROJECT_KEY = "automation.stage_timeout_seconds"
STAGE_KEY = "timeout"

SOURCE_CALLER = "caller"
SOURCE_STAGE = "stage"
SOURCE_PROJECT = "project"
SOURCE_DEFAULT = "default"

_INVALID_HINT = "the stage timeout must be a positive integer number of seconds"


@dataclass(frozen=True)
class Resolved:
    """The effective cap and where it came from (``source`` in the log)."""

    seconds: int
    source: str


def is_valid(value: object) -> bool:
    """True for a positive integer number of seconds (``bool`` excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_caller(value: object, name: str = "--timeout") -> int | None:
    """Normalise a caller value; ``None`` stays "not given".

    Raises:
        AwfApiError: the value is not a positive integer — names ``name``.
    """
    if value is None:
        return None
    if not is_valid(value):
        raise AwfApiError(f"invalid {name} {value!r}: {_INVALID_HINT}")
    return int(value)


def validate_project(config: dict) -> int | None:
    """``automation.stage_timeout_seconds``; ``None`` when the key is absent."""
    value = _config.get(config, PROJECT_KEY)
    if value is None:
        return None
    if not is_valid(value):
        raise AwfApiError(f"invalid {PROJECT_KEY} {value!r}: {_INVALID_HINT}")
    return int(value)


def project_refusal(config: dict) -> str | None:
    """The launch-refusal line for a bad project cap, or ``None`` when it is fine.

    One home for that wording: ``start_pipeline``/``continue_pipeline``/
    ``retry_stage`` and ``run_next`` carry the message in different result
    types, so they must not each spell the rule and the sentence again.
    """
    try:
        validate_project(config)
    except AwfApiError as e:
        return f"Launch refused: {e}"
    return None


def resolve(
    caller: object,
    stage_timeout: object,
    config: dict,
) -> Resolved:
    """Resolve the effective cap and its source from the three inputs."""
    caller_seconds = validate_caller(caller)
    if caller_seconds is not None:
        return Resolved(caller_seconds, SOURCE_CALLER)
    if stage_timeout is not None:
        if not is_valid(stage_timeout):
            raise AwfApiError(
                f"invalid stage key '{STAGE_KEY}' {stage_timeout!r}: "
                f"{_INVALID_HINT}"
            )
        return Resolved(int(stage_timeout), SOURCE_STAGE)
    project_seconds = validate_project(config)
    if project_seconds is not None:
        return Resolved(project_seconds, SOURCE_PROJECT)
    return Resolved(DEFAULT_SECONDS, SOURCE_DEFAULT)


__all__ = [
    "DEFAULT_SECONDS",
    "PROJECT_KEY",
    "STAGE_KEY",
    "Resolved",
    "is_valid",
    "project_refusal",
    "resolve",
    "validate_caller",
    "validate_project",
]
