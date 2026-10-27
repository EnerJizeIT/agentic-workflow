"""Role management API: add_role + analyze_roles + zone analysis logic.

All business logic for role analysis lives here (was in
``awf.cmd_analyze_roles`` — audit T2.3 fixed wrong-direction dependency
where api imported from cmd). ``cmd_analyze_roles.run`` is now a thin
CLI wrapper that imports from this module.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .. import config as cfg_mod
from .. import paths
from ..xdg import xdg_config_home
from ._errors import AwfApiError
from ._helpers import (
    is_slug_name,
    managed_write_api_text,
    refuse_unsafe_surfaces,
    require_agentic,
)
from ._results import (
    AddRoleResult,
    AdoptRoleDraftResult,
    AnalyzeRolesResult,
    DiscardRoleDraftResult,
    RoleDraftsResult,
    SeedRolesResult,
)
from ._templates import _ROLE_TEMPLATE, package_role_template

log = logging.getLogger(__name__)

# ─── add_role ───────────────────────────────────────────────────────────

_SKILL_FILENAME = "SKILL.md"

# TODO-0175: the role file carries a uniform model line for every source
# (built-in / template / from_skill). The config stays the source of
# truth — the engine behavior is unchanged; ``check_model_config``
# cross-checks the line against the config and reports drift.
MODEL_LINE_RE = re.compile(
    r"^\*\*(?:Модель|Model):\*\*[ \t]*(?P<value>.*)$", re.MULTILINE
)
UNASSIGNED_MODEL_TEXT = "не назначена (действует модель конфига по умолчанию)"


def model_line_value(model: str) -> str:
    """The value part of the model line: `` `vllm/llm` `` or the
    unassigned placeholder text (empty model → nothing to name)."""
    if model:
        return f"`{model}`"
    return UNASSIGNED_MODEL_TEXT


def model_line(model: str) -> str:
    """The canonical model line for a role file (uniform for all sources).

    ``model_line("vllm/llm")`` → ``**Модель:** `vllm/llm` ``; an empty
    model renders the unassigned placeholder (the config's default model
    applies — nothing to cross-check against).
    """
    return f"**Модель:** {model_line_value(model)}"


def parse_role_model_line(content: str) -> str | None:
    """Extract the model from a role file's model line.

    Returns the model id or None: the line is absent, or it carries the
    unassigned placeholder. A legacy line without backticks
    (``**Model:** vllm/llm``) is read too — old placeholder files are
    still cross-checked.
    """
    m = MODEL_LINE_RE.search(content)
    if not m:
        return None
    value = m.group("value").strip()
    if value == UNASSIGNED_MODEL_TEXT:
        return None
    if value.startswith("`"):
        end = value.find("`", 1)
        if end > 1:
            model = value[1:end].strip()
            if model:
                return model
        return None
    return value or None


def _resolve_role_model(project_dir: Path, role_name: str, explicit: str) -> str:
    """Resolve the role's model: explicit ``model=`` → config ``models:``.

    Priority: an explicit ``model=`` wins; otherwise
    ``models.<role_name>.model`` of ``.agentic/config.yaml``. Empty
    result = unassigned (the config's default model applies).
    """
    if explicit:
        return explicit.strip()
    try:
        config_data = cfg_mod.load(project_dir)
    except Exception:  # noqa: BLE001 — a broken config degrades to unassigned
        return ""
    models_cfg = cfg_mod.get(config_data, "models", {}) or {}
    if not isinstance(models_cfg, dict):
        return ""
    role_cfg = models_cfg.get(role_name)
    if isinstance(role_cfg, dict):
        m = role_cfg.get("model", "")
        if isinstance(m, str) and m.strip():
            return m.strip()
    return ""


def _inject_model_line(content: str, line: str) -> str:
    """Ensure the uniform model line is present in the role content.

    Placement: after the provenance comment (from_skill), after the
    ``# ROLE:`` title's blank line (built-in), at the top otherwise.
    Content that already carries a model line (the placeholder template)
    is left untouched — no duplicate line.
    """
    if MODEL_LINE_RE.search(content):
        return content
    lines = content.split("\n")
    if lines and lines[0].startswith("<!-- copied from skill"):
        pos = 1
    elif lines and lines[0].startswith("# ROLE:"):
        pos = 2 if len(lines) > 2 and lines[1] == "" else 1
    else:
        pos = 0
    lines[pos:pos] = [line, ""]
    return "\n".join(lines)


# REVIEW R4 (TODO-0230): the description is public input on add_role. The
# create path renders it into the placeholder template; a refresh rebuilds
# the body from the skill, so an explicit description must be written (and a
# value already in the file kept when none is passed) — otherwise the input
# is silently lost.
ROLE_DESC_RE = re.compile(r"^\*\*Role:\*\*[ \t]*(?P<value>.*)$", re.MULTILINE)


def parse_role_description_line(content: str) -> str:
    """The description carried by the role file, "" when there is no line."""
    m = ROLE_DESC_RE.search(content)
    return m.group("value").strip() if m else ""


def _inject_role_description(content: str, description: str) -> str:
    """Set (or add) the ``**Role:**`` line — an empty description is a no-op."""
    if not description.strip():
        return content
    line = f"**Role:** {description.strip()}"
    if ROLE_DESC_RE.search(content):
        return ROLE_DESC_RE.sub(lambda _m: line, content, count=1)
    lines = content.split("\n")
    pos = 1 if lines and lines[0].startswith("<!-- copied from skill") else 0
    for i, existing in enumerate(lines):
        if MODEL_LINE_RE.match(existing):
            pos = i + 1
            if pos < len(lines) and lines[pos] == "":
                pos += 1
            break
    lines[pos:pos] = [line, ""]
    return "\n".join(lines)

# TODO-0208 (REPORTS33): a skill-sourced role carries the awf override
# block. The SKILL.md body is a STANDALONE protocol — it ships its own
# "Commit. Stage only the needed files" instructions that contradict the
# awf cycle, where the ENGINE commits after the supervisor's verify. A
# weak model following the skill body could commit inside a unit, bypassing
# the engine. The block overrides exactly that (BD-31 style: appended at
# the end, marker-delimited, idempotent, never touches the skill body):
# commit ownership stays with the engine, no nested agents, work inside
# the TODO's boundaries, finish with a signal — not a commit.

_AWF_OVERRIDES_HEADING = (
    "## AWF-OVERRIDE: awf pipeline overrides (added by awf add_role)"
)
_AWF_OVERRIDES_BLOCK = (
    "\n---\n\n"
    f"{_AWF_OVERRIDES_HEADING}\n"
    "\n"
    "**Commit ownership:** the awf engine commits after the supervisor's "
    "verify and approve — never run `git add`, `git commit`, or "
    "`git push` in a unit; leave your changes in the working tree.\n"
    "\n"
    "**No nested agents:** do not launch nested opencode sessions or "
    "other agents from inside the unit — you are one stage of a single "
    "pipeline run.\n"
    "\n"
    "**Scope and finish:** work strictly inside the TODO's boundaries "
    "(the planned files). When done, write the signal file "
    "(`DONE-<todo>.ready` / `BLOCKED-<todo>.ready`) — not a commit.\n"
)
# TODO-0157-style removal: from the block's `---` separator + heading to
# the NEXT `^## ` heading (or EOF). A following `## BD-31` addendum (or
# vice versa) is a boundary, not part of this block — the two blocks
# coexist, each refreshable independently.
_AWF_OVERRIDES_RE = re.compile(
    r"\n?---\n+## AWF-OVERRIDE: awf pipeline overrides"
    r".*?(?=\n## (?!AWF-OVERRIDE)|\Z)",
    re.DOTALL,
)


def apply_awf_overrides(content: str) -> str:
    """Append (or refresh) the awf override block — idempotent.

    An existing block (same heading) is removed first — up to the next
    ``## `` heading or EOF — before the fresh one is appended, so a
    repeated materialization never duplicates it. The skill body is
    never touched; the block is the file's last section (a BD-31
    addendum appended later by ``analyze_roles_core`` follows it).
    """
    if _AWF_OVERRIDES_HEADING in content:
        content = _AWF_OVERRIDES_RE.sub("", content).rstrip()
    return content.rstrip() + _AWF_OVERRIDES_BLOCK


# TODO-0230 (M6): the delivery discipline is a DEFAULT of the materialization,
# not a hand-written addendum of this repo. Every skill-sourced role (and so
# every preset role) carries it — marker-delimited, idempotent, appended after
# the skill body and before a later BD-31 addendum (analyze_roles appends).
_DISCIPLINE_HEADING = (
    "## Дисциплина доставки инвариантов (added by awf add_role)"
)
_DISCIPLINE_START = "<!-- awf-discipline:start -->"
_DISCIPLINE_END = "<!-- awf-discipline:end -->"
_DISCIPLINE_BLOCK = (
    "\n---\n\n"
    f"{_DISCIPLINE_START}\n"
    f"{_DISCIPLINE_HEADING}\n"
    "\n"
    "Для изменений класса «байты/пути/идентичность/замки» инвариант "
    "считается доставленным только с таблицей «поверхность × исход»: все "
    "писатели, читатели и ветки (например logs+dispatch+verify, "
    "manifest+changes, reset+force-init+rollback, start+refused start), и по "
    "каждой строке — переведено / исключено-с-причиной / явный отказ. "
    "Строки «и так сойдёт» не бывает.\n"
    "\n"
    "- **implementer:** таблица идёт в DONE вместе с кодом; пропущенная "
    "клетка — неготовый юнит.\n"
    "- **architect-review:** обязательный вопрос «где ЕЩЁ должен держаться "
    "этот же инвариант» — пройди соседние поверхности и подтверди или найди "
    "разрыв.\n"
    "- **qa-review:** сначала спустись в каждую клетку таблицы (код+тест), "
    "потом ищи пробелы вне неё.\n"
    "- **project-auditor:** сверь таблицу с реальным деревом (анти-вакуум: "
    "grep по соседям, а не доверие тексту).\n"
    f"{_DISCIPLINE_END}\n"
)

# TODO-0230 (M6, добор M1): the canonical-suite rule used to live only in the
# gitignored ``.agentic/roles/*`` of this repo — a regenerated role lost it.
# Materialization appends it to every skill-sourced role, so the rule reaches
# any project (the script ships with ``awf init``).
#
# TODO-0258 (RB-1): the rule is not the same for every role. One suite run
# belongs to QA; a reviewer reads the artifact instead of paying for a second
# run. The marker pair stays one (existing tests and refresh migration key on
# it) — only the body and the heading follow ``role_test_policy``.
_SUITE_CANON_HEADING = "## Политика тестов (added by awf add_role)"
_SUITE_CANON_START = "<!-- awf-suite-canon:start -->"
_SUITE_CANON_END = "<!-- awf-suite-canon:end -->"

#: Role-name tokens, checked in this order — the first hit wins.
#: ``agent-qa-review`` carries both "qa" and "review", so QA must be first;
#: the remaining token sets do not overlap.
_TEST_POLICY_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("qa", ("qa",)),
    ("auditor", ("auditor",)),
    ("implementer", ("implement",)),
    ("code-reading", ("architect", "review")),
)


def role_test_policy(role_name: str) -> str:
    """The test-run policy for a role: ``qa``/``implementer``/``code-reading``/``auditor``/``targeted``.

    Classification is by name token, not by a role registry: roles are
    created freely (skills, presets, templates), and an unknown name must
    still get a safe default — ``targeted``, never an implicit full suite.
    """
    slug = (role_name or "").strip().lower().replace("_", "-")
    if slug.startswith("agent-"):
        slug = slug[len("agent-"):]
    for policy, tokens in _TEST_POLICY_TOKENS:
        if any(token in slug for token in tokens):
            return policy
    return "targeted"


def _suite_canon_body(policy: str) -> str:
    """The managed test-policy text for one policy id."""
    if policy == "qa":
        return (
            "Полный сьют — ОДИН канонический прогон на attempt: "
            "`scripts/suite-canonical.sh`. Команда по умолчанию — "
            "`verification.test_cmd` из `.agentic/config.yaml`; переопределяется "
            "`AWF_SUITE_CMD` (узкий прогон — своя команда). Артефакт — "
            "`.agentic/context/SUITE-<tree-sha>-<cmd8>.json`; повторный прогон на том "
            "же дереве и той же команде — только с `--force`. Целевые и контрактные "
            "проверки роль делает сама; `scripts/run-all.sh` целиком — только при "
            "сомнении в воротах."
        )
    if policy == "implementer":
        return (
            "Тесты в этой роли — только целевые: файлы изменённого кода и "
            "регрессия соседей. Полный сьют НЕ по умолчанию: без прямой строки "
            "в контракте не запускай `pytest tests/`, `scripts/suite-canonical.sh` "
            "и `run-all.sh`. Если контракт прямо требует полный сьют — ровно ОДИН "
            "прогон `scripts/suite-canonical.sh` (он создаёт артефакт "
            "`.agentic/context/SUITE-<tree-sha>-<cmd8>.json`), не сырой "
            "`pytest tests/`. Канонический прогон — зона QA; реализация не должна "
            "оплачивать его дважды."
        )
    if policy == "code-reading":
        return (
            "Тесты в этой роли НЕ запускаются: ни `pytest tests/`, ни "
            "`scripts/run-all.sh`, ни `scripts/suite-canonical.sh`. Факт состояния "
            "проверок берёшь из готового артефакта "
            "`.agentic/context/SUITE-<tree-sha>-<cmd8>.json` на том же tree-sha "
            "(его создала QA или реализация). Артефакта нет — это находка для "
            "verify, а не повод гонять сьют самому."
        )
    if policy == "auditor":
        return (
            "Тесты: опирайся на артефакт QA "
            "`.agentic/context/SUITE-<tree-sha>-<cmd8>.json` на том же tree-sha и "
            "запускай только целевые проверки под свои находки. Полный сьют "
            "(`scripts/suite-canonical.sh`) сам не гоняй: дубль канонического "
            "прогона даёт те же данные заново. Артефакта нет — назови это в "
            "отчёте и ограничься целевыми."
        )
    return (
        "Тесты: только целевые проверки изменённой поверхности. Полный сьют "
        "(`pytest tests/`, `scripts/suite-canonical.sh`, `run-all.sh`) по умолчанию "
        "не запускай — только по прямой строке контракта."
    )


def _suite_canon_block(role_name: str) -> str:
    """The marker-delimited test-policy block for one role."""
    return (
        "\n---\n\n"
        f"{_SUITE_CANON_START}\n"
        f"{_SUITE_CANON_HEADING}\n"
        "\n"
        f"{_suite_canon_body(role_test_policy(role_name))}\n"
        f"{_SUITE_CANON_END}\n"
    )


def _marker_block_pattern(start: str, end: str) -> re.Pattern[str]:
    """The one spelling of a managed block, separators included.

    REVIEW architect (M6): the apply helper and the tail stripper
    each built this pattern — two owners of one layout, free to drift apart.
    """
    # ``^`` (MULTILINE) is zero-width: the newline BEFORE the separator
    # stays in place, so removing a block cannot glue the lines around it
    # together.
    return re.compile(
        rf"(?:^---[ \t]*\n(?:\n)*)?\n*"
        rf"{re.escape(start)}.*?{re.escape(end)}[ \t]*\n?",
        re.DOTALL | re.MULTILINE,
    )


def _orphan_marker_pattern(start: str) -> re.Pattern[str]:
    """A start marker whose end marker is gone — the marker + its heading."""
    return re.compile(
        rf"(?:^---[ \t]*\n(?:\n)*)?\n*{re.escape(start)}[ \t]*\n"
        rf"(?:#{{2,3}}[^\n]*\n)?",
        re.MULTILINE,
    )


def _drop_marker_blocks(text: str, *, start: str, end: str) -> str:
    """Remove every managed copy of a block — complete pairs and orphans.

    The block brings its own ``---`` separator on re-append, so the old one
    goes with it; otherwise every re-apply leaves one more horizontal rule.

    An ORPHAN start marker (the end marker was hand-deleted) cannot be
    bounded: only the marker line and the managed heading right below it
    go, the body survives as unowned text. Dropping to EOF would be the
    other guess — and it would eat the owner's notes that happen to follow
    the broken block (the QA F5 class). No managed copy survives either
    way, so a re-apply never appends a SECOND start marker (X1).
    """
    pair = _marker_block_pattern(start, end)
    while pair.search(text):
        text = pair.sub("", text, count=1)
    return _orphan_marker_pattern(start).sub("", text)


def _apply_marker_block(content: str, *, start: str, end: str, block: str) -> str:
    """Insert ``block`` once: drop an existing marker-delimited copy first."""
    return _drop_marker_blocks(content, start=start, end=end).rstrip() + block


def apply_discipline_block(content: str) -> str:
    """Append (or refresh) the delivery-discipline block — idempotent."""
    return _apply_marker_block(
        content,
        start=_DISCIPLINE_START,
        end=_DISCIPLINE_END,
        block=_DISCIPLINE_BLOCK,
    )


def apply_suite_canon_block(content: str, role_name: str = "") -> str:
    """Append (or refresh) the role's test-policy block — idempotent.

    An empty ``role_name`` falls back to the ``targeted`` policy: the caller
    that does not know the role must not silently hand out a full suite.
    """
    return _apply_marker_block(
        content,
        start=_SUITE_CANON_START,
        end=_SUITE_CANON_END,
        block=_suite_canon_block(role_name),
    )


def _strip_managed_role_blocks(text: str) -> str:
    """Drop marker-delimited managed blocks from a preserved tail (X1).

    The tail keeps the owner's lines verbatim, but the two managed rules are
    regenerated above it: a stale copy in the tail would either duplicate the
    block or (worse) shadow the refreshed text. Only managed spans go;
    anything outside the markers is the owner's and stays.
    """
    for start, end in (
        (_DISCIPLINE_START, _DISCIPLINE_END),
        (_SUITE_CANON_START, _SUITE_CANON_END),
    ):
        text = _drop_marker_blocks(text, start=start, end=end)
    return text.strip("\n")


def _apply_managed_role_blocks(content: str, role_name: str = "") -> str:
    """Every managed default a materialized role carries — one list.

    Two paths materialize a role: ``add_role`` (create) and the setup
    flow-variant (``_materialize_variant_roles``). A new managed block
    added here reaches both; a block appended at one call site only would
    silently split the two owners (REVIEW architect).

    ``role_name`` picks the test policy (TODO-0258): the block's body is
    owned by the role's kind, not by the call site.
    """
    return apply_suite_canon_block(
        apply_discipline_block(content), role_name=role_name
    )


#: Wrappers of the pre-M6 canonical-suite copy (the M1 task shipped it with
#: markers but without the managed discipline/suite wording).
_LEGACY_SUITE_START = "<!-- M1-canonical-suite:start -->"
_LEGACY_SUITE_END = "<!-- M1-canonical-suite:end -->"

#: The hand-written discipline section (REVIEW V7): recognised by its exact
#: intro line plus the known role bullets, so an edited section is kept
#: whole instead of leaving orphan bullet lines behind.
_LEGACY_DISCIPLINE_INTRO_PREFIX = (
    "Для изменений класса «байты/пути/идентичность/замки»"
)
_LEGACY_DISCIPLINE_BULLETS = (
    "- implementer:",
    "- architect-review:",
    "- qa-review:",
    "- project-auditor:",
)


def _drop_trailing_separator(out: list[str]) -> None:
    """Drop the blank lines / ``---`` separator that introduced a legacy block."""
    while out and not out[-1].strip():
        out.pop()
    if out and out[-1].strip() == "---":
        out.pop()
        while out and not out[-1].strip():
            out.pop()


def _strip_legacy_sections(text: str) -> str:
    """Drop pre-marker copies of the managed rules from a preserved tail.

    Only a KNOWN legacy block is dropped, never «the heading and whatever
    follows» (REVIEW T1 / QA F5): a note appended after the block is the
    owner's text and stays. The hand-written discipline block is recognised
    by its exact intro line and its known bullet lines — a blank line
    between the intro and the list does not leave orphan bullets behind
    (REVIEW V7 / A4); any other shape is left untouched. The canonical-suite
    copy is bounded by its M1 markers (or, without them, by the contiguous
    block under the heading).
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == _LEGACY_SUITE_START:
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and _is_legacy_canon_heading(lines[j]):
                i = j  # drop the marker, keep processing its heading below
                continue
            out.append(line)
            i += 1
            continue
        if _is_legacy_discipline_heading(line):
            end = _legacy_discipline_block_end(lines, i)
            _drop_trailing_separator(out)
            i = end
            continue
        if _is_legacy_canon_heading(line):
            _drop_trailing_separator(out)
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            while (
                i < len(lines)
                and lines[i].strip()
                and lines[i].strip() != _LEGACY_SUITE_END
            ):
                i += 1
            if i < len(lines) and lines[i].strip() == _LEGACY_SUITE_END:
                i += 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
            continue
        out.append(line)
        i += 1
    return "".join(out).strip("\n")


def _is_legacy_discipline_heading(line: str) -> bool:
    return line.strip().startswith(
        "## Дисциплина доставки инвариантов (супервизор"
    )


def _is_legacy_canon_heading(line: str) -> bool:
    return line.strip().startswith("## Канонический полный сьют")


def _legacy_discipline_block_end(
    lines: list[str], start: int
) -> int:
    """End index of a hand-written discipline block.

    Two accepted shapes. The KNOWN block (exact intro line + the known role
    bullets, blank lines allowed anywhere) is dropped whole — a blank line
    between the intro and the list must not leave orphan bullets (REVIEW V7
    / A4). An unrecognised body is dropped only up to the next blank line:
    the block is then heading + one contiguous paragraph, exactly what the
    pre-M6 copy looked like — free text after the blank is the owner's note
    (QA F5).
    """
    j = start + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines) and lines[j].strip().startswith(
        _LEGACY_DISCIPLINE_INTRO_PREFIX
    ):
        # the intro paragraph (usually one long line)
        while j < len(lines) and lines[j].strip():
            if lines[j].lstrip().startswith("## "):
                break
            j += 1
        # the known role bullets, blank lines between them allowed
        while True:
            k = j
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k >= len(lines) or not lines[k].lstrip().startswith(
                _LEGACY_DISCIPLINE_BULLETS
            ):
                return k
            j = k + 1
            while j < len(lines) and lines[j].strip():
                j += 1
    # unknown body: heading + the contiguous paragraph, stop at a blank
    j = start + 1
    while (
        j < len(lines)
        and lines[j].strip()
        and not lines[j].lstrip().startswith("## ")
        and lines[j].strip() != _LEGACY_SUITE_END
    ):
        j += 1
    if j < len(lines) and lines[j].strip() == _LEGACY_SUITE_END:
        j += 1
    return j


_PROVENANCE_DATE_RE = re.compile(r" on \d{4}-\d{2}-\d{2} -->", re.MULTILINE)


def _without_provenance_date(content: str) -> str:
    """Content without the provenance date — refresh's no-op comparison.

    The date records the last real re-materialization, so it must not turn
    an otherwise identical refresh into a rewrite on the next calendar day.
    """
    return _PROVENANCE_DATE_RE.sub(" -->", content)


#: Headings that start the "local additions" tail of a materialized role.
#: A refresh rebuilds the skill body above them and keeps this tail verbatim
#: (AWF-OVERRIDE, BD-31, discipline, suite canon and the user's own lines).
_LOCAL_TAIL_HEADINGS = (
    _AWF_OVERRIDES_HEADING,
    "## BD-31: Pipeline-specific disambiguation",
    _DISCIPLINE_HEADING,
    _SUITE_CANON_HEADING,
)


def _split_local_tail(content: str) -> str:
    """Everything from the earliest awf marker heading to EOF (verbatim)."""
    anchors = _LOCAL_TAIL_HEADINGS + (
        _DISCIPLINE_START,
        _SUITE_CANON_START,
    )
    positions = [
        content.find(anchor) for anchor in anchors if anchor in content
    ]
    if not positions:
        return ""
    start = min(positions)
    # Include the ``---`` separator line right above the heading when present.
    head = content[:start].rstrip("\n")
    if head.endswith("---"):
        start = content.rfind("---", 0, start)
    return content[start:].strip("\n")


# ORCH M5.1: draft area — role candidates isolated from live roles until
# checked. A candidate is ``roles/draft/<slug>.md``; the first line is a
# machine marker that records its source (``skill:<name>`` / ``builtin`` /
# ``template``). A hand-placed candidate without the marker lists as
# ``file``. Live ``roles/*.md``, config and the pipeline are never touched
# by the draft path, and ``adopt_role_draft`` refuses a slug conflict with
# a live role — awf never overwrites a live role.
_DRAFT_DIRNAME = "draft"
_DRAFT_MARKER_RE = re.compile(
    r"^<!-- awf-draft source=(?P<source>[^ >]+) created=(?P<created>\S+) -->[ \t]*\n?"
)


def _skill_roots(project_dir: Path) -> list[Path]:
    """Candidate skill roots, project-local first.

    Mirrors ``resolve_role_file`` (awf/supervisor.py): a project skill in
    ``.opencode/skills/`` of the same name shadows the global one in
    ``$XDG_CONFIG_HOME/opencode/skills/``.
    """
    return [
        project_dir / ".opencode" / "skills",
        xdg_config_home() / "opencode" / "skills",
    ]


def _list_available_skills(project_dir: Path) -> list[str]:
    """Skill names found in the candidate roots (sorted, deduped).

    Never raises: missing or unreadable roots are skipped, so an empty
    home yields a usable "(none)" listing instead of a crash.
    """
    names: set[str] = set()
    for root in _skill_roots(project_dir):
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir() and (entry / _SKILL_FILENAME).is_file():
                    names.add(entry.name)
            except OSError:
                continue
    return sorted(names)


def _find_skill_file(skill_name: str, project_dir: Path) -> Path:
    """Locate ``<root>/<skill_name>/SKILL.md`` or raise with the listing.

    ``skill_name`` is validated here as well (defense in depth): callers
    pass public input (``from_skill``) or repo-controlled provenance data,
    and ``root / skill_name`` with an absolute name silently replaces the
    root — the same escape class as a ``..`` segment.
    """
    if not is_slug_name(skill_name):
        raise AwfApiError(
            f"Invalid skill name {skill_name!r}: use lowercase letters, "
            "digits, '-' or '_', starting with a letter or digit."
        )
    roots = _skill_roots(project_dir)
    for root in roots:
        candidate = root / skill_name / _SKILL_FILENAME
        if candidate.is_file():
            return candidate
    available = ", ".join(_list_available_skills(project_dir)) or "(none)"
    raise AwfApiError(
        f"Skill {skill_name!r} not found. Looked in: "
        + "; ".join(str(r) for r in roots)
        + f". Available skills: {available}."
    )


# W7: roles whose protocol ships with awf (built-in templates in
# awf/templates/roles/). Only these names take the built-in template;
# every other name (including "supervisor" — that template belongs to
# awf init) keeps the placeholder behavior.
# REVIEW W2: every shipped role template is reachable through add_role —
# a variant can materialize the same file, so a hand-written add_role must
# not fall back to an empty "new role" placeholder.
_BUILTIN_ROLE_NAMES = (
    "agent-qa-review",
    "agent-implementer",
    "agent-architect-review",
    "agent-project-auditor",
    "agent-refactoring-specialist",
)


def _builtin_role_template(role_name: str) -> Path | None:
    """W7: built-in role template, or None for the placeholder fallback.

    ``role_name`` is slug-validated by the caller before this is reached,
    so the path cannot escape the templates dir. Returns None when the
    name is not a built-in role or the shipped file is missing (old
    installs) — the caller falls back to the placeholder, so previous
    names and behavior are unchanged.
    """
    if role_name not in _BUILTIN_ROLE_NAMES:
        return None
    return package_role_template(role_name)


def _strip_yaml_frontmatter(content: str) -> str:
    """Drop a leading ``---`` YAML block if present; keep the rest verbatim.

    A SKILL.md starts with its own front-matter (name/description/
    metadata); a role file must not inherit it as YAML (RUN3 #3).
    """
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return content
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :]).lstrip("\n")
    return content  # opening fence without a closing one — not frontmatter


# ─── add_role: config.yaml model write (REPORTS32, TODO-0202) ────────────
#
# The config is the source of truth for a role's model
# (awf/supervisor.py get_role_model reads ``models.<role>.model``). Before
# REPORTS32, ``add_role(model=…)`` wrote only the ``**Модель:**`` line in
# the role file and left the config entry a manual step — "easy to be
# fooled into thinking the role is already configured". Now the explicit
# ``model=`` is also merged into ``.agentic/config.yaml``.
#
# The write is a SURGICAL text edit, not a full yaml re-dump: a re-dump
# would drop the user's comments in config.yaml (the hard rules forbid
# touching what is not the model entry). The edit is validated before the
# file is written: the result must parse as YAML and carry the exact
# value, otherwise the original file is left untouched.


def _yaml_scalar(value: str) -> str:
    """Canonical YAML scalar for *value* (round-trips through safe_load).

    A top-level scalar document may carry a ``...`` document-end marker
    (    plain scalars do) — only the scalar line itself is kept, so the
    value can be embedded in a mapping without ending the document.
    """
    for line in yaml.safe_dump(value, default_flow_style=False).splitlines():
        line = line.strip()
        if line and line != "...":
            return line
    return ""


def _line_indent(line: str) -> int:
    """Number of leading spaces (YAML indent is spaces, not tabs)."""
    return len(line) - len(line.lstrip(" "))


class _StrictYamlLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys.

    Plain PyYAML silently tolerates duplicates (last one wins) — writing a
    config with two ``qa:`` lines would be a structural corruption the
    engine reads as "the last one", so the validator must catch it.
    """


def _construct_no_dup_mapping(loader, node):
    seen = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if key in seen:
            raise yaml.YAMLError(f"duplicate key {key!r} in mapping")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=False)


_StrictYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_no_dup_mapping,
)


def _validate_config_update(new_text: str, role_name: str, model: str) -> None:
    """The edit must round-trip: valid YAML with the exact value set.

    Raises :class:`AwfApiError` otherwise — the caller does not write the
    file, so a failed edit can never corrupt config.yaml. Duplicate mapping
    keys (silently tolerated by plain PyYAML) are refused too.
    """
    try:
        data = yaml.load(new_text, Loader=_StrictYamlLoader)
    except yaml.YAMLError as e:
        raise AwfApiError(
            f"config.yaml edit for models.{role_name}.model produced invalid "
            f"YAML — the file was left untouched: {e}"
        ) from e
    models = data.get("models") if isinstance(data, dict) else None
    entry = models.get(role_name) if isinstance(models, dict) else None
    got = entry.get("model") if isinstance(entry, dict) else None
    if got != model:
        raise AwfApiError(
            f"config.yaml edit for models.{role_name}.model did not take "
            f"(got {got!r}, want {model!r}) — the file was left untouched."
        )


def _compute_config_update(
    raw: str, role_name: str, model: str
) -> tuple[str, str] | None:
    """Surgically set ``models.<role_name>.model`` in *raw* config text.

    Returns ``(new_text, description)`` when a write is needed, or
    ``None`` when the value already equals *model* (idempotent no-op).
    Every other byte (comments, unrelated keys, ordering) is preserved.
    """
    value = _yaml_scalar(model)
    lines = raw.split("\n")

    # Idempotency: the value is already there (as a YAML value) → no-op.
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        data = {}
    models = data.get("models") if isinstance(data, dict) else None
    entry = models.get(role_name) if isinstance(models, dict) else None
    current = entry.get("model") if isinstance(entry, dict) else None
    if current == model:
        return None

    # Locate the top-level ``models:`` key (column 0, optional comment).
    models_idx = next(
        (i for i, ln in enumerate(lines) if re.match(r"^models:\s*(#.*)?$", ln)),
        None,
    )

    if models_idx is None:
        # No models section: append one (trim trailing blank lines first).
        out = list(lines)
        while out and not out[-1].strip():
            out.pop()
        if out:
            out.append("")
        out += ["models:", f"  {role_name}:", f"    model: {value}"]
        new_text = "\n".join(out)
        if not new_text.endswith("\n"):
            new_text += "\n"
        _validate_config_update(new_text, role_name, model)
        return new_text, "created the models section"

    # The models block spans until the next top-level key (column 0,
    # non-blank, non-comment) or EOF.
    block_end = len(lines)
    for i in range(models_idx + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln.lstrip().startswith("#") and _line_indent(ln) == 0:
            block_end = i
            break

    # Role-key indent = the shallowest content line in the block; the
    # child delta comes from the first existing role's first child
    # (2-space files → 2, 4-space → 4; default 2).
    role_indent: int | None = None
    child_delta = 2
    for i in range(models_idx + 1, block_end):
        ln = lines[i]
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        ind = _line_indent(ln)
        if role_indent is None:
            role_indent = ind
        elif ind > role_indent:
            child_delta = ind - role_indent
            break
    if role_indent is None:
        # ``models:`` with no entries (empty block).
        role_indent = 2

    role_re = re.compile(rf"^\s*{re.escape(role_name)}:\s*(#.*)?$")
    role_idx = None
    for i in range(models_idx + 1, block_end):
        if role_re.match(lines[i]) and _line_indent(lines[i]) == role_indent:
            role_idx = i
            break

    if role_idx is not None:
        # The role block spans until a line back at (or above) role_indent.
        role_block_end = block_end
        for i in range(role_idx + 1, block_end):
            ln = lines[i]
            if (
                ln.strip()
                and not ln.lstrip().startswith("#")
                and _line_indent(ln) <= role_indent
            ):
                role_block_end = i
                break
        model_re = re.compile(r"^(\s*)model:\s*(.*)$")
        model_idx = None
        for i in range(role_idx + 1, role_block_end):
            m = model_re.match(lines[i])
            if m:
                model_idx = i
                break
        if model_idx is not None:
            indent = " " * _line_indent(lines[model_idx])
            lines[model_idx] = f"{indent}model: {value}"
            desc = "updated the existing model value"
        else:
            # Add the model key as the role block's last child.
            child_indent = None
            for i in range(role_idx + 1, role_block_end):
                ln = lines[i]
                if (
                    ln.strip()
                    and not ln.lstrip().startswith("#")
                    and _line_indent(ln) > role_indent
                ):
                    child_indent = _line_indent(ln)
                    break
            if child_indent is None:
                child_indent = role_indent + child_delta
            insert_idx = role_idx + 1
            for i in range(role_idx + 1, role_block_end):
                if lines[i].strip():
                    insert_idx = i + 1
            lines.insert(
                insert_idx, f"{' ' * child_indent}model: {value}"
            )
            desc = "added the model key to the role block"
    else:
        # No role block: append one at the end of the models block.
        insert_idx = models_idx + 1
        for i in range(models_idx + 1, block_end):
            if lines[i].strip():
                insert_idx = i + 1
        lines[
            insert_idx:insert_idx
        ] = [
            f"{' ' * role_indent}{role_name}:",
            f"{' ' * (role_indent + child_delta)}model: {value}",
        ]
        desc = "added the role block under models"

    new_text = "\n".join(lines)
    _validate_config_update(new_text, role_name, model)
    return new_text, desc


#: The provenance line add_role writes for a skill-sourced role. Refresh reads
#: it to find the source skill; a role without it is not refreshable.
_SKILL_PROVENANCE_RE = re.compile(r"<!-- copied from skill `(?P<skill>[^`]+)`")


def _skill_role_content(skill_name: str, skill_file: Path, raw: str) -> str:
    """Provenance line + the skill body — the create/refresh shared core.

    ONE format for both paths: a refresh compares its rebuilt content with
    the file, so a second, slightly different header would report a
    perpetual ``updated``.
    """
    body = _strip_yaml_frontmatter(raw).strip("\n")
    return (
        f"<!-- copied from skill `{skill_name}` ({skill_file}) "
        f"on {date.today().isoformat()} -->\n{body}\n"
    )


def materialize_builtin_role_content(
    project_dir: Path, role_name: str, raw: str
) -> str:
    """The exact content ``add_role`` produces from a package role template.

    The setup flow-variant copies package roles too; it must not grow a
    second copy of the rule set (model line + managed blocks), or the same
    role differs depending on the path that created it (REVIEW architect —
    the variant path was missing the uniform model line).
    """
    resolved_model = _resolve_role_model(project_dir, role_name, "")
    content = _inject_model_line(raw, model_line(resolved_model))
    return _apply_managed_role_blocks(content, role_name=role_name)


def _plan_model_config_update(
    project_dir: Path, role_name: str, model: str
) -> tuple[tuple[str, str] | None, str]:
    """The config edit for an explicit ``model=`` — computed and validated.

    Returns ``(None, "")`` for an empty model. The create and refresh paths
    share this: the edit must be validated before the role file is written
    (the REPORTS32 invariant), or a failed edit leaves an orphaned role file.
    """
    if not model.strip():
        return None, ""
    config_path = paths.config_file(project_dir)
    raw_cfg = (
        config_path.read_text(encoding="utf-8")
        if config_path.is_file()
        else ""
    )
    return _compute_config_update(raw_cfg, role_name, model.strip()), (
        f"models.{role_name}.model"
    )


def _refresh_skill_role(
    project_dir: Path,
    role_name: str,
    model: str = "",
    description: str = "",
    force: bool = False,
) -> AddRoleResult:
    """Re-materialize an existing role from its skill, keeping local blocks.

    The skill body and the model line are rebuilt; everything from the first
    awf marker heading to EOF (AWF-OVERRIDE, BD-31, the discipline and suite
    canon blocks, the user's own lines) is kept. The one exception is a
    pre-marker (hand-written) copy of a managed rule: it is replaced by the
    managed block, never kept next to it. A role that is not skill-sourced,
    or whose skill is gone, is a ``skipped`` row with a reason — not an
    error and not a rewrite. A repeated call on an unchanged skill returns
    ``unchanged`` without touching the file, even on a later day.

    REVIEW N1 (TODO-0230): the model is NOT re-resolved from the config
    alone — a value that lives only in the role file (a draft created with
    ``model=`` and then adopted: neither path writes the config) survives
    the refresh. Priority: explicit ``model=`` → ``models.<role>.model`` →
    the model line of the existing file. An explicit ``model=`` is also
    merged into the config, exactly like the create path.

    REVIEW R4 (TODO-0230): the same rule for ``description`` — an explicit
    value updates the ``**Role:**`` line, an absent one keeps the file's
    value. ``force=True`` writes an otherwise unchanged file anyway (the
    caller asked for a rewrite); the status still reflects what happened.
    """
    role_file = project_dir / ".agentic" / "roles" / f"{role_name}.md"
    if not role_file.is_file():
        return AddRoleResult(
            role_name=role_name,
            role_file=str(role_file),
            model="",
            refresh_status="conflict",
            refresh_reason=(
                "role file not found — create it with add_role first"
            ),
        )
    try:
        existing = role_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return AddRoleResult(
            role_name=role_name,
            role_file=str(role_file),
            model="",
            refresh_status="conflict",
            refresh_reason=f"cannot read the role file: {e}",
        )

    m = _SKILL_PROVENANCE_RE.search(existing)
    if not m:
        return AddRoleResult(
            role_name=role_name,
            role_file=str(role_file),
            model="",
            refresh_status="skipped",
            refresh_reason=(
                "not skill-sourced (built-in/template or hand-written) — "
                "nothing to re-materialize"
            ),
        )
    skill_name = m.group("skill")

    # REVIEW W1 (QA F7): the provenance comment is repo-controlled data —
    # a crafted role file could name an absolute path or ``..`` segments and
    # make refresh copy ANY readable file into .agentic/roles/. Validate the
    # name as a slug BEFORE it reaches ``_find_skill_file`` (pathlib would
    # let an absolute name replace the search root).
    if not is_slug_name(skill_name):
        return AddRoleResult(
            role_name=role_name,
            role_file=str(role_file),
            model="",
            refresh_status="skipped",
            refresh_reason=(
                f"provenance skill name {skill_name!r} is not a valid slug "
                "— the role file was not rebuilt"
            ),
        )

    # REVIEW V2 (QA Q2): a pre-M6 role has neither managed markers nor
    # managed headings. Its whole tail cannot be told apart from the skill
    # body, so rebuilding would silently drop the owner's lines. Refuse
    # without --force; --force is the explicit "replace it" answer.
    if not _split_local_tail(existing) and not force:
        return AddRoleResult(
            role_name=role_name,
            role_file=str(role_file),
            model="",
            refresh_status="skipped",
            refresh_reason=(
                "role has no managed markers; pass --force to replace it "
                "or edit manually"
            ),
        )

    try:
        skill_file = _find_skill_file(skill_name, project_dir)
    except AwfApiError as e:
        return AddRoleResult(
            role_name=role_name,
            role_file=str(role_file),
            model="",
            refresh_status="skipped",
            refresh_reason=str(e),
        )
    try:
        raw = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return AddRoleResult(
            role_name=role_name,
            role_file=str(role_file),
            model="",
            refresh_status="skipped",
            refresh_reason=f"cannot read skill file {skill_file}: {e}",
        )

    resolved_model = _resolve_role_model(project_dir, role_name, model)
    if not resolved_model:
        # N1: the config has no say — keep the value the file carries
        # (e.g. an adopted draft whose model was never merged into config).
        resolved_model = parse_role_model_line(existing) or ""
    # R4: an explicit description wins; otherwise the file's line survives.
    resolved_description = description.strip() or parse_role_description_line(
        existing
    )
    content = _skill_role_content(skill_name, skill_file, raw)
    content = _inject_model_line(content, model_line(resolved_model))
    content = _inject_role_description(content, resolved_description)

    # Defaults the existing file may predate: add them above the preserved
    # tail, so the AWF-OVERRIDE block (part of the tail) stays last. X1: the
    # helpers are marker-idempotent, so calling them always also refreshes a
    # stale or hand-broken managed block; lines outside it stay untouched.
    # The one managed list, not a second copy of it (REVIEW architect): a
    # block added to ``_apply_managed_role_blocks`` must reach refresh too.
    content = _apply_managed_role_blocks(content, role_name=role_name)

    # The preserved tail may still carry the pre-marker (hand-written) copy
    # of a rule the block above now manages — replace it, never keep both.
    tail = _strip_managed_role_blocks(
        _strip_legacy_sections(_split_local_tail(existing))
    )
    if tail:
        # Keep the exact spacing the create path produced: a ``---``
        # separator follows the body with one newline, a bare marker block
        # with a blank line.
        gap = "\n" if tail.startswith("---") else "\n\n"
        content = content.rstrip() + gap + tail + "\n"
    if _AWF_OVERRIDES_HEADING not in content:
        content = apply_awf_overrides(content)

    status = (
        "updated"
        if _without_provenance_date(content) != _without_provenance_date(existing)
        else "unchanged"
    )
    # R4: force is an explicit "rewrite it" — do not let a byte-identical
    # rebuild swallow the request.
    write_body = status == "updated" or force
    if force and status == "unchanged":
        status = "updated"

    # N1 (create parity): an explicit model= is merged into config.yaml.
    # Compute AND validate the edit before any write, exactly like create.
    config_update, model_config = _plan_model_config_update(
        project_dir, role_name, model
    )

    if write_body:
        managed_write_api_text(
            role_file, content, operation="refresh role from skill"
        )
    if config_update is not None:
        managed_write_api_text(
            paths.config_file(project_dir),
            config_update[0],
            operation="update config.yaml",
        )
    return AddRoleResult(
        role_name=role_name,
        role_file=str(role_file),
        model=resolved_model,
        model_config=model_config,
        refresh_status=status,
    )


def add_role(
    project_dir: Path,
    role_name: str,
    *,
    description: str = "",
    model: str = "",
    from_skill: str = "",
    force: bool = False,
    draft: bool = False,
    refresh: bool = False,
) -> AddRoleResult:
    """Generate a new role at ``.agentic/roles/{role_name}.md``.

    Content sources:
    - built-in (W7): ``agent-qa-review`` / ``agent-implementer`` take
      the protocol templates shipped with awf (``awf/templates/roles/``)
      — the supervisor strategy works in other projects too;
    - default (no ``from_skill``, no built-in template): a placeholder
      template (``description``/``model`` fill its sections);
    - ``from_skill="name"``: an opencode skill — the SKILL.md body with
      its own YAML front-matter stripped, under a one-line provenance
      comment (source path + date). ``description`` is ignored for that
      source; the uniform model line is still added (TODO-0175).
      Search order: project-local
      ``.opencode/skills/<name>/SKILL.md`` first, then global
      ``$XDG_CONFIG_HOME/opencode/skills/<name>/SKILL.md``.

    AUD06-07: an existing role file is user data — refused without
    ``force``; ``force=True`` overwrites it (both sources).

    RUN3 #3: an empty ``role_name`` together with ``from_skill`` takes the
    skill name as the role name; an empty ``role_name`` without it is an
    error as before.

    ORCH M5.1: ``draft=True`` writes the candidate to
    ``.agentic/roles/draft/{role_name}.md`` instead — the live roles,
    config and pipeline files are not touched, and the file carries the
    ``awf-draft`` source marker on its first line (see
    :func:`list_role_drafts` / :func:`adopt_role_draft` /
    :func:`discard_role_draft`). An existing candidate is refused without
    ``force`` like a live role file (``force`` replaces the candidate
    only — never a live role).

    TODO-0175: every generated role file carries a uniform model line
    near the top (``**Модель:** `vllm/llm` ``), resolved as: explicit
    ``model=`` → ``models.<role_name>.model`` of the project config →
    the unassigned placeholder (the config's default model applies).
    The config stays the source of truth — the engine behavior is
    unchanged; ``check_model_config`` cross-checks the line.
    ``AddRoleResult.model`` is the resolved model ("" when unassigned).

    TODO-0208 (REPORTS33): a skill-sourced role (``from_skill``) ALSO
    carries the awf override block at the end of the file (BD-31 style,
    marker-delimited): the engine commits after the supervisor's verify
    — the role never runs ``git add``/``git commit``/``git push``; no
    nested opencode/agents; work inside the TODO's boundaries, finish
    with a signal, not a commit. The block overrides the skill body's
    standalone "Commit." instructions without touching the body, is
    refreshed (never duplicated) on a repeated materialization, and
    coexists with the BD-31 disambiguation addendum. Existing role files
    are never modified (creation only); built-in and template sources
    keep their own protocols and get no block.

    REPORTS32 (TODO-0202): an explicit ``model=`` is ALSO merged into
    ``.agentic/config.yaml`` as ``models.<role_name>.model`` (the config
    is the source of truth the engine reads) — the manual "add model
    config" step is gone. The merge is a comment-preserving surgical
    edit: it creates the ``models:`` section when absent, never touches
    unrelated keys, is idempotent (same model → no rewrite), and refuses
    (leaving the file untouched) if the result would not round-trip as
    valid YAML. The ``**Модель:**`` line in the role file stays in sync
    as the human reference. Only a non-empty ``model=`` writes the
    config; a model resolved from the config is already there, and the
    draft path (ORCH M5.1) never touches the config.
    ``AddRoleResult.model_config`` names the written locus
    (``models.<role_name>.model``) when the config was set, "" otherwise.

    TODO-0230 (M6): ``refresh=True`` re-materializes an existing role from
    its provenance skill instead of creating one. The skill body and the
    model line are rebuilt; everything from the first awf marker heading to
    EOF (AWF-OVERRIDE, BD-31, discipline, suite canon and the user's own
    lines) is kept. Pre-marker copies of the managed rules are replaced by
    the managed blocks, never duplicated. ``refresh_status`` reports
    ``updated`` / ``unchanged`` / ``skipped`` (not skill-sourced, or the
    skill is gone) / ``conflict`` (no role file); a repeated call on an
    unchanged skill is a no-op even on a later calendar day.
    """
    # RUN3 #3: from_skill is public input — slug-validated first, because
    # it also supplies the default role name.
    skill_name = ""
    if from_skill:
        skill_name = from_skill.strip().lower()
        if not is_slug_name(skill_name):
            raise AwfApiError(
                f"Invalid skill name {from_skill!r}: use lowercase letters, "
                "digits, '-' or '_', starting with a letter or digit."
            )

    if not role_name:
        if not skill_name:
            raise AwfApiError("role_name is required")
        role_name = skill_name
    else:
        # AUD06-06: role_name is public input (MCP awf_add_role / CLI).
        # Normalize (strip + lowercase) then validate as a slug —
        # "../../x", "a/b" or an absolute path must not be able to write
        # outside .agentic/roles/.
        role_name = role_name.strip().lower()
        if not is_slug_name(role_name):
            raise AwfApiError(
                f"Invalid role name {role_name!r}: use lowercase letters, digits, "
                "'-' or '_', starting with a letter or digit (e.g. 'my-role')."
            )

    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    # TODO-0230 (M6): refresh re-materializes an existing skill-sourced role.
    # It shares the slug validation above but not the create path: the file
    # already exists, so "exists → refuse" does not apply.
    if refresh:
        # REVIEW N2: refresh rewrites the LIVE role, so pairing it with the
        # draft invitation ("live roles are NOT touched") is contradictory.
        # Refuse before any write — the MCP/CLI wrappers answer the same.
        if draft:
            raise AwfApiError(
                "Pick ONE of draft / refresh — they are mutually exclusive."
            )
        # The provenance skill is the refresh source. ``from_skill`` is
        # accepted by the create path; here it would be silently ignored —
        # refuse instead of letting the caller believe it was used.
        if from_skill:
            raise AwfApiError(
                "refresh re-materializes from the provenance skill recorded "
                "in the role file — drop from_skill (create mode is the "
                "only one that takes a source)."
            )
        return _refresh_skill_role(
            project_dir,
            role_name,
            model,
            description=description,
            force=force,
        )

    # TODO-0175: one model resolution for every source — explicit model=
    # beats the project config; empty = unassigned (config default applies).
    resolved_model = _resolve_role_model(project_dir, role_name, model)

    roles_dir = project_dir / ".agentic" / "roles"
    # ORCH M5.1: draft candidates live in roles/draft/ — live roles,
    # config and the pipeline are not touched by the draft path.
    target_dir = roles_dir / _DRAFT_DIRNAME if draft else roles_dir
    # REVIEW (architect): the managed writer creates the directory through
    # the verified parent. A bare ``mkdir`` here walked a symlinked
    # ``roles``/``roles/draft`` — the same leftover removed from the
    # pipeline, plan and inbox writers in this unit.
    role_file = target_dir / f"{role_name}.md"
    # AUD06-06: defense in depth — even a slug that passed validation must
    # resolve back inside the target dir (guards against future
    # validation drift).
    if not role_file.resolve().is_relative_to(target_dir.resolve()):
        raise AwfApiError(
            f"Invalid role name {role_name!r}: resolves outside the roles area."
        )

    if skill_name:
        skill_file = _find_skill_file(skill_name, project_dir)
        try:
            raw = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise AwfApiError(f"Cannot read skill file {skill_file}: {e}") from e
        content = _skill_role_content(skill_name, skill_file, raw)
        source = f"skill:{skill_name}"
    else:
        builtin = _builtin_role_template(role_name)
        if builtin is not None:
            # W7: ship the project's quality protocol — the reviewer and
            # the implementer get their built-in templates instead of the
            # empty placeholder (the supervisor strategy works in other
            # projects, not only in this repo).
            content = builtin.read_text(encoding="utf-8")
            source = "builtin"
        else:
            content = _ROLE_TEMPLATE.format(
                role_name=role_name,
                description=description or "new role",
                model=model_line_value(resolved_model),
            )
            source = "template"

    # TODO-0175: the uniform model line for every source. The placeholder
    # template already renders it in its header (the injector is a no-op
    # there); built-in and skill-sourced content get it injected.
    content = _inject_model_line(content, model_line(resolved_model))

    # TODO-0208 (REPORTS33): the awf override block for skill-sourced
    # roles — the standalone "Commit." instructions of the skill body are
    # overridden by the awf cycle (the engine commits after verify).
    # Idempotent via the AWF-OVERRIDE marker; a repeated materialization
    # (force) refreshes the block, never duplicates it. Existing role
    # files are never touched (add_role only creates).
    # TODO-0230 (M6): discipline + suite canon are defaults of every role —
    # skill-sourced, preset and built-in alike. They sit before the
    # AWF-OVERRIDE block (skill-only), which stays the file's last section.
    content = _apply_managed_role_blocks(content, role_name=role_name)
    if skill_name:
        content = apply_awf_overrides(content)

    # ORCH M5.1: the draft candidate records its source on the first line
    # so list_role_drafts can report it; live role files carry no marker.
    if draft:
        content = (
            f"<!-- awf-draft source={source} "
            f"created={date.today().isoformat()} -->\n{content}"
        )

    # AUD06-07: an existing role file is user data (hand-edited instructions)
    # — silently replacing it lost it without a trace or a backup. Refuse;
    # the caller edits the file instead (or passes force to overwrite).
    # For a draft candidate, force replaces the candidate — never a live
    # role (that path is adopt_role_draft's, and it has no force at all).
    if role_file.exists() and not force:
        raise AwfApiError(
            f"Role file already exists: {role_file} — edit it in place "
            "(add_role only creates new roles)."
        )

    # REPORTS32 (TODO-0202): an explicit ``model=`` is also merged into
    # config.yaml (the engine's source of truth). Compute AND VALIDATE the
    # edit before the role file is written, so a failed edit never leaves
    # an orphaned role file. ``draft`` never touches the config (ORCH M5.1);
    # a model already resolved from the config is a no-op.
    config_update: tuple[str, str] | None = None
    model_config = ""
    if not draft:
        config_update, model_config = _plan_model_config_update(
            project_dir, role_name, model
        )
    config_path = paths.config_file(project_dir)

    managed_write_api_text(
        role_file, content, operation="write role"
    )

    if config_update is not None:
        managed_write_api_text(
            config_path,
            config_update[0],
            operation="update config.yaml",
        )

    return AddRoleResult(
        role_name=role_name,
        role_file=str(role_file),
        model=resolved_model,
        model_config=model_config,
    )


# ─── Role preset seeding (REPORTS32, TODO-0203) ─────────────────────────
#
# The "typical office" is assembled by hand in every project: each role
# copied from its agent-* skill one by one, then models.<role>.model
# written into config.yaml manually. A preset catalog makes it one call.
# The catalog is embedded (it is awf's own office, not user data); skill
# search mirrors from_skill: project .opencode/skills/ first, then
# $XDG_CONFIG_HOME/opencode/skills/. A missing skill is a NOTE (a row in
# the result), not an error — the machine decides which part of the
# office it can afford. Existing role files are user data: never touched,
# never overwritten (the hard rule — there is no force on this path).


#: role slug → source skill. "product" lands in the office only when its
#: skill exists on the machine (the skip row says so).
OFFICE_PRESET: tuple[tuple[str, str], ...] = (
    ("system-analyst", "agent-system-analyst"),
    ("spec-writer", "agent-spec-writer"),
    ("architector", "agent-architector"),
    ("project-auditor", "agent-project-auditor"),
    ("qa-review", "agent-qa-review"),
    ("qa-table", "agent-qa-table"),
    ("auditor-table", "agent-auditor-table"),
    ("product", "agent-product"),
)

#: preset name → catalog. New presets are added here, not in callers.
PRESETS: dict[str, tuple[tuple[str, str], ...]] = {
    "office": OFFICE_PRESET,
}


def seed_roles(
    project_dir: Path,
    *,
    preset: str = "office",
    models: dict[str, str] | None = None,
) -> SeedRolesResult:
    """Seed the built-in role catalog into an existing project.

    Example (MCP): ``awf_add_role(project_dir=..., preset="office",
    models={"qa-review": "vllm/llm"})``.

    For every ``(role, skill)`` pair of the preset catalog:
    - the role file ``.agentic/roles/<role>.md`` exists → skipped
      (``"already exists"``) — an existing role is user data and is never
      touched or overwritten (no force on this path);
    - the skill is not found (project ``.opencode/skills/<skill>/``
      first, then ``$XDG_CONFIG_HOME/opencode/skills/<skill>/`` — the
      same search as ``from_skill``) → skipped
      (``"skill not found: <skill>"``) — a note, not an error;
    - otherwise the role is created through :func:`add_role`
      (``from_skill`` content: provenance comment + body, front-matter
      stripped; the uniform model line per the resolved model).

    ``models={role: model}`` writes the role→model mapping into
    ``.agentic/config.yaml`` as ``models.<role>.model`` — the same
    surgical, comment-preserving, idempotent merge as the explicit
    ``model=`` of :func:`add_role` (the config is the source of truth the
    engine reads). Keys are validated against the preset catalog (an
    unknown key is a typed refusal). Models are applied to every catalog
    role they name — created in this run (via the add_role path, so the
    role file's model line stays in sync) or already present (config
    only, the role file is not touched).

    The answer is the table the owner reads to check the office:
    ``created`` (role / role_file / skill / model), ``skipped``
    (role / reason), ``models`` (role / model / locus / action —
    ``written`` or ``unchanged`` on an idempotent repeat). A second call
    on the same project is a no-op: created empty, everything skipped,
    config and role files byte-for-byte untouched.

    Raises :class:`AwfApiError` on an unknown preset or a ``models=`` key
    outside the preset catalog (nothing is written in that case).
    """
    preset = (preset or "").strip().lower()
    if preset not in PRESETS:
        raise AwfApiError(
            f"Unknown preset {preset!r}. Available presets: "
            + ", ".join(sorted(PRESETS))
            + "."
        )
    catalog = PRESETS[preset]
    catalog_roles = [role for role, _ in catalog]

    normalized_models: dict[str, str] = {}
    for key, value in (models or {}).items():
        slug = str(key).strip().lower()
        if slug not in catalog_roles:
            raise AwfApiError(
                f"models= key {key!r} is not a role of the {preset!r} "
                f"preset. Preset roles: {', '.join(catalog_roles)}."
            )
        model = str(value or "").strip()
        if model:
            normalized_models[slug] = model

    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    roles_dir = project_dir / ".agentic" / "roles"
    # Pre-creation snapshot of the config: it labels the models= rows of
    # the roles this run creates (written vs already there) — the file
    # itself is re-read after the run for the remaining roles.
    config_path = paths.config_file(project_dir)
    pre_cfg = (
        config_path.read_text(encoding="utf-8")
        if config_path.is_file()
        else ""
    )
    created: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for role, skill in catalog:
        if (roles_dir / f"{role}.md").is_file():
            skipped.append({"role": role, "reason": "already exists"})
            continue
        try:
            _find_skill_file(skill, project_dir)
        except AwfApiError:
            skipped.append(
                {"role": role, "reason": f"skill not found: {skill}"}
            )
            continue
        model = normalized_models.get(role, "")
        result = add_role(project_dir, role, from_skill=skill, model=model)
        created.append(
            {
                "role": role,
                "role_file": result.role_file,
                "skill": skill,
                "model": result.model,
            }
        )

    # models → config.yaml (the TODO-0202 mechanism: surgical, validated,
    # idempotent). Roles created above already carry their model in the
    # config (add_role merged it with the file write); the pass below
    # covers the rest and reports the truth for every models= entry.
    models_rows: list[dict[str, str]] = []
    raw_cfg = (
        config_path.read_text(encoding="utf-8")
        if config_path.is_file()
        else ""
    )
    created_roles = {row["role"] for row in created}
    for role in catalog_roles:
        model = normalized_models.get(role)
        if not model:
            continue
        if role in created_roles:
            action = (
                "unchanged"
                if _compute_config_update(pre_cfg, role, model) is None
                else "written"
            )
        else:
            update = _compute_config_update(raw_cfg, role, model)
            if update is None:
                action = "unchanged"
            else:
                raw_cfg = update[0]
                managed_write_api_text(
                    config_path,
                    raw_cfg,
                    operation="update config.yaml",
                )
                action = "written"
        models_rows.append(
            {
                "role": role,
                "model": model,
                "locus": f"models.{role}.model",
                "action": action,
            }
        )

    return SeedRolesResult(
        preset=preset, created=created, skipped=skipped, models=models_rows
    )


# ─── Office: who is on the team and on which model (REPORTS32) ──────────


def role_model_office(project_dir: Path) -> list[dict[str, Any]]:
    """One call: the project's roles, their model, and where it comes from.

    For every live ``.agentic/roles/*.md`` (the draft area excluded — a
    candidate is not team yet) report ``{"role", "model", "source"}``:

    - ``source="config"`` — ``models.<role>.model`` is set in
      ``.agentic/config.yaml`` (the source of truth; ``model`` is that
      value);
    - ``source="md"`` — the model is set ONLY in the role file's
      ``**Модель:**`` line (``model`` is that value);
    - ``source="none"`` — no model anywhere (``model`` is None).

    Sorted by slug. Deliberately narrower than ``check_model_config``
    (which validates models against opencode.json): this only answers
    «who do I have and on which model» (awf_status / awf_brief office
    section).
    """
    project_dir = Path(project_dir).resolve()

    config_data = cfg_mod.load(project_dir)
    models_cfg = cfg_mod.get(config_data, "models", {}) or {}
    if not isinstance(models_cfg, dict):
        models_cfg = {}

    roles_dir = paths.agentic_dir(project_dir) / "roles"
    entries: list[dict[str, Any]] = []
    if roles_dir.is_dir():
        for f in sorted(roles_dir.glob("*.md")):
            if not f.is_file():
                continue
            role = f.stem
            role_cfg = models_cfg.get(role)
            config_model = (
                role_cfg.get("model") if isinstance(role_cfg, dict) else None
            )
            if not isinstance(config_model, str) or not config_model.strip():
                config_model = None
            md_model: str | None = None
            try:
                md_model = parse_role_model_line(
                    f.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError):
                md_model = None

            if config_model:
                model, source = config_model, "config"
            elif md_model:
                model, source = md_model, "md"
            else:
                model, source = None, "none"
            entries.append({"role": role, "model": model, "source": source})
    return entries


# ─── Role draft area (ORCH M5.1) ────────────────────────────────────────
#
# Role candidates are isolated in .agentic/roles/draft/ until checked:
# list_role_drafts reports them (name + source), adopt_role_draft moves a
# candidate to the live roles area (refusing a slug conflict with a live
# role), discard_role_draft removes it (kept as a trace in .agentic/
# context/ — a discarded candidate may be hand-written content that cannot
# be regenerated, and the context/ area already holds awf's traces).


def _validate_draft_slug(role_name: str) -> str:
    """Slug-validate a draft slug (public input from MCP/CLI).

    Same rules as add_role's role_name (AUD06-06) — a draft slug is a
    future role name, so it must be one already.
    """
    slug = role_name.strip().lower()
    if not is_slug_name(slug):
        raise AwfApiError(
            f"Invalid role name {role_name!r}: use lowercase letters, digits, "
            "'-' or '_', starting with a letter or digit (e.g. 'my-role')."
        )
    return slug


def _draft_path(project_dir: Path, slug: str) -> Path:
    return project_dir / ".agentic" / "roles" / _DRAFT_DIRNAME / f"{slug}.md"


def _draft_marker(content: str) -> tuple[str, str]:
    """Parse the first-line marker → (source, created); ("file", "") if absent."""
    m = _DRAFT_MARKER_RE.match(content)
    if not m:
        return "file", ""
    return m.group("source"), m.group("created")


def _strip_draft_marker(content: str) -> str:
    """Drop the first-line awf-draft marker (adopted roles look regular).

    A skill-sourced candidate keeps its own provenance comment — that one
    is the second line and stays.
    """
    return _DRAFT_MARKER_RE.sub("", content, count=1)


def _available_draft_names(project_dir: Path) -> list[str]:
    draft_dir = project_dir / ".agentic" / "roles" / _DRAFT_DIRNAME
    if not draft_dir.is_dir():
        return []
    return sorted(f.stem for f in draft_dir.glob("*.md") if f.is_file())


def list_role_drafts(project_dir: Path) -> RoleDraftsResult:
    """List role candidates in ``.agentic/roles/draft/`` (ORCH M5.1).

    Returns one ``{"name", "source", "created", "file"}`` dict per
    candidate. ``source`` comes from the draft marker written by
    :func:`add_role` (``skill:<name>`` / ``builtin`` / ``template``); a
    hand-placed file without the marker lists as ``file`` with an empty
    ``created``. An absent or empty draft area yields ``drafts=[]``.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    drafts: list[dict[str, str]] = []
    for slug in _available_draft_names(project_dir):
        path = _draft_path(project_dir, slug)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            source, created = "file", ""
        else:
            source, created = _draft_marker(content)
        drafts.append(
            {
                "name": slug,
                "source": source,
                "created": created,
                "file": str(path),
            }
        )
    return RoleDraftsResult(drafts=drafts)


def adopt_role_draft(project_dir: Path, role_name: str) -> AdoptRoleDraftResult:
    """Move a draft candidate into the live roles area (ORCH M5.1).

    ``roles/draft/{slug}.md`` becomes ``roles/{slug}.md`` (the awf-draft
    marker line is stripped) and the candidate is removed. A slug that
    already has a live role is an EXPLICIT refusal — awf never
    overwrites a live role, and there is deliberately no force for this
    path: resolve the conflict (adopt under a different slug, discard
    the candidate, or remove the live role — the supervisor/owner's
    call) first.

    ORCH M5.3: the adopt is ALSO the normalization step — the adopted
    role joins the team, so zone analysis is re-run through the existing
    ``analyze_roles_core`` mechanism and the BD-31 disambiguation addenda
    are refreshed. That is idempotent (the BD-31 marker block is
    replaced, never duplicated) and touches ONLY the BD-31 blocks of the
    role files — the role bodies and every other file (config, pipeline,
    supervisor.md) are left byte-for-byte. The setup phase is not
    re-run, and the role's pipeline participation (a stage + model
    mapping) stays an explicit supervisor step — adopt does not rebuild
    the pipeline. Normalization is best-effort: a failure is reported in
    the result's ``normalization`` dict but does not undo the adopt.
    """
    slug = _validate_draft_slug(role_name)
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    draft = _draft_path(project_dir, slug)
    roles_dir = project_dir / ".agentic" / "roles"
    target = roles_dir / f"{slug}.md"
    # REVIEW (architect): adopt reads the candidate and then UNLINKS it — a
    # symlinked ``roles``/``roles/draft`` used to delete an outside file.
    # The preflight runs before the read, so no component is followed.
    refuse_unsafe_surfaces(project_dir, "adopt role draft", draft, target)
    if not draft.is_file():
        available = ", ".join(_available_draft_names(project_dir)) or "(none)"
        raise AwfApiError(
            f"Draft role {slug!r} not found in .agentic/roles/draft/. "
            f"Available drafts: {available}."
        )
    if target.exists():
        raise AwfApiError(
            f"Adopt refused: live role {slug!r} already exists at {target}. "
            "awf never overwrites a live role — adopt under a different "
            "slug, or discard the draft (discard_role_draft / "
            "awf add-role --discard), or remove the live role, then "
            "adopt again."
        )

    try:
        raw = draft.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # ORCH M6.4: a candidate that is not valid UTF-8 is a corrupt
        # input — typed refusal, the file is kept (the listing already
        # reports it as source="file"), nothing is created.
        raise AwfApiError(
            f"Draft role {slug!r} is unreadable (not valid UTF-8) — fix the "
            "candidate or discard it, then adopt again."
        )
    content = _strip_draft_marker(raw)
    managed_write_api_text(
        target, content, operation="adopt role draft"
    )
    draft.unlink()

    # ORCH M5.3: normalization — see the docstring. analyze_roles_core
    # is best-effort here: the role is already live, a broken analysis
    # (corrupt pipeline, unreadable config) must not fail the adopt.
    normalization: dict[str, Any] = {}
    try:
        data = analyze_roles_core(project_dir, dry_run=False)
        normalization = {
            "overlaps": [
                {"role_a": a, "role_b": b, "zone": zone}
                for a, b, zone in data.overlaps
            ],
            "addenda": [
                r for r in data.patches if data.applied and r not in data.failed
            ],
            "failed": list(data.failed),
        }
    except Exception as e:  # noqa: BLE001 — analysis must not fail adopt
        log.warning("adopt_role_draft: normalization skipped: %s", e)
        normalization = {"error": str(e)}

    return AdoptRoleDraftResult(
        role_name=slug, role_file=str(target), normalization=normalization
    )


def discard_role_draft(project_dir: Path, role_name: str) -> DiscardRoleDraftResult:
    """Remove a draft candidate, keeping it as a trace in context/ (ORCH M5.1).

    The candidate leaves ``roles/draft/`` and lands in
    ``.agentic/context/role-draft-{slug}-{YYYYMMDD}.md`` (same-day
    collisions get a ``-2``/``-3`` suffix). Decision (TODO-0143): keep
    the trace, not delete — a discarded candidate may be hand-written
    content that cannot be regenerated, and context/ already holds awf's
    one-way artifacts (BASELINE-*, GATES-*, DONE-*).
    """
    slug = _validate_draft_slug(role_name)
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    draft = _draft_path(project_dir, slug)
    # REVIEW (architect): the candidate is unlinked after the trace write —
    # the same preflight as adopt, before anything is read or followed.
    refuse_unsafe_surfaces(project_dir, "discard role draft", draft)
    if not draft.is_file():
        available = ", ".join(_available_draft_names(project_dir)) or "(none)"
        raise AwfApiError(
            f"Draft role {slug!r} not found in .agentic/roles/draft/. "
            f"Available drafts: {available}."
        )

    context_dir = project_dir / ".agentic" / "context"
    stem = f"role-draft-{slug}-{date.today():%Y%m%d}"
    trace = context_dir / f"{stem}.md"
    counter = 2
    while trace.exists():
        trace = context_dir / f"{stem}-{counter}.md"
        counter += 1

    managed_write_api_text(
        trace,
        draft.read_text(encoding="utf-8"),
        operation="discard role draft",
    )
    draft.unlink()
    return DiscardRoleDraftResult(
        role_name=slug,
        draft_file=str(draft),
        trace_file=str(trace),
    )


# ─── Role analysis internals ────────────────────────────────────────────
#
# Moved here from awf/cmd_analyze_roles.py (audit T2.3 — wrong-direction
# dependency). cmd_*.py is now a thin CLI wrapper, this module owns the
# business logic.


class AnalyzeError(Exception):
    """Raised on analyze_roles_core precondition failures (missing roles dir, etc.).

    Message is user-readable; callers print + return non-zero exit.
    """


@dataclass
class AnalyzeData:
    """Structured result of :func:`analyze_roles_core`.

    All fields are pure data — no print/stdout side effects. Callers
    (cmd_analyze_roles.run for CLI, analyze_roles for MCP) format
    this for their medium.
    """

    zones: dict[str, str] = field(default_factory=dict)
    overlaps: list[tuple[str, str, str]] = field(default_factory=list)
    patches: dict[str, str] = field(default_factory=dict)
    pipeline_roles: list[str] = field(default_factory=list)
    applied: bool = False  # True if at least one patch was written to disk
    failed: list[str] = field(default_factory=list)  # roles whose patch write raised OSError


def _read_role_files(project_dir: Path) -> dict[str, str]:
    """Read all .agentic/roles/*.md → {role_slug: content}.

    AUD06-16: an unreadable role file (non-UTF-8, permissions) is skipped
    with a log line — one corrupt file must not kill the whole analysis.
    """
    roles_dir = paths.agentic_dir(project_dir) / "roles"
    if not roles_dir.is_dir():
        return {}
    result: dict[str, str] = {}
    for f in sorted(roles_dir.glob("*.md")):
        try:
            result[f.stem] = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log.warning("analyze_roles: skipping unreadable role file %s: %s", f, e)
    return result


def _read_pipeline_roles(project_dir: Path, config: dict) -> list[str]:
    """Return ordered list of agent role slugs from default pipeline.

    Skips supervisor (it's built-in). Returns [] if no pipeline.yaml.
    AUD06-16: a corrupted pipeline file (non-UTF-8, broken YAML, unreadable)
    degrades to [] — role analysis without pipeline order is still useful,
    and the loader already prints the reason to stderr.
    """
    import yaml

    from ..pipeline import load_stages, resolve_pipeline_file

    try:
        pipeline_file = resolve_pipeline_file(project_dir, None, config)
    except FileNotFoundError:
        return []
    try:
        stages = load_stages(pipeline_file)
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        log.warning("analyze_roles: cannot read pipeline %s: %s", pipeline_file, e)
        return []
    # CLOSER IMP7 (R7): a conditional gate (``skip_if``) is not a position
    # in the agent chain — the closer shares ``agent-implementer``, and
    # counting it broke the D9 promise that the last working role reads
    # "LAST agent" (``test_rebuild_refreshes_positions``). ``skip_if``
    # accepts only ``checklist_green`` (IMP1), so every such stage IS a gate.
    return [s.role for s in stages if s.role != "supervisor" and not s.skip_if]


# Default role → zone mapping. T3.4 extraction: data lives in
# ``awf/data/role_zones.yaml`` (loaded once at first use, cached).
# Fallback to small built-in if data file missing/corrupt.
_DEFAULT_ZONES_FALLBACK: dict[str, str] = {
    "system-analyst": "writes requirements / vision",
    "architect": "designs system structure",
    "developer": "writes code",
    "qa": "verifies implementation against requirements",
    "audit": "verifies code health / project quality",
}

_ZONES_CACHE: dict[str, str] | None = None


def _load_role_zones() -> dict[str, str]:
    """T3.4: load role → zone mapping from ``awf/data/role_zones.yaml``.

    Loaded once, cached in module global. Falls back to
    ``_DEFAULT_ZONES_FALLBACK`` if file missing or YAML error.
    Users extend by editing the .yaml — no code changes required.
    """
    global _ZONES_CACHE
    if _ZONES_CACHE is not None:
        return _ZONES_CACHE

    from pathlib import Path

    import yaml

    data_file = Path(__file__).resolve().parent.parent / "data" / "role_zones.yaml"
    try:
        loaded = yaml.safe_load(data_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            # Normalize all keys/values to str
            zones = {str(k): str(v) for k, v in loaded.items()}
        else:
            zones = dict(_DEFAULT_ZONES_FALLBACK)
    except (OSError, yaml.YAMLError):
        zones = dict(_DEFAULT_ZONES_FALLBACK)

    _ZONES_CACHE = zones
    return zones


def _infer_zone(role_slug: str, content: str) -> str:
    """Best-effort guess of a role's zone from its slug + content head.

    BD-31a fix: slug match takes PRIORITY over content match.
    T3.4: zones loaded from ``awf/data/role_zones.yaml`` (extensible
    without code changes).
    """
    zones = _load_role_zones()
    slug_lower = role_slug.lower()
    # Pass 1: slug-only (highest confidence)
    for needle, zone in zones.items():
        if needle in slug_lower:
            return zone
    # Pass 2: content head (fallback for roles with generic slug)
    head = content[:600].lower()
    for needle, zone in zones.items():
        if needle in head:
            return zone
    return "generalist (undefined zone)"


def _zone_family(zone: str) -> str:
    """BD-31c fix: group zones into families for broader overlap detection.

    'verifies implementation against requirements' and
    'verifies code health / project quality' are different zones, but both
    are 'verify' family → potential overlap in pipeline.
    """
    if zone.startswith("verifies"):
        return "verify"
    if zone.startswith("writes code"):
        return "code"
    if zone.startswith("writes/runs tests"):
        return "test"
    if zone.startswith("writes requirements"):
        return "requirements"
    return zone  # unique zones are their own family


def _detect_overlaps(roles_with_zones: dict[str, str]) -> list[tuple[str, str, str]]:
    """Find pairs of roles whose zones overlap.

    BD-31c fix: uses _zone_family for broader matching. Two roles with
    different verify zones (qa-review + project-auditor) still overlap
    because both do verification work — pipeline risks duplicate effort.

    Returns list of (role_a, role_b, shared_zone) tuples.
    """
    overlaps: list[tuple[str, str, str]] = []
    items = list(roles_with_zones.items())
    for i, (a, za) in enumerate(items):
        fa = _zone_family(za)
        if fa == za and za == "generalist (undefined zone)":
            continue
        for b, zb in items[i + 1:]:
            fb = _zone_family(zb)
            if fb == zb and zb == "generalist (undefined zone)":
                continue
            # Exact zone match OR same family (broader)
            if za == zb:
                overlaps.append((a, b, za))
            elif fa == fb and fa not in (za, zb):
                # Same family but different specific zones — still overlap
                overlaps.append((a, b, f"{fa} (different focus: {za} vs {zb})"))
    return overlaps


def _build_disambiguation_addendum(
    role: str,
    zone: str,
    overlaps: list[tuple[str, str, str]],
    pipeline_roles: list[str],
) -> str:
    """Build a markdown addendum that disambiguates this role from others
    sharing the same zone. Strengthened instruction = unique contribution."""
    same_zone = [other for a, b, _ in overlaps for other in (a, b)
                 if other != role and (a == role or b == role)]
    same_zone = sorted(set(same_zone))

    # Pipeline position determines priority
    try:
        my_pos = pipeline_roles.index(role) if role in pipeline_roles else -1
    except ValueError:
        my_pos = -1

    position_hint = ""
    if my_pos >= 0:
        if my_pos == 0:
            position_hint = "You are the FIRST agent in the pipeline — start the work, set the foundation."
        elif my_pos == len(pipeline_roles) - 1:
            position_hint = f"You are the LAST agent in the pipeline (position {my_pos + 1}/{len(pipeline_roles)}) — final pass before supervisor verify."
        else:
            position_hint = f"You are at position {my_pos + 1}/{len(pipeline_roles)} in the pipeline."

    parts = [
        "",
        "---",
        "",
        "## BD-31: Pipeline-specific disambiguation (added by awf analyze-roles)",
        "",
        f"**Your zone:** {zone}",
        "",
    ]
    if position_hint:
        parts.append(position_hint)
        parts.append("")

    if same_zone:
        parts.append(f"**Other role(s) with similar zone:** {', '.join(f'`{r}`' for r in same_zone)}")
        parts.append("")
        parts.append("**To avoid wasted duplicate work, your unique contribution is:**")
        # Heuristic disambiguation rules — role-specific
        disambiguation_added = False
        if zone.startswith("verif"):  # covers "verifies..." and "verify..."
            if "qa" in role.lower():
                parts.append("- Check TODO requirements are met + write/run tests.")
                parts.append(f"- Do NOT do full project audit (that's `{'project-auditor' if 'project-auditor' in same_zone else same_zone[0]}`).")
                disambiguation_added = True
            elif "review" in role.lower() or "audit" in role.lower() or "project-auditor" in role.lower():
                parts.append("- Focus on code health: maintainability, design, risky patterns.")
                parts.append("- Do NOT re-verify TODO requirements line-by-line (qa did that).")
                disambiguation_added = True
        elif zone == "writes code" or zone == "code":
            if "implement" in role.lower() or role.lower() in ("dev", "developer", "worker"):
                parts.append("- You are the primary code writer. Other 'code' roles (system-analyst, architect) write requirements/design, not code.")
                disambiguation_added = True
            elif "refactor" in role.lower():
                parts.append("- You improve existing structure. Do NOT add new features.")
                disambiguation_added = True
        elif zone == "writes/runs tests" or zone == "test":
            parts.append("- You write and run tests. Do NOT fix bugs you find — report them via BLOCKED signal.")
            disambiguation_added = True
        elif zone == "writes requirements / vision" or zone == "requirements":
            parts.append("- You write the vision/requirements file. Do NOT write code or tests — `dev` does that.")
            disambiguation_added = True
        # BD-31b fix: fallback if no role-specific rule matched
        if not disambiguation_added:
            parts.append(f"- Focus on YOUR specific zone: {zone}.")
            parts.append(f"- Do NOT duplicate work that {', '.join(f'`{r}`' for r in same_zone)} already did.")
        parts.append("")

    parts.append("**Pipeline contract:** read the handoff from the previous role before starting. Do NOT redo prior work.")
    parts.append("")
    return "\n".join(parts)


def analyze_roles_core(
    project_dir: Path,
    *,
    dry_run: bool = False,
) -> AnalyzeData:
    """Pure-data role analysis — no printing, no stdout capture.

    Steps:
    1. Read role .md files (excluding supervisor).
    2. Read pipeline.yaml for role ordering.
    3. Infer zone for each role (slug + content heuristic).
    4. Detect overlaps (same zone OR same zone-family).
    5. Build disambiguation addendum per role.
    6. Apply patches to disk unless ``dry_run=True``.

    Raises :class:`AnalyzeError` on precondition failures:
    - No .agentic/roles/ directory
    - No team role files (only supervisor.md)

    Returns :class:`AnalyzeData` with zones, overlaps, patches,
    pipeline_roles, and ``applied`` flag.
    """
    if not (paths.agentic_dir(project_dir) / "roles").is_dir():
        raise AnalyzeError(
            f"No .agentic/roles/ at {project_dir}. Run 'awf init' first."
        )

    config = cfg_mod.load(project_dir)
    role_contents = _read_role_files(project_dir)
    pipeline_roles = _read_pipeline_roles(project_dir, config)

    if not role_contents:
        raise AnalyzeError("No role .md files found in .agentic/roles/.")

    # Skip supervisor — it's built-in, not part of team analysis.
    team_roles = {r: c for r, c in role_contents.items() if r != "supervisor"}
    if not team_roles:
        raise AnalyzeError("No team role files (only supervisor.md found).")

    # Step 1: infer zone for each role
    zones: dict[str, str] = {}
    for role, content in team_roles.items():
        zones[role] = _infer_zone(role, content)

    # Step 2: detect overlaps
    overlaps = _detect_overlaps(zones)

    # Step 3: build disambiguation addenda
    patches: dict[str, str] = {}
    for role in team_roles:
        addendum = _build_disambiguation_addendum(
            role, zones[role], overlaps, pipeline_roles
        )
        if "BD-31: Pipeline-specific disambiguation" in addendum:
            patches[role] = addendum

    data = AnalyzeData(
        zones=zones,
        overlaps=overlaps,
        patches=patches,
        pipeline_roles=pipeline_roles,
        applied=False,
    )

    if not patches or dry_run:
        return data

    # Step 4: apply patches to disk (idempotent via BD-31 marker check)
    roles_dir = paths.agentic_dir(project_dir) / "roles"
    failed: list[str] = []
    for role, addendum in patches.items():
        path = roles_dir / f"{role}.md"
        try:
            current = path.read_text(encoding="utf-8")
            if "BD-31: Pipeline-specific disambiguation" in current:
                # TODO-0157: remove exactly the BD-31 block — from its
                # `---` separator + heading to the NEXT `^## ` heading (or
                # EOF) — so text written after the block (supervisor
                # sections) survives byte-for-byte. A following BD-31
                # heading is a legacy duplicate, not a boundary: the run
                # swallows it whole.
                pattern = re.compile(
                    r"\n?---\n+## BD-31: Pipeline-specific disambiguation"
                    r".*?(?=\n## (?!BD-31)|\Z)",
                    re.DOTALL,
                )
                current = pattern.sub("", current).rstrip()
            new_content = current.rstrip() + "\n" + addendum
            managed_write_api_text(
                path,
                new_content,
                operation="analyze roles",
            )
        except (OSError, AwfApiError):
            # The structured result exists to report PER-ROLE failures: one
            # role file whose write is refused (a symlinked leaf) must not
            # abort the patches of the others. A refusal still writes
            # nothing outside the project — the barrier runs before the
            # first byte.
            failed.append(role)

    if failed:
        # Surface failed writes in the structured result so callers (api/MCP)
        # can report them accurately instead of claiming success.
        data.failed = failed

    data.applied = True  # apply step ran; per-patch success derivable from `failed`
    return data


# ─── Public MCP-facing analyze_roles ────────────────────────────────────


def analyze_roles(
    project_dir: Path,
    *,
    dry_run: bool = False,
) -> AnalyzeRolesResult:
    """Analyze team roles for zone overlaps, optionally add disambiguation.

    Wraps :func:`analyze_roles_core` (pure data, no I/O side effects beyond
    patch writes) and formats the result for MCP/CLI consumption.
    Patch application is idempotent via BD-31 marker check.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    try:
        data = analyze_roles_core(project_dir, dry_run=dry_run)
    except AnalyzeError as e:
        raise AwfApiError(str(e))

    overlaps = [
        {"role_a": a, "role_b": b, "zone": zone}
        for a, b, zone in data.overlaps
    ]
    patches_applied = [
        {
            "role": role,
            "preview": addendum.replace("\n", " ")[:200],
            "applied": data.applied and role not in data.failed,
        }
        for role, addendum in data.patches.items()
    ]

    return AnalyzeRolesResult(
        overlaps=overlaps,
        patches_applied=patches_applied,
        dry_run=dry_run,
    )


__all__ = [
    "add_role",
    "adopt_role_draft",
    "analyze_roles",
    "analyze_roles_core",
    "AnalyzeData",
    "AnalyzeError",
    "apply_awf_overrides",
    "discard_role_draft",
    "list_role_drafts",
    "role_model_office",
]
