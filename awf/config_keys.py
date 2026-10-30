"""R5 (ops reliability): the registry of ``.agentic/config.yaml`` keys the code reads.

``awf doctor`` names the keys of a live config that no code reads (OPS7: the
live project carried ``retry.max_attempts`` / ``retry.backoff_seconds`` long
after their readers were removed, and the template still advertised them).
"Dead" here means "not read anywhere in ``awf``" — not "absent from the
template": a key the template dropped may still be read.

Keeping the registry honest is not a documentation chore: the ratchet test
(``tests/unit/test_config_keys_registry.py``) re-scans ``awf/**/*.py`` for
literal config keys and fails when one is missing here. A new reader therefore
cannot ship without touching this file (D5: runtime introspection cannot see
literal reads, so the registry plus the scan is the only reliable pair).

Wildcards: a single trailing ``.*`` is allowed for subtrees whose leaves are
data, not code (``models.*`` — the role names are the user's).
"""
from __future__ import annotations

from typing import Any

#: Every dotted key (or ``prefix.*`` family) that some code under ``awf/``
#: reads from ``.agentic/config.yaml``.
READ_KEYS: frozenset[str] = frozenset({
    # ── top-level ────────────────────────────────────────────────────────
    "config_version",
    "default_pipeline",
    "discipline",
    "discipline.dirty_policy",
    # models is a per-role map: the role names are the user's, so the
    # whole subtree counts as read (models.<role>.model / .description /
    # .agent_name).
    "models.*",
    # ── project ──────────────────────────────────────────────────────────
    # the section is read as a dict; ``project.name`` is its only live leaf.
    # ``project.root`` is deliberately absent: it had no reader and the init
    # template no longer writes it (AUD-0296 CTX3), so a config that still
    # carries it is naming a genuinely dead key.
    "project",
    "project.name",
    # ── verification ─────────────────────────────────────────────────────
    # verify.py::verify_command_keys is the single home for the four
    # command keys; the ratchet test imports it and checks each one here.
    "verification.baseline_cmd",
    "verification.baseline_timeout",
    "verification.build_cmd",
    "verification.lint_cmd",
    "verification.test_cmd",
    "verification.typecheck_cmd",
    # ── automation ───────────────────────────────────────────────────────
    "automation.auto_done",
    "automation.net_retry_limit",
    "automation.no_output_timeout_seconds",
    "automation.owner_idle_minutes",
    "automation.plan_checkpoint",
    "automation.preflight_timeout_seconds",
    "automation.readonly_roles",
    "automation.runner_dir",
    "automation.stage_timeout_seconds",
    "automation.stale_stage_minutes",
    "automation.verify_pack",
    # ── engine ───────────────────────────────────────────────────────────
    "engine.heartbeat_seconds",
    "engine.heartbeat_stale_seconds",
    "engine.kill_with_parent",
    "engine.reuse_session",
    "engine.session_max_age_minutes",
    # ── limits / checklist ───────────────────────────────────────────────
    "limits.max_review_passes",
    "checklist.closer_attempts",
    # ── retry / wait / run ───────────────────────────────────────────────
    # retry.max_attempts and retry.backoff_seconds are deliberately absent:
    # nothing reads them (that is what doctor reports).
    "retry.silent_work_attempts",
    "wait.cap_seconds",
    "run.stall_minutes",
    # ── phases / supervisor / context / feedback ─────────────────────────
    "phases.current",
    "supervisor.instructions",
    "context.message",
    "feedback.dir",
    # ── spec budget ──────────────────────────────────────────────────────
    "spec.budget.document",
    "spec.budget.package",
    # ── worker ───────────────────────────────────────────────────────────
    "worker.mcp_servers",
    # ── opencode 2.x (K1–K4: launch mode, managed opt-in) ────────────────
    "opencode.standalone",
    "opencode.managed",
    # ── metrics (awf metrics) ────────────────────────────────────────────
    "metrics.directory",
    "metrics.mirror_dir",
    "metrics.models_cache",
    "metrics.output_dir",
    "metrics.reference_model",
    "metrics.since",
    "metrics.subscriptions_cache",
    "metrics.subscriptions_url",
    "metrics.supervisor_titles",
    # tiers is a map {top, optimal, cheap} of id lists: the class names are
    # data, not code, so the subtree counts as read (its leaves would
    # otherwise be reported dead).
    "metrics.tiers.*",
})


def is_read_key(leaf: str) -> bool:
    """True when *leaf* is read by some code (exact key or a registered family)."""
    if leaf in READ_KEYS:
        return True
    for key in READ_KEYS:
        if key.endswith(".*") and (
            leaf == key[:-2] or leaf.startswith(key[:-1])
        ):
            return True
    return False


def leaf_keys(config: Any, prefix: str = "") -> list[str]:
    """Expand *config* to its dotted leaves, in the config's own order.

    A non-dict value is a leaf; an empty mapping has no keys to report (an
    empty section is not a dead key). Ordering is the caller's concern —
    ``dead_keys`` sorts and de-duplicates.
    """
    if isinstance(config, dict):
        leaves: list[str] = []
        for key, value in config.items():
            leaves.extend(leaf_keys(value, f"{prefix}{key}."))
        return leaves
    return [prefix[:-1]] if prefix else []


def dead_keys(config: dict[str, Any]) -> list[str]:
    """Dotted leaves of *config* that no code reads (sorted, unique)."""
    if not isinstance(config, dict):
        return []
    return sorted({leaf for leaf in leaf_keys(config) if not is_read_key(leaf)})


__all__ = ["READ_KEYS", "dead_keys", "is_read_key", "leaf_keys"]
