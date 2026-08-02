"""Increment planning: persist supervisor-proposed decomposition variants.

Pipeline:
  1. supervisor studies vision/requirements (via awf_load_supervisor_context).
  2. supervisor generates 2-3 increment variants (creative LLM work).
  3. supervisor calls awf_open_increment_planning_form(options, project_dir).
  4. user picks a variant in browser form.
  5. plugin submit handler calls apply_increment_plan() — this function.
  6. plan.md updated with selected variant; supervisor dispatches TODOs.

This module handles step 6 only (the persist). Variant generation is LLM
work and lives in supervisor's reasoning, not in deterministic code.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .._atomic import atomic_write_text
from ._errors import AwfApiError
from ._helpers import require_agentic
from ._results import ApplyIncrementPlanResult


def apply_increment_plan(
    project_dir: Path,
    plan_markdown: str,
    *,
    selected_variant_id: str | None = None,
    variants: list[dict[str, Any]] | None = None,
) -> ApplyIncrementPlanResult:
    """Persist supervisor's selected increment plan to ``plan.md``.

    Replaces the plan.md stub created by ``init_project`` with the actual
    increment sequence chosen by the user.

    Args:
        project_dir: awf project root.
        plan_markdown: full markdown body for plan.md. Supervisor writes
            this from the selected variant — typically includes:
            - Chosen increment strategy (vertical slice / horizontal layers / ...)
            - Ordered list of increments, each with:
              - Goal (what user-visible value lands)
              - Artefacts produced
              - Dependencies on previous increments
              - Estimated TODOs count
            - Out-of-scope for this iteration
        selected_variant_id: when variants were offered to user via form,
            id of the variant they picked. Stored as frontmatter for
            traceability.
        variants: full list of variants that were offered (for history).
            Optional — if supervisor offered verbally in chat, omit.

    Returns:
        ApplyIncrementPlanResult with path written + summary.

    Raises:
        AwfApiError: if .agentic/ missing or plan_markdown empty.
    """
    if not plan_markdown or not plan_markdown.strip():
        raise AwfApiError("plan_markdown is required (non-empty)")

    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    plan_path = project_dir / ".agentic" / "phases" / "plan.md"

    # Build frontmatter for traceability (which variant was chosen, when)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    frontmatter_data: dict[str, Any] = {
        "updated_at": ts,
    }
    if selected_variant_id:
        frontmatter_data["selected_variant"] = selected_variant_id
    if variants:
        frontmatter_data["variants_offered"] = len(variants)

    frontmatter = "---\n" + yaml.safe_dump(
        frontmatter_data, default_flow_style=False, allow_unicode=True, sort_keys=False
    ) + "---\n\n"

    atomic_write_text(plan_path, frontmatter + plan_markdown.lstrip() + "\n")

    return ApplyIncrementPlanResult(
        plan_path=str(plan_path),
        selected_variant_id=selected_variant_id,
        variants_offered=len(variants) if variants else None,
        updated_at=ts,
    )


__all__ = ["apply_increment_plan"]
