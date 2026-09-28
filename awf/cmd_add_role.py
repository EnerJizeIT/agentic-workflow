"""Port of lib/add-role.sh — ``awf add-role`` command.

Thin CLI wrapper around :func:`awf.api.add_role` and the ORCH M5.1
draft-area functions (``list_role_drafts`` / ``adopt_role_draft`` /
``discard_role_draft``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def _active_modes(args: Any) -> list[str]:
    """Which draft-area flags are set (empty list = create mode)."""
    return [
        m
        for m, on in (
            ("list", bool(getattr(args, "list_drafts", False))),
            ("adopt", bool(getattr(args, "adopt", "") or "")),
            ("discard", bool(getattr(args, "discard", "") or "")),
            ("draft", bool(getattr(args, "draft", False))),
        )
        if on
    ]


def run(args: Any) -> int:
    """Execute ``awf add-role`` and return exit code."""
    modes = _active_modes(args)
    if len(modes) > 1:
        print(
            "Pick ONE of --draft / --adopt NAME / --discard NAME / "
            "--list-drafts."
        )
        return 1
    # --draft is the create mode with draft=True; no flag = plain create.
    if "draft" in modes or not modes:
        return run_create(args)

    mode = modes[0]  # list / adopt / discard
    project_dir = Path(getattr(args, "project_dir", "."))
    try:
        if mode == "list":
            result = api.list_role_drafts(project_dir=project_dir)
        elif mode == "adopt":
            result = api.adopt_role_draft(
                project_dir=project_dir,
                role_name=getattr(args, "adopt", ""),
            )
        else:  # discard
            result = api.discard_role_draft(
                project_dir=project_dir,
                role_name=getattr(args, "discard", ""),
            )
    except api.AwfApiError as e:
        print(str(e))
        return 1

    if mode == "list":
        if result.drafts:
            for d in result.drafts:
                print(f"{d['name']}\t{d['source']}\t{d['created']}\t{d['file']}")
        else:
            print("No draft candidates (.agentic/roles/draft/ is empty or absent).")
        return 0
    if mode == "adopt":
        print(f"Adopted: {result.role_file} (draft candidate removed)")
        print()
        print("Next steps:")
        print(f"  1. Edit the role instructions in {result.role_file}")
        print(
            "  2. Add model config to .agentic/config.yaml "
            f"under models:{result.role_name}"
        )
        print(
            "  3. Add a stage to your pipeline YAML "
            f"that uses role: {result.role_name}"
        )
        return 0
    # mode == "discard"
    print(f"Discarded: candidate removed, trace kept at {result.trace_file}")
    return 0


def run_create(args: Any) -> int:
    """Create mode (live or --draft): the original add-role behavior."""
    role_name = getattr(args, "name", "") or ""
    description = getattr(args, "description", "") or ""
    model = getattr(args, "model", "") or ""
    from_skill = getattr(args, "from_skill", "") or ""
    force = bool(getattr(args, "force", False))
    draft = bool(getattr(args, "draft", False))
    project_dir = Path(getattr(args, "project_dir", "."))

    if not role_name:
        print(
            "name is required to create a role "
            "(or use --list-drafts / --adopt NAME / --discard NAME)"
        )
        return 1

    # --from-skill replaces the template — there is no model slot to fill,
    # so no interactive prompt (also keeps the command scriptable).
    if not model and not from_skill:
        model = input(
            f"Model id for role '{role_name}' (e.g. claude-sonnet-4-20250514, gpt-4.1): "
        ).strip()

    try:
        result = api.add_role(
            project_dir=project_dir,
            role_name=role_name,
            description=description,
            model=model,
            from_skill=from_skill,
            force=force,
            draft=draft,
        )
    except api.AwfApiError as e:
        print(str(e))
        return 1

    print(f"Created: {result.role_file}")
    if from_skill:
        print(f"  (content copied from skill '{from_skill}')")
    print()
    if draft:
        print("Next steps:")
        print(f"  1. Check the candidate: {result.role_file}")
        print(f"  2. Adopt: awf add-role --adopt {result.role_name}")
        print(
            f"     (or discard: awf add-role --discard {result.role_name})"
        )
        return 0
    print("Next steps:")
    print(f"  1. Edit the role instructions in {result.role_file}")
    print(f"  2. Add model config to .agentic/config.yaml under models:{role_name}")
    print(f"  3. Add a stage to your pipeline YAML that uses role: {role_name}")
    return 0
