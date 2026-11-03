"""A25: resolve, order and render a batch — the pure half of ``awf plan-batch``.

Split out of :mod:`awf.api.plan_batch` (REVIEW-фикс, виток 1: the 400-line
ratchet; architect-review: the resolve step moved here too, so the whole
"backlog entry + call overrides → ordered queue → TODO text" pipeline is one
module). Nothing here touches the disk: ``resolve_entries`` merges overrides
into backlog entries (three refusals, one per cause), ``as_list`` /
``split_scope_line`` are the one field parser for both sources,
``order_entries`` sorts the queue by ``after`` (a cycle refuses),
``render_draft`` builds the TODO text, ``_plan_item`` shapes one plan record.
The caller keeps all I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ._errors import AwfApiError


@dataclass
class _Entry:
    """One resolved backlog item — backlog text plus call-level overrides."""

    id: str
    title: str
    scope: str
    refs: list[str]
    after: list[str]
    accept: list[str]
    bounds: list[str]


def split_scope_line(value: str) -> list[str]:
    """Acceptance/boundary lines: ``- a; b`` → ``["a", "b"]``."""
    return [
        re.sub(r"^\s*[-*]\s*", "", part).strip()
        for part in re.split(r"\s*;\s*", value)
    ]


def as_list(value: Any, *, key: str, item_id: str) -> list[str]:
    """Normalize a field value (backlog line or call override) to a list.

    The separator follows the field, not the source: ``after`` holds ids
    (commas or spaces), ``refs`` holds paths (commas), ``accept`` /
    ``bounds`` hold sentences — those use the same ``;`` split as the
    backlog's ``accept:`` / ``bounds:`` lines (one field, one parser).
    """
    if value is None:
        return []
    if isinstance(value, str):
        if key in ("accept", "bounds"):
            return [part for part in split_scope_line(value) if part]
        parts = re.split(r"[,\s]+", value) if key == "after" else value.split(",")
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    raise AwfApiError(f"пункт {item_id}: {key} must be a list or a string")


def resolve_entries(
    items: list[dict[str, Any]],
    backlog: dict[str, dict[str, Any]],
    backlog_display: str,
    *,
    backlog_found: bool = True,
) -> list[_Entry]:
    """Merge call overrides into backlog entries; refuse a scope-less item.

    Three refusals, three causes — a missing backlog file, an id that file
    does not carry, and an entry without scope text. One message for all
    three would send the owner to the wrong place.
    """
    resolved: list[_Entry] = []
    for raw in items:
        item_id = raw["id"]
        source = backlog.get(item_id) or {}
        scope = str(raw.get("scope") or "").strip() or source.get("scope", "")
        if not scope.strip():
            if not backlog_found:
                raise AwfApiError(
                    f"пункт {item_id}: бэклог {backlog_display} не найден — "
                    "укажите plan.batch_backlog_path или backlog_path, "
                    "либо передайте scope"
                )
            if not source:
                raise AwfApiError(
                    f"пункт {item_id}: нет в {backlog_display} — добавьте "
                    f"заголовок «### {item_id} — …» или передайте scope"
                )
            raise AwfApiError(
                f"пункт {item_id}: нет скоупа — добавьте текст под заголовком "
                f"{item_id} в {backlog_display} или передайте scope"
            )
        title = (
            str(raw.get("title") or "").strip()
            or str(source.get("title") or "").strip()
            or item_id
        )
        refs = as_list(raw.get("refs"), key="refs", item_id=item_id) or list(
            source.get("refs") or []
        )
        after = as_list(raw.get("after"), key="after", item_id=item_id) or list(
            source.get("after") or []
        )
        accept = as_list(raw.get("accept"), key="accept", item_id=item_id) or list(
            source.get("accept") or []
        )
        bounds = as_list(raw.get("bounds"), key="bounds", item_id=item_id) or list(
            source.get("bounds") or []
        )
        resolved.append(
            _Entry(
                id=item_id,
                title=title,
                scope=scope,
                refs=refs,
                after=after,
                accept=accept,
                bounds=bounds,
            )
        )
    return resolved



def order_entries(entries: list[_Entry]) -> list[_Entry]:
    """Stable topological order by ``after``; a cycle refuses the batch.

    Dependencies outside the batch are kept in the plan but ignored here:
    they name work that is already done (or lives in another batch) and
    cannot block this one.
    """
    position = {entry.id: i for i, entry in enumerate(entries)}
    deps = {
        entry.id: [d for d in entry.after if d in position and d != entry.id]
        for entry in entries
    }
    ordered: list[_Entry] = []
    done: set[str] = set()
    while len(ordered) < len(entries):
        ready = [
            entry
            for entry in entries
            if entry.id not in done and all(d in done for d in deps[entry.id])
        ]
        if not ready:
            stuck = [entry.id for entry in entries if entry.id not in done]
            raise AwfApiError(
                "цикл в зависимостях пачки: " + " → ".join(stuck + [stuck[0]])
            )
        nxt = min(ready, key=lambda entry: position[entry.id])
        ordered.append(nxt)
        done.add(nxt.id)
    return ordered


def render_draft(
    todo_id: str,
    entry: _Entry,
    *,
    goal: str,
    backlog_display: str,
) -> str:
    """The draft TODO text — self-sufficient, never runnable (no .ready)."""
    accept = entry.accept or [
        f"Скоуп пункта {entry.id} выполнен полностью.",
        "verify-команды контракта зелёные (черновик: `verify:` дописывает супервизор).",
    ]
    bounds = entry.bounds or [
        "Только файлы и пункты из скоупа; смежное — отдельный TODO.",
    ]
    lines = [
        "---",
        "# Черновик пачки A25: НЕ запущен — файла .ready нет.",
        "---",
        f"# {todo_id} — {entry.id}: {entry.title}",
        "",
        "## Контекст",
        "",
        f"- Пункт бэклога: `{entry.id}` из `{backlog_display}`.",
        f"- Цель пачки: {goal.strip() or '—'}.",
    ]
    if entry.after:
        lines.append(f"- После: {', '.join(entry.after)}.")
    if entry.refs:
        lines.append(f"- Первоисточники: {', '.join(f'`{ref}`' for ref in entry.refs)}.")
    lines += [
        "",
        "## Что сделать",
        "",
        entry.scope.strip(),
        "",
        "## Приёмка",
        "",
    ]
    lines += [f"- {item}" for item in accept]
    lines += [
        "",
        "## Границы",
        "",
    ]
    lines += [f"- {item}" for item in bounds]
    lines.append("")
    return "\n".join(lines)


def _plan_item(entry: _Entry, todo_id: str) -> dict[str, Any]:
    return {
        "id": entry.id,
        "title": entry.title,
        "todo_id": todo_id,
        "after": list(entry.after),
    }


__all__ = ["as_list", "order_entries", "render_draft", "resolve_entries"]
