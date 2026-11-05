"""K5: the block renderers — one owner per block (R2, arch §3/§5).

Every block reads exactly one artifact. An absent artifact is a line
``нет данных: <причина> — <путь>`` and a reason in ``missing`` (AC9/AC25):
the stage still runs, the reader still knows what was not there. A secret
never removes its block: :func:`awf.redact.redact` masks values (AC27).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import paths
from .bounded_prompt import Block
from .redact import redact
from .stage_context import (
    StageInput,
    gates_report,
    read_section,
    split_front_matter,
    todo_file,
    todo_text,
)
from .stage_context import (
    missing_block as _missing,
)
from .stage_context import (
    present_block as _present,
)
from .stage_context import (
    read_text as _read,
)
from .stage_context_review import render_REVIEW
from .stage_context_roles import select_role_core

#: A ``## REVIEW-фикс`` round appended to the TODO by the fix round; the
#: findings belong to ``REVIEW``, never to the unit block (R2).
_REVIEW_ROUNDS = re.compile(r"(?ms)^##\s+REVIEW-фикс[^\n]*\n.*?(?=^##\s|\Z)")

#: marker of an empty-but-present artifact (AC25).
EMPTY_REASON = "блок пуст"

#: The sections ``CRIT`` owns: ``UNIT`` carries the task and the borders
#: (D5), ``CRIT`` the acceptance criteria and the check commands (R2). The
#: contract walker returns the whole TODO contract, so each block drops what
#: the other one owns — two copies of the acceptance criteria cost 1 100
#: characters on TODO-0383 and squeezed ``ROLE`` down to its marker.
_UNIT_DROPS = ("Приёмка",)


def _drop_sections(lines: list[str], names: tuple[str, ...]) -> list[str]:
    """The contract lines without the ``##`` sections named in *names*."""
    kept: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith("## "):
            skipping = any(line[3:].strip().startswith(name) for name in names)
        if not skipping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


def _unit_header(lines: list[str]) -> list[str]:
    """The TODO title, without the front matter ``STAGE`` already carries.

    ``contract_section`` keeps the whole head of the file: on TODO-0383 the
    ``files:`` allowlist alone is 1 600 characters and it repeats the STAGE
    block. The unit block keeps the title and starts at the first section.
    """
    title: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith("## "):
            return title + lines[index:]
        if line.startswith("# "):
            title = [line]
    return lines


def _artifact(block_id: str, ctx: StageInput, path: Path, reason: str, header: str = "") -> Block:
    """A file block: absent, empty and present are three different answers."""
    if not path.is_file():
        return _missing(block_id, ctx, reason, path)
    text = _read(path)
    if not text.strip():
        return _missing(block_id, ctx, EMPTY_REASON, path)
    return _present(block_id, ctx, header + text, path)


def _snapshot_stage(ctx: StageInput) -> dict:
    for stage in ctx.snapshot.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        name = stage.get("id") or stage.get("name")
        if name == ctx.stage_name:
            return stage
    return {}


def render_STAGE(ctx: StageInput) -> Block:
    source = paths.pipeline_snapshot_path(ctx.project_dir, ctx.todo_id)
    if not ctx.snapshot:
        return _missing("STAGE", ctx, "снимка стадий нет", source)
    stage = _snapshot_stage(ctx)
    task = stage.get("task") or stage.get("description") or "нет в снимке"
    files = split_front_matter(todo_text(ctx))[0].get("files") or []
    allow = ", ".join(str(f) for f in files) if isinstance(files, list) else str(files)
    # A unit's allowlist can grow past the block's 400-char share (0383 has
    # 40 files): STAGE is P0 and is never cut, so it trims its own list.
    if len(allow) > 280:
        allow = allow[:279].rstrip(", ") + " …"
    text = (
        "## Стадия\n"
        f"- задача: {task}\n"
        f"- todo: {ctx.todo_id}; стадия: {ctx.stage_name}; роль: {ctx.role}; виток: {ctx.attempt}\n"
        f"- проект: {ctx.project_dir}\n"
        f"- файлы юнита: {allow or 'не объявлены'}"
    )
    return _present("STAGE", ctx, text, source)


def render_UNIT(ctx: StageInput) -> Block:
    """The unit's contract, not the whole growing file (R2, D5, AC3/AC36).

    ``hygiene.review_append`` appends every fix round to the TODO, so the
    file grows past the cap on its own (TODO-0357 = 16 369 chars, 0357
    review rounds included): a whole-file P0 block cannot fit 9 000 next to
    ``CRIT``/``RULES``/``OUTPUT`` and returned ``CTX-FAIL`` on the very units
    AC3 names. The contract block has one owner already —
    :func:`awf.context_pack_sections.contract_section`, the same walker the
    pack shows as section 2; the review rounds stay with ``REVIEW`` and the
    acceptance criteria with ``CRIT`` (one fact, one owner).
    """
    text = todo_text(ctx)
    if not text:
        return _missing("UNIT", ctx, "TODO не найден", todo_file(ctx))
    from .context_pack_sections import contract_section

    lines, found = contract_section(text, str(todo_file(ctx)))
    if found:
        lines = _unit_header(_drop_sections(lines, _UNIT_DROPS))
        return _present("UNIT", ctx, "\n".join(lines), todo_file(ctx))
    # No contract at all: the unit still needs its task, so the TODO goes in
    # without the rounds REVIEW owns.
    body = _REVIEW_ROUNDS.sub("", text).strip() or text
    lines = _unit_header(body.split("\n"))
    return _present("UNIT", ctx, "\n".join(lines), todo_file(ctx))


def render_CRIT(ctx: StageInput) -> Block:
    text = todo_text(ctx)
    meta, body = split_front_matter(text)
    verify = meta.get("verify") or []
    acceptance = read_section(body, "Приёмка (факты)") or read_section(body, "Приёмка")
    parts = ["## Критерии приёмки и команды"]
    if isinstance(verify, list) and verify:
        parts.append("Команды проверки:\n" + "\n".join(f"- {v}" for v in verify))
    if acceptance:
        parts.append(acceptance)
    if len(parts) == 1:
        return _missing("CRIT", ctx, "в TODO нет блока приёмки", todo_file(ctx))
    return _present("CRIT", ctx, "\n\n".join(parts), todo_file(ctx))


def render_CHECK(ctx: StageInput) -> Block:
    section = read_section(todo_text(ctx), "Чек-лист завершения")
    if not section:
        return _missing("CHECK", ctx, "чек-листа нет", todo_file(ctx))
    return _present("CHECK", ctx, section, todo_file(ctx))


def render_PHASES(ctx: StageInput) -> Block:
    return _artifact("PHASES", ctx, paths.phases_dir(ctx.project_dir) / "plan.md", "плана фаз нет")


def render_VISION(ctx: StageInput) -> Block:
    path = paths.find_vision_file(ctx.project_dir)
    if path is None:
        return _missing("VISION", ctx, "файла видения нет", ctx.project_dir)
    return _artifact("VISION", ctx, path, "файла видения нет")


def render_ROLE(ctx: StageInput) -> Block:
    path = paths.roles_dir(ctx.project_dir) / f"{ctx.role}.md"
    text = _read(path)
    if not text:
        return _missing("ROLE", ctx, "файла роли нет", path)
    row = f"{ctx.family}/{ctx.row}" if ctx.row else ctx.family
    selection = select_role_core(text, row, ctx.limit("ROLE"), source=str(path))
    block = _present("ROLE", ctx, selection.text, path)
    # The coverage numbers travel as fields, not as text in ``owner``: the
    # builder computes ``role_pct`` from them, and a display label must never
    # be the only place a measurement lives.
    block.taken, block.total = selection.taken, selection.total
    return block


def render_DOCTRINE(ctx: StageInput) -> Block:
    directory = paths.agentic_dir(ctx.project_dir) / "doctrine"
    files = sorted(directory.glob("*.md")) if directory.is_dir() else []
    if not files:
        return _missing("DOCTRINE", ctx, "доктрины нет", directory)
    lines = ["## Доктрина проекта"]
    for path in files:
        body = _read(path).strip()
        if not body:
            continue
        title = body.split("\n", 1)[0].lstrip("# ").strip()
        lines.append(f"- {title} — {path.relative_to(paths.agentic_dir(ctx.project_dir))}")
    return _present("DOCTRINE", ctx, "\n".join(lines), directory)


def render_STATE(ctx: StageInput) -> Block:
    sha_file = paths.context_dir(ctx.project_dir) / f"BASELINE-{ctx.todo_id}.sha"
    sha = _read(sha_file).strip().split("\n")[0]
    lines = ["## Состояние", f"- baseline: {sha or 'снят не был'}"]
    try:
        from .infra.git_model import GitRepository

        changes = GitRepository(ctx.project_dir).changes()
    except Exception:  # noqa: BLE001 - a git read never kills the stage
        lines.append("- git status: не читается")
    else:
        if not changes:
            lines.append("- git status: чисто")
        else:
            lines.append("- git status:")
            lines.extend(f"  {change.xy} {change.display_path}" for change in changes[:20])
    return _present("STATE", ctx, "\n".join(lines), sha_file)


def render_GATES(ctx: StageInput) -> Block:
    path, facts = gates_report(ctx)
    if not path.is_file():
        return _missing("GATES", ctx, "отчёта гейтов нет", path)
    if not facts:
        return _missing("GATES", ctx, "отчёт гейтов не читается", path)
    sections = facts.get("sections") or {}
    rendered = ", ".join(f"{name}: {state}" for name, state in sections.items())
    text = f"## Гейты\n- verdict: {facts.get('verdict', '?')}\n- разделы: {rendered or 'нет'}"
    first = facts.get("first_failure")
    if first:
        text += f"\n- первое падение: {first}"
    return _present("GATES", ctx, text, path)


def render_DIFF(ctx: StageInput) -> Block:
    sha_file = paths.context_dir(ctx.project_dir) / f"BASELINE-{ctx.todo_id}.sha"
    if not sha_file.is_file():
        return _missing("DIFF", ctx, "baseline не снят", sha_file)
    from .verify import diff_stat_for_todo

    try:
        stat = diff_stat_for_todo(ctx.project_dir, ctx.todo_id).strip()
    except Exception:  # noqa: BLE001 - a git read never kills the stage
        stat = ""
    if not stat:
        return _missing("DIFF", ctx, "изменений против baseline нет", sha_file)
    return _present("DIFF", ctx, "## Дифф\n```\n" + stat + "\n```", sha_file)


def render_DONE(ctx: StageInput) -> Block:
    md = paths.outbox(ctx.project_dir) / f"DONE-{ctx.todo_id}.md"
    if not md.is_file():
        return _missing("DONE", ctx, "DONE-отчёта нет", md)
    text = _read(md)
    if not text.strip():
        return _missing("DONE", ctx, EMPTY_REASON, md)
    js = paths.outbox(ctx.project_dir) / f"DONE-{ctx.todo_id}.json"
    body = _read(js).strip()
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            facts = {k: parsed.get(k) for k in ("files_changed", "gates", "notes") if parsed.get(k)}
            body = json.dumps(facts, ensure_ascii=False, indent=2)
    return _present("DONE", ctx, text + ("\n\n" + body if body else ""), md)


def render_BLOCKED(ctx: StageInput) -> Block:
    path = paths.outbox(ctx.project_dir) / f"BLOCKED-{ctx.todo_id}.md"
    return _artifact("BLOCKED", ctx, path, "BLOCKED-отчёта нет")


def _handoffs(ctx: StageInput) -> list[Path]:
    directory = paths.handoff_dir(ctx.project_dir)
    if not directory.is_dir():
        return []
    try:
        found = [p for p in directory.glob(f"*-{ctx.todo_id}.md") if p.is_file()]
        return sorted(found, key=lambda p: p.stat().st_mtime)
    except OSError:
        return []


def render_PREV(ctx: StageInput) -> Block:
    found = _handoffs(ctx)
    if not found:
        return _missing("PREV", ctx, "handoff-ов нет", paths.handoff_dir(ctx.project_dir))
    return _artifact("PREV", ctx, found[-1], "handoff не читается")


def render_PREVLINK(ctx: StageInput) -> Block:
    found = _handoffs(ctx)[:-1]
    if not found:
        return _missing("PREVLINK", ctx, "других handoff-ов нет", paths.handoff_dir(ctx.project_dir))
    lines = ["## Прошлые handoff-ы"]
    for path in found:
        first = _read(path).strip().split("\n", 1)[0]
        lines.append(f"- {path} — {first}")
    return _present("PREVLINK", ctx, "\n".join(lines), paths.handoff_dir(ctx.project_dir))


def render_INPUTART(ctx: StageInput) -> Block:
    text = todo_text(ctx)
    if not text:
        return _missing("INPUTART", ctx, "TODO не найден", todo_file(ctx))
    found = sorted(set(re.findall(r"(?:docs|\.agentic)/[A-Za-z0-9_./-]+\.md", text)))
    if not found:
        return _missing("INPUTART", ctx, "путей пакета в TODO нет", todo_file(ctx))
    lines = ["## Входные артефакты"] + [f"- {p}" for p in found[:12]]
    return _present("INPUTART", ctx, "\n".join(lines), todo_file(ctx))


_RULES_TEXT = """## Правила
- не коммить, не stash, не add, не reset: коммитит движок после verify и одобрения;
- правки только в границах юнита; временные файлы — только /tmp/opencode/**;
- прошлая сессия не источник фактов: факты — только в артефактах этого витка;
- секреты не печатать: значение заменяется пометкой."""

_OUTPUT_TEXT = """## Контракт вывода
- DONE: `.agentic/outbox/DONE-<todo>.md` + пустой `.ready`; `.json` — files_changed, tests_run, gates;
- findings (ревью) в `DONE-<todo>.json` или `PROBES-<todo>.json`: `[{id, severity: blocker|major|minor, file, claim}]`; нет поля — находок нет; severity вне трёх значений = blocker;
- BLOCKED: `.agentic/outbox/BLOCKED-<todo>.md` с причиной + `.ready`;
- REVIEW: секция `## REVIEW-фикс` с находками;
- молчание: провал."""


def render_RULES(ctx: StageInput) -> Block:
    return _present("RULES", ctx, _RULES_TEXT, "правила стадии")


def render_OUTPUT(ctx: StageInput) -> Block:
    return _present("OUTPUT", ctx, _OUTPUT_TEXT, "контракт вывода")


RENDERERS = {
    "REVIEW": render_REVIEW,
    "STAGE": render_STAGE,
    "UNIT": render_UNIT,
    "CRIT": render_CRIT,
    "CHECK": render_CHECK,
    "PHASES": render_PHASES,
    "VISION": render_VISION,
    "ROLE": render_ROLE,
    "DOCTRINE": render_DOCTRINE,
    "STATE": render_STATE,
    "GATES": render_GATES,
    "DIFF": render_DIFF,
    "DONE": render_DONE,
    "BLOCKED": render_BLOCKED,
    "PREV": render_PREV,
    "PREVLINK": render_PREVLINK,
    "INPUTART": render_INPUTART,
    "RULES": render_RULES,
    "OUTPUT": render_OUTPUT,
}


def render_block(block_id: str, ctx: StageInput) -> Block:
    """K5: one block, masked, with its owner and its missing reason."""
    renderer = RENDERERS.get(block_id)
    if renderer is None:
        return _missing(block_id, ctx, "блока нет в словаре", block_id)
    block = renderer(ctx)
    block.text, replaced = redact(block.text)
    block.tail, tail_replaced = redact(block.tail)
    if replaced or tail_replaced:
        block.owner = f"{block.owner} (секретов скрыто: {replaced + tail_replaced})"
    return block


__all__ = ["EMPTY_REASON", "RENDERERS", "render_block"]
