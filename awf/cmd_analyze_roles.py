"""``awf analyze-roles`` — thin CLI wrapper.

All business logic lives in :mod:`awf.api.roles`. This module only handles
argparse glue + human-readable printing.

BD-31: supervisor (current opencode in user's chat) reads all role .md
files, identifies overlaps/duplications in zones of responsibility, and
proposes targeted patches to strengthen each role's instructions for the
current pipeline. User confirms patches before they're applied.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .api.roles import AnalyzeData, AnalyzeError, analyze_roles_core


def run(args: Any) -> int:
    """Execute ``awf analyze-roles`` — CLI thin wrapper over analyze_roles_core."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    dry_run = getattr(args, "dry_run", False)

    try:
        data = analyze_roles_core(project_dir, dry_run=dry_run)
    except AnalyzeError as e:
        print(str(e))
        return 1

    _print_report(data, dry_run=dry_run)
    return 0


def _print_report(data: AnalyzeData, *, dry_run: bool) -> None:
    """Format AnalyzeData as human-readable CLI output."""
    print("=" * 60)
    print("  BD-31: SKILL-AWARE ROLE ANALYSIS")
    print("=" * 60)
    print()
    print(f"Pipeline team (in order): {', '.join(data.pipeline_roles) or '(no pipeline.yaml found)'}")
    print(f"Role files analyzed: {len(data.zones)}")
    print()

    print("Inferred zones:")
    for role, zone in data.zones.items():
        marker = "✓" if zone != "generalist (undefined zone)" else "?"
        print(f"  {marker} {role}: {zone}")
    print()

    if data.overlaps:
        print(f"Overlaps detected ({len(data.overlaps)}):")
        for a, b, zone in data.overlaps:
            print(f"  ⚠ {a}  ⟷  {b}  (both: {zone})")
        print()
    else:
        print("No overlaps detected. Roles are well-differentiated.")
        print()

    if not data.patches:
        print("Nothing to patch. Roles look clean.")
        return

    print("Proposed patches:")
    for role, addendum in data.patches.items():
        preview = addendum.replace("\n", " ")[:120]
        print(f"  + {role}.md: {preview}...")
    print()

    if dry_run:
        print("--dry-run: patches generated but NOT applied.")
        print("Re-run without --dry-run to write them.")
        return

    if data.failed:
        for role in data.failed:
            print(f"  ✗ failed {role}.md (OSError during write)")
    for role in data.patches:
        if role not in data.failed:
            print(f"  ✓ patched {role}.md")
    print()
    print(f"Done. {len(data.patches) - len(data.failed)} role(s) strengthened with pipeline-specific disambiguation.")
    print("Re-commit role files if you want to track changes in git.")
