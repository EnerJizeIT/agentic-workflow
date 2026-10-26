"""``awf dispatch`` — create a unit atomically (TODO + baseline + .ready).

Thin CLI wrapper around :func:`awf.api.dispatch_todo`, the same entry point
the MCP ``awf_dispatch_todo`` uses. Content comes from ``--content`` or
``--content-file``; ``--source-ref`` inserts the source-of-truth section.
``--resume TODO-NNNN`` finishes a dispatch killed mid-baseline from its
write-ahead journal, ``--no-baseline-tests`` skips the smoke command.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def _print_result(result: Any) -> None:
    print(
        f"Dispatched {result.todo_id} (baseline {result.baseline_sha})"
        + (" [resumed]" if getattr(result, "resumed", False) else "")
    )
    if getattr(result, "baseline_test_status", ""):
        timeout = getattr(result, "baseline_timeout", None)
        suffix = f", timeout {timeout:g}s" if timeout is not None else ""
        print(
            f"  baseline tests: {result.baseline_test_status}{suffix}"
        )
    for path in result.files_written:
        print(f"  {path}")
    for warning in result.pre_check_warnings:
        print(f"  warning: {warning}")
    # TODO-0242 (B.13): the soft size lock reaches the CLI too (parity with
    # the MCP answer) — a warning, never a refusal.
    if result.size_warning:
        print(f"  warning: {result.size_warning}")


def run(args: Any) -> int:
    """Execute ``awf dispatch`` and return exit code."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    no_baseline_tests = bool(getattr(args, "no_baseline_tests", False))
    resume_id = str(getattr(args, "resume", "") or "")

    if resume_id:
        try:
            result = api.resume_dispatch(
                project_dir, resume_id, no_baseline_tests=no_baseline_tests
            )
        except api.AwfApiError as e:
            print(f"ERROR: {e}")
            return 1
        _print_result(result)
        return 0

    content = str(getattr(args, "content", "") or "")
    content_file = str(getattr(args, "content_file", "") or "")
    if not content and not content_file:
        print("ERROR: --content or --content-file is required (or --resume TODO-NNNN)")
        return 1
    if content_file:
        try:
            content = Path(content_file).resolve().read_text(encoding="utf-8")
        except OSError as e:
            print(f"ERROR: cannot read --content-file {content_file!r}: {e}")
            return 1

    source_ref = str(getattr(args, "source_ref", "") or "") or None
    try:
        result = api.dispatch_todo(
            project_dir,
            content,
            role=getattr(args, "role", None) or None,
            todo_id=getattr(args, "todo_id", None) or None,
            pipeline=getattr(args, "pipeline", None) or None,
            source_ref=source_ref,
            no_baseline_tests=no_baseline_tests,
            force=bool(getattr(args, "force", False)),
        )
    except api.AwfApiError as e:
        print(f"ERROR: {e}")
        return 1

    _print_result(result)
    return 0
