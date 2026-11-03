"""A25: ``awf plan-batch`` — a batch of backlog units as inbox drafts.

One call turns 3-4 backlog entries into TODO *drafts* in ``.agentic/inbox``
plus an ordered plan. A draft is deliberately NOT a dispatched unit: no
baseline, no ``.ready`` signal, the pipeline never picks it up. The
supervisor refines the draft and dispatches it when the batch reaches it —
``awf_dispatch_todo(content=..., todo_id=<draft id>, force=True)``, because
the draft already owns that inbox path.

Backlog source: ``plan.batch_backlog_path`` from ``.agentic/config.yaml``,
else ``docs/backlog.md``. An entry is a level 2+ heading whose text starts
with an id, followed by its scope paragraph::

    ### A37 — Batch planning

    Text of the scope (what to do). Its lines become «Что сделать».

    after: A36          # ids that must precede this one (optional)
    refs: docs/x.md#s   # source references (optional)
    accept: - ...       # extra acceptance lines (optional, one line)
    bounds: - ...       # extra boundary lines (optional, one line)

An item whose scope is empty both in the backlog and in the call refuses
the whole batch by name — before any file is written. The same holds for an
id the backlog does not carry and for a backlog file that does not exist:
three causes, three messages.

Headings are read with the fence-aware walker from ``awf._markdown``: a
``###`` or ``after:`` line inside a code fence is scope text, not structure.
Resolving, ordering and rendering live in ``plan_batch_render`` (the pure
half); this module keeps the backlog parse and every disk touch.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .. import config as _config
from .. import paths
from .._atomic import MODE_OWNER_EDITABLE
from .._markdown import HEADING_RE, fence_flags
from ..infra import managed_fs
from ._errors import AwfApiError
from ._helpers import (
    managed_write_api_text,
    require_awf_project,
    surfaces_are_managed,
)
from ._results import PlanBatchResult
from .dispatch import _next_todo_id
from .plan_batch_render import (
    _plan_item,
    as_list,
    order_entries,
    render_draft,
    resolve_entries,
)

#: Backlog file used when neither the call nor the config names one.
DEFAULT_BACKLOG = "docs/backlog.md"

#: Entry heading: ``### A37 — Title`` (id, separator, title). The separator
#: is an em/en dash or a colon — a plain hyphen would be ambiguous with ids
#: that contain one (``FU-13``).
_ENTRY_HEADING_RE = re.compile(
    r"^(?P<level>#{2,6})\s+(?P<id>[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)"
    r"\s*[—–:]\s*(?P<title>.+?)\s*$"
)

#: Entry metadata lines (``after:`` / ``refs:`` / ``accept:`` / ``bounds:``).
_META_RE = re.compile(
    r"^\s*[-*]?\s*(?P<key>after|refs|accept|bounds)\s*:\s*(?P<value>.*)$",
    re.IGNORECASE,
)


def parse_backlog(text: str) -> dict[str, dict[str, Any]]:
    """Parse the backlog into ``{id: entry}``; headings without an id are skipped.

    Entries are FLAT (the module docstring's format): body lines are the
    scope, minus the metadata lines. A non-entry heading ends the scope; an
    entry heading of the same or a shallower level opens the next entry; a
    deeper entry heading is skipped (neither a new entry nor scope text —
    the flat format has no nested entries). Every line inside a code fence
    is scope text, and the heading question is the fence-aware one from
    ``awf._markdown`` — a ``###`` in an example is not an entry. An entry
    with an empty scope keeps an empty ``scope`` — the caller names it in
    the refusal.
    """
    entries: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    current_level = 0
    lines = (text or "").splitlines()
    for line, fenced in zip(lines, fence_flags(lines)):
        if fenced:
            # Inside a fence a ``#`` line is a comment and ``- after: x`` is
            # an example — the one answer ``awf._markdown`` gives every other
            # reader. Fenced text is scope text, never structure.
            if current is not None:
                current["lines"].append(line)
            continue
        heading = _ENTRY_HEADING_RE.match(line)
        if heading:
            level = len(heading.group("level"))
            if current is None or level <= current_level:
                current = {
                    "id": heading.group("id"),
                    "title": heading.group("title"),
                    "lines": [],
                    "refs": [],
                    "after": [],
                    "accept": [],
                    "bounds": [],
                }
                entries[current["id"]] = current
                current_level = level
            continue
        if current is None:
            continue
        # A heading of ANY kind ends the scope of the current entry; the
        # next matching entry heading opens a new one above.
        if HEADING_RE.match(line):
            current = None
            continue
        meta = _META_RE.match(line)
        if meta:
            key = meta.group("key").lower()
            value = meta.group("value").strip()
            if key == "refs":
                current["refs"].extend(
                    as_list(value, key="refs", item_id=current["id"])
                )
            elif key in ("accept", "bounds"):
                current[key].extend(
                    as_list(value, key=key, item_id=current["id"])
                )
            else:
                current["after"].extend(
                    as_list(value, key="after", item_id=current["id"])
                )
            continue
        current["lines"].append(line)
    for entry in entries.values():
        entry["scope"] = _clean_scope(entry.pop("lines"))
    return entries


def _clean_scope(lines: list[str]) -> str:
    """Trim blank edges, keep internal paragraphs, drop trailing spaces."""
    cleaned = [line.rstrip() for line in lines]
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned).strip()


def _coerce_items(items: Any) -> list[dict[str, Any]]:
    """Normalize the call input to ``[{id, scope?, ...}]``."""
    if not isinstance(items, list) or not items:
        raise AwfApiError("items is required: a non-empty list of backlog ids")
    coerced: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        if isinstance(raw, str):
            entry = {"id": raw.strip()}
        elif isinstance(raw, dict):
            entry = dict(raw)
            entry["id"] = str(entry.get("id") or "").strip()
        else:
            raise AwfApiError(
                f"items must be ids or dicts, got {type(raw).__name__}"
            )
        if not entry["id"]:
            raise AwfApiError("every item needs a non-empty id")
        if entry["id"] in seen:
            raise AwfApiError(f"пункт {entry['id']} указан в пачке дважды")
        seen.add(entry["id"])
        coerced.append(entry)
    return coerced


def _resolve_backlog_path(
    project_dir: Path, backlog_path: str | None
) -> tuple[Path, str]:
    """The backlog file the batch reads: call → config → default."""
    raw = (backlog_path or "").strip()
    if not raw:
        cfg = _config.load(project_dir)
        raw = str(_config.get(cfg, "plan.batch_backlog_path", "") or "").strip()
    if not raw:
        raw = DEFAULT_BACKLOG
    path = Path(raw)
    if not path.is_absolute():
        path = project_dir / path
    return path, raw


def plan_batch(
    project_dir: Path,
    items: list[Any],
    goal: str = "",
    *,
    backlog_path: str | None = None,
) -> PlanBatchResult:
    """Create inbox drafts for a batch of backlog units and return the plan.

    Nothing is dispatched: no baseline, no ``.ready``. Every refusal happens
    before the first file is written (the batch is validated and ordered
    first), and a failure while writing removes the files this call created —
    a refused batch leaves the inbox as it found it.
    """
    project_dir = require_awf_project(project_dir)
    coerced = _coerce_items(items)

    path, display = _resolve_backlog_path(project_dir, backlog_path)
    backlog: dict[str, dict[str, Any]] = {}
    backlog_found = path.is_file()
    if backlog_found:
        backlog = parse_backlog(path.read_text(encoding="utf-8"))
    entries = resolve_entries(coerced, backlog, display, backlog_found=backlog_found)
    ordered = order_entries(entries)

    inbox = paths.inbox(project_dir)
    next_id = _next_todo_id(project_dir)
    start_number = int(next_id.split("-", 1)[1])
    todo_ids = {entry.id: f"TODO-{start_number + i:04d}" for i, entry in enumerate(ordered)}

    # Render first: the text is the only thing a failure here could lose,
    # and nothing has touched the disk yet.
    prepared = [
        (
            inbox / f"{todo_ids[entry.id]}.md",
            render_draft(
                todo_ids[entry.id],
                entry,
                goal=goal,
                backlog_display=display,
            ),
        )
        for entry in ordered
    ]
    _reserve_todo_files(project_dir, [target for target, _ in prepared])

    drafts: list[dict[str, Any]] = []
    try:
        for (target, content), entry in zip(prepared, ordered):
            managed_write_api_text(
                target,
                content,
                operation="plan-batch",
                mode=MODE_OWNER_EDITABLE,
            )
            drafts.append(
                {
                    "id": entry.id,
                    "todo_id": todo_ids[entry.id],
                    "path": str(target),
                    "title": f"{entry.id}: {entry.title}",
                }
            )
    except BaseException:
        # All-or-nothing: the ids are already reserved, so a failed write
        # would otherwise leave empty TODO files behind. Every path here was
        # created by THIS call (O_EXCL) — ownership is ours; the surface
        # check below is about a link swapped in behind our back.
        _discard_files(project_dir, [target for target, _ in prepared])
        raise

    plan = [_plan_item(entry, todo_ids[entry.id]) for entry in ordered]
    return PlanBatchResult(
        drafts=drafts,
        plan=plan,
        goal=goal,
        backlog_path=str(path),
        next_action=(
            f"{len(drafts)} draft(s) in inbox (no .ready). Refine a draft, "
            'then dispatch it with awf_dispatch_todo(content=..., '
            'todo_id="<draft todo_id>", force=True).'
        ),
    )


def _reserve_todo_files(project_dir: Path, targets: list[Path]) -> None:
    """Claim every id with ``O_CREAT|O_EXCL`` before any content lands.

    The same exclusive-create the dispatch uses, so two parallel batches
    cannot land on one id. One taken number refuses the WHOLE batch: the ids
    this call already claimed are removed, so a refusal leaves no empty TODO
    in the inbox (the module's all-or-nothing promise).
    """
    reserved: list[Path] = []
    try:
        for target in targets:
            try:
                # Root derived from the LEAF (``open_managed`` default), the
                # same convention as ``managed_write_api_text``: two spellings
                # of one root is how a correct path once read as an escape.
                fd = managed_fs.open_managed(
                    target,
                    flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    mode=MODE_OWNER_EDITABLE,
                )
            except FileExistsError as e:
                raise AwfApiError(
                    f"{target} уже существует — нумерация TODO разъехалась, "
                    "повторите вызов"
                ) from e
            except managed_fs.ManagedPathError as e:
                raise AwfApiError(
                    f"не удалось зарезервировать {target}: {e}"
                ) from e
            reserved.append(target)
            os.close(fd)
    except BaseException:
        _discard_files(project_dir, reserved)
        raise


def _discard_files(project_dir: Path, targets: list[Path]) -> None:
    """Best-effort removal of files THIS call created; never raises.

    The unlink is a plain one (no managed primitive removes a leaf), so the
    surface is checked first with the same best-effort barrier the other
    rollbacks use: an unsafe surface is SKIPPED, never followed. Ownership
    alone does not cover the race — if ``inbox`` is swapped for a symlink
    after the reservation, a bare ``unlink`` would delete a same-named file
    outside the project (the S9-01 class ``dispatch``/``hygiene`` guard).
    """
    for target in targets:
        if not surfaces_are_managed(project_dir, "plan-batch rollback", target):
            continue
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = ["DEFAULT_BACKLOG", "parse_backlog", "plan_batch"]
