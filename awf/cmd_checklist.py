"""TODO-0244: ``awf checklist show|check|apply|hash <todo>``.

Local CLI only — no MCP tool: the checklist is a dispatch/verify surface,
not a conversation surface. ``apply`` is the deliberate exception for an
ALREADY dispatched unit that is not started: same composition as dispatch,
idempotent, backup first; a started or archived unit is refused.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import paths, todos
from ._markdown import append_block
from .api.hygiene import backup_and_write_todo, not_started_reason
from .checklist_gate import run as run_gate
from .checklists import (
    ChecklistError,
    block_hash,
    block_of,
    compose_block,
    validate_readable_block,
)
from .infra import managed_fs
from .todo_ids import is_valid_todo_id
from .unit_contract import parse_todo_contract


def _inbox_todo(project: Path, todo_id: str) -> Path:
    path = paths.inbox(project) / f"{todo_id}.md"
    if not path.is_file():
        raise ChecklistError(f"{todo_id}: no TODO in the inbox ({path})")
    return path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="surrogateescape")


def _shape_refusal(project: Path, todo_id: str) -> str:
    """Why ``apply`` must not touch this unit — empty means it is allowed."""
    if (paths.done_dir(project) / todo_id).is_dir():
        return (
            f"{todo_id} is archived in done/ — the checklist is frozen at "
            "dispatch for a finished unit"
        )
    reason = not_started_reason(project, todo_id)
    if reason:
        return (
            f"{reason} — the block is frozen for a live unit; only a "
            "not-started unit may be re-composed"
        )
    return ""


def _cmd_show(args: Any, project: Path) -> int:
    text = todos.read_todo_text(project, args.todo_id)
    if not text:
        raise ChecklistError(f"{args.todo_id}: no TODO found (inbox or archive)")
    block = block_of(text)
    if block is None:
        print(f"{args.todo_id}: checklist: none")
        return 0
    print(block)
    return 0


def _cmd_check(args: Any, project: Path) -> int:
    # A broken frozen block is the same refusal as an unreadable unit:
    # ``run`` raises ``ChecklistError``/``ChecklistGateError`` and the
    # top-level handler prints ``ERROR:`` + rc 2. No local conversion here —
    # both are ``ValueError`` and would land in the same place.
    result = run_gate(project, args.todo_id)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if result["status"] == "none":
            print(f"{args.todo_id}: checklist: none")
        else:
            for item in result["items"]:
                mark = {"ok": "PASS", "missing": "MISS", "manual": "MAN "}[
                    item["status"]
                ]
                line = f"{mark} {item['id']}"
                if item["detail"]:
                    line += f" — {item['detail']}"
                print(line)
            if result["missing"]:
                print("missing: " + ", ".join(result["missing"]))
            for warning in result["warnings"]:
                print(f"warning: {warning}")
    # ``none`` (no block at all) is not a failure — legacy units stay green;
    # an empty block never reaches here as none (the gate reports it failed).
    return 0 if result["status"] == "none" or result["ok"] else 1


def _cmd_hash(args: Any, project: Path) -> int:
    text = todos.read_todo_text(project, args.todo_id)
    if not text:
        raise ChecklistError(f"{args.todo_id}: no TODO found (inbox or archive)")
    digest = block_hash(text)
    if not digest:
        print(f"{args.todo_id}: checklist: none", file=sys.stderr)
        return 1
    print(digest)
    return 0


def _cmd_apply(args: Any, project: Path) -> int:
    # Shape FIRST: an archived unit has no inbox file, so an inbox-existence
    # check ahead of this would answer "no TODO in the inbox" instead of the
    # real reason (the frozen block of a finished unit is not re-composed).
    refusal = _shape_refusal(project, args.todo_id)
    if refusal:
        raise ChecklistError(refusal)
    path = _inbox_todo(project, args.todo_id)
    text = _read(path)
    contract, _unknown = parse_todo_contract(text)
    # The SAME composition + round-trip guard the dispatch uses: ``apply``
    # is the second writer of the frozen block, and a block the gate cannot
    # read must never reach the TODO through either door.
    composition, block = compose_block(project, text, contract)
    new_block = block.rstrip("\n")
    existing = block_of(text)
    if existing is not None and existing.rstrip("\n") == new_block:
        print(f"{args.todo_id}: checklist already up to date")
        return 0

    if existing is None:
        new_text = append_block(text, new_block)
    else:
        new_text = text.replace(existing, new_block, 1)
        if not new_text.endswith("\n"):
            new_text += "\n"
    # F14 (TODO-0244): ``apply`` is the third writer of the frozen block. The
    # composed block round-trips, but the TODO is what the gate reads — a body
    # ending in an unterminated fence would swallow the appended block and
    # leave the unit green with zero checks. Refuse before the write.
    validate_readable_block(new_text)
    # The ONE TODO write trace (backup with a unique name, CAS under the
    # project lock, orphan-backup cleanup on a failed write) — the same
    # helper update_todo/review_append use. A local backup+write here would
    # be a second spelling of the same invariant.
    backup = backup_and_write_todo(project, path, text, new_text)
    print(f"{args.todo_id}: checklist applied ({len(composition.items)} items)")
    print(f"backup: {backup}")
    if composition.suggested:
        print(
            "checklist_suggested: "
            + ", ".join(composition.suggested)
            + " — declare in checklist_classes to merge"
        )
    return 0


_ACTIONS = {
    "show": _cmd_show,
    "check": _cmd_check,
    "apply": _cmd_apply,
    "hash": _cmd_hash,
}


def run(args: Any) -> int:
    # F12: the argument is a TODO id, not a path. ``Path / token`` with an
    # absolute token silently drops the inbox prefix, so a reader/writer
    # must refuse before building any path (no reads, writes or backups
    # outside the project — the one declared write surface is the inbox).
    if not is_valid_todo_id(args.todo_id):
        print(
            f"ERROR: {args.todo_id!r} is not a TODO id (TODO-NNNN)",
            file=sys.stderr,
        )
        return 2
    project = Path(args.project_dir).resolve()
    try:
        return _ACTIONS[args.action](args, project)
    except (ChecklistError, managed_fs.ManagedPathError, ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
