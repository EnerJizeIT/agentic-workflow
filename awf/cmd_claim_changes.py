"""``awf claim-changes`` — R2.2 (roadmap R2) code transfer of a unit.

Preview (default): read the current changes of ``source_todo`` and print the
PLAN (its digest and files). Apply (``--apply <digest>``): write the claim
record after the owner confirms the exact digest — refused when the tree
moved since the preview.

Thin CLI wrapper around :mod:`awf.claim_changes`; the engine is MCP-agnostic
and lives in the core module.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    project_dir = Path(getattr(args, "project_dir", "."))
    source_todo = args.source_todo
    # LOW-2: ``--paths`` is repeatable (one path per occurrence) — a path that
    # CONTAINS a comma can be named, unlike the old comma-joined string.
    # ``splitlines`` keeps a shell value with a trailing newline from becoming
    # a phantom empty entry; each non-blank line stays one whole path.
    raw_paths = getattr(args, "paths", None)
    if raw_paths is None:
        # No ``--paths`` → the documented default: claim EVERY change. The
        # comprehension below iterates ``raw_paths``, so the None must become
        # an empty list here (the old dead ``paths = []`` never reached it and
        # the default crashed with TypeError).
        raw_paths = []
    elif isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    else:
        raw_paths = list(raw_paths)
    paths = [
        p for chunk in raw_paths for p in chunk.splitlines() if p.strip()
    ]
    apply_digest = getattr(args, "apply", None)

    try:
        if apply_digest:
            plan = api.plan_claim_changes(
                project_dir, source_todo, paths=paths or None
            )
            api.apply_claim_changes(
                project_dir, plan, confirmed_digest=apply_digest
            )
            print(f"claim applied: {source_todo} — {len(plan.files)} change(s)")
            print(f"  digest: {plan.digest}")
            return 0

        plan = api.plan_claim_changes(project_dir, source_todo, paths=paths or None)
    except api.AwfApiError as e:
        print(f"ERROR: {e}")
        return 1

    print(f"claim plan for {source_todo} ({len(plan.files)} change(s)):")
    print(f"  baseline: {plan.baseline_sha}")
    print(f"  digest:   {plan.digest}")
    if not plan.files:
        print("  (no changes — nothing to claim)")
    for f in plan.files:
        extra = f" from {f.old_path}" if f.old_path else ""
        print(f"  {f.op:<10} {f.path}{extra}  mode={f.mode or '-'} blob={f.blob or '-'}")
    print()
    print("Apply with: awf claim-changes "
          f"{source_todo} --apply {plan.digest}")
    return 0
