"""K7: the stage context builder — one text, one file, one measurement.

Type/blocks/role/cut: :mod:`.stage_context_types`, :data:`.FAMILIES`,
:mod:`.stage_context_roles`, :func:`awf.bounded_prompt.fit_blocks`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import _log, paths
from ._atomic import atomic_write_text
from ._errors import AwfApiError
from .bounded_prompt import SUFFIX_MAX, Block, fit_blocks, fit_to_limit, minimum_blocks_len
from .stage_context_roles import ROLE_LOW_PCT
from .stage_context_types import display_type, resolve

#: R3: one cap for all five families; ``checklist`` inherits ``reviewer``.
CAP = 9000

#: arch §5: the block order of each type (``checklist`` — no role/no findings).
FAMILIES: dict[str, tuple[str, ...]] = {
    "plan": (
        "STAGE", "PHASES", "VISION", "ROLE", "DOCTRINE", "STATE", "PREVLINK",
        "CRIT", "RULES", "OUTPUT",
    ),
    "execute": (
        "STAGE", "UNIT", "REVIEW", "DONE", "BLOCKED", "ROLE", "DOCTRINE",
        "STATE", "PREV", "PREVLINK", "CRIT", "RULES", "OUTPUT",
    ),
    "reviewer": (
        "STAGE", "UNIT", "INPUTART", "DONE", "DIFF", "GATES", "REVIEW",
        "ROLE", "DOCTRINE", "PREV", "PREVLINK", "CRIT", "RULES", "OUTPUT",
    ),
    "reviewer/checklist": ("STAGE", "CHECK", "STATE", "CRIT", "RULES", "OUTPUT"),
    "supervisor": (
        "STAGE", "STATE", "DONE", "DIFF", "GATES", "REVIEW", "ROLE",
        "DOCTRINE", "PREVLINK", "CRIT", "RULES", "OUTPUT",
    ),
    "spec": (
        "STAGE", "UNIT", "INPUTART", "REVIEW", "ROLE", "DOCTRINE", "PREV",
        "PREVLINK", "CRIT", "RULES", "OUTPUT",
    ),
}

#: arch §6: per-block limits. An empty cell means "no such block".
LIMITS: dict[str, dict[str, int]] = {
    "plan": {
        "STAGE": 400, "PHASES": 700, "VISION": 500, "ROLE": 900,
        "DOCTRINE": 250, "STATE": 400, "PREVLINK": 100, "CRIT": 1200,
        "RULES": 350, "OUTPUT": 800,
    },
    "execute": {
        "STAGE": 400, "UNIT": 1800, "REVIEW": 800, "DONE": 400, "BLOCKED": 100,
        "ROLE": 1200, "DOCTRINE": 150, "STATE": 150, "PREV": 250,
        "PREVLINK": 100, "CRIT": 2500, "RULES": 350, "OUTPUT": 800,
    },
    "reviewer": {
        "STAGE": 400, "UNIT": 1800, "INPUTART": 100, "DONE": 300, "DIFF": 500,
        "GATES": 250, "REVIEW": 350, "ROLE": 400, "DOCTRINE": 50, "PREV": 50,
        "PREVLINK": 50, "CRIT": 2500, "RULES": 350, "OUTPUT": 800,
    },
    "reviewer/checklist": {
        "STAGE": 400, "CHECK": 800, "STATE": 300, "CRIT": 2500,
        "RULES": 350, "OUTPUT": 800,
    },
    "supervisor": {
        "STAGE": 400, "STATE": 400, "DONE": 400, "DIFF": 3830, "GATES": 300,
        "REVIEW": 500, "ROLE": 1000, "DOCTRINE": 150, "PREVLINK": 70,
        "CRIT": 800, "RULES": 350, "OUTPUT": 800,
    },
    "spec": {
        "STAGE": 400, "UNIT": 2800, "INPUTART": 200, "REVIEW": 450,
        "ROLE": 800, "DOCTRINE": 100, "PREV": 400, "PREVLINK": 200,
        "CRIT": 2500, "RULES": 350, "OUTPUT": 800,
    },
}


#: arch §6: blocks outside the family matrix (the replan stage injects
#: ``BLOCKED``). An empty :data:`LIMITS` cell still means "no such block".
EXTRA_LIMITS: dict[str, int] = {"BLOCKED": 100}


class StageContextError(AwfApiError):
    """The stage text cannot be built at all: ``CTX-FAIL``."""


@dataclass
class StageInput:
    """Everything a renderer may read: the unit, the stage, the artifact dirs."""
    project_dir: Path
    todo_id: str
    stage_name: str
    role: str
    family: str
    row: str = ""
    attempt: int = 1
    limits: dict[str, int] = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)

    def limit(self, block_id: str) -> int:
        return int(self.limits.get(block_id) or EXTRA_LIMITS.get(block_id, 0))


@dataclass
class StageContext:
    """The builder's answer: text, blocks, what was cut and what was missing."""

    text: str
    blocks: list[Block]
    cut: list[str]
    missing: dict[str, str]
    conflicts: list[str]
    positions: dict[str, int]
    file: Path
    cap: int = CAP
    family: str = ""
    row: str = ""
    role_taken: int = 0
    role_total: int = 0
    role_pct: int = 100

    def ctx_line(self, todo_id: str, stage_name: str, attempt: int) -> str:
        """arch §12: the one measurement line."""
        type_name = display_type(self.family, self.row)
        blocks = ",".join(f"{b.id}:{len(b.full())}" for b in self.blocks if b.full())
        cut = ",".join(f"{b.id}:{b.cut}" for b in self.blocks if b.cut)
        missing = ",".join(f"{block_id}:{reason}" for block_id, reason in self.missing.items())
        return (
            f"CTX {todo_id} {stage_name} attempt={attempt} type={type_name} cap={self.cap} "
            f"total={len(self.text)} file={self.file} role={self.role_taken}/{self.role_total} "
            f"role_pct={self.role_pct} blocks={blocks} cut={cut} missing={missing}"
        ).strip()


def split_front_matter(text: str) -> tuple[dict, str]:
    """``(meta, body)`` of TODO text; the parser is ``unit_contract``'s splitter."""
    from .unit_contract import split_contract_front_matter

    meta, body = split_contract_front_matter(text)
    return (meta or {}, body) if meta is not None else ({}, text)


def read_section(text: str, heading: str) -> str:
    """The ``## <heading>`` section of *text*, heading included; ``""`` if none."""
    pattern = re.compile(rf"^##\s+{re.escape(heading)}.*?(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(text)
    return match.group(0).strip("\n") if match else ""


def read_text(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def todo_file(ctx: StageInput) -> Path:
    return paths.todo_path(ctx.project_dir, ctx.todo_id) or paths.inbox(ctx.project_dir) / f"{ctx.todo_id}.md"


def todo_text(ctx: StageInput) -> str:
    return read_text(todo_file(ctx))


def missing_block(block_id: str, ctx: StageInput, reason: str, path: Path | str) -> Block:
    text = f"нет данных: {reason} — {path}"
    return Block(id=block_id, text=text, limit=ctx.limit(block_id), source=str(path), missing=reason)


def present_block(block_id: str, ctx: StageInput, text: str, path: Path | str) -> Block:
    """A present block, cut to its own limit; an empty artifact is "блок пуст"."""
    if not text.strip():
        return missing_block(block_id, ctx, "блок пуст", path)
    block = Block(id=block_id, text=text.strip(), limit=ctx.limit(block_id), source=str(path), owner=str(path))
    # PREVLINK's share is a budget, not a clip: AC5 wants every path named.
    if block_id == "PREVLINK":
        return block
    return fit_to_limit(block)


def load_config(project_dir: str | Path) -> dict[str, Any]:
    path = paths.config_file(project_dir)
    try:
        data = yaml.safe_load(read_text(path)) or {}
    except yaml.YAMLError as e:
        raise StageContextError(f"CTX-FAIL: config.yaml не читается: {e}") from e
    return data if isinstance(data, dict) else {}


def load_snapshot(project_dir: str | Path, todo_id: str) -> dict:
    path = paths.pipeline_snapshot_path(project_dir, todo_id)
    try:
        data = yaml.safe_load(read_text(path)) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def detect_conflicts(
    todo_meta: dict, done_meta: dict, gates_facts: dict | None
) -> list[str]:
    """AC8: the same fact from two owners — a line per pair, the stage chooses nothing."""
    conflicts: list[str] = []
    todo_status = todo_meta.get("status")
    done_status = done_meta.get("status")
    if todo_status and done_status and str(todo_status) != str(done_status):
        conflicts.append(
            f"CTX-CONFLICT unit.status: {todo_status!r} (TODO) против {done_status!r} (DONE.json)"
        )
    verdict = (gates_facts or {}).get("verdict")
    gates = todo_meta.get("gates")
    if verdict and isinstance(gates, list):
        stated = {str(g).lower() for g in gates}
        failed = {"failed", "fail"}
        passed = {"ok", "passed", "pass"}
        if (stated & failed and verdict in passed) or (stated & passed and verdict in failed):
            conflicts.append(
                f"CTX-CONFLICT gates.verdict: {sorted(stated)} (TODO) против {verdict!r} (GATES)"
            )
    return conflicts


def _done_meta(project_dir: Path, todo_id: str) -> dict:
    path = paths.outbox(project_dir) / f"DONE-{todo_id}.json"
    body = read_text(path).strip()
    if not body:
        return {}
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def gates_report(ctx: StageInput) -> tuple[Path, dict | None]:
    """The GATES artifact: its path and its facts; one reader for block and conflict check."""
    path = paths.context_dir(ctx.project_dir) / f"GATES-{ctx.todo_id}.md"
    if not path.is_file():
        return path, None
    from .verify_pack import read_gates_facts

    return path, read_gates_facts(path)


def build_stage_context(
    project_dir: str | Path,
    todo_id: str,
    stage_name: str,
    role: str,
    *,
    attempt: int = 1,
    config: dict | None = None,
    snapshot: dict | None = None,
    cap: int = CAP,
    exclude: tuple[str, ...] = (),
    extra: tuple[str, ...] = (),
    suffix: str = "",
) -> StageContext:
    """K7: build, check P0, cut P1–P5, write the file, log ``CTX``.

    ``exclude``/``extra`` change the type's block set (a replan drops
    ``PREVLINK`` and adds ``BLOCKED``); ``suffix`` shares the cap.
    """
    from . import stage_context_blocks as blocks_module

    project = Path(project_dir).resolve()
    config_path = paths.config_file(project)
    # The project's ``stage_types`` is the owner; an in-memory config (the
    # spawn passes the keys it reads) only overrides what it carries.
    if not isinstance(config, dict) or "stage_types" not in config:
        disk = load_config(project)
        config = disk if not config else {**disk, **config}
    family, row = resolve(config, stage_name, role, config_path=config_path)
    type_name = display_type(family, row)
    base = [b for b in FAMILIES.get(type_name, ()) if b not in exclude]
    at = base.index("CRIT") if "CRIT" in base else len(base)
    order = tuple(base[:at] + [b for b in extra if b not in exclude] + base[at:])
    if any(b not in LIMITS[type_name] and b not in EXTRA_LIMITS for b in extra):
        raise StageContextError(f"CTX-FAIL: extra без доли в {type_name}: {', '.join(extra)}")
    if not order:
        raise StageContextError(f"CTX-FAIL: у типа {type_name!r} нет набора блоков")
    if snapshot is None:
        snapshot = load_snapshot(project, todo_id)

    ctx = StageInput(
        project_dir=project,
        todo_id=todo_id,
        stage_name=stage_name,
        role=role,
        family=family,
        row=row,
        attempt=attempt,
        limits=LIMITS[type_name],
        snapshot=snapshot,
    )
    blocks = [blocks_module.render_block(block_id, ctx) for block_id in order]
    suffix = suffix.strip()[:SUFFIX_MAX]
    floor = LIMITS[type_name].get("ROLE", 0)
    # Vitok 5: the tail (the stage's own policy) is reserved whole, then the
    # role floor; where both do not fit (verify at 6 000) the floor yields
    # and the cut is visible in ``CTX``.
    room = cap - (len(suffix) + 2 if suffix else 0)
    reserve = minimum_blocks_len(blocks, floor)
    if reserve > room:
        reserve = minimum_blocks_len(blocks)
    if reserve > room:
        raise StageContextError(
            f"CTX-FAIL: минимум блоков {reserve} > {room} (роль {floor}, хвост {len(suffix)})"
        )
    text, cut, failed = fit_blocks(blocks, max(1, room))
    role_block = next((block for block in blocks if block.id == "ROLE"), None)
    role_taken, role_total = (role_block.taken, role_block.total) if role_block else (0, 0)
    if role_block is not None and role_block.cut:
        # AC38 measures what the stage actually reads: a role cut down to its
        # marker must not report the whole selected core as delivered.
        role_taken = max(0, role_taken - role_block.cut)
    if failed:
        sizes = ", ".join(f"{block.id}:{len(block.full())}/{block.limit}" for block in blocks)
        raise StageContextError(
            f"CTX-FAIL: P0 не влез в потолок ({cap}); сработало: {', '.join(failed)}; "
            f"блоки {sizes}"
        )
    if suffix:
        out = next((b.full() for b in reversed(blocks) if b.id == "OUTPUT" and b.full()), "")
        at = text.rfind(out) if out else -1
        text = text[:at] + suffix + "\n\n" + text[at:] if at >= 0 else f"{text}\n\n{suffix}"
    target = paths.context_dir(project) / f"STAGE-{todo_id}-{stage_name}.md"
    if not paths.context_dir(project).is_dir():
        raise StageContextError(f"CTX-FAIL: каталога нет — {paths.context_dir(project)}")
    try:
        atomic_write_text(target, text, encoding="utf-8")
    except OSError as e:
        raise StageContextError(f"CTX-FAIL: файл стадии не записан — {target}: {e}") from e

    positions: dict[str, int] = {}
    cursor = 0
    for block in blocks:
        if not block.full():
            continue
        positions[block.id] = cursor
        cursor += len(block.full()) + 2
    missing = {block.id: block.missing for block in blocks if block.missing}
    conflicts = detect_conflicts(
        split_front_matter(read_text(todo_file(ctx)))[0],
        _done_meta(project, todo_id),
        gates_report(ctx)[1],
    )
    result = StageContext(
        text=text,
        blocks=blocks,
        cut=cut,
        missing=missing,
        conflicts=conflicts,
        positions=positions,
        file=target,
        cap=cap,
        family=family,
        row=row,
        role_taken=role_taken,
        role_total=role_total,
        role_pct=100 if role_total <= 0 else min(100, (role_taken * 100) // role_total),
    )
    try:
        _log.log(paths.logs_dir(project), result.ctx_line(todo_id, stage_name, attempt))
    except OSError:
        pass
    if result.role_pct < ROLE_LOW_PCT:
        print(f"WARN CTX-ROLE-LOW: {todo_id} {stage_name} role={result.role_pct}%", flush=True)
    return result


__all__ = [
    "CAP",
    "EXTRA_LIMITS",
    "FAMILIES",
    "LIMITS",
    "StageContext",
    "StageContextError",
    "StageInput",
    "build_stage_context",
    "detect_conflicts",
    "gates_report",
    "load_config",
    "load_snapshot",
    "missing_block",
    "present_block",
    "read_section",
    "read_text",
    "split_front_matter",
    "todo_file", "todo_text",
]
