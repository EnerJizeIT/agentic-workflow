"""R2/R5/R9: the deterministic stage context pack.

``.agentic/context/STAGE-<todo>-<stage>.md`` is ONE file that names what a
stage starts from: the contract, the tree state, the previous handoff, the
review findings, the input sha256s. No model calls, no timestamps, no
environment reads — the same inputs produce the same bytes (AC6, AC8).

Section bodies live in :mod:`awf.context_pack_sections`; this module owns
the spec order, the budget and the file. A missing input is a visible
``n/a`` line with the reason (AC9), never silence: an empty section and an
absent input must not look the same.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import git_utils, paths
from ._atomic import atomic_write_text
from ._errors import AwfApiError
from .context_pack_sections import (
    LOG_TAIL,
    SECTION_CAPS,
    TOTAL_CAP,
    baseline_sha,
    clip,
    contract_section,
    delta,
    digests,
    failed_gates,
    handoff_path,
    na,
    previous_stage,
    refs,
    rel_path,
    review_sections,
    stage_log,
)
from .pipeline import Stage, load_stages
from .todo_ids import is_valid_todo_id
from .todos import read_todo_text

__all__ = [
    "ContextPackResult",
    "SECTION_CAPS",
    "SECTIONS",
    "TOTAL_CAP",
    "build_context_pack",
    "input_shas",
    "pack_path",
    "parse_input_shas",
    "render_context_pack",
]

#: Section titles — one vocabulary for the writer and every reader (IMP3's
#: freshness check parses section 8 back). Bodies carry verbatim ``## ``
#: headings (``## REVIEW-фикс``, GATES chunks), so a reader splits the pack
#: by ``^## <number>. `` — never by any ``^## `` (TODO-0367 review).
SECTIONS: tuple[tuple[str, str], ...] = (
    ("1", "Задача и роль"),
    ("2", "Контракт"),
    ("3", "Состояние"),
    ("4", "Прошлая стадия"),
    ("5", "Находки ревью"),
    ("6", "Ссылки"),
    ("7", "Как работать"),
    ("8", "sha входов"),
    ("9", "С прошлого хода"),
)

_SHA_LINE_RE = re.compile(
    r"^- (?P<label>[^:]+): (?P<path>.+?) sha256:(?P<sha>[0-9a-f]+|absent)$"
)


@dataclass(frozen=True)
class ContextPackResult:
    """The built pack: its text, its path and the inputs it could not find."""

    todo_id: str
    stage: str
    text: str
    path: Path
    missing: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "todo_id": self.todo_id,
            "stage": self.stage,
            "path": str(self.path),
            "missing": list(self.missing),
        }


def pack_path(project_dir: str | Path, todo_id: str, stage: str) -> Path:
    """K2 path: ``.agentic/context/STAGE-<todo>-<stage>.md``."""
    return paths.context_dir(project_dir) / f"STAGE-{todo_id}-{stage}.md"


def _stages(project_dir: Path, todo_id: str) -> tuple[list[Stage], str]:
    """Stages of the unit's frozen snapshot and the reason it is unusable."""
    snap = paths.pipeline_snapshot_path(project_dir, todo_id)
    if not snap.is_file():
        return [], f"нет {rel_path(project_dir, snap)}"
    try:
        stages = load_stages(snap)
    except AwfApiError as e:
        raise AwfApiError(
            f"broken pipeline snapshot {rel_path(project_dir, snap)}: {e}"
        ) from e
    return stages, "" if stages else f"пустой {rel_path(project_dir, snap)}"


def _find_stage(stages: list[Stage], stage: str) -> Stage | None:
    return next((s for s in stages if s.name == stage or s.id == stage), None)


def parse_input_shas(text: str) -> dict[str, str]:
    """Section 8 read back as ``{label: sha}`` — IMP3's freshness check."""
    out: dict[str, str] = {}
    for line in text.split("\n"):
        m = _SHA_LINE_RE.match(line)
        if m:
            out[m.group("label").strip()] = m.group("sha")
    return out


def input_shas(project_dir: str | Path, todo_id: str, stage: str) -> dict[str, str]:
    """The CURRENT sha of every pack input — the same labels as section 8."""
    project = Path(project_dir).resolve()
    stages, _reason = _stages(project, todo_id)
    info = _find_stage(stages, stage)
    prev = previous_stage(stages, stage)
    handoff = handoff_path(project, todo_id, prev)
    return {label: sha for label, _path, sha in digests(project, todo_id, info, handoff)}


def _section_one(todo_id: str, stage: str, info: Stage | None, gap: str) -> list[str]:
    one = [f"- todo: {todo_id}", f"- стадия: {stage}"]
    if info is None:
        one.append(f"- роль: {na(gap or 'стадия не найдена в снимке пайплайна')}")
        return one
    one.append(f"- роль: {info.role or na('роль не задана в снимке пайплайна')}")
    if info.task:
        one.append(f"- задание: {info.task}")
    return one


def _section_three(
    project_dir: Path, todo_id: str, own: str, baseline: str
) -> list[str]:
    baseline_path = paths.context_dir(project_dir) / f"BASELINE-{todo_id}.sha"
    status = git_utils.status_porcelain(project_dir).strip()
    # The pack is an OUTPUT of this run, not an input: its own untracked line
    # would change section 3 on the second build and break AC6/AC18.
    status = "\n".join(line for line in status.split("\n") if own not in line)
    diffstat = git_utils.diff_stat(project_dir, baseline).strip() if baseline else ""
    untracked_path = paths.context_dir(project_dir) / f"BASELINE-{todo_id}.untracked"
    untracked = ""
    if untracked_path.is_file():
        untracked = untracked_path.read_text(encoding="utf-8", errors="surrogateescape")
    three = [f"- baseline: {baseline or na('нет ' + rel_path(project_dir, baseline_path))}"]
    three.append(f"- git status --porcelain: {'да' if status else 'чисто/нет'}")
    three.extend(f"  {line}" for line in status.split("\n") if line)
    three.append("- git diff --stat:")
    if diffstat:
        three.extend(f"  {line}" for line in diffstat.split("\n") if line)
    else:
        three.append(f"  {na('нет baseline')}")
    count = len([line for line in untracked.split("\n") if line.strip()])
    three.append(
        f"- untracked в baseline: {count if untracked_path.is_file() else 'n/a'}"
    )
    return three


def _section_four(project_dir: Path, prev: Stage | None, todo_id: str, handoff: Path | None) -> list[str]:
    if handoff is None:
        reason = (
            f"нет .agentic/handoff/{prev.name}-{todo_id}.md"
            if prev is not None else "прошлая стадия не определена"
        )
        return [na(reason)]
    body = handoff.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    return [f"- путь: {rel_path(project_dir, handoff)}", f"- строк: {len(body)}"] + [
        f"  {line}" for line in body
    ]


def _section_five(
    reviews: list[tuple[str, str]], gates: list[str], log: Path | None, role: str
) -> list[str]:
    """Section 5 from the inputs the caller already read (one read each)."""
    five: list[str] = [f"- секций `## REVIEW-фикс`: {len(reviews)}"]
    for heading, body in reviews:
        five.append(heading)
        if body:
            five.extend(body.split("\n"))
        five.append("")
    five.append("- проваленные секции GATES:")
    five.extend(gates if gates else [f"  {na('нет GATES или провалов')}"])
    five.append(f"- хвост stage-log ({LOG_TAIL} строк):")
    if log is not None:
        tail = log.read_text(encoding="utf-8", errors="surrogateescape").split("\n")[-LOG_TAIL:]
        five.extend(f"  {line}" for line in tail)
    else:
        five.append(f"  {na('нет stage-log для ' + (role or 'стадии'))}")
    return five


def _build(project_dir: Path, todo_id: str, stage: str, write: bool) -> ContextPackResult:
    if not is_valid_todo_id(todo_id):
        raise AwfApiError(f"unknown TODO: {todo_id}")
    text = read_todo_text(project_dir, todo_id)
    todo_file = paths.todo_path(project_dir, todo_id)
    if not text.strip() or todo_file is None:
        raise AwfApiError(f"unknown TODO: {todo_id} (no TODO file in inbox/done)")

    stages, gap = _stages(project_dir, todo_id)
    info = _find_stage(stages, stage)
    if info is None and stages:
        known = ", ".join(s.name for s in stages)
        raise AwfApiError(f"unknown stage: {stage} (known: {known})")
    prev = previous_stage(stages, stage) if info else None
    handoff = handoff_path(project_dir, todo_id, prev)
    path = pack_path(project_dir, todo_id, stage)
    own = rel_path(project_dir, path)

    missing: list[str] = []
    sections: dict[str, list[str]] = {}
    # Every input is read ONCE and handed to the section that renders it:
    # a section that re-reads its own artifact lets two sections disagree
    # about the same fact and makes the second build differ (AC6).
    role = info.role if info else ""
    baseline = baseline_sha(project_dir, todo_id)
    reviews = review_sections(text)
    gates = failed_gates(project_dir, todo_id)
    log = stage_log(project_dir, todo_id, role)

    sections["1"] = clip(
        _section_one(todo_id, stage, info, gap), SECTION_CAPS["1"],
        rel_path(project_dir, todo_file),
    )
    two, found_contract = contract_section(text, rel_path(project_dir, todo_file))
    if not found_contract:
        missing.append("contract")
    sections["2"] = two
    sections["3"] = clip(
        _section_three(project_dir, todo_id, own, baseline), SECTION_CAPS["3"],
        rel_path(project_dir, paths.context_dir(project_dir) / f"BASELINE-{todo_id}.sha"),
    )
    if not baseline:
        missing.append("baseline")
    sections["4"] = clip(
        _section_four(project_dir, prev, todo_id, handoff), SECTION_CAPS["4"],
        rel_path(project_dir, handoff) if handoff else "handoff",
    )
    if handoff is None:
        missing.append("handoff")
    sections["5"] = _section_five(reviews, gates, log, role)
    if log is None:
        missing.append("stage-log")
    if not reviews and not gates:
        missing.append("review")

    found = refs(text)
    if not found:
        missing.append("refs")
    sections["6"] = clip(
        [f"- {ref}" for ref in found] if found else [na("TODO не называет путей")],
        SECTION_CAPS["6"], rel_path(project_dir, todo_file),
    )

    sections["7"] = clip([
        "- прочитай TODO и этот пакет до первого действия",
        "- проверяй утверждения пробами: код, тесты, git — не память",
        f"- пиши сигнал стадии в .agentic/outbox/ (DONE-{todo_id}.ready)",
        "- правки оставь в рабочем дереве; коммитит движок",
    ], SECTION_CAPS["7"], rel_path(project_dir, todo_file))

    sections["8"] = [
        f"- {label}: {shown} sha256:{sha_value}"
        for label, shown, sha_value in digests(project_dir, todo_id, info, handoff)
    ]

    session = paths.context_dir(project_dir) / f"SESSION-{todo_id}-{stage}.json"
    if session.is_file():
        sections["9"] = clip(
            delta(project_dir, baseline or None, gates, reviews),
            SECTION_CAPS["9"], rel_path(project_dir, session),
        )

    out: list[str] = [f"# STAGE-{todo_id}-{stage}", ""]
    for number, title in SECTIONS:
        if number in sections:
            out.append(f"## {number}. {title}")
            out.extend(sections[number])
            out.append("")
    rendered = "\n".join(out).rstrip("\n") + "\n"

    if write:
        atomic_write_text(path, rendered)
    return ContextPackResult(todo_id, stage, rendered, path, tuple(sorted(set(missing))))


def build_context_pack(
    project_dir: str | Path = ".", todo_id: str = "", stage: str = ""
) -> ContextPackResult:
    """Build and WRITE the pack — the API entry point (IMP4 uses it)."""
    return _build(Path(project_dir).resolve(), todo_id, stage, write=True)


def render_context_pack(
    project_dir: str | Path = ".", todo_id: str = "", stage: str = ""
) -> str:
    """Build the pack without touching the disk — the ``--stdout`` path."""
    return _build(Path(project_dir).resolve(), todo_id, stage, write=False).text
